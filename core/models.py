from decimal import Decimal
from datetime import timedelta
import json
from uuid import uuid4

from django.core.validators import MinValueValidator
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q, Sum
from django.utils import timezone

from .date_utils import add_months, contract_months, money
from .reporting import replace_text_period, shorter_than_months, text_period, validate_report_period


class ApartmentSettings(models.Model):
    fee_defaults = models.JSONField(default=dict)


class Room(models.Model):
    class Status(models.TextChoices):
        VACANT = "vacant", "空房"
        OCCUPIED = "occupied", "在住"
        EXPIRING = "expiring", "合同即将到期"
        SELF_USE = "self_use", "自用"
        MAINTENANCE = "maintenance", "维修中"

    class Floor(models.TextChoices):
        FIRST = "1层", "1层"
        SECOND = "2层", "2层"

    number = models.CharField("房间号", max_length=20, unique=True)
    area = models.DecimalField("面积", max_digits=8, decimal_places=2, null=True, blank=True)
    orientation = models.CharField("朝向", max_length=30, blank=True)
    floor = models.CharField("楼层", max_length=30, choices=Floor.choices, default=Floor.SECOND)
    listing_price = models.DecimalField("挂牌价", max_digits=10, decimal_places=2, null=True, blank=True)
    commission_base = models.DecimalField("默认佣金", max_digits=10, decimal_places=2, default=Decimal("2000.00"))
    status = models.CharField("房态", max_length=20, choices=Status.choices, default=Status.VACANT)
    room_password = models.CharField("房间密码", max_length=80, blank=True)
    qr_code_url = models.URLField("自主申报二维码", blank=True)
    qr_code_image = models.ImageField("自主申报二维码图片", upload_to="room_qr_codes/", blank=True, null=True)
    water_fee = models.CharField("水费", max_length=80, default="9.5/吨")
    electricity_fee = models.CharField("电费", max_length=80, default="1.4/度")
    property_fee = models.CharField("物业费", max_length=80, default="免")
    internet_fee = models.CharField("网费", max_length=80, default="免")
    heating_fee = models.DecimalField("取暖费/月", max_digits=8, decimal_places=2, default=Decimal("380.00"))
    parking_fee = models.DecimalField("停车费/月", max_digits=8, decimal_places=2, default=Decimal("150.00"))
    notes = models.TextField("备注", blank=True)

    class Meta:
        ordering = ["number"]
        verbose_name = "房间"
        verbose_name_plural = "房间"

    def __str__(self):
        return self.number

    def active_tenancy(self, today=None):
        today = today or timezone.localdate()
        return (
            self.tenancies.filter(
                status=Tenancy.Status.ACTIVE,
                start_date__lte=today,
            )
            .order_by("-start_date")
            .first()
        )

    def refresh_status(self, today=None, save=True):
        today = today or timezone.localdate()
        if self.status in {self.Status.SELF_USE, self.Status.MAINTENANCE}:
            return self.status
        previous_status = self.status
        tenancy = self.active_tenancy(today)
        if not tenancy:
            if self.stays.current(today).exists():
                self.status = self.Status.OCCUPIED
            elif self.tenancies.filter(status__in=[Tenancy.Status.UPCOMING, Tenancy.Status.ACTIVE], start_date__gt=today).exists():
                self.status = self.Status.OCCUPIED
            elif not self.tenancies.exists() and self.status in {self.Status.OCCUPIED, self.Status.EXPIRING}:
                return self.status
            else:
                self.status = self.Status.VACANT
        elif (tenancy.room_status_date - today).days <= 30:
            self.status = self.Status.EXPIRING
        else:
            self.status = self.Status.OCCUPIED
        if save and self.status != previous_status:
            self.save(update_fields=["status"])
        return self.status


