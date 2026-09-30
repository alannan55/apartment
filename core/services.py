from datetime import date, timedelta
from decimal import Decimal

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .date_utils import add_months, contract_months, long_term_first_rent, money, month_end, prorate_by_month
from .models import Adjustment, Allocation, Broker, Charge, Payment, Person, RecurringRule, Room, Stay, Tenancy


CYCLE_MONTHS = {
    Tenancy.PaymentCycle.MONTHLY: 1,
    Tenancy.PaymentCycle.QUARTERLY: 3,
    Tenancy.PaymentCycle.HALF_YEAR: 6,
    Tenancy.PaymentCycle.YEARLY: 12,
}


def refresh_all_room_statuses(today=None):
    today = today or timezone.localdate()
    for room in Room.objects.all():
        room.refresh_status(today=today)


def create_charge(
    *,
    direction,
    category,
    due_date,
    amount,
    description,
    tenancy=None,
    room=None,
    person=None,
    period_start=None,
    period_end=None,
    source=Charge.Source.AUTO,
    generated_key=None,
    notes="",
):
    amount = money(amount)
    payload = {
        "direction": direction,
        "category": category,
        "due_date": due_date,
        "amount": amount,
        "description": description,
        "tenancy": tenancy,
        "room": room or (tenancy.room if tenancy else None),
        "person": person or (tenancy.primary_person if tenancy else None),
        "period_start": period_start,
        "period_end": period_end,
        "source": source,
        "notes": notes,
    }
    if generated_key:
        charge, created = Charge.objects.get_or_create(generated_key=generated_key, defaults=payload)
        if created:
            return charge
        changed_fields = []
        foreign_keys = {"tenancy", "room", "person"}
        for field, value in payload.items():
            current_value = getattr(charge, f"{field}_id") if field in foreign_keys else getattr(charge, field)
            expected_value = value.pk if field in foreign_keys and value is not None else value
            if current_value != expected_value:
                setattr(charge, field, value)
                changed_fields.append(field)
        if changed_fields:
            charge.save(update_fields=changed_fields)
        return charge
    return Charge.objects.create(**payload)


def commission_amount_for_tenancy(tenancy):
    if tenancy.commission_manual_amount is not None:
        return money(tenancy.commission_manual_amount)
    months = min(contract_months(tenancy.start_date, tenancy.end_date), 12)
    return money(tenancy.commission_base * Decimal(months) / Decimal("12"))


def commission_due_date_for_tenancy(tenancy):
    return add_months(tenancy.start_date, 1)


def create_commission_charge(tenancy):
    if not tenancy.broker:
        return None
    due_date = tenancy.commission_due_date or commission_due_date_for_tenancy(tenancy)
    if tenancy.commission_due_date != due_date:
        tenancy.commission_due_date = due_date
        tenancy.save(update_fields=["commission_due_date"])
    return create_charge(
        direction=Charge.Direction.EXPENSE,
        category=Charge.Category.COMMISSION,
        due_date=due_date,
        amount=commission_amount_for_tenancy(tenancy),
        description=f"{tenancy.room.number} {tenancy.primary_person.name} 中介佣金",
        tenancy=tenancy,
        period_start=tenancy.start_date,
        period_end=tenancy.end_date,
        generated_key=f"tenancy:{tenancy.id}:commission",
    )


def create_initial_charges(tenancy, first_month_discount=Decimal("0.00")):
    deposit = create_charge(
        direction=Charge.Direction.INCOME,
        category=Charge.Category.DEPOSIT,
        due_date=tenancy.start_date,
        amount=tenancy.deposit_amount,
        description=f"{tenancy.room.number} {tenancy.primary_person.name} 押金",
        tenancy=tenancy,
        period_start=tenancy.start_date,
        period_end=tenancy.start_date,
        generated_key=f"tenancy:{tenancy.id}:deposit",
    )

    if tenancy.is_short_term:
        months = CYCLE_MONTHS.get(tenancy.payment_cycle, 1)
        period_end = add_months(tenancy.start_date, months) - timedelta(days=1)
        rent_amount = money(tenancy.monthly_rent * Decimal(months))
        rent_description = f"{tenancy.room.number} {tenancy.start_date:%Y.%m.%d}-{period_end:%Y.%m.%d} 房租"
    else:
        period_end = month_end(tenancy.start_date)
        rent_amount = long_term_first_rent(tenancy.monthly_rent, tenancy.start_date)
        rent_description = f"{tenancy.room.number} 首月房租"

    discount = money(first_month_discount or Decimal("0.00"))
    if discount:
        Adjustment.objects.create(
            tenancy=tenancy,
            room=tenancy.room,
            person=tenancy.primary_person,
            adjustment_type=Adjustment.Type.DISCOUNT,
            effective_date=tenancy.start_date,
            amount=discount,
            description="首月租金优惠",
        )
    rent = create_charge(
        direction=Charge.Direction.INCOME,
        category=Charge.Category.RENT,
        due_date=tenancy.start_date,
        amount=max(money(rent_amount - discount), Decimal("0.00")),
        description=rent_description,
        tenancy=tenancy,
        period_start=tenancy.start_date,
        period_end=period_end,
        generated_key=f"tenancy:{tenancy.id}:rent:{tenancy.start_date:%Y%m%d}",
    )
    return deposit, rent


