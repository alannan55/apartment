from decimal import Decimal

from django import forms

from .models import Broker, Charge, Payment, Person, RecurringRule, Room, Stay, Tenancy


PHONE_ERROR = "电话号码需要是 11 位数字。"
ID_ERROR = "身份证号需要是 15 位或 18 位。"


class DateInput(forms.DateInput):
    input_type = "date"


def clean_id_number_value(value):
    value = (value or "").strip()
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


class PaymentForm(BaseFormMixin, forms.ModelForm):

    class Meta:
        model = Payment
        fields = ["direction", "category", "date", "room", "amount", "memo"]
        widgets = {
            "date": DateInput,
            "memo": forms.TextInput,
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["room"].required = False
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
        self.apply_widget_attrs()

    def clean_id_number(self):
        return clean_id_number_value(self.cleaned_data["id_number"])

    def clean_phone(self):
        return clean_phone_value(self.cleaned_data.get("phone"))

    def clean_emergency_phone(self):
        return clean_phone_value(self.cleaned_data.get("emergency_phone"))


class PersonCreateForm(BaseFormMixin, forms.Form):
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

    def clean(self):
        cleaned = super().clean()
        start = cleaned.get("start_date")
        end = cleaned.get("end_date")
        stay_type = cleaned.get("stay_type")
        if stay_type != Stay.Type.MANAGER and not start:
            self.add_error("start_date", "常住或暂住人员需要填写入住开始。")
        if start and end and end < start:
            raise forms.ValidationError("入住结束日期不能早于开始日期。")
        if stay_type == Stay.Type.MANAGER:
            cleaned["end_date"] = None
            cleaned["report_note"] = "管理员"
        elif stay_type == Stay.Type.VISITOR and not cleaned.get("report_note"):
            cleaned["report_note"] = "探望、暂住"
        return cleaned

    def clean_id_number(self):
        return clean_id_number_value(self.cleaned_data["id_number"])

    def clean_phone(self):
        return clean_phone_value(self.cleaned_data.get("phone"))

    def clean_emergency_phone(self):
        return clean_phone_value(self.cleaned_data.get("emergency_phone"))


class PersonStayForm(BaseFormMixin, forms.Form):
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

    def clean(self):
        cleaned = super().clean()
        start = cleaned.get("start_date")
        end = cleaned.get("end_date")
        stay_type = cleaned.get("stay_type")
        if stay_type != Stay.Type.MANAGER and not start:
            self.add_error("start_date", "常住或暂住人员需要填写入住开始。")
        if start and end and end < start:
            raise forms.ValidationError("入住结束日期不能早于开始日期。")
        if stay_type == Stay.Type.MANAGER:
            cleaned["end_date"] = None
            cleaned["report_note"] = "管理员"
        elif stay_type == Stay.Type.VISITOR and not cleaned.get("report_note"):
            cleaned["report_note"] = "探望、暂住"
        return cleaned


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
            "planned_move_out_date",
            "planned_deposit_refund_amount",
            "monthly_rent",
            "payment_cycle",
            "deposit_amount",
            "commission_manual_amount",
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
            "move_out_date": DateInput,
            "planned_move_out_note": forms.Textarea(attrs={"rows": 2}),
            "notes": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance and self.instance.pk and self.instance.broker:
            self.fields["broker_name"].initial = self.instance.broker.name
        self.apply_widget_attrs()

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
        (Payment.Direction.RECEIVE, "收"),
        (Payment.Direction.PAY, "付"),
        (Charge.Direction.INCOME, "应收"),
        (Charge.Direction.EXPENSE, "应付"),
    ]

    direction = forms.ChoiceField(label="方向", choices=DIRECTION_CHOICES)
    category = forms.ChoiceField(label="类别", choices=Charge.Category.choices)
    room = forms.ModelChoiceField(label="房间", queryset=Room.objects.all(), required=False)
    date = forms.DateField(label="日期", widget=DateInput)
    amount = forms.DecimalField(label="金额", max_digits=12, decimal_places=2, min_value=0)
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


class ImportWorkbookForm(BaseFormMixin, forms.Form):
    file = forms.FileField(label="导入模板文件")
    clear_existing = forms.BooleanField(label="导入前清空现有业务数据", required=False, initial=True)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.apply_widget_attrs()


class RoomPersonForm(BaseFormMixin, forms.Form):
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

    def clean(self):
        cleaned = super().clean()
        start = cleaned.get("start_date")
        end = cleaned.get("end_date")
        stay_type = cleaned.get("stay_type")
        if stay_type != Stay.Type.MANAGER and not start:
            self.add_error("start_date", "常住或暂住人员需要填写入住开始。")
        if start and end and end < start:
            raise forms.ValidationError("入住结束日期不能早于开始日期。")
        if stay_type == Stay.Type.MANAGER:
            cleaned["end_date"] = None
            cleaned["report_note"] = "管理员"
        elif stay_type == Stay.Type.VISITOR and not cleaned.get("report_note"):
            cleaned["report_note"] = "探望、暂住"
        return cleaned

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
    manual_diff_amount = forms.DecimalField(label="手动补差金额", max_digits=10, decimal_places=2, min_value=0, required=False)
    note = forms.CharField(label="备注", widget=forms.Textarea(attrs={"rows": 3}), required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.apply_widget_attrs()


class CheckoutForm(BaseFormMixin, forms.Form):
    checkout_date = forms.DateField(label="退租日期", widget=DateInput)
    refund_deposit_amount = forms.DecimalField(label="退还押金", max_digits=10, decimal_places=2, min_value=0, initial=Decimal("3500.00"))
    note = forms.CharField(label="退租备注", widget=forms.Textarea(attrs={"rows": 3}), required=False)

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
    start_date = forms.DateField(label="入住开始", widget=DateInput)
    end_date = forms.DateField(label="入住结束", widget=DateInput)
    report_note = forms.CharField(label="报备备注", max_length=200, initial="探望、暂住", required=False)
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
