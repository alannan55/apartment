from decimal import Decimal

from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import Sum
from django.utils import timezone

from .date_utils import add_months, contract_months, money


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
                end_date__gte=today,
            )
            .order_by("-start_date")
            .first()
        )

    def refresh_status(self, today=None, save=True):
        today = today or timezone.localdate()
        if self.status == self.Status.SELF_USE:
            return self.status
        previous_status = self.status
        tenancy = self.active_tenancy(today)
        if not tenancy:
            if not self.tenancies.exists() and self.status in {self.Status.OCCUPIED, self.Status.EXPIRING}:
                return self.status
            self.status = self.Status.VACANT
        elif tenancy.planned_move_out_date or (tenancy.end_date - today).days <= 30:
            self.status = self.Status.EXPIRING
        else:
            self.status = self.Status.OCCUPIED
        if save and self.status != previous_status:
            self.save(update_fields=["status"])
        return self.status


class Person(models.Model):
    name = models.CharField("姓名", max_length=80)
    id_number = models.CharField("身份证号", max_length=30, unique=True)
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
    primary_person = models.ForeignKey(Person, verbose_name="主租客", related_name="primary_tenancies", on_delete=models.PROTECT)
    start_date = models.DateField("合同开始日期")
    end_date = models.DateField("合同结束日期")
    billing_start_date = models.DateField("系统计费起始日期", null=True, blank=True)
    planned_move_out_date = models.DateField("预计退租日期", null=True, blank=True)
    planned_deposit_refund_amount = models.DecimalField("预计退还押金", max_digits=10, decimal_places=2, null=True, blank=True)
    planned_move_out_note = models.TextField("预计退租备注", blank=True)
    move_out_date = models.DateField("实际退租日期", null=True, blank=True)
    monthly_rent = models.DecimalField("月租金", max_digits=10, decimal_places=2, validators=[MinValueValidator(0)])
    payment_cycle = models.CharField("付款周期", max_length=20, choices=PaymentCycle.choices, default=PaymentCycle.MONTHLY)
    deposit_amount = models.DecimalField("押金", max_digits=10, decimal_places=2, default=Decimal("3500.00"))
    broker = models.ForeignKey(Broker, verbose_name="中介", related_name="tenancies", null=True, blank=True, on_delete=models.SET_NULL)
    commission_base = models.DecimalField("佣金基数", max_digits=10, decimal_places=2, default=Decimal("2000.00"))
    commission_manual_amount = models.DecimalField("佣金手动金额", max_digits=10, decimal_places=2, null=True, blank=True)
    commission_due_date = models.DateField("佣金应付日期", null=True, blank=True)
    commission_paid_date = models.DateField("佣金实付日期", null=True, blank=True)
    police_report_end_date = models.DateField("报备展示结束日期", null=True, blank=True)
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
        return contract_months(self.start_date, self.end_date) < 6

    @property
    def police_end_date(self):
        if self.police_report_end_date:
            return self.police_report_end_date
        if self.is_short_term:
            return add_months(self.start_date, 6)
        return self.end_date

    @property
    def contract_text_for_report(self):
        return f"{self.start_date:%Y.%m.%d}-{self.police_end_date:%Y.%m.%d}"

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
        charges = self.charges.exclude(status=Charge.Status.VOID)
        return money(sum((charge.balance for charge in charges), Decimal("0.00")))


class Stay(models.Model):
    class Type(models.TextChoices):
        PERMANENT = "permanent", "常住"
        VISITOR = "visitor", "探望/暂住"
        MANAGER = "manager", "管理员"

    person = models.ForeignKey(Person, verbose_name="人员", related_name="stays", on_delete=models.CASCADE)
    room = models.ForeignKey(Room, verbose_name="房间", related_name="stays", on_delete=models.PROTECT)
    tenancy = models.ForeignKey(Tenancy, verbose_name="合同", related_name="stays", null=True, blank=True, on_delete=models.CASCADE)
    stay_type = models.CharField("入住类型", max_length=20, choices=Type.choices, default=Type.PERMANENT)
    start_date = models.DateField("入住开始日期")
    end_date = models.DateField("入住结束日期", null=True, blank=True)
    is_active = models.BooleanField("当前在住", default=True)
    report_note = models.CharField("报备备注", max_length=200, blank=True)
    notes = models.TextField("备注", blank=True)

    class Meta:
        ordering = ["room__number", "person__name"]
        verbose_name = "入住记录"
        verbose_name_plural = "入住记录"

    def __str__(self):
        return f"{self.room} {self.person.name} {self.get_stay_type_display()}"

    def active_on(self, today=None):
        today = today or timezone.localdate()
        return self.is_active and self.start_date <= today and (self.end_date is None or self.end_date >= today)


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
    created_at = models.DateTimeField("创建时间", auto_now_add=True)

    class Meta:
        ordering = ["-date", "-id"]
        verbose_name = "收付款"
        verbose_name_plural = "收付款"

    def __str__(self):
        return f"{self.get_direction_display()} {self.amount} {self.date:%Y-%m-%d}"

    @property
    def allocated_amount(self):
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