@transaction.atomic
def sign_contract(
    *,
    room,
    person_data,
    start_date,
    end_date,
    monthly_rent,
    payment_cycle=Tenancy.PaymentCycle.MONTHLY,
    deposit_amount=Decimal("3500.00"),
    broker_name="",
    commission_manual_amount=None,
    first_month_discount=Decimal("0.00"),
    notes="",
):
    person, _ = Person.objects.update_or_create(
        id_number=person_data["id_number"],
        defaults={
            "name": person_data["name"],
            "phone": person_data.get("phone", ""),
            "emergency_name": person_data.get("emergency_name", ""),
            "emergency_phone": person_data.get("emergency_phone", ""),
            "emergency_address": person_data.get("emergency_address", ""),
        },
    )
    broker = None
    if broker_name:
        broker, _ = Broker.objects.get_or_create(name=broker_name)

    tenancy = Tenancy.objects.create(
        room=room,
        primary_person=person,
        start_date=start_date,
        end_date=end_date,
        billing_start_date=start_date,
        monthly_rent=monthly_rent,
        payment_cycle=payment_cycle,
        deposit_amount=deposit_amount,
        broker=broker,
        commission_base=room.commission_base,
        commission_manual_amount=commission_manual_amount or None,
        notes=notes,
    )
    Stay.objects.update_or_create(
        tenancy=tenancy,
        person=person,
        defaults={
            "room": room,
            "stay_type": Stay.Type.PERMANENT,
            "start_date": start_date,
            "end_date": end_date,
            "is_active": True,
        },
    )
    create_initial_charges(tenancy, first_month_discount=first_month_discount)
    generate_heating_charges_until(tenancy, month_end(start_date))
    create_commission_charge(tenancy)
    allocate_unallocated_payments()
    room.refresh_status(today=start_date)
    return tenancy


def next_natural_month_start(value):
    return month_end(value) + timedelta(days=1)


def billing_end_for_tenancy(tenancy):
    candidates = [tenancy.end_date]
    if tenancy.move_out_date:
        candidates.append(tenancy.move_out_date)
    if tenancy.planned_move_out_date:
        candidates.append(tenancy.planned_move_out_date)
    return min(candidates)


def scheduled_due_through_date(today=None):
    today = today or timezone.localdate()
    current_month_end = month_end(today)
    if today >= current_month_end - timedelta(days=1):
        return month_end(next_natural_month_start(today))
    return current_month_end


def generate_scheduled_due_charges(today=None):
    return generate_due_charges(scheduled_due_through_date(today))


