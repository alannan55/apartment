"""Explicit deposit receipts for current households, including imported leases."""
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from .date_utils import money
from .models import Charge, Payment, Tenancy
from .services import allocate_payment_to_charges, create_charge, settle_charges, tenancy_family_ids


def deposit_row(tenancy):
    family = tenancy_family_ids(tenancy)
    deposits = list(Charge.objects.filter(
        tenancy_id__in=family, direction=Charge.Direction.INCOME, category=Charge.Category.DEPOSIT,
    ).prefetch_related("allocations"))
    charges = [charge for charge in deposits if charge.status != Charge.Status.VOID]
    receipts = Payment.objects.filter(
        tenancy_id__in=family, direction=Payment.Direction.RECEIVE, category=Payment.Category.DEPOSIT,
    ).prefetch_related("allocations")
    unallocated = money(sum((receipt.unallocated_amount for receipt in receipts), Decimal("0.00")))
    allocated = money(sum((charge.allocated_amount for charge in charges), Decimal("0.00")))
    amount = money(sum((charge.amount for charge in charges), Decimal("0.00"))) if charges else money(
        tenancy.deposit_amount if tenancy.deposit_amount is not None else 3500,
    )
    paid = money(allocated + unallocated)
    balance = max(money(amount - paid), Decimal("0.00"))
    void = bool(deposits) and not charges
    return {
        "key": str(tenancy.pk), "tenancy": tenancy, "room": tenancy.room,
        "person": tenancy.primary_person, "charges": charges, "draft": not deposits,
        "amount": amount, "paid": paid, "allocated": allocated,
        "unallocated": unallocated, "balance": balance, "void": void,
        "can_record": not void and (balance > 0 or unallocated > 0 and amount > allocated),
        "status": "void" if void else "paid" if balance <= 0 else "partial" if paid > 0 else "open",
    }


def deposit_rows():
    return [deposit_row(tenancy) for tenancy in Tenancy.objects.filter(
        status=Tenancy.Status.ACTIVE, start_date__lte=timezone.localdate(),
    ).select_related("room", "primary_person").order_by("room__number")]


@transaction.atomic
def collect_deposit(tenancy_id, *, date, amount=None, deposit_amount=None, single=False):
    tenancy = Tenancy.objects.select_for_update().select_related("room", "primary_person").get(pk=tenancy_id)
    if tenancy.status != Tenancy.Status.ACTIVE or tenancy.start_date > timezone.localdate():
        raise ValueError("合同状态已变更，请刷新押金页面。")
    row = deposit_row(tenancy)
    if row["void"]:
        raise ValueError(f"{tenancy.room.number} 的押金账单已作废，请先核对账单。")
    charges = row["charges"]
    if row["draft"]:
        target = money(deposit_amount if deposit_amount is not None else row["amount"])
        if target < row["paid"] and target < row["amount"]:
            raise ValueError(f"{tenancy.room.number} 应交押金不能少于已登记实收 ¥{row['paid']}。")
        if target <= 0:
            return None
        charge = create_charge(
            direction=Charge.Direction.INCOME, category=Charge.Category.DEPOSIT,
            tenancy=tenancy, due_date=tenancy.start_date, amount=target,
            description=f"{tenancy.room.number} {tenancy.primary_person.name} 押金",
            source=Charge.Source.MANUAL, generated_key=f"tenancy:{tenancy.pk}:deposit",
        )
        charges = [charge]
        if tenancy.deposit_amount != target:
            tenancy.deposit_amount = target
            tenancy.save(update_fields=["deposit_amount"])
    charges = list(Charge.objects.filter(pk__in=[charge.pk for charge in charges]).exclude(status=Charge.Status.VOID))
    # Read uncached balances as receipts are applied; never count rent prepayments.
    for receipt in Payment.objects.filter(
        tenancy_id__in=tenancy_family_ids(tenancy), direction=Payment.Direction.RECEIVE,
        category=Payment.Category.DEPOSIT,
    ).order_by("date", "id"):
        if receipt.unallocated_amount > 0:
            allocate_payment_to_charges(receipt, charges)
    charges = list(Charge.objects.filter(pk__in=[charge.pk for charge in charges]).exclude(status=Charge.Status.VOID))
    if sum((charge.balance for charge in charges), Decimal("0.00")) <= 0:
        return None
    return settle_charges(
        charges, amount=amount if single else None, date=date,
        memo="单笔登记押金" if single else "批量登记押金",
    )
