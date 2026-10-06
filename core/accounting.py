"""One entry point for bills and actual receipts; never infer a tenant from a room."""
from decimal import Decimal

from django import forms
from django.db import transaction
from django.utils import timezone

from .forms import BaseFormMixin, DateInput
from .models import Charge, Payment, Room, Tenancy
from .services import create_charge, record_payment, settle_charges


class OpenBillChoice(forms.ModelChoiceField):
    def label_from_instance(self, obj):
        person = obj.person.name if obj.person else "公共/房间费用"
        period = f" · {obj.period_start:%Y-%m-%d} 至 {obj.period_end:%Y-%m-%d}" if obj.period_start and obj.period_end else ""
        return f"{obj.room or '公寓'} · {person} · {obj.description}{period} · 未结 ¥{obj.balance}"


class AccountingEntryForm(BaseFormMixin, forms.Form):
    charge = OpenBillChoice(label="记录哪件事", required=False, empty_label="新事项（还没有登记过）", queryset=Charge.objects.none())
    direction = forms.ChoiceField(label="收还是付", choices=[("income", "收钱"), ("expense", "付钱")])
    category = forms.ChoiceField(label="款项", choices=[*Charge.Category.choices, ("prepaid", "预收租金（余款预存）")])
    room = forms.ModelChoiceField(label="房间", queryset=Room.objects.all(), required=False, empty_label="公共收支")
    tenancy = forms.ModelChoiceField(label="归属合同 / 租客", queryset=Tenancy.objects.select_related("room", "primary_person"), required=False, empty_label="公共或房间费用，不归属租客")
    amount = forms.DecimalField(label="金额", max_digits=12, decimal_places=2, min_value=Decimal("0.01"))
    state = forms.ChoiceField(label="收付情况", choices=[("paid", "已经收付"), ("pending", "尚未收付，加入待办"), ("partial", "只收付了一部分")])
    paid_amount = forms.DecimalField(label="本次实际收付", required=False, max_digits=12, decimal_places=2, min_value=Decimal("0.01"))
    date = forms.DateField(label="实际收付日期", widget=DateInput, initial=timezone.localdate)
    due_date = forms.DateField(label="应收付日期", widget=DateInput, required=False, help_text="留空沿用上面的日期。")
    description = forms.CharField(label="说明", max_length=200, required=False)
    period_start = forms.DateField(label="费用期间开始", widget=DateInput, required=False)
    period_end = forms.DateField(label="费用期间结束", widget=DateInput, required=False)
    notes = forms.CharField(label="补充备注", widget=forms.Textarea(attrs={"rows": 2}), required=False)
    token = forms.UUIDField(widget=forms.HiddenInput)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["charge"].queryset = Charge.objects.exclude(status__in=[Charge.Status.PAID, Charge.Status.VOID]).select_related("room", "person", "tenancy").prefetch_related("allocations", "adjustments")
        self.apply_widget_attrs()

    def clean(self):
        data = super().clean()
        charge = data.get("charge")
        if charge:
            if data.get("amount") and data["amount"] > charge.balance:
                self.add_error("amount", f"本次收付不能超过未结金额 ¥{charge.balance}。")
            return data
        tenancy, room = data.get("tenancy"), data.get("room")
        if tenancy and room and tenancy.room_id != room.pk:
            self.add_error("tenancy", "合同与房间不一致，请选择对应租客的合同。")
        if data.get("category") in {"rent", "deposit", "heating", "deposit_refund", "prepaid"} and not tenancy:
            self.add_error("tenancy", "请明确选择这笔租金、押金或取暖费属于哪位租客。")
        if data.get("category") == "deposit" and data.get("direction") != "income":
            self.add_error("category", "退押金请选择“押金退款”。")
        if data.get("category") == "rent" and data.get("direction") != "income":
            self.add_error("category", "租客房租是应收；付给产权方请选择“产权方租金”，其他退款请选择“其他”并注明原因。")
        if data.get("category") == "deposit_refund":
            self.add_error("category", "押金退款请在对应合同的退租结算中登记，再从待办确认付款。")
        if data.get("category") == "prepaid" and (data.get("direction") != "income" or data.get("state") != "paid"):
            self.add_error("state", "预收租金请选择收钱、已经收付，并填写实际到账金额。")
        if data.get("category") == "rent" and not data.get("period_start"):
            self.add_error("period_start", "新建房租请填写费用期间，也可以直接在收租页登记。")
        if data.get("state") == "partial":
            paid = data.get("paid_amount")
            if paid is None or paid >= (data.get("amount") or 0):
                self.add_error("paid_amount", "请填写大于零、小于总金额的实际收付金额。")
        start, end = data.get("period_start"), data.get("period_end")
        if bool(start) != bool(end) or start and end and end < start:
            self.add_error("period_end", "请填写完整且先后顺序正确的费用期间。")
        return data