def generate_rent_charges_until(tenancy, through_date=None):
    through_date = through_date or timezone.localdate()
    through_date = min(through_date, billing_end_for_tenancy(tenancy))
    billing_start = tenancy.billing_start_date or tenancy.start_date
    created = []

    if tenancy.is_short_term:
        months = CYCLE_MONTHS.get(tenancy.payment_cycle, 1)
        cursor = tenancy.start_date
        while add_months(cursor, months) <= billing_start:
            cursor = add_months(cursor, months)
        while cursor <= through_date:
            period_end = min(add_months(cursor, months) - timedelta(days=1), tenancy.end_date)
            charge = create_charge(
                direction=Charge.Direction.INCOME,
                category=Charge.Category.RENT,
                due_date=cursor,
                amount=tenancy.monthly_rent * Decimal(months),
                description=f"{tenancy.room.number} {cursor:%Y.%m.%d}-{period_end:%Y.%m.%d} 房租",
                tenancy=tenancy,
                period_start=cursor,
                period_end=period_end,
                generated_key=f"tenancy:{tenancy.id}:rent:{cursor:%Y%m%d}",
            )
            created.append(charge)
            cursor = add_months(cursor, months)
        return created

    if billing_start <= tenancy.start_date:
        first_period_end = month_end(tenancy.start_date)
        create_charge(
            direction=Charge.Direction.INCOME,
            category=Charge.Category.RENT,
            due_date=tenancy.start_date,
            amount=long_term_first_rent(tenancy.monthly_rent, tenancy.start_date),
            description=f"{tenancy.room.number} 首月房租",
            tenancy=tenancy,
            period_start=tenancy.start_date,
            period_end=first_period_end,
            generated_key=f"tenancy:{tenancy.id}:rent:{tenancy.start_date:%Y%m%d}",
        )
        cursor = next_natural_month_start(tenancy.start_date)
    else:
        cursor = date(billing_start.year, billing_start.month, 1)
    while cursor <= through_date:
        period_end = min(month_end(cursor), tenancy.end_date)
        due_date = cursor - timedelta(days=1)
        charge = create_charge(
            direction=Charge.Direction.INCOME,
            category=Charge.Category.RENT,
            due_date=due_date,
            amount=tenancy.monthly_rent,
            description=f"{tenancy.room.number} {cursor:%Y.%m} 房租",
            tenancy=tenancy,
            period_start=cursor,
            period_end=period_end,
            generated_key=f"tenancy:{tenancy.id}:rent:{cursor:%Y%m}",
        )
        created.append(charge)
        cursor = next_natural_month_start(cursor)
    return created


def heating_cycle_starts(year):
    return [
        date(year, 11, 15),
        date(year, 12, 15),
        date(year + 1, 1, 15),
        date(year + 1, 2, 15),
    ]


def generate_heating_charges_until(tenancy, through_date=None):
    through_date = through_date or timezone.localdate()
    active_start = max(tenancy.start_date, tenancy.billing_start_date or tenancy.start_date)
    active_end = billing_end_for_tenancy(tenancy)
    years = {active_start.year - 1, active_start.year, through_date.year - 1, through_date.year}
    created = []
    for year in sorted(years):
        for start in heating_cycle_starts(year):
            end = add_months(start, 1)
            if end < active_start or start > active_end or start > through_date:
                continue
            overlap_start = max(start, active_start)
            overlap_end = min(end, active_end)
            if overlap_start > overlap_end:
                continue
            amount = tenancy.room.heating_fee
            if overlap_start != start or overlap_end != end:
                amount = money(tenancy.room.heating_fee * Decimal((overlap_end - overlap_start).days + 1) / Decimal((end - start).days + 1))
            charge = create_charge(
                direction=Charge.Direction.INCOME,
                category=Charge.Category.HEATING,
                due_date=start,
                amount=amount,
                description=f"{tenancy.room.number} {start:%m.%d}-{end:%m.%d} 取暖费",
                tenancy=tenancy,
                period_start=overlap_start,
                period_end=overlap_end,
                generated_key=f"tenancy:{tenancy.id}:heating:{start:%Y%m%d}",
            )
            created.append(charge)
    return created


def recurring_rule_months(rule):
    return {
        RecurringRule.Frequency.MONTHLY: 1,
        RecurringRule.Frequency.QUARTERLY: 3,
        RecurringRule.Frequency.YEARLY: 12,
    }[rule.frequency]


def recurring_rule_due_date(rule, value):
    return date(value.year, value.month, min(rule.day_of_month, month_end(value).day))


def generate_property_rent_charges_until(rule, through_date=None):
    through_date = through_date or timezone.localdate()
    if not rule.active or rule.category != Charge.Category.PROPERTY_RENT:
        return []
    final_date = min(through_date, rule.end_date) if rule.end_date else through_date
    cursor = date(rule.start_date.year, rule.start_date.month, 1)
    step = recurring_rule_months(rule)
    created = []
    while cursor <= final_date:
        due_date = recurring_rule_due_date(rule, cursor)
        if due_date >= rule.start_date and due_date <= final_date:
            generated_key = f"recurring:{rule.id}:property_rent:{due_date:%Y%m%d}"
            existing = Charge.objects.filter(generated_key=generated_key).first()
            if existing and existing.allocations.exists():
                created.append(existing)
                cursor = add_months(cursor, step)
                continue
            created.append(
                create_charge(
                    direction=Charge.Direction.EXPENSE,
                    category=Charge.Category.PROPERTY_RENT,
                    due_date=due_date,
                    amount=rule.amount,
                    description=rule.name,
                    source=Charge.Source.AUTO,
                    generated_key=generated_key,
                    notes=rule.notes,
                )
            )
        cursor = add_months(cursor, step)
    return created