class Person(models.Model):
    name = models.CharField("姓名", max_length=80)
    id_number = models.CharField("身份证号", max_length=30, unique=True, null=True, blank=True)
    phone = models.CharField("手机号", max_length=40, blank=True)
    emergency_name = models.CharField("紧急联系人", max_length=80, blank=True)
    emergency_phone = models.CharField("紧急联系人电话", max_length=40, blank=True)
    emergency_address = models.CharField("紧急联系人地址", max_length=200, blank=True)
    notes = models.TextField("备注", blank=True)

    class Meta:
        ordering = ["name"]
        verbose_name = "人员"
        verbose_name_plural = "人员"

    def __str__(self):
        return f"{self.name} ({self.id_number})"

    def save(self, *args, **kwargs):
        if self.id_number:
            self.id_number = self.id_number.strip().upper()
        return super().save(*args, **kwargs)

    @property
    def display_notes(self):
        text, marker, source = self.notes.partition("[悦山资料同步 2026-10-01]")
        if marker:
            try:
                note = json.loads(source).get("residents_note") or ""
                return text.strip() or note
            except (ValueError, TypeError, AttributeError):
                pass
        return self.notes


class Broker(models.Model):
    name = models.CharField("中介/渠道", max_length=100, unique=True)
    contact = models.CharField("联系方式", max_length=80, blank=True)
    notes = models.TextField("备注", blank=True)

    class Meta:
        ordering = ["name"]
        verbose_name = "中介"
        verbose_name_plural = "中介"

    def __str__(self):
        return self.name


class Tenancy(models.Model):
    class Status(models.TextChoices):
        UPCOMING = "upcoming", "未入住"
        ACTIVE = "active", "在租"
        ENDED = "ended", "已退租"

    class PaymentCycle(models.TextChoices):
        MONTHLY = "monthly", "月付"
        QUARTERLY = "quarterly", "季付"
        HALF_YEAR = "half_year", "半年付"
        YEARLY = "yearly", "年付"

    room = models.ForeignKey(Room, verbose_name="房间", related_name="tenancies", on_delete=models.PROTECT)
    previous_tenancy = models.OneToOneField("self", verbose_name="续租前合同", related_name="renewal", null=True, blank=True, on_delete=models.PROTECT)
    primary_person = models.ForeignKey(Person, verbose_name="主租客", related_name="primary_tenancies", on_delete=models.PROTECT)
    start_date = models.DateField("合同开始日期")
    end_date = models.DateField("合同结束日期")
    billing_start_date = models.DateField("系统计费起始日期", null=True, blank=True)
    billing_enabled = models.BooleanField("启用自动计费", default=True, help_text="导入资料待补时关闭；确认押金、期初账务及计费起点后启用。")
    planned_move_out_date = models.DateField("预计退租日期", null=True, blank=True)
    planned_deposit_refund_amount = models.DecimalField("预计退还押金", max_digits=10, decimal_places=2, null=True, blank=True)
    planned_move_out_note = models.TextField("预计退租备注", blank=True)
    move_out_date = models.DateField("实际退租日期", null=True, blank=True)
    monthly_rent = models.DecimalField("月租金", max_digits=10, decimal_places=2, validators=[MinValueValidator(0)])
    payment_cycle = models.CharField("付款周期", max_length=20, choices=PaymentCycle.choices, default=PaymentCycle.MONTHLY)
    deposit_amount = models.DecimalField("押金", max_digits=10, decimal_places=2, default=Decimal("3500.00"), null=True, blank=True)
    broker = models.ForeignKey(Broker, verbose_name="中介", related_name="tenancies", null=True, blank=True, on_delete=models.SET_NULL)
    commission_base = models.DecimalField("佣金基数", max_digits=10, decimal_places=2, default=Decimal("2000.00"))
    commission_manual_amount = models.DecimalField("佣金手动金额", max_digits=10, decimal_places=2, null=True, blank=True)
    commission_due_date = models.DateField("佣金应付日期", null=True, blank=True)
    commission_paid_date = models.DateField("佣金实付日期", null=True, blank=True)
    police_report_start_date = models.DateField("报备开始日期", null=True, blank=True)
    police_report_end_date = models.DateField("报备结束日期", null=True, blank=True)
    status = models.CharField("状态", max_length=20, choices=Status.choices, default=Status.ACTIVE)
    notes = models.TextField("备注", blank=True)
    created_at = models.DateTimeField("创建时间", auto_now_add=True)
    updated_at = models.DateTimeField("更新时间", auto_now=True)

    class Meta:
        ordering = ["-start_date", "room__number"]
        verbose_name = "合同"
        verbose_name_plural = "合同"

    def __str__(self):
        return f"{self.room} {self.primary_person.name} {self.start_date:%Y-%m-%d}"

    @property
    def is_short_term(self):
        # Billing keeps its existing rounded-month convention.
        return contract_months(self.start_date, self.end_date) < 6

    @property
    def requires_year_police_report(self):
        return shorter_than_months(self.start_date, self.end_date, 6)

    @property
    def police_start_date(self):
        if self.requires_year_police_report:
            return self.start_date
        return self.police_report_start_date or self.start_date

    @property
    def police_end_date(self):
        if self.requires_year_police_report:
            return add_months(self.start_date, 12) - timedelta(days=1)
        return self.police_report_end_date or self.end_date

    @property
    def contract_text_for_report(self):
        return f"{self.police_start_date:%Y.%m.%d}-{self.police_end_date:%Y.%m.%d}"

    def clean(self):
        super().clean()
        if self.start_date and self.end_date:
            if self.end_date < self.start_date:
                raise ValidationError({"end_date": "收租结束日期不能早于开始日期。"})
            try:
                validate_report_period(self.police_report_start_date or self.start_date,
                                       self.police_report_end_date or self.end_date)
            except ValidationError as exc:
                raise ValidationError({"police_report_end_date": exc.messages}) from exc

    def save(self, *args, **kwargs):
        self.clean()
        return super().save(*args, **kwargs)

    @property
    def room_status_date(self):
        return self.planned_move_out_date or self.end_date

    @property
    def checkout_plan_text(self):
        if not self.planned_move_out_date:
            return ""
        amount = self.planned_deposit_refund_amount
        amount_text = f"，预计退还押金 ¥{money(amount)}" if amount is not None else ""
        return f"预计 {self.planned_move_out_date:%Y-%m-%d} 退租{amount_text}"

    @property
    def balance(self):
        charges = self.charges.filter(direction=Charge.Direction.INCOME, due_date__lte=timezone.localdate()).exclude(status=Charge.Status.VOID)
        return money(sum((charge.balance for charge in charges), Decimal("0.00")))


