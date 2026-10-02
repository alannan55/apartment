from decimal import Decimal
from django.utils import timezone

from django import forms

from .models import ApartmentSettings, Broker, Charge, Payment, Person, RecurringRule, Room, Stay, Tenancy
from .reporting import text_period


PHONE_ERROR = "电话号码需要是 11 位数字。"
ID_ERROR = "身份证号需要是 15 位或 18 位。"


class DateInput(forms.DateInput):
    input_type = "date"

    def __init__(self, attrs=None, format=None):
        super().__init__(attrs=attrs, format=format or "%Y-%m-%d")


def clean_id_number_value(value):
    value = (value or "").strip()
    if not value:
        return None
    if len(value) not in {15, 18}:
        raise forms.ValidationError(ID_ERROR)
    return value.upper()


def clean_phone_value(value):
    value = (value or "").strip()
    if value and (not value.isdigit() or len(value) != 11):
        raise forms.ValidationError(PHONE_ERROR)
    return value


class BaseFormMixin:
    def apply_widget_attrs(self):
        for field in self.fields.values():
            css = field.widget.attrs.get("class", "")
            field.widget.attrs["class"] = f"{css} input".strip()


class PoliceReportRowForm(BaseFormMixin, forms.Form):
    stay_id = forms.IntegerField(widget=forms.HiddenInput)
    departure_token = forms.CharField(required=False, widget=forms.HiddenInput)
    text = forms.CharField(label="报备内容", max_length=200, required=False, widget=forms.Textarea(attrs={"rows": 2}))
    report_departure = forms.ChoiceField(label="退租是否报备", choices=[("yes", "报备"), ("no", "不报备")], required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.apply_widget_attrs()

    def clean_text(self):
        value = self.cleaned_data["text"]
        text_period(value)
        return value


class MonthlyRentCollectionForm(BaseFormMixin, forms.Form):
    month = forms.DateField(label="房租月份", input_formats=["%Y-%m"], widget=forms.DateInput(format="%Y-%m", attrs={"type": "month"}))
    date = forms.DateField(label="记账日期", widget=DateInput(), initial=timezone.localdate)
    rows = forms.MultipleChoiceField(required=False)
    action = forms.ChoiceField(choices=[("bulk", "批量收租"), ("single", "单笔收租")])
    amount = forms.DecimalField(label="本次实收", required=False, min_value=Decimal("0.01"), max_digits=12, decimal_places=2)

    def __init__(self, *args, rent_rows=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["rows"].choices = [(row["key"], row["room"].number) for row in rent_rows]
        for row in rent_rows:
            if row["draft"]:
                name = f"rent_amount_{row['key']}"
                self.fields[name] = forms.DecimalField(
                    label=f"{row['room'].number} 本期应收", required=False,
                    min_value=Decimal("0.01"), max_digits=12, decimal_places=2,
                    initial=row["amount"],
                )
        self.apply_widget_attrs()


class DepositCollectionForm(BaseFormMixin, forms.Form):
    date = forms.DateField(label="实际收款日期", widget=DateInput(), initial=timezone.localdate)
    rows = forms.MultipleChoiceField(label="房间", required=False)
    action = forms.ChoiceField(choices=[("bulk", "批量登记押金"), ("single", "单笔登记押金")])
    amount = forms.DecimalField(label="本次实收", required=False, min_value=Decimal("0.01"), max_digits=12, decimal_places=2)

    def __init__(self, *args, deposit_rows=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["rows"].choices = [(str(row["tenancy"].pk), row["room"].number) for row in deposit_rows]
        for row in deposit_rows:
            if row["draft"]:
                self.fields[f"deposit_amount_{row['tenancy'].pk}"] = forms.DecimalField(
                    label=f"{row['room'].number} 应交押金", required=False, min_value=0,
                    max_digits=10, decimal_places=2, initial=row["amount"],
                )
        self.apply_widget_attrs()


class SignContractForm(BaseFormMixin, forms.Form):
    room = forms.ModelChoiceField(label="房间", queryset=Room.objects.none())
    person_name = forms.CharField(label="主租客姓名", max_length=80)
    id_number = forms.CharField(label="身份证号", max_length=30)
    phone = forms.CharField(label="手机号", max_length=40, required=False)
    emergency_name = forms.CharField(label="紧急联系人", max_length=80, required=False)
    emergency_phone = forms.CharField(label="紧急联系人电话", max_length=40, required=False)
    emergency_address = forms.CharField(label="紧急联系人地址", max_length=200, required=False)
    start_date = forms.DateField(label="合同开始", widget=DateInput)
    end_date = forms.DateField(label="合同结束", widget=DateInput)
    monthly_rent = forms.DecimalField(label="月租金", max_digits=10, decimal_places=2, min_value=0)
    payment_cycle = forms.ChoiceField(label="付款周期", choices=Tenancy.PaymentCycle.choices, initial=Tenancy.PaymentCycle.MONTHLY)
    deposit_amount = forms.DecimalField(label="押金", max_digits=10, decimal_places=2, min_value=0, initial=Decimal("3500.00"))
    broker_name = forms.CharField(label="中介/渠道", max_length=100, required=False)
    commission_manual_amount = forms.DecimalField(label="实际佣金覆盖", max_digits=10, decimal_places=2, min_value=0, required=False)
    first_month_discount = forms.DecimalField(label="首月优惠", max_digits=10, decimal_places=2, min_value=0, initial=Decimal("0.00"), required=False)
    notes = forms.CharField(label="合同备注", widget=forms.Textarea(attrs={"rows": 3}), required=False)
    received_amount = forms.DecimalField(label="现在已收到的金额", max_digits=12, decimal_places=2, min_value=0, required=False, help_text="未收款留空；保存后自动抵扣，超出部分作为预存。")

    def __init__(self, *args, room_queryset=None, fixed_room=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["room"].queryset = room_queryset if room_queryset is not None else Room.objects.filter(status=Room.Status.VACANT)
        if fixed_room:
            self.fields["room"].queryset = Room.objects.filter(pk=fixed_room.pk)
            self.fields["room"].initial = fixed_room
            self.fields["room"].widget = forms.HiddenInput()
        placeholders = {
            "person_name": "请输入主租客姓名",
            "phone": "11 位手机号",
            "id_number": "15 或 18 位身份证号",
            "emergency_name": "请输入联系人姓名",
            "emergency_phone": "11 位手机号",
            "emergency_address": "请输入联系地址",
            "monthly_rent": "每月租金",
            "deposit_amount": "默认 3500",
            "first_month_discount": "没有优惠可填 0",
            "broker_name": "无中介可留空",
            "commission_manual_amount": "仅实际佣金不同时填写",
            "notes": "特殊约定、优惠或其它说明",
        }
        for name, placeholder in placeholders.items():
            self.fields[name].widget.attrs.setdefault("placeholder", placeholder)
        self.apply_widget_attrs()

    def clean(self):
        cleaned = super().clean()
        start = cleaned.get("start_date")
        end = cleaned.get("end_date")
        if start and end and end < start:
            raise forms.ValidationError("合同结束日期不能早于开始日期。")
        return cleaned

    def clean_id_number(self):
        return clean_id_number_value(self.cleaned_data["id_number"])

    def clean_phone(self):
        return clean_phone_value(self.cleaned_data.get("phone"))

    def clean_emergency_phone(self):
        return clean_phone_value(self.cleaned_data.get("emergency_phone"))


class RoommateForm(BaseFormMixin, forms.Form):
    name = forms.CharField(label="同住人姓名", max_length=80)
    id_number = forms.CharField(label="身份证号", max_length=30)
    phone = forms.CharField(label="手机号", max_length=40, required=False)
    emergency_name = forms.CharField(label="紧急联系人", max_length=80, required=False)
    emergency_phone = forms.CharField(label="紧急联系人电话", max_length=40, required=False)
    emergency_address = forms.CharField(label="紧急联系人地址", max_length=200, required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.apply_widget_attrs()

    def clean_id_number(self):
        return clean_id_number_value(self.cleaned_data["id_number"])

    def clean_phone(self):
        return clean_phone_value(self.cleaned_data.get("phone"))

    def clean_emergency_phone(self):
        return clean_phone_value(self.cleaned_data.get("emergency_phone"))


class PaymentForm(BaseFormMixin, forms.ModelForm):

    class Meta:
        model = Payment
        fields = ["direction", "room", "amount", "date", "category", "tenancy", "memo"]
        labels = {"direction": "记录什么", "category": "款项用途", "tenancy": "指定合同（补交往期欠款时选择）"}
        widgets = {
            "date": DateInput,
            "memo": forms.TextInput,
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["room"].required = False
        self.fields["tenancy"].required = False
        self.fields["tenancy"].queryset = Tenancy.objects.select_related("room", "primary_person")
        self.fields["amount"].min_value = Decimal("0.01")
        self.fields["direction"].choices = [(Payment.Direction.RECEIVE, "收款"), (Payment.Direction.PAY, "记支出")]
        self.fields["category"].choices = [(Payment.Category.OTHER, "自动抵扣（租金优先）"), (Payment.Category.DEPOSIT, "指定押金"), (Payment.Category.RENT, "租金及其他欠款")]
        self.apply_widget_attrs()

    def clean(self):
        cleaned = super().clean()
        room, tenancy = cleaned.get("room"), cleaned.get("tenancy")
        if tenancy and room and tenancy.room_id != room.pk:
            self.add_error("tenancy", "请选择该房间的合同。")
        if cleaned.get("amount") is not None and cleaned["amount"] <= 0:
            self.add_error("amount", "金额必须大于 0。")
        return cleaned


class RenewalForm(BaseFormMixin, forms.Form):
    end_date = forms.DateField(label="续租结束日期", widget=DateInput)
    monthly_rent = forms.DecimalField(label="新月租", max_digits=10, decimal_places=2, min_value=0)
    payment_cycle = forms.ChoiceField(label="付款周期", choices=Tenancy.PaymentCycle.choices)
    note = forms.CharField(label="续租约定", required=False, widget=forms.Textarea(attrs={"rows": 2}))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.apply_widget_attrs()


class BillPaymentEditForm(BaseFormMixin, forms.Form):
    date = forms.DateField(label="收付日期", widget=DateInput)
    amount = forms.DecimalField(label="金额", max_digits=12, decimal_places=2, min_value=Decimal("0.01"))
    memo = forms.CharField(label="备注", max_length=240, required=False)

    def __init__(self, *args, maximum=None, **kwargs):
        self.maximum = maximum
        super().__init__(*args, **kwargs)
        self.apply_widget_attrs()

    def clean_amount(self):
        amount = self.cleaned_data["amount"]
        if self.maximum is not None and amount > self.maximum:
            raise forms.ValidationError(f"金额不能超过 ¥{self.maximum}。")
        return amount


class PropertyRentRuleForm(BaseFormMixin, forms.ModelForm):
    class Meta:
        model = RecurringRule
        fields = [
            "name",
            "amount",
            "frequency",
            "day_of_month",
            "start_date",
            "end_date",
            "active",
            "notes",
        ]
        labels = {
            "name": "规则名称",
            "amount": "每期应付金额",
            "frequency": "付款周期",
            "day_of_month": "付款日",
            "start_date": "开始日期",
            "end_date": "结束日期",
            "active": "启用",
            "notes": "备注",
        }
        widgets = {
            "start_date": DateInput,
            "end_date": DateInput,
            "notes": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.apply_widget_attrs()

    def clean_day_of_month(self):
        value = self.cleaned_data["day_of_month"]
        if not 1 <= value <= 31:
            raise forms.ValidationError("付款日需要在 1 至 31 日之间。")
        return value

    def clean(self):
        cleaned = super().clean()
        start = cleaned.get("start_date")
        end = cleaned.get("end_date")
        if start and end and end < start:
            raise forms.ValidationError("结束日期不能早于开始日期。")
        return cleaned

    def save(self, commit=True):
        rule = super().save(commit=False)
        rule.direction = Charge.Direction.EXPENSE
        rule.category = Charge.Category.PROPERTY_RENT
        if commit:
            rule.save()
        return rule


class RoomForm(BaseFormMixin, forms.ModelForm):
    class Meta:
        model = Room
        fields = [
            "number",
            "status",
            "listing_price",
            "commission_base",
            "orientation",
            "floor",
            "area",
            "room_password",
            "water_fee",
            "electricity_fee",
            "property_fee",
            "internet_fee",
            "heating_fee",
            "parking_fee",
            "qr_code_image",
            "notes",
        ]
        labels = {"commission_base": "佣金基数", "qr_code_image": "自主申报二维码图片"}
        widgets = {"notes": forms.Textarea(attrs={"rows": 3})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if not self.instance.pk and not self.is_bound:
            settings = ApartmentSettings.objects.filter(pk=1).first()
            if settings:
                self.initial.update(settings.fee_defaults)
        self.apply_widget_attrs()


class AgentFeesForm(BaseFormMixin, forms.Form):
    agent_water_fee = forms.CharField(label="水费", max_length=80, required=False, widget=forms.TextInput(attrs={"placeholder": "如 9.5元/吨"}))
    agent_electricity_fee = forms.CharField(label="电费", max_length=80, required=False, widget=forms.TextInput(attrs={"placeholder": "如 1.2元/度"}))
    agent_property_fee = forms.CharField(label="物业费", max_length=80, required=False, help_text="填 0 表示免费。", widget=forms.TextInput(attrs={"placeholder": "0 表示免费"}))
    agent_internet_fee = forms.CharField(label="网费", max_length=80, required=False, help_text="填 0 表示免费。", widget=forms.TextInput(attrs={"placeholder": "0 表示免费 / 50元/月"}))
    agent_heating_fee = forms.DecimalField(label="取暖费（元/月）", max_digits=8, decimal_places=2, min_value=0, required=False)
    agent_parking_fee = forms.DecimalField(label="停车费（元/月）", max_digits=8, decimal_places=2, min_value=0, required=False)
    agent_parking_annual_fee = forms.DecimalField(label="停车费（元/包年）", max_digits=8, decimal_places=2, min_value=0, required=False, help_text="如 1440；留空只展示月付价格。")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.apply_widget_attrs()


class RoomListingPriceForm(BaseFormMixin, forms.ModelForm):
    listing_price = forms.DecimalField(label="挂牌价（元/月）", max_digits=10, decimal_places=2, min_value=0, required=False, widget=forms.NumberInput(attrs={"placeholder": "面议"}))
    commission_base = forms.DecimalField(label="佣金基数（元）", max_digits=10, decimal_places=2, min_value=0)

    class Meta:
        model = Room
        fields = ["listing_price", "commission_base"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.apply_widget_attrs()


class CommonFeesForm(BaseFormMixin, forms.ModelForm):
    apply_existing = forms.BooleanField(label="同时更新所有现有房间的费用", required=False, help_text="已生成的账单不变；以后新生成的费用使用新标准。")
    agent_electricity_fee = forms.CharField(label="中介对外电费", max_length=80, required=False, help_text="仅用于中介房态展示；留空时使用房间实际电费。")
    agent_heating_fee = forms.DecimalField(label="中介对外取暖费/月", max_digits=8, decimal_places=2, min_value=0, required=False, help_text="仅用于中介房态展示；留空时使用房间实际取暖费。")

    class Meta:
        model = Room
        fields = ["water_fee", "electricity_fee", "property_fee", "internet_fee", "heating_fee", "parking_fee"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.apply_widget_attrs()


class PersonForm(BaseFormMixin, forms.ModelForm):
    class Meta:
        model = Person
        fields = [
            "name",
            "id_number",
            "phone",
            "emergency_name",
            "emergency_phone",
            "emergency_address",
            "notes",
        ]
        widgets = {"notes": forms.Textarea(attrs={"rows": 3})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        parts = self.instance.notes.partition("[悦山资料同步 2026-10-01]")
        self.source_notes = parts[1] + parts[2]
        self.initial["notes"] = self.instance.display_notes
        self.apply_widget_attrs()

    def save(self, commit=True):
        person = super().save(commit=False)
        if self.source_notes:
            person.notes = person.notes.rstrip() + "\n" + self.source_notes
        if commit:
            person.save()
        return person

    def clean_id_number(self):
        return clean_id_number_value(self.cleaned_data["id_number"])

    def clean_phone(self):
        return clean_phone_value(self.cleaned_data.get("phone"))

    def clean_emergency_phone(self):
        return clean_phone_value(self.cleaned_data.get("emergency_phone"))


class StayDetailsMixin(BaseFormMixin):
    def __init__(self, *args, person=None, stay=None, room=None, **kwargs):
        self.person, self.stay, self.room = person, stay, room
        super().__init__(*args, **kwargs)
        self.apply_widget_attrs()
        self.fields["report_note"].widget = forms.Textarea(attrs={"rows": 2, "class": "input"})
        self.fields["start_date"].help_text = "实际住进来的日期；探望、假期暂住不清楚日期时可以留空。"
        self.fields["end_date"].label = "预计离开日期"
        self.fields["end_date"].help_text = "到期只提醒，确认离开后再登记离开。"
        self.fields["report_note"].label = "个人报备内容"
        self.fields["report_note"].help_text = "留空自动生成；暂住可填“假期暂住”。合同不足6个月的常住租客自动报备从合同开始日起1年，保留文字说明。"
        self.field_groups = [
            {"title": "人员与入住", "advanced": False, "fields": [self[n] for n in ("name", "person_name", "id_number", "phone", "room", "stay_type", "start_date", "end_date", "is_active") if n in self.fields]},
            {"title": "报备内容", "advanced": False, "fields": [self["report_note"]]},
            {"title": "联系人与补充说明（选填）", "advanced": True, "fields": [self[n] for n in ("emergency_name", "emergency_phone", "emergency_address", "notes", "person_notes", "stay_notes") if n in self.fields]},
        ]

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("stay_type") == Stay.Type.PERMANENT and not cleaned.get("start_date"):
            self.add_error("start_date", "常住人员请填写实际入住日期。")
        if cleaned.get("stay_type") == Stay.Type.MANAGER:
            cleaned["end_date"] = None
            cleaned["report_note"] = cleaned.get("report_note") or "管理员"
        room = cleaned.get("room") or self.room
        person = self.person
        if cleaned.get("id_number"):
            person = Person.objects.filter(id_number=cleaned["id_number"]).first()
        tenancy = room.active_tenancy() if room and cleaned.get("stay_type") == Stay.Type.PERMANENT else None
        candidate = Stay(person=person, room=room, tenancy=tenancy,
            stay_type=cleaned.get("stay_type") or Stay.Type.PERMANENT,
            start_date=cleaned.get("start_date"), end_date=cleaned.get("end_date"),
            is_active=cleaned.get("is_active", True), report_note=cleaned.get("report_note", ""),
            pk=self.stay.pk if self.stay else None)
        try:
            candidate.clean()
        except forms.ValidationError as exc:
            if hasattr(exc, "error_dict"):
                for key, errors in exc.error_dict.items():
                    self.add_error(key if key in self.fields else None, errors)
            else:
                self.add_error(None, exc)
        return cleaned


class PersonCreateForm(StayDetailsMixin, forms.Form):
    name = forms.CharField(label="姓名", max_length=80)
    id_number = forms.CharField(label="身份证号", max_length=30)
    phone = forms.CharField(label="手机号", max_length=40, required=False)
    emergency_name = forms.CharField(label="紧急联系人", max_length=80, required=False)
    emergency_phone = forms.CharField(label="紧急联系人电话", max_length=40, required=False)
    emergency_address = forms.CharField(label="紧急联系人地址", max_length=200, required=False)
    notes = forms.CharField(label="人员备注", widget=forms.Textarea(attrs={"rows": 2}), required=False)
    room = forms.ModelChoiceField(label="房间", queryset=Room.objects.all())
    stay_type = forms.ChoiceField(label="入住类型", choices=Stay.Type.choices, initial=Stay.Type.PERMANENT)
    start_date = forms.DateField(label="入住开始", widget=DateInput, required=False)
    end_date = forms.DateField(label="入住结束", widget=DateInput, required=False)
    report_note = forms.CharField(label="报备备注", max_length=200, required=False)
    stay_notes = forms.CharField(label="入住备注", widget=forms.Textarea(attrs={"rows": 2}), required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.apply_widget_attrs()

    def clean_id_number(self):
        return clean_id_number_value(self.cleaned_data["id_number"])

    def clean_phone(self):
        return clean_phone_value(self.cleaned_data.get("phone"))

    def clean_emergency_phone(self):
        return clean_phone_value(self.cleaned_data.get("emergency_phone"))


class PersonStayForm(StayDetailsMixin, forms.Form):
    room = forms.ModelChoiceField(label="房间", queryset=Room.objects.all())
    stay_type = forms.ChoiceField(label="入住类型", choices=Stay.Type.choices, initial=Stay.Type.PERMANENT)
    start_date = forms.DateField(label="入住开始", widget=DateInput, required=False)
    end_date = forms.DateField(label="入住结束", widget=DateInput, required=False)
    is_active = forms.BooleanField(label="当前在住", required=False, initial=True)
    report_note = forms.CharField(label="报备备注", max_length=200, required=False)
    notes = forms.CharField(label="入住备注", widget=forms.Textarea(attrs={"rows": 3}), required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.apply_widget_attrs()


class TenancyEditForm(BaseFormMixin, forms.ModelForm):
    broker_name = forms.CharField(label="中介/渠道", max_length=100, required=False)

    class Meta:
        model = Tenancy
        fields = [
            "room",
            "primary_person",
            "start_date",
            "end_date",
            "billing_start_date",
            "billing_enabled",
            "planned_move_out_date",
            "planned_deposit_refund_amount",
            "monthly_rent",
            "payment_cycle",
            "deposit_amount",
            "commission_manual_amount",
            "police_report_start_date",
            "police_report_end_date",
            "status",
            "move_out_date",
            "planned_move_out_note",
            "notes",
        ]
        widgets = {
            "start_date": DateInput,
            "end_date": DateInput,
            "billing_start_date": DateInput,
            "planned_move_out_date": DateInput,
            "police_report_end_date": DateInput,
            "police_report_start_date": DateInput,
            "move_out_date": DateInput,
            "planned_move_out_note": forms.Textarea(attrs={"rows": 2}),
            "notes": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance and self.instance.pk and self.instance.broker:
            self.fields["broker_name"].initial = self.instance.broker.name
        self.apply_widget_attrs()
        self.fields["start_date"].label = "收租开始日期"
        self.fields["end_date"].label = "收租结束日期"
        self.fields["start_date"].help_text = "按实际收租周期填写，可以与实际入住和报备日期不同。"
        self.fields["police_report_start_date"].help_text = "留空使用合同开始日；合同不足6个月时，报备起日统一使用合同开始日。"
        self.fields["police_report_end_date"].help_text = "留空自动生成；合同不足6个月时，报备期间统一为从合同开始日起1年，实际合同不变。"
        self.field_groups = [
            {"title": "房间与收租", "advanced": False, "fields": [self[n] for n in ("room", "primary_person", "start_date", "end_date", "monthly_rent", "payment_cycle")]},
            {"title": "独立报备日期（按需填写）", "advanced": True, "fields": [self[n] for n in ("police_report_start_date", "police_report_end_date")]},
            {"title": "押金与计费设置", "advanced": True, "fields": [self[n] for n in ("deposit_amount", "billing_start_date", "billing_enabled", "broker_name", "commission_manual_amount")]},
            {"title": "退租计划与补充说明", "advanced": True, "fields": [self[n] for n in ("status", "planned_move_out_date", "planned_deposit_refund_amount", "move_out_date", "planned_move_out_note", "notes")]},
        ]

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("billing_enabled"):
            if cleaned.get("deposit_amount") is None:
                self.add_error("deposit_amount", "启用计费前请补充押金金额。")
            if not cleaned.get("billing_start_date"):
                self.add_error("billing_start_date", "启用计费前请确认计费起始日期。")
        return cleaned

    def save(self, commit=True):
        tenancy = super().save(commit=False)
        broker_name = self.cleaned_data.get("broker_name", "").strip()
        tenancy.broker = Broker.objects.get_or_create(name=broker_name)[0] if broker_name else None
        if tenancy.room_id and not tenancy.commission_base:
            tenancy.commission_base = tenancy.room.commission_base
        if commit:
            tenancy.save()
            self.save_m2m()
        return tenancy


class ManualChargeForm(BaseFormMixin, forms.Form):
    DIRECTION_CHOICES = [
        (Charge.Direction.INCOME, "以后要收的钱"),
        (Charge.Direction.EXPENSE, "以后要付的钱"),
    ]

    direction = forms.ChoiceField(label="方向", choices=DIRECTION_CHOICES)
    category = forms.ChoiceField(label="类别", choices=Charge.Category.choices)
    room = forms.ModelChoiceField(label="房间", queryset=Room.objects.all(), required=False)
    date = forms.DateField(label="日期", widget=DateInput)
    amount = forms.DecimalField(label="金额", max_digits=12, decimal_places=2, min_value=Decimal("0.01"))
    description = forms.CharField(label="说明", max_length=200, required=False)
    notes = forms.CharField(label="备注", widget=forms.Textarea(attrs={"rows": 3}), required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.apply_widget_attrs()


class ChargeEditForm(BaseFormMixin, forms.ModelForm):
    class Meta:
        model = Charge
        fields = ["direction", "category", "room", "due_date", "amount", "description", "status", "notes"]
        widgets = {
            "due_date": DateInput,
            "notes": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["room"].required = False
        self.apply_widget_attrs()

    def clean(self):
        cleaned = super().clean()
        if self.instance.pk and self.instance.allocations.exists():
            if cleaned.get("amount") is not None and cleaned["amount"] < self.instance.allocated_amount:
                self.add_error("amount", "应收付金额不能小于已收付金额，请先修改对应流水。")
            if cleaned.get("status") == Charge.Status.VOID:
                self.add_error("status", "这笔账单已有收付款，请先撤销相应流水。")
            for field in ("direction", "category", "room"):
                if field in self.changed_data:
                    self.add_error(field, "已有收付款的账单不能更换归属或类别，请先撤销相应流水。")
        return cleaned


class ImportWorkbookForm(BaseFormMixin, forms.Form):
    file = forms.FileField(label="导入模板文件")
    clear_existing = forms.BooleanField(label="导入前清空现有业务数据", required=False, initial=True)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.apply_widget_attrs()


class RoomPersonForm(StayDetailsMixin, forms.Form):
    stay_type = forms.ChoiceField(label="入住类型", choices=Stay.Type.choices, initial=Stay.Type.PERMANENT)
    person_name = forms.CharField(label="姓名", max_length=80)
    id_number = forms.CharField(label="身份证号", max_length=30)
    phone = forms.CharField(label="手机号", max_length=40, required=False)
    emergency_name = forms.CharField(label="紧急联系人", max_length=80, required=False)
    emergency_phone = forms.CharField(label="紧急联系人电话", max_length=40, required=False)
    emergency_address = forms.CharField(label="紧急联系人地址", max_length=200, required=False)
    start_date = forms.DateField(label="入住开始", widget=DateInput, required=False)
    end_date = forms.DateField(label="入住结束", widget=DateInput, required=False)
    report_note = forms.CharField(label="报备备注", max_length=200, required=False)
    person_notes = forms.CharField(label="人员备注", widget=forms.Textarea(attrs={"rows": 2}), required=False)
    stay_notes = forms.CharField(label="入住备注", widget=forms.Textarea(attrs={"rows": 2}), required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.apply_widget_attrs()

    def clean_id_number(self):
        return clean_id_number_value(self.cleaned_data["id_number"])

    def clean_phone(self):
        return clean_phone_value(self.cleaned_data.get("phone"))

    def clean_emergency_phone(self):
        return clean_phone_value(self.cleaned_data.get("emergency_phone"))


class MoveRoomForm(BaseFormMixin, forms.Form):
    new_room = forms.ModelChoiceField(label="新房间", queryset=Room.objects.filter(status=Room.Status.VACANT))
    move_date = forms.DateField(label="换房日期", widget=DateInput)
    new_monthly_rent = forms.DecimalField(label="新月租", max_digits=10, decimal_places=2, min_value=0)
    manual_diff_amount = forms.DecimalField(label="调整后差价（留空按系统计算）", max_digits=10, decimal_places=2, required=False, help_text="正数为补收，负数为应退。")
    note = forms.CharField(label="备注", widget=forms.Textarea(attrs={"rows": 3}), required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.apply_widget_attrs()


class CheckoutForm(BaseFormMixin, forms.Form):
    checkout_date = forms.DateField(label="退租日期", widget=DateInput)
    refund_deposit_amount = forms.DecimalField(label="退还押金", max_digits=10, decimal_places=2, min_value=0, initial=Decimal("3500.00"))
    note = forms.CharField(label="退租备注", widget=forms.Textarea(attrs={"rows": 3}), required=False)

    def clean_checkout_date(self):
        value = self.cleaned_data["checkout_date"]
        if value > timezone.localdate():
            raise forms.ValidationError("尚未搬走请保存预计退租，实际退租日期不能晚于今天。")
        return value

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.apply_widget_attrs()


class PlannedCheckoutForm(BaseFormMixin, forms.Form):
    planned_move_out_date = forms.DateField(label="预计退租日期", widget=DateInput)
    planned_deposit_refund_amount = forms.DecimalField(
        label="预计退还押金",
        max_digits=10,
        decimal_places=2,
        min_value=0,
        initial=Decimal("3500.00"),
    )
    note = forms.CharField(label="预计退租备注", widget=forms.Textarea(attrs={"rows": 3}), required=False)

    def __init__(self, *args, tenancy=None, **kwargs):
        self.tenancy = tenancy
        super().__init__(*args, **kwargs)
        self.apply_widget_attrs()

    def clean_planned_move_out_date(self):
        planned_date = self.cleaned_data["planned_move_out_date"]
        if self.tenancy and planned_date < self.tenancy.start_date:
            raise forms.ValidationError("预计退租日期不能早于合同开始日期。")
        return planned_date


class VisitorStayForm(BaseFormMixin, forms.Form):
    room = forms.ModelChoiceField(label="房间", queryset=Room.objects.all())
    person_name = forms.CharField(label="姓名", max_length=80)
    id_number = forms.CharField(label="身份证号", max_length=30)
    phone = forms.CharField(label="联系电话", max_length=40, required=False)
    start_date = forms.DateField(label="实际入住日期", widget=DateInput, required=False, help_text="不清楚日期时可以留空。")
    end_date = forms.DateField(label="预计离开日期", widget=DateInput, required=False, help_text="到期只提醒，确认离开后再登记。")
    report_note = forms.CharField(label="个人报备内容", max_length=200, required=False, help_text="可填“假期暂住”“偶尔探望女友”；留空自动生成。")
    notes = forms.CharField(label="备注", widget=forms.Textarea(attrs={"rows": 3}), required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.apply_widget_attrs()

    def clean(self):
        cleaned = super().clean()
        start = cleaned.get("start_date")
        end = cleaned.get("end_date")
        if start and end and end < start:
            raise forms.ValidationError("结束日期不能早于开始日期。")
        person = Person.objects.filter(id_number=cleaned.get("id_number")).first() if cleaned.get("id_number") else None
        try:
            Stay(person=person, room=cleaned.get("room"), stay_type=Stay.Type.VISITOR,
                 start_date=start, end_date=end, report_note=cleaned.get("report_note", "")).clean()
        except forms.ValidationError as exc:
            self.add_error(None, "；".join(exc.messages))
        return cleaned

    def clean_id_number(self):
        return clean_id_number_value(self.cleaned_data["id_number"])

    def clean_phone(self):
        return clean_phone_value(self.cleaned_data.get("phone"))


class StayForm(BaseFormMixin, forms.ModelForm):
    class Meta:
        model = Stay
        fields = ["person", "room", "tenancy", "stay_type", "start_date", "end_date", "is_active", "report_note", "notes"]
        widgets = {
            "start_date": DateInput,
            "end_date": DateInput,
            "notes": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.apply_widget_attrs()