def clear_unpaid_property_rent_charges(rule):
    charges = Charge.objects.filter(
        category=Charge.Category.PROPERTY_RENT,
        source=Charge.Source.AUTO,
        generated_key__startswith=f"recurring:{rule.id}:property_rent:",
    )
    for charge in charges:
        if not charge.allocations.exists():
            charge.delete()


def generate_due_charges(through_date=None):
    through_date = through_date or timezone.localdate()
    created = []
    qs = Tenancy.objects.filter(status=Tenancy.Status.ACTIVE, start_date__lte=through_date)
    for tenancy in qs.select_related("room", "primary_person", "broker"):
        created.extend(generate_rent_charges_until(tenancy, through_date))
        created.extend(generate_heating_charges_until(tenancy, through_date))
        if tenancy.broker:
            created.append(create_commission_charge(tenancy))
    for rule in RecurringRule.objects.filter(
        active=True,
        direction=Charge.Direction.EXPENSE,
        category=Charge.Category.PROPERTY_RENT,
        start_date__lte=through_date,
    ):
        created.extend(generate_property_rent_charges_until(rule, through_date))
    allocate_unallocated_payments()
    refresh_all_room_statuses(through_date)
    return created


def remove_unpaid_auto_income_after(tenancy, cutoff_date):
    qs = tenancy.charges.filter(
        direction=Charge.Direction.INCOME,
        category__in=[Charge.Category.RENT, Charge.Category.HEATING],
        source=Charge.Source.AUTO,
        period_start__gt=cutoff_date,
    )
    for charge in qs:
        if charge.allocations.exists():
            continue
        charge.delete()


def allocation_candidates(payment):
    charge_direction = (
        Charge.Direction.INCOME
        if payment.direction == Payment.Direction.RECEIVE
        else Charge.Direction.EXPENSE
    )
    charges = Charge.objects.filter(direction=charge_direction).exclude(status__in=[Charge.Status.PAID, Charge.Status.VOID])
    if payment.tenancy_id:
        charges = charges.filter(tenancy=payment.tenancy)
    else:
        filters = Q()
        if payment.person_id:
            filters |= Q(person=payment.person)
        if payment.room_id:
            filters |= Q(room=payment.room)
        if filters:
            charges = charges.filter(filters)
    if payment.category == Payment.Category.DEPOSIT:
        if payment.direction == Payment.Direction.RECEIVE:
            charges = charges.filter(category=Charge.Category.DEPOSIT)
        else:
            charges = charges.filter(category=Charge.Category.DEPOSIT_REFUND)
    if payment.direction == Payment.Direction.RECEIVE:
        priority = {
            Charge.Category.RENT: 0,
            Charge.Category.DEPOSIT: 1,
            Charge.Category.HEATING: 2,
            Charge.Category.UTILITY: 3,
            Charge.Category.MOVE_DIFF: 4,
        }
    else:
        priority = {
            Charge.Category.COMMISSION: 0,
            Charge.Category.DEPOSIT_REFUND: 1,
            Charge.Category.REPAIR: 2,
            Charge.Category.WAGE: 3,
            Charge.Category.PROPERTY_RENT: 4,
        }
    return sorted(charges, key=lambda charge: (priority.get(charge.category, 99), charge.due_date, charge.id))


@transaction.atomic
def allocate_payment(payment):
    remaining = payment.unallocated_amount
    for charge in allocation_candidates(payment):
        if remaining <= 0:
            break
        balance = charge.balance
        if balance <= 0:
            charge.refresh_status()
            continue
        amount = min(remaining, balance)
        Allocation.objects.create(payment=payment, charge=charge, amount=amount)
        remaining = money(remaining - amount)
        charge.refresh_status()
    return payment


def allocate_unallocated_payments():
    for payment in Payment.objects.all().order_by("date", "id"):
        if payment.unallocated_amount > 0:
            allocate_payment(payment)