class StayQuerySet(models.QuerySet):
    def current(self, today=None):
        today = today or timezone.localdate()
        return self.filter(is_active=True).filter(Q(start_date__isnull=True) | Q(start_date__lte=today))


class Stay(models.Model):
    class Type(models.TextChoices):
        PERMANENT = "permanent", "常住"
        VISITOR = "visitor", "探望/暂住"
        MANAGER = "manager", "管理员"

    person = models.ForeignKey(Person, verbose_name="人员", related_name="stays", on_delete=models.CASCADE)
    room = models.ForeignKey(Room, verbose_name="房间", related_name="stays", on_delete=models.PROTECT)
    tenancy = models.ForeignKey(Tenancy, verbose_name="合同", related_name="stays", null=True, blank=True, on_delete=models.CASCADE)
    stay_type = models.CharField("入住类型", max_length=20, choices=Type.choices, default=Type.PERMANENT)
    start_date = models.DateField("实际入住日期", null=True, blank=True)
    end_date = models.DateField("入住结束日期", null=True, blank=True)
    is_active = models.BooleanField("当前在住", default=True)
    report_note = models.CharField("个人报备内容", max_length=200, blank=True, help_text="留空自动生成；可填写独立期间或“假期暂住”等原文。")
    notes = models.TextField("备注", blank=True)
    police_departure_token = models.UUIDField("本次离开报备标识", null=True, blank=True, editable=False)
    police_departure_reported_at = models.DateTimeField("离开报备确认时间", null=True, blank=True, editable=False)
    police_departure_required = models.BooleanField("退租是否报备", default=True)
    police_report_text_override = models.CharField("手动报备内容", max_length=200, blank=True)
    objects = StayQuerySet.as_manager()

    class Meta:
        ordering = ["room__number", "person__name"]
        verbose_name = "入住记录"
        verbose_name_plural = "入住记录"
        constraints = [models.UniqueConstraint(fields=["person"], condition=Q(is_active=True), name="one_active_stay_per_person")]

    def __str__(self):
        return f"{self.room} {self.person.name} {self.get_stay_type_display()}"

    def active_on(self, today=None):
        today = today or timezone.localdate()
        return self.is_active and (self.start_date is None or self.start_date <= today)

    @property
    def report_text(self):
        return self.report_text_for_tenancy(self.tenancy if self.tenancy_id else None)

    def report_text_for_tenancy(self, tenancy):
        if self.police_report_text_override.strip():
            return self.police_report_text_override.strip()
        if self.stay_type == self.Type.PERMANENT and tenancy and tenancy.requires_year_police_report:
            return replace_text_period(self.report_note.strip(), tenancy.police_start_date, tenancy.police_end_date)
        if self.report_note.strip():
            return self.report_note.strip()
        if self.stay_type == self.Type.MANAGER:
            return "管理员"
        if self.stay_type == self.Type.PERMANENT and tenancy:
            return tenancy.contract_text_for_report
        if self.start_date:
            period = f"{self.start_date:%Y.%m.%d}"
            period += f"-{self.end_date:%Y.%m.%d}" if self.end_date else "起"
            return f"探望、暂住 {period}" if self.stay_type == self.Type.VISITOR else period
        return "探望、暂住" if self.stay_type == self.Type.VISITOR else "入住日期未提供"

    def clean(self):
        super().clean()
        if self.start_date and self.end_date and self.end_date < self.start_date:
            raise ValidationError({"end_date": "入住结束日期不能早于开始日期。"})
        if self.is_active and self.person_id and Stay.objects.filter(person_id=self.person_id, is_active=True).exclude(pk=self.pk).exists():
            raise ValidationError("该人员已有未结束的入住记录，请先登记离开或使用换房功能。")
        primary = self.tenancy if self.tenancy_id and self.person_id == self.tenancy.primary_person_id else None
        if self.is_active and not primary and self.person_id and self.room_id:
            primary = Tenancy.objects.filter(primary_person_id=self.person_id, room_id=self.room_id, status=Tenancy.Status.ACTIVE).first()
        self.validate_report(primary)

    def validate_report(self, primary=None):
        try:
            text_period(self.police_report_text_override or self.report_note)
            if self.is_active and primary:
                if self.stay_type != self.Type.PERMANENT:
                    raise ValidationError("主租客应登记为常住；探望/暂住用于其他人员。")
        except ValidationError as exc:
            raise ValidationError({"report_note": exc.messages}) from exc

    def save(self, *args, **kwargs):
        self.clean()
        previous = Stay.objects.filter(pk=self.pk).values(
            "is_active", "person_id", "room_id", "end_date", "police_departure_token", "police_departure_reported_at"
        ).first() if self.pk else None
        if self.is_active:
            if previous and not previous["is_active"]:
                self.police_report_text_override = ""
            self.police_departure_required = True
            self.police_departure_token = None
            self.police_departure_reported_at = None
        elif previous is None or previous["is_active"] or any(
            previous[field] != getattr(self, field) for field in ("person_id", "room_id", "end_date")
        ):
            self.police_departure_token = uuid4()
            self.police_departure_reported_at = None
            self.police_departure_required = True
            self.police_report_text_override = ""
        else:
            self.police_departure_token = previous["police_departure_token"]
            self.police_departure_reported_at = previous["police_departure_reported_at"]
        if kwargs.get("update_fields") is not None:
            kwargs["update_fields"] = set(kwargs["update_fields"]) | {
                "police_departure_token", "police_departure_reported_at", "police_departure_required", "police_report_text_override"
            }
        return super().save(*args, **kwargs)


