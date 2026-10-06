"""Monthly rent collection, including explicit bills for uninitialized imports."""
from datetime import date
from decimal import Decimal

from django.db.models import Q

from .date_utils import add_months, money, month_end
from .models import Charge, Payment, Tenancy
from .services import CYCLE_MONTHS, billing_end_for_tenancy, rent_charge_spec


def monthly_rent_rows(month, include_heating=False):
    end = month_end(month)
    charges = Charge.objects.filter(
        direction=Charge.Direction.INCOME, category__in=[Charge.Category.RENT, Charge.Category.HEATING] if include_heating else [Charge.Category.RENT],
    ).filter(
        Q(period_start__range=(month, end))
        | Q(period_start__isnull=True, due_date__range=(month, end))
    ).select_related("tenancy", "room", "person").prefetch_related("allocations", "adjustments")
    grouped = {}
    billed_tenancies = set()
    for charge in charges:
        if charge.category == Charge.Category.RENT:
            billed_tenancies.add(charge.tenancy_id)
        if charge.status == Charge.Status.VOID or not charge.room_id:
            continue
        key = f"tenancy-{charge.tenancy_id}" if charge.tenancy_id else f"room-{charge.room_id}"
        row = grouped.setdefault(key, dict(
            key=key, tenancy=charge.tenancy, room=charge.room, person=charge.person,
            charges=[], draft=None,
        ))
        row["charges"].append(charge)

    tenancies = Tenancy.objects.filter(
        status__in=[Tenancy.Status.ACTIVE, Tenancy.Status.UPCOMING],
        start_date__lte=end, end_date__gte=month,
    ).select_related("room", "primary_person")
    for tenancy in tenancies:
        if tenancy.pk in billed_tenancies:
            continue
        billing_start = tenancy.billing_start_date or tenancy.start_date
        if billing_start > end or billing_end_for_tenancy(tenancy) < month:
            continue
        if tenancy.is_short_term:
            months = CYCLE_MONTHS.get(tenancy.payment_cycle, 1)
            cursor = tenancy.start_date
            while cursor < month or add_months(cursor, months) <= billing_start:
                cursor = add_months(cursor, months)
        else:
            cursor = max(month, tenancy.start_date) if billing_start <= tenancy.start_date else month
        if cursor > min(end, billing_end_for_tenancy(tenancy)):
            continue
        key = f"tenancy-{tenancy.pk}"
        draft_row = dict(
            key=key, tenancy=tenancy, room=tenancy.room, person=tenancy.primary_person,
            charges=[], draft=rent_charge_spec(
                tenancy, cursor, first_month=cursor == tenancy.start_date and billing_start <= tenancy.start_date,
            ),
        )
        if key in grouped:
            grouped[key]["draft"] = draft_row["draft"]
        else:
            grouped[key] = draft_row

    prepaid = {}
    receipts = Payment.objects.filter(
        tenancy_id__in=[row["tenancy"].pk for row in grouped.values() if row["tenancy"]],
        direction=Payment.Direction.RECEIVE, auto_allocate=True,
    ).exclude(category=Payment.Category.DEPOSIT).prefetch_related("allocations")
    for receipt in receipts:
        prepaid[receipt.tenancy_id] = prepaid.get(receipt.tenancy_id, Decimal("0.00")) + receipt.unallocated_amount
    for row in grouped.values():
        row["amount"] = money((row["draft"]["amount"] if row["draft"] else 0) + sum(
            (charge.amount for charge in row["charges"]), Decimal("0.00"),
        ))
        row["paid"] = money(sum((charge.allocated_amount for charge in row["charges"]), Decimal("0.00")))
        row["balance"] = money(row["amount"] - row["paid"])
        row["prepaid"] = money(prepaid.get(row["tenancy"].pk, 0)) if row["tenancy"] else Decimal("0.00")
        row["status"] = "paid" if row["balance"] <= 0 else "partial" if row["paid"] else "open"
        row["periods"] = ([row["draft"]] if row["draft"] else []) + row["charges"]
        row["heating_charges"] = [c for c in row["charges"] if c.category == Charge.Category.HEATING]
        row["other_balance"] = money(row["balance"] - (row["draft"]["amount"] if row["draft"] else 0))
    return sorted(grouped.values(), key=lambda row: row["room"].number)