@transaction.atomic
def record_payment(*, direction, date, amount, tenancy=None, room=None, person=None, category=Payment.Category.OTHER, method="", memo="", auto_allocate=True):
    payment = Payment.objects.create(
        direction=direction,
        category=category,
        date=date,
        amount=money(amount),
        tenancy=tenancy,
        room=room or (tenancy.room if tenancy else None),
        person=person or (tenancy.primary_person if tenancy else None),
        method=method,
        memo=memo,
    )
    if auto_allocate:
        allocate_payment(payment)
    return payment


def payment_category_for_charge(charge):
    if charge.category == Charge.Category.DEPOSIT:
        return Payment.Category.DEPOSIT
    if charge.category == Charge.Category.RENT:
        return Payment.Category.RENT
    return Payment.Category.OTHER


def settlement_priority(direction):
    if direction == Charge.Direction.INCOME:
        return {
            Charge.Category.RENT: 0,
            Charge.Category.HEATING: 1,
            Charge.Category.DEPOSIT: 2,
        }
    return {
        Charge.Category.COMMISSION: 0,
        Charge.Category.DEPOSIT_REFUND: 1,
        Charge.Category.PROPERTY_RENT: 2,
    }


def allocate_payment_to_charges(payment, charges):
    charge_direction = (
        Charge.Direction.INCOME
        if payment.direction == Payment.Direction.RECEIVE
        else Charge.Direction.EXPENSE
    )
    priority = settlement_priority(charge_direction)
    remaining = payment.amount
    for charge in sorted(charges, key=lambda item: (priority.get(item.category, 99), item.due_date, item.id)):
        if remaining <= 0:
            break
        allocated = min(remaining, charge.balance)
        if allocated <= 0:
            continue
        Allocation.objects.create(payment=payment, charge=charge, amount=allocated)
        remaining = money(remaining - allocated)
        charge.refresh_status()
    return payment


@transaction.atomic
def settle_charges(charges, *, amount=None, date=None, memo=""):
    charges = list({charge.id: charge for charge in charges}.values())
    if not charges:
        raise ValueError("没有可处理的账单。")
    directions = {charge.direction for charge in charges}
    if len(directions) != 1:
        raise ValueError("应收和应付账单不能合并处理。")
    total_balance = money(sum((charge.balance for charge in charges), Decimal("0.00")))
    amount = money(amount if amount not in [None, ""] else total_balance)
    if amount <= 0:
        raise ValueError("收付金额必须大于 0。")
    if amount > total_balance:
        raise ValueError("收付金额不能超过本行待处理金额。")

    direction = directions.pop()
    payment_direction = (
        Payment.Direction.RECEIVE
        if direction == Charge.Direction.INCOME
        else Payment.Direction.PAY
    )
    categories = {charge.category for charge in charges}
    if categories == {Charge.Category.DEPOSIT}:
        payment_category = Payment.Category.DEPOSIT
    elif categories == {Charge.Category.RENT}:
        payment_category = Payment.Category.RENT
    else:
        payment_category = Payment.Category.OTHER

    first = charges[0]
    room = first.room if all(charge.room_id == first.room_id for charge in charges) else None
    tenancy = first.tenancy if all(charge.tenancy_id == first.tenancy_id for charge in charges) else None
    person = first.person if all(charge.person_id == first.person_id for charge in charges) else None
    payment = Payment.objects.create(
        direction=payment_direction,
        category=payment_category,
        date=date or timezone.localdate(),
        amount=amount,
        tenancy=tenancy,
        room=room,
        person=person,
        memo=memo or ("账单收款" if payment_direction == Payment.Direction.RECEIVE else "账单付款"),
    )

    return allocate_payment_to_charges(payment, charges)


@transaction.atomic
def revise_settlement_payment(payment, *, amount, date, memo=""):
    charges = list(
        Charge.objects.filter(allocations__payment=payment)
        .select_related("room", "person", "tenancy")
        .distinct()
    )
    if not charges:
        raise ValueError("这笔流水没有可重新核销的账单。")
    payment.allocations.all().delete()
    for charge in charges:
        charge.refresh_status()
    maximum = money(sum((charge.balance for charge in charges), Decimal("0.00")))
    amount = money(amount)
    if amount <= 0:
        raise ValueError("收付金额必须大于 0。")
    if amount > maximum:
        raise ValueError(f"金额不能超过原账单合计 ¥{maximum}。")
    payment.amount = amount
    payment.date = date
    payment.memo = memo
    payment.save(update_fields=["amount", "date", "memo"])
    return allocate_payment_to_charges(payment, charges)