@transaction.atomic
def save_entry(data):
    """Form token prevents replay; explicit bills prevent accidental rent allocation."""
    key = f"entry:{data['token']}"
    previous = Payment.objects.filter(entry_token=data["token"]).first()
    if previous:
        allocation = previous.allocations.first()
        return allocation.charge if allocation else None, previous
    charge = data.get("charge")
    if charge:
        charge = Charge.objects.select_for_update().get(pk=charge.pk)
        if charge.status == Charge.Status.VOID or charge.balance <= 0:
            raise ValueError("这笔账单已结清或作废，请刷新后核对。")
        payment = settle_charges([charge], amount=data["amount"], date=data["date"], memo=data.get("description") or charge.description)
        payment.entry_token = data["token"]
        payment.save(update_fields=["entry_token"])
        return charge, payment
    existing = Charge.objects.filter(entry_token=data["token"]).first()
    if existing:
        return existing, None
    tenancy = data.get("tenancy")
    room = data.get("room") or (tenancy.room if tenancy else None)
    category = data["category"]
    if category == "prepaid":
        payment = record_payment(direction=Payment.Direction.RECEIVE, category=Payment.Category.RENT,
            amount=data["amount"], date=data["date"], tenancy=tenancy,
            memo=data.get("description") or "预收租金，余款预存")
        payment.entry_token = data["token"]
        payment.save(update_fields=["entry_token"])
        return None, payment
    # Rent and deposit already have a bill: surface it instead of creating it twice.
    candidates = Charge.objects.filter(tenancy=tenancy, direction=data["direction"], category=category).exclude(status=Charge.Status.VOID) if tenancy else Charge.objects.none()
    if category == "deposit":
        from .services import tenancy_family_ids
        candidates = Charge.objects.filter(tenancy_id__in=tenancy_family_ids(tenancy), direction="income", category="deposit").exclude(status="void")
        if candidates.exists():
            raise ValueError("这份合同已有押金账单，请在“记录哪件事”选择原账单；收齐的押金无需重复登记。")
    elif category == "rent" and data.get("period_start") and candidates.filter(period_start__lte=data["period_end"], period_end__gte=data["period_start"]).exists():
        raise ValueError("这段期间已有房租账单，请选择原账单登记收款。")
    if category == "deposit":
        key = f"tenancy:{tenancy.pk}:deposit"
    elif category == "rent":
        from .services import rent_charge_spec
        spec = rent_charge_spec(tenancy, data["period_start"], first_month=data["period_start"] == tenancy.start_date)
        if data["period_end"] != spec["period_end"]:
            raise ValueError(f"本合同该期房租应截至 {spec['period_end']}，请核对费用期间。")
        key = spec["generated_key"]
    charge = create_charge(
        direction=data["direction"], category=category, tenancy=tenancy, room=room,
        amount=data["amount"], due_date=data["date"] if data["state"] == "pending" else data.get("due_date") or data["date"],
        period_start=data.get("period_start"), period_end=data.get("period_end"),
        description=data.get("description") or f"{room or '公寓'} {dict(Charge.Category.choices)[category]}",
        notes=data.get("notes", ""), source=Charge.Source.MANUAL, generated_key=key,
    )
    if charge.status == Charge.Status.VOID:
        raise ValueError("该期账单已作废，请核对原账单，不要重复登记。")
    charge.entry_token = data["token"]
    charge.save(update_fields=["entry_token"])
    if category == "deposit" and tenancy.deposit_amount is None:
        tenancy.deposit_amount = data["amount"]
        tenancy.save(update_fields=["deposit_amount"])
    payment = None
    if data["state"] != "pending":
        payment = settle_charges([charge], amount=data["paid_amount"] if data["state"] == "partial" else data["amount"], date=data["date"], memo=charge.description)
        payment.auto_allocate = False
        payment.entry_token = data["token"]
        payment.save(update_fields=["auto_allocate", "entry_token"])
    return charge, payment