class PoliceReportExport(models.Model):
    report_date = models.DateField("报备日期")
    rows = models.JSONField("报备人员快照", default=list)
    created_at = models.DateTimeField("生成时间", auto_now_add=True)
    sent_at = models.DateTimeField("发送确认时间", null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        verbose_name = "警务报备记录"
        verbose_name_plural = "警务报备记录"

    @property
    def departure_count(self):
        return sum(bool(row["departure_token"]) for row in self.rows)


class Charge(models.Model):
    class Direction(models.TextChoices):
        INCOME = "income", "应收"
        EXPENSE = "expense", "应付"

    class Category(models.TextChoices):
        RENT = "rent", "房租"
        DEPOSIT = "deposit", "押金"
        HEATING = "heating", "取暖费"
        COMMISSION = "commission", "中介佣金"
        WAGE = "wage", "工资"
        PROPERTY_RENT = "property_rent", "产权方租金"
        REPAIR = "repair", "维修"
        UTILITY = "utility", "水电等费用"
        MOVE_DIFF = "move_diff", "换房补差"
        DEPOSIT_REFUND = "deposit_refund", "押金退款"
        OTHER = "other", "其他"

    class Status(models.TextChoices):
        OPEN = "open", "待处理"
        PARTIAL = "partial", "部分处理"
        PAID = "paid", "已结清"
        VOID = "void", "已作废"

    class Source(models.TextChoices):
        AUTO = "auto", "系统生成"
        MANUAL = "manual", "手动录入"
        ADJUSTMENT = "adjustment", "调整生成"
        OPENING = "opening", "期初导入"

    direction = models.CharField("方向", max_length=10, choices=Direction.choices)
    category = models.CharField("类别", max_length=30, choices=Category.choices)
    tenancy = models.ForeignKey(Tenancy, verbose_name="合同", related_name="charges", null=True, blank=True, on_delete=models.CASCADE)
    room = models.ForeignKey(Room, verbose_name="房间", related_name="charges", null=True, blank=True, on_delete=models.SET_NULL)
    person = models.ForeignKey(Person, verbose_name="人员", related_name="charges", null=True, blank=True, on_delete=models.SET_NULL)
    due_date = models.DateField("应收/应付日期")
    period_start = models.DateField("周期开始", null=True, blank=True)
    period_end = models.DateField("周期结束", null=True, blank=True)
    amount = models.DecimalField("金额", max_digits=12, decimal_places=2, validators=[MinValueValidator(0)])
    description = models.CharField("说明", max_length=200)
    status = models.CharField("状态", max_length=20, choices=Status.choices, default=Status.OPEN)
    source = models.CharField("来源", max_length=20, choices=Source.choices, default=Source.AUTO)
    generated_key = models.CharField("生成键", max_length=120, unique=True, null=True, blank=True)
    notes = models.TextField("备注", blank=True)
    created_at = models.DateTimeField("创建时间", auto_now_add=True)

    class Meta:
        ordering = ["due_date", "id"]
        indexes = [
            models.Index(fields=["direction", "status", "due_date"]),
            models.Index(fields=["category", "due_date"]),
        ]
        verbose_name = "账单"
        verbose_name_plural = "账单"

    def __str__(self):
        return f"{self.get_direction_display()} {self.get_category_display()} {self.amount}"

    @property
    def allocated_amount(self):
        if "allocations" in getattr(self, "_prefetched_objects_cache", {}):
            return money(sum((item.amount for item in self.allocations.all()), Decimal("0.00")))
        total = self.allocations.aggregate(total=Sum("amount"))["total"] or Decimal("0.00")
        return money(total)

    @property
    def balance(self):
        return money(self.amount - self.allocated_amount)

    def refresh_status(self, save=True):
        if self.status == self.Status.VOID:
            return self.status
        paid = self.allocated_amount
        if paid <= 0:
            self.status = self.Status.OPEN
        elif paid < self.amount:
            self.status = self.Status.PARTIAL
        else:
            self.status = self.Status.PAID
        if save:
            self.save(update_fields=["status"])
        return self.status


class Payment(models.Model):
    class Direction(models.TextChoices):
        RECEIVE = "receive", "收"
        PAY = "pay", "付"

    class Category(models.TextChoices):
        DEPOSIT = "deposit", "押金"
        RENT = "rent", "租金"
        OTHER = "other", "其它"

    direction = models.CharField("方向", max_length=10, choices=Direction.choices)
    category = models.CharField("款项类型", max_length=20, choices=Category.choices, default=Category.OTHER)
    date = models.DateField("日期", default=timezone.localdate)
    amount = models.DecimalField("金额", max_digits=12, decimal_places=2, validators=[MinValueValidator(0)])
    tenancy = models.ForeignKey(Tenancy, verbose_name="合同", related_name="payments", null=True, blank=True, on_delete=models.SET_NULL)
    room = models.ForeignKey(Room, verbose_name="房间", related_name="payments", null=True, blank=True, on_delete=models.SET_NULL)
    person = models.ForeignKey(Person, verbose_name="人员", related_name="payments", null=True, blank=True, on_delete=models.SET_NULL)
    method = models.CharField("方式", max_length=60, blank=True)
    memo = models.CharField("备注", max_length=240, blank=True)
    auto_allocate = models.BooleanField("自动抵扣", default=True)
    created_at = models.DateTimeField("创建时间", auto_now_add=True)

    class Meta:
        ordering = ["-date", "-id"]
        verbose_name = "收付款"
        verbose_name_plural = "收付款"

    def __str__(self):
        return f"{self.get_direction_display()} {self.amount} {self.date:%Y-%m-%d}"

    @property
    def allocated_amount(self):
        if "allocations" in getattr(self, "_prefetched_objects_cache", {}):
            return money(sum((item.amount for item in self.allocations.all()), Decimal("0.00")))
        total = self.allocations.aggregate(total=Sum("amount"))["total"] or Decimal("0.00")
        return money(total)

    @property
    def unallocated_amount(self):
        return money(self.amount - self.allocated_amount)


class Allocation(models.Model):
    payment = models.ForeignKey(Payment, verbose_name="收付款", related_name="allocations", on_delete=models.CASCADE)
    charge = models.ForeignKey(Charge, verbose_name="账单", related_name="allocations", on_delete=models.CASCADE)
    amount = models.DecimalField("核销金额", max_digits=12, decimal_places=2, validators=[MinValueValidator(0)])
    created_at = models.DateTimeField("创建时间", auto_now_add=True)

    class Meta:
        ordering = ["created_at", "id"]
        verbose_name = "核销"
        verbose_name_plural = "核销"

    def __str__(self):
        return f"{self.payment_id} -> {self.charge_id}: {self.amount}"


class Adjustment(models.Model):
    class Type(models.TextChoices):
        DISCOUNT = "discount", "优惠/减免"
        MANUAL_OVERRIDE = "manual_override", "手动改价"
        MOVE_DIFF = "move_diff", "换房补差"
        DEPOSIT_DEDUCTION = "deposit_deduction", "押金扣款"
        OTHER = "other", "其他调整"

    tenancy = models.ForeignKey(Tenancy, verbose_name="合同", related_name="adjustments", null=True, blank=True, on_delete=models.CASCADE)
    charge = models.ForeignKey(Charge, verbose_name="账单", related_name="adjustments", null=True, blank=True, on_delete=models.CASCADE)
    room = models.ForeignKey(Room, verbose_name="房间", related_name="adjustments", null=True, blank=True, on_delete=models.SET_NULL)
    person = models.ForeignKey(Person, verbose_name="人员", related_name="adjustments", null=True, blank=True, on_delete=models.SET_NULL)
    adjustment_type = models.CharField("类型", max_length=30, choices=Type.choices)
    effective_date = models.DateField("生效日期", default=timezone.localdate)
    amount = models.DecimalField("金额", max_digits=12, decimal_places=2, default=Decimal("0.00"))
    description = models.CharField("说明", max_length=240)
    created_at = models.DateTimeField("创建时间", auto_now_add=True)

    class Meta:
        ordering = ["-effective_date", "-id"]
        verbose_name = "调整"
        verbose_name_plural = "调整"

    def __str__(self):
        return f"{self.get_adjustment_type_display()} {self.amount}"


class RecurringRule(models.Model):
    class Frequency(models.TextChoices):
        MONTHLY = "monthly", "每月"
        QUARTERLY = "quarterly", "每季度"
        YEARLY = "yearly", "每年"

    name = models.CharField("名称", max_length=100)
    direction = models.CharField("方向", max_length=10, choices=Charge.Direction.choices, default=Charge.Direction.EXPENSE)
    category = models.CharField("类别", max_length=30, choices=Charge.Category.choices, default=Charge.Category.OTHER)
    amount = models.DecimalField("金额", max_digits=12, decimal_places=2)
    frequency = models.CharField("频率", max_length=20, choices=Frequency.choices, default=Frequency.MONTHLY)
    day_of_month = models.PositiveSmallIntegerField("每月几日", default=10)
    start_date = models.DateField("开始日期")
    end_date = models.DateField("结束日期", null=True, blank=True)
    active = models.BooleanField("启用", default=True)
    notes = models.TextField("备注", blank=True)

    class Meta:
        ordering = ["name"]
        verbose_name = "周期规则"
        verbose_name_plural = "周期规则"

    def __str__(self):
        return self.name