def collect_charge(charge, *, amount=None, date=None, memo=""):
    return settle_charges([charge], amount=amount, date=date, memo=memo or f"{charge.description} 收款")


def planned_deposit_refund_key(tenancy):
    return f"tenancy:{tenancy.id}:planned_deposit_refund"


def _planned_deposit_refund_charge(tenancy):
    return Charge.objects.filter(generated_key=planned_deposit_refund_key(tenancy)).first()


def _remove_planned_deposit_refund_charge(tenancy, note=""):
    charge = _planned_deposit_refund_charge(tenancy)
    if not charge:
        return
    if charge.allocations.exists():
        charge.status = Charge.Status.VOID
        charge.notes = f"{charge.notes}\n{note}".strip()
        charge.save(update_fields=["status", "notes"])
        return
    charge.delete()


@transaction.atomic
def set_planned_checkout(tenancy, *, planned_date, refund_deposit_amount=Decimal("3500.00"), note=""):
    refund_amount = money(refund_deposit_amount or Decimal("0.00"))
    tenancy.planned_move_out_date = planned_date
    tenancy.planned_deposit_refund_amount = refund_amount
    tenancy.planned_move_out_note = note
    tenancy.save(
        update_fields=[
            "planned_move_out_date",
            "planned_deposit_refund_amount",
            "planned_move_out_note",
            "updated_at",
        ]
    )
    if refund_amount:
        charge = _planned_deposit_refund_charge(tenancy)
        payload = {
            "direction": Charge.Direction.EXPENSE,
            "category": Charge.Category.DEPOSIT_REFUND,
            "due_date": planned_date,
            "amount": refund_amount,
            "description": f"{tenancy.room.number} {tenancy.primary_person.name} 预计退租押金退款",
            "tenancy": tenancy,
            "room": tenancy.room,
            "person": tenancy.primary_person,
            "source": Charge.Source.ADJUSTMENT,
            "notes": note,
            "status": Charge.Status.OPEN,
        }
        if charge:
            for field, value in payload.items():
                setattr(charge, field, value)
            charge.save(update_fields=[*payload.keys()])
            charge.refresh_status()
        else:
            Charge.objects.create(generated_key=planned_deposit_refund_key(tenancy), **payload)
    else:
        _remove_planned_deposit_refund_charge(tenancy, note="预计退租押金退款金额改为 0")
    remove_unpaid_auto_income_after(tenancy, planned_date)
    tenancy.room.refresh_status(today=timezone.localdate())
    return tenancy


@transaction.atomic
def cancel_planned_checkout(tenancy):
    tenancy.planned_move_out_date = None
    tenancy.planned_deposit_refund_amount = None
    tenancy.planned_move_out_note = ""
    tenancy.save(
        update_fields=[
            "planned_move_out_date",
            "planned_deposit_refund_amount",
            "planned_move_out_note",
            "updated_at",
        ]
    )
    _remove_planned_deposit_refund_charge(tenancy, note="预计退租已取消")
    generate_scheduled_due_charges()
    tenancy.room.refresh_status(today=timezone.localdate())
    return tenancy


