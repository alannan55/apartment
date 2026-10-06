"""Read-side totals. Separate subqueries avoid multiplying allocations by offsets."""
from decimal import Decimal

from django.db.models import Count, DecimalField, F, OuterRef, Q, Subquery, Sum, Value
from django.db.models.functions import Coalesce

from .date_utils import money
from .models import Adjustment, Allocation, Charge, Payment, Tenancy


def total_subquery(queryset, group):
    return Coalesce(Subquery(queryset.order_by().values(group).annotate(total=Sum("amount")).values("total")),
                    Value(Decimal("0.00")), output_field=DecimalField(max_digits=16, decimal_places=2))


def charge_totals(queryset):
    return queryset.annotate(
        _cash_total=total_subquery(Allocation.objects.filter(charge_id=OuterRef("pk")), "charge_id"),
        _offset_total=total_subquery(Adjustment.objects.filter(charge_id=OuterRef("pk"), adjustment_type="deposit_deduction"), "charge_id"),
    ).annotate(_balance=F("amount") - F("_cash_total") - F("_offset_total"))


def payment_totals(queryset):
    return queryset.annotate(_cash_total=total_subquery(Allocation.objects.filter(payment_id=OuterRef("pk")), "payment_id"))


def tenancy_families():
    parents = dict(Tenancy.objects.order_by().values_list("pk", "previous_tenancy_id"))
    roots, families = {}, {}
    for pk in parents:
        cursor, chain = pk, []
        while cursor is not None and cursor not in roots and cursor not in chain:
            chain.append(cursor)
            cursor = parents.get(cursor)
        root = roots[cursor] if cursor in roots else chain[-1]
        for item in chain:
            roots[item] = root
        families.setdefault(root, set()).add(pk)
    return {pk: families[root] for pk, root in roots.items()}


def room_finance_summaries(rooms, today):
    families = tenancy_families()
    current = {room.pk: room.current_tenancies[0] if room.current_tenancies else None for room in rooms}
    keys = ("rent_due", "deposit_due", "heating_due", "other_due", "tenant_due", "current_due",
            "historical_due", "overdue", "future_due", "prepaid", "expense_due")
    summaries = {room.pk: dict.fromkeys(keys, Decimal("0.00")) | {"has_receivables": False} for room in rooms}
    open_bill = ~Q(status__in=[Charge.Status.PAID, Charge.Status.VOID])
    charges = charge_totals(Charge.objects.filter(Q(tenancy__room_id__in=summaries) | Q(tenancy__isnull=True, room_id__in=summaries)).exclude(status=Charge.Status.VOID)).annotate(
        effective_room=Coalesce("tenancy__room_id", "room_id"),
    ).order_by().values("effective_room", "tenancy_id", "direction", "category").annotate(
        due=Sum("_balance", filter=open_bill & Q(due_date__lte=today), default=0),
        overdue=Sum("_balance", filter=open_bill & Q(due_date__lt=today), default=0),
        future=Sum("_balance", filter=open_bill & Q(due_date__gt=today), default=0),
        outstanding=Sum("_balance", filter=open_bill, default=0), count=Count("pk"),
    )
    categories = {"rent": "rent_due", "deposit": "deposit_due", "heating": "heating_due"}
    for row in charges:
        room_id = row["effective_room"]
        if room_id not in summaries:
            continue
        summary, tenancy = summaries[room_id], current[room_id]
        ids = families.get(tenancy.pk, {tenancy.pk}) if tenancy else set()
        if row["direction"] == "expense":
            summary["expense_due"] += row["outstanding"]
            continue
        summary["has_receivables"] |= not tenancy or row["tenancy_id"] in ids
        summary[categories.get(row["category"], "other_due")] += row["due"]
        summary["tenant_due"] += row["due"]
        summary["overdue"] += row["overdue"]
        summary["future_due"] += row["future"]
        if row["tenancy_id"] in ids:
            summary["current_due"] += row["due"]
        elif row["tenancy_id"]:
            summary["historical_due"] += row["due"]
    receipts = payment_totals(Payment.objects.filter(direction="receive", auto_allocate=True, tenancy__room_id__in=summaries)).annotate(
        effective_room=Coalesce("tenancy__room_id", "room_id"),
    ).order_by().values("effective_room").annotate(prepaid=Sum(F("amount") - F("_cash_total")))
    for row in receipts:
        if row["effective_room"] in summaries:
            summaries[row["effective_room"]]["prepaid"] = row["prepaid"]
    for room in rooms:
        room.finance_summary = {key: value if isinstance(value, bool) else money(value) for key, value in summaries[room.pk].items()}