@transaction.atomic
def checkout_tenancy(tenancy, *, checkout_date, refund_deposit_amount=Decimal("0.00"), deposit_deduction_amount=Decimal("0.00"), note=""):
    tenancy.status = Tenancy.Status.ENDED
    tenancy.move_out_date = checkout_date
    tenancy.planned_move_out_date = None
    tenancy.planned_deposit_refund_amount = None
    tenancy.planned_move_out_note = ""
    tenancy.notes = f"{tenancy.notes}\n退租：{note}".strip()
    tenancy.save(
        update_fields=[
            "status",
            "move_out_date",
            "planned_move_out_date",
            "planned_deposit_refund_amount",
            "planned_move_out_note",
            "notes",
            "updated_at",
        ]
    )
    tenancy.stays.update(is_active=False, end_date=checkout_date)
    if deposit_deduction_amount:
        Adjustment.objects.create(
            tenancy=tenancy,
            room=tenancy.room,
            person=tenancy.primary_person,
            adjustment_type=Adjustment.Type.DEPOSIT_DEDUCTION,
            effective_date=checkout_date,
            amount=money(deposit_deduction_amount),
            description=note or "押金扣款",
        )
    refund_deposit_amount = money(refund_deposit_amount or Decimal("0.00"))
    planned_charge = _planned_deposit_refund_charge(tenancy)
    if planned_charge and refund_deposit_amount:
        planned_charge.generated_key = f"tenancy:{tenancy.id}:deposit_refund:{checkout_date:%Y%m%d}"
        planned_charge.due_date = checkout_date
        planned_charge.amount = refund_deposit_amount
        planned_charge.description = f"{tenancy.room.number} {tenancy.primary_person.name} 押金退款"
        planned_charge.notes = note
        planned_charge.status = Charge.Status.OPEN
        planned_charge.save(update_fields=["generated_key", "due_date", "amount", "description", "notes", "status"])
        planned_charge.refresh_status()
    elif planned_charge:
        _remove_planned_deposit_refund_charge(tenancy, note="实际退租时押金退款金额为 0")
    elif refund_deposit_amount:
        create_charge(
            direction=Charge.Direction.EXPENSE,
            category=Charge.Category.DEPOSIT_REFUND,
            due_date=checkout_date,
            amount=refund_deposit_amount,
            description=f"{tenancy.room.number} {tenancy.primary_person.name} 押金退款",
            tenancy=tenancy,
            source=Charge.Source.ADJUSTMENT,
            generated_key=f"tenancy:{tenancy.id}:deposit_refund:{checkout_date:%Y%m%d}",
            notes=note,
        )
    remove_unpaid_auto_income_after(tenancy, checkout_date)
    tenancy.room.refresh_status(today=checkout_date)
    return tenancy


@transaction.atomic
def move_tenancy(tenancy, *, new_room, move_date, new_monthly_rent, manual_diff_amount=None, note=""):
    old_room = tenancy.room
    old_rent = tenancy.monthly_rent
    remaining_days = (month_end(move_date) - move_date).days + 1
    calculated_diff = money((Decimal(new_monthly_rent) - old_rent) * Decimal(remaining_days) / Decimal((month_end(move_date) - date(move_date.year, move_date.month, 1)).days + 1))
    diff_amount = money(manual_diff_amount) if manual_diff_amount not in [None, ""] else calculated_diff
    if diff_amount:
        charge = create_charge(
            direction=Charge.Direction.INCOME,
            category=Charge.Category.MOVE_DIFF,
            due_date=move_date,
            amount=diff_amount,
            description=f"{old_room.number} 换至 {new_room.number} 补差",
            tenancy=tenancy,
            room=new_room,
            person=tenancy.primary_person,
            period_start=move_date,
            period_end=month_end(move_date),
            source=Charge.Source.ADJUSTMENT,
            generated_key=f"tenancy:{tenancy.id}:move_diff:{move_date:%Y%m%d}",
            notes=note,
        )
        if diff_amount != calculated_diff:
            Adjustment.objects.create(
                tenancy=tenancy,
                charge=charge,
                room=new_room,
                person=tenancy.primary_person,
                adjustment_type=Adjustment.Type.MANUAL_OVERRIDE,
                effective_date=move_date,
                amount=diff_amount - calculated_diff,
                description=f"换房补差手动调整，原计算 {calculated_diff}",
            )
    tenancy.room = new_room
    tenancy.monthly_rent = new_monthly_rent
    tenancy.notes = f"{tenancy.notes}\n{move_date:%Y-%m-%d} {old_room.number} 换至 {new_room.number}。{note}".strip()
    tenancy.save(update_fields=["room", "monthly_rent", "notes", "updated_at"])
    tenancy.stays.filter(is_active=True).update(room=new_room)
    old_room.refresh_status(today=move_date)
    new_room.refresh_status(today=move_date)
    return tenancy


@transaction.atomic
def create_visitor_stay(*, room, person_data, start_date, end_date, report_note="", notes=""):
    person, _ = Person.objects.update_or_create(
        id_number=person_data["id_number"],
        defaults={"name": person_data["name"], "phone": person_data.get("phone", "")},
    )
    return Stay.objects.create(
        person=person,
        room=room,
        stay_type=Stay.Type.VISITOR,
        start_date=start_date,
        end_date=end_date,
        is_active=True,
        report_note=report_note or "探望、暂住",
        notes=notes,
    )
