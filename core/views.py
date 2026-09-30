from django.contrib import messages
from django.db import OperationalError, transaction
from django.db.models import Prefetch, Q
from django.shortcuts import get_object_or_404, redirect, render
from datetime import date, timedelta
from decimal import Decimal
import re

from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from .exports import export_agent_room_status, export_import_template, export_police_report
from .forms import (
    ChargeEditForm,
    CheckoutForm,
    BillPaymentEditForm,
    ImportWorkbookForm,
    ManualChargeForm,
    MoveRoomForm,
    PersonCreateForm,
    PaymentForm,
    PersonForm,
    PersonStayForm,
    PlannedCheckoutForm,
    PropertyRentRuleForm,
    RoomPersonForm,
    RoomForm,
    SignContractForm,
    TenancyEditForm,
    VisitorStayForm,
)
from .models import Charge, Payment, Person, RecurringRule, Room, Stay, Tenancy
from .date_utils import money
from .services import (
    cancel_planned_checkout,
    checkout_tenancy,
    clear_unpaid_property_rent_charges,
    collect_charge,
    create_visitor_stay,
    generate_due_charges,
    generate_property_rent_charges_until,
    generate_scheduled_due_charges,
    allocate_payment,
    allocate_unallocated_payments,
    move_tenancy,
    record_payment,
    refresh_all_room_statuses,
    revise_settlement_payment,
    scheduled_due_through_date,
    settle_charges,
    set_planned_checkout,
    sign_contract,
)
from .spreadsheet_import import TemplateImportError, import_template


def _redirect_back(request, fallback_name, **fallback_kwargs):
    referer = request.META.get("HTTP_REFERER")
    if referer and url_has_allowed_host_and_scheme(
        referer,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return redirect(referer)
    return redirect(fallback_name, **fallback_kwargs)


def _month_bounds(value, default_start=None):
    today = timezone.localdate()
    fallback = default_start or date(today.year, today.month, 1)
    if value:
        try:
            year, month = [int(part) for part in value.split("-", 1)]
            start = date(year, month, 1)
        except (TypeError, ValueError):
            start = fallback
    else:
        start = fallback
    next_month = date(start.year + (1 if start.month == 12 else 0), 1 if start.month == 12 else start.month + 1, 1)
    return start, next_month - timedelta(days=1)


def _room_number_sort_key(value):
    return tuple(int(part) if part.isdigit() else part.casefold() for part in re.split(r"(\d+)", value))


def _generate_due_charges_or_warn(request, through_date, scheduled=False):
    try:
        if scheduled:
            return generate_scheduled_due_charges(through_date)
        return generate_due_charges(through_date)
    except OperationalError as exc:
        if "locked" not in str(exc).lower():
            raise
        messages.warning(
            request,
            "数据库暂时被占用，本次未刷新自动待收；页面仍展示现有记录。稍后重新打开即可。",
        )
        return []


def dashboard(request):
    today = timezone.localdate()
    _generate_due_charges_or_warn(request, today, scheduled=True)
    open_charges = Charge.objects.exclude(status__in=[Charge.Status.PAID, Charge.Status.VOID])
    overdue = open_charges.filter(due_date__lt=today)
    due_soon = open_charges.filter(due_date__gte=today, due_date__lte=today + timedelta(days=7))
    stats = {
        "rooms": Room.objects.count(),
        "vacant": Room.objects.filter(status=Room.Status.VACANT).count(),
        "expiring": Room.objects.filter(status=Room.Status.EXPIRING).count(),
        "active_tenancies": Tenancy.objects.filter(status=Tenancy.Status.ACTIVE).count(),
        "open_income": money(sum((charge.balance for charge in open_charges.filter(direction=Charge.Direction.INCOME)), Decimal("0.00"))),
        "open_expense": money(sum((charge.balance for charge in open_charges.filter(direction=Charge.Direction.EXPENSE)), Decimal("0.00"))),
    }
    context = {
        "today": today,
        "stats": stats,
        "overdue_charges": overdue.select_related("room", "person", "tenancy")[:8],
        "due_soon_charges": due_soon.select_related("room", "person", "tenancy")[:8],
        "recent_payments": Payment.objects.select_related("room", "person", "tenancy")[:8],
    }
    return render(request, "core/dashboard.html", context)


@require_POST
def generate_charges_view(request):
    through = scheduled_due_through_date()
    created = generate_due_charges(through)
    messages.success(request, f"已刷新到 {through:%Y-%m-%d} 的待收待付，生成或更新 {len(created)} 条记录。")
    return redirect("dashboard")


def _sum_balances(charges):
    return money(sum((charge.balance for charge in charges), Decimal("0.00")))


def _room_finance_summary(room):
    charges = [
        charge
        for charge in room.charges.all()
        if charge.status not in {Charge.Status.PAID, Charge.Status.VOID}
    ]
    open_income = [charge for charge in charges if charge.direction == Charge.Direction.INCOME]
    open_expense = [charge for charge in charges if charge.direction == Charge.Direction.EXPENSE]
    deposit_due = _sum_balances([charge for charge in open_income if charge.category == Charge.Category.DEPOSIT])
    rent_due = _sum_balances([charge for charge in open_income if charge.category == Charge.Category.RENT])
    heating_due = _sum_balances([charge for charge in open_income if charge.category == Charge.Category.HEATING])
    other_due = _sum_balances(
        [
            charge
            for charge in open_income
            if charge.category not in {Charge.Category.DEPOSIT, Charge.Category.RENT, Charge.Category.HEATING}
        ]
    )
    prepaid = money(
        sum(
            (
                payment.unallocated_amount
                for payment in room.payments.all()
                if payment.direction == Payment.Direction.RECEIVE
                if payment.unallocated_amount > 0
            ),
            Decimal("0.00"),
        )
    )
    return {
        "deposit_due": deposit_due,
        "rent_due": rent_due,
        "heating_due": heating_due,
        "other_due": other_due,
        "tenant_due": money(deposit_due + rent_due + heating_due + other_due),
        "prepaid": prepaid,
        "expense_due": _sum_balances(open_expense),
    }


def _ledger_rows(charges, payments, reverse=False, sort_by="date"):
    rows = []
    for charge in charges:
        rows.append(
            {
                "kind": "charge",
                "date": charge.due_date,
                "direction": charge.get_direction_display(),
                "category": charge.get_category_display(),
                "room": charge.room,
                "person": charge.person,
                "amount": charge.amount,
                "balance": charge.balance,
                "status": charge.get_status_display(),
                "description": charge.description,
                "period_start": charge.period_start,
                "period_end": charge.period_end,
                "obj": charge,
                "open": charge.balance > 0,
            }
        )
    for payment in payments:
        unallocated = payment.unallocated_amount
        status = "已处理"
        if unallocated > 0:
            status = "预收" if payment.direction == Payment.Direction.RECEIVE else "预付"
        rows.append(
            {
                "kind": "payment",
                "date": payment.date,
                "direction": payment.get_direction_display(),
                "category": payment.get_category_display(),
                "room": payment.room,
                "person": payment.person,
                "amount": payment.amount,
                "balance": unallocated,
                "status": status,
                "description": payment.memo,
                "period_start": None,
                "period_end": None,
                "obj": payment,
                "open": unallocated > 0,
            }
        )
    if sort_by == "room":
        return sorted(
            rows,
            key=lambda row: (
                row["room"].number if row["room"] else "ZZZ",
                row["date"],
                row["kind"],
                row["obj"].id,
            ),
        )
    return sorted(rows, key=lambda row: (row["date"], row["kind"], row["obj"].id), reverse=reverse)


def room_list(request):
    today = timezone.localdate()
    refresh_all_room_statuses(today)
    room_rows = []
    rooms = Room.objects.all().prefetch_related(
        Prefetch(
            "tenancies",
            queryset=Tenancy.objects.filter(
                status=Tenancy.Status.ACTIVE,
                start_date__lte=today,
                end_date__gte=today,
            )
            .select_related("primary_person")
            .order_by("-start_date"),
            to_attr="current_tenancies",
        ),
        Prefetch(
            "stays",
            queryset=Stay.objects.filter(is_active=True).select_related("person").order_by("person__name"),
            to_attr="current_stays",
        ),
        "charges",
        "payments",
    )
    for room in sorted(rooms, key=lambda item: _room_number_sort_key(item.number)):
        finance = _room_finance_summary(room)
        room_rows.append(
            {
                "room": room,
                "finance": finance,
                "tenancy": room.current_tenancies[0] if room.current_tenancies else None,
                "people": room.current_stays,
            }
        )
    status_summaries = [
        {
            "value": value,
            "label": label,
            "count": sum(1 for row in room_rows if row["room"].status == value),
        }
        for value, label in Room.Status.choices
    ]
    return render(
        request,
        "core/room_list.html",
        {
            "room_rows": room_rows,
            "status_summaries": status_summaries,
            "total_due": money(sum((row["finance"]["tenant_due"] for row in room_rows), Decimal("0.00"))),
            "total_prepaid": money(sum((row["finance"]["prepaid"] for row in room_rows), Decimal("0.00"))),
        },
    )


def room_create(request):
    if request.method == "POST":
        form = RoomForm(request.POST, request.FILES)
        if form.is_valid():
            room = form.save()
            messages.success(request, f"已新增房间：{room.number}。")
            return redirect("room_detail", pk=room.pk)
    else:
        form = RoomForm()
    return render(request, "core/form_page.html", {"form": form, "title": "新增房间", "submit_label": "保存房间"})


def room_edit(request, pk):
    room = get_object_or_404(Room, pk=pk)
    if request.method == "POST":
        form = RoomForm(request.POST, request.FILES, instance=room)
        if form.is_valid():
            room = form.save()
            messages.success(request, f"已更新房间：{room.number}。")
            return redirect("room_detail", pk=room.pk)
    else:
        form = RoomForm(instance=room)
    return render(request, "core/form_page.html", {"form": form, "title": f"{room.number} 修改房间", "submit_label": "保存修改"})


@require_POST
def room_delete(request, pk):
    room = get_object_or_404(Room, pk=pk)
    if room.tenancies.exists() or room.stays.exists() or room.charges.exists() or room.payments.exists():
        messages.error(request, "这个房间已有合同、人员、账单或收付款记录，不能直接删除；可以改为自用或维修中。")
        return redirect("room_detail", pk=room.pk)
    number = room.number
    room.delete()
    messages.success(request, f"已删除房间：{number}。")
    return redirect("room_list")


def room_detail(request, pk):
    today = timezone.localdate()
    room = get_object_or_404(Room.objects.prefetch_related("tenancies", "stays", "charges", "payments"), pk=pk)
    room.refresh_status(today=today)
    active_tenancy = room.active_tenancy(today)
    latest_tenancy = room.tenancies.select_related("primary_person", "broker").order_by("-start_date").first()
    open_charges = room.charges.select_related("person", "tenancy").exclude(status__in=[Charge.Status.PAID, Charge.Status.VOID])
    collection_charges = sorted(
        open_charges.filter(category__in=[Charge.Category.DEPOSIT, Charge.Category.RENT, Charge.Category.HEATING]),
        key=lambda charge: (
            {
                Charge.Category.DEPOSIT: 0,
                Charge.Category.RENT: 1,
                Charge.Category.HEATING: 2,
            }.get(charge.category, 99),
            charge.due_date,
            charge.id,
        ),
    )
    all_charges = room.charges.exclude(status=Charge.Status.VOID)
    payments = room.payments.select_related("person", "tenancy").order_by("-date", "-id")
    charge_total = money(sum((charge.amount for charge in all_charges), Decimal("0.00")))
    payment_total = money(sum((payment.amount for payment in payments), Decimal("0.00")))
    balance = money(charge_total - payment_total)
    context = {
        "today": today,
        "room": room,
        "active_tenancy": active_tenancy,
        "latest_tenancy": latest_tenancy,
        "active_stays": room.stays.select_related("person", "tenancy").filter(is_active=True),
        "open_charges": open_charges.order_by("due_date", "id"),
        "collection_charges": collection_charges,
        "ledger_charges": all_charges.select_related("person", "tenancy").order_by("due_date", "id"),
        "ledger_rows": _ledger_rows(all_charges.select_related("person", "tenancy"), payments, reverse=False),
        "recent_payments": payments[:10],
        "charge_total": charge_total,
        "payment_total": payment_total,
        "balance": balance,
    }
    return render(request, "core/room_detail.html", context)


def person_list(request):
    rows = []
    people = Person.objects.prefetch_related("stays__room").all()
    for person in people:
        active_stays = [stay for stay in person.stays.all() if stay.is_active]
        rooms = sorted({stay.room.number for stay in active_stays})
        types = sorted({stay.get_stay_type_display() for stay in active_stays})
        rows.append(
            {
                "person": person,
                "rooms": "、".join(rooms),
                "types": "、".join(types),
                "sort_room": rooms[0] if rooms else "ZZZ",
            }
        )
    rows.sort(key=lambda row: (row["sort_room"], row["person"].name))
    return render(request, "core/person_list.html", {"person_rows": rows})


def person_detail(request, pk):
    person = get_object_or_404(Person.objects.prefetch_related("stays", "charges", "payments"), pk=pk)
    stays = person.stays.select_related("room", "tenancy").order_by("-is_active", "room__number", "-start_date")
    charges = person.charges.select_related("room", "tenancy").exclude(status=Charge.Status.VOID).order_by("due_date", "id")
    payments = person.payments.select_related("room", "tenancy").order_by("-date", "-id")
    return render(
        request,
        "core/person_detail.html",
        {
            "person": person,
            "stays": stays,
            "charges": charges,
            "payments": payments[:10],
            "ledger_rows": _ledger_rows(charges, payments, reverse=True),
        },
    )


def _stay_start_date(cleaned_data):
    return cleaned_data.get("start_date") or timezone.localdate()


def person_create(request):
    if request.method == "POST":
        form = PersonCreateForm(request.POST)
        if form.is_valid():
            cd = form.cleaned_data
            person, _ = Person.objects.update_or_create(
                id_number=cd["id_number"],
                defaults={
                    "name": cd["name"],
                    "phone": cd["phone"],
                    "emergency_name": cd["emergency_name"],
                    "emergency_phone": cd["emergency_phone"],
                    "emergency_address": cd["emergency_address"],
                    "notes": cd["notes"],
                },
            )
            room = cd["room"]
            start_date = _stay_start_date(cd)
            tenancy = room.active_tenancy(start_date) if cd["stay_type"] == Stay.Type.PERMANENT else None
            Stay.objects.create(
                person=person,
                room=room,
                tenancy=tenancy,
                stay_type=cd["stay_type"],
                start_date=start_date,
                end_date=cd["end_date"],
                is_active=True,
                report_note=cd["report_note"],
                notes=cd["stay_notes"],
            )
            room.refresh_status(save=True)
            messages.success(request, f"已新增人员并关联房间：{room.number} {person.name}。")
            return redirect("person_detail", pk=person.pk)
    else:
        initial = {"start_date": timezone.localdate()}
        if room_id := request.GET.get("room"):
            initial["room"] = room_id
        form = PersonCreateForm(initial=initial)
    return render(request, "core/form_page.html", {"form": form, "title": "新增人员", "submit_label": "保存人员"})


def person_edit(request, pk):
    person = get_object_or_404(Person, pk=pk)
    if request.method == "POST":
        form = PersonForm(request.POST, instance=person)
        if form.is_valid():
            person = form.save()
            messages.success(request, f"已更新人员：{person.name}。")
            return redirect("person_detail", pk=person.pk)
    else:
        form = PersonForm(instance=person)
    return render(request, "core/form_page.html", {"form": form, "title": f"{person.name} 修改人员", "submit_label": "保存修改"})


@require_POST
def person_delete(request, pk):
    person = get_object_or_404(Person, pk=pk)
    if person.stays.exists() or person.primary_tenancies.exists() or person.charges.exists() or person.payments.exists():
        messages.error(request, "这个人员已有入住、合同、账单或收付款记录，不能直接删除；可以先结束入住并保留历史。")
        return redirect("person_detail", pk=person.pk)
    name = person.name
    person.delete()
    messages.success(request, f"已删除人员：{name}。")
    return redirect("person_list")


def _save_person_stay(person, form, stay=None):
    cd = form.cleaned_data
    room = cd["room"]
    start_date = _stay_start_date(cd)
    tenancy = room.active_tenancy(start_date) if cd["stay_type"] == Stay.Type.PERMANENT else None
    if stay is None:
        stay = Stay(person=person)
    stay.person = person
    stay.room = room
    stay.tenancy = tenancy
    stay.stay_type = cd["stay_type"]
    stay.start_date = start_date
    stay.end_date = cd["end_date"]
    stay.is_active = cd["is_active"]
    stay.report_note = cd["report_note"]
    stay.notes = cd["notes"]
    stay.save()
    room.refresh_status(save=True)
    return stay


def person_stay_create(request, pk):
    person = get_object_or_404(Person, pk=pk)
    if request.method == "POST":
        form = PersonStayForm(request.POST)
        if form.is_valid():
            stay = _save_person_stay(person, form)
            messages.success(request, f"已添加入住记录：{stay.room.number}。")
            return redirect("person_detail", pk=person.pk)
    else:
        form = PersonStayForm(initial={"start_date": timezone.localdate(), "is_active": True})
    return render(request, "core/form_page.html", {"form": form, "title": f"{person.name} 添加入住", "submit_label": "保存入住"})


def person_stay_edit(request, person_pk, stay_pk):
    person = get_object_or_404(Person, pk=person_pk)
    stay = get_object_or_404(Stay, pk=stay_pk, person=person)
    if request.method == "POST":
        form = PersonStayForm(request.POST)
        if form.is_valid():
            _save_person_stay(person, form, stay=stay)
            messages.success(request, "已更新入住记录。")
            return redirect("person_detail", pk=person.pk)
    else:
        form = PersonStayForm(
            initial={
                "room": stay.room,
                "stay_type": stay.stay_type,
                "start_date": stay.start_date,
                "end_date": stay.end_date,
                "is_active": stay.is_active,
                "report_note": stay.report_note,
                "notes": stay.notes,
            }
        )
    return render(request, "core/form_page.html", {"form": form, "title": f"{person.name} 修改入住", "submit_label": "保存修改"})


@require_POST
def person_stay_remove(request, person_pk, stay_pk):
    person = get_object_or_404(Person, pk=person_pk)
    stay = get_object_or_404(Stay, pk=stay_pk, person=person)
    stay.is_active = False
    stay.end_date = stay.end_date or timezone.localdate()
    stay.save(update_fields=["is_active", "end_date"])
    messages.success(request, "已结束该入住记录。")
    return redirect("person_detail", pk=person.pk)


def import_workbook_view(request):
    if request.method == "POST":
        form = ImportWorkbookForm(request.POST, request.FILES)
        if form.is_valid():
            try:
                counts = import_template(request.FILES["file"], clear=form.cleaned_data["clear_existing"])
            except TemplateImportError as exc:
                messages.error(request, str(exc))
            else:
                summary = "，".join(f"{key}{value}" for key, value in counts.items())
                messages.success(request, f"导入完成：{summary}。")
                return redirect("dashboard")
    else:
        form = ImportWorkbookForm()
    return render(request, "core/form_page.html", {"form": form, "title": "导入标准模板", "submit_label": "开始导入"})


def tenancy_list(request):
    sort = request.GET.get("sort", "room")
    tenancies = list(Tenancy.objects.select_related("room", "primary_person", "broker").all())
    if sort == "start":
        tenancies.sort(key=lambda tenancy: (tenancy.start_date, tenancy.room.number))
    elif sort == "checkout":
        tenancies.sort(
            key=lambda tenancy: (
                tenancy.planned_move_out_date or tenancy.move_out_date or tenancy.end_date,
                tenancy.room.number,
            )
        )
    else:
        sort = "room"
        tenancies.sort(key=lambda tenancy: (tenancy.room.number, tenancy.start_date))
    return render(request, "core/tenancy_list.html", {"tenancies": tenancies, "sort": sort})


def tenancy_edit(request, pk):
    tenancy = get_object_or_404(Tenancy.objects.select_related("room", "primary_person", "broker"), pk=pk)
    old_room = tenancy.room
    had_planned_checkout = bool(tenancy.planned_move_out_date)
    if request.method == "POST":
        form = TenancyEditForm(request.POST, instance=tenancy)
        if form.is_valid():
            tenancy = form.save()
            if tenancy.status == Tenancy.Status.ACTIVE and tenancy.planned_move_out_date:
                set_planned_checkout(
                    tenancy,
                    planned_date=tenancy.planned_move_out_date,
                    refund_deposit_amount=tenancy.planned_deposit_refund_amount or tenancy.deposit_amount,
                    note=tenancy.planned_move_out_note,
                )
            elif had_planned_checkout:
                cancel_planned_checkout(tenancy)
            tenancy.commission_base = tenancy.room.commission_base
            tenancy.save(update_fields=["commission_base"])
            tenancy.room.refresh_status(save=True)
            if old_room.pk != tenancy.room_id:
                old_room.refresh_status(save=True)
                tenancy.stays.filter(is_active=True).update(room=tenancy.room)
            messages.success(request, "已更新合同。")
            return redirect("room_detail", pk=tenancy.room_id)
    else:
        form = TenancyEditForm(instance=tenancy)
    return render(request, "core/form_page.html", {"form": form, "title": f"{tenancy.room.number} 修改合同", "submit_label": "保存合同"})


@require_POST
def tenancy_delete(request, pk):
    tenancy = get_object_or_404(Tenancy, pk=pk)
    room_id = tenancy.room_id
    if tenancy.charges.exists() or tenancy.payments.exists():
        messages.error(request, "这个合同已有账单或收付款记录，不能直接删除；可以退租或改为已退租。")
        return redirect("room_detail", pk=room_id)
    tenancy.stays.update(tenancy=None)
    tenancy.delete()
    Room.objects.get(pk=room_id).refresh_status(save=True)
    messages.success(request, "已删除合同。")
    return redirect("room_detail", pk=room_id)


def sign_contract_view(request):
    refresh_all_room_statuses()
    vacant_rooms = Room.objects.filter(status=Room.Status.VACANT).order_by("number")
    if request.method == "POST":
        form = SignContractForm(request.POST, room_queryset=vacant_rooms)
        if form.is_valid():
            cd = form.cleaned_data
            tenancy = sign_contract(
                room=cd["room"],
                person_data={
                    "name": cd["person_name"],
                    "id_number": cd["id_number"],
                    "phone": cd["phone"],
                    "emergency_name": cd["emergency_name"],
                    "emergency_phone": cd["emergency_phone"],
                    "emergency_address": cd["emergency_address"],
                },
                start_date=cd["start_date"],
                end_date=cd["end_date"],
                monthly_rent=cd["monthly_rent"],
                payment_cycle=cd["payment_cycle"],
                deposit_amount=cd["deposit_amount"],
                broker_name=cd["broker_name"],
                commission_manual_amount=cd["commission_manual_amount"],
                first_month_discount=cd["first_month_discount"] or 0,
                notes=cd["notes"],
            )
            messages.success(request, f"已签约：{tenancy.room.number} {tenancy.primary_person.name}。")
            return redirect("room_detail", pk=tenancy.room_id)
    else:
        form = SignContractForm(room_queryset=vacant_rooms)
    return render(
        request,
        "core/sign_contract.html",
        {"form": form, "title": "签约", "submit_label": "保存合同", "contract_room": None},
    )


def room_contract_create(request, pk):
    room = get_object_or_404(Room, pk=pk)
    if room.status != Room.Status.VACANT:
        messages.error(request, "只有空房可以签约，请先确认房态。")
        return redirect("room_detail", pk=room.pk)
    active_tenancy = room.active_tenancy()
    if active_tenancy:
        messages.error(request, f"{room.number} 已有关联合同，不能重复登记。")
        return redirect("room_detail", pk=room.pk)
    if request.method == "POST":
        form = SignContractForm(request.POST, fixed_room=room)
        if form.is_valid():
            cd = form.cleaned_data
            tenancy = sign_contract(
                room=room,
                person_data={
                    "name": cd["person_name"],
                    "id_number": cd["id_number"],
                    "phone": cd["phone"],
                    "emergency_name": cd["emergency_name"],
                    "emergency_phone": cd["emergency_phone"],
                    "emergency_address": cd["emergency_address"],
                },
                start_date=cd["start_date"],
                end_date=cd["end_date"],
                monthly_rent=cd["monthly_rent"],
                payment_cycle=cd["payment_cycle"],
                deposit_amount=cd["deposit_amount"],
                broker_name=cd["broker_name"],
                commission_manual_amount=cd["commission_manual_amount"],
                first_month_discount=cd["first_month_discount"] or 0,
                notes=cd["notes"],
            )
            messages.success(request, f"已登记合同：{tenancy.room.number} {tenancy.primary_person.name}。")
            return redirect("room_detail", pk=room.pk)
    else:
        form = SignContractForm(fixed_room=room)
    return render(
        request,
        "core/sign_contract.html",
        {
            "form": form,
            "title": f"{room.number} 登记合同",
            "submit_label": "保存合同",
            "contract_room": room,
        },
    )


def _room_person_initial(stay):
    person = stay.person
    return {
        "stay_type": stay.stay_type,
        "person_name": person.name,
        "id_number": person.id_number,
        "phone": person.phone,
        "emergency_name": person.emergency_name,
        "emergency_phone": person.emergency_phone,
        "emergency_address": person.emergency_address,
        "start_date": stay.start_date,
        "end_date": stay.end_date,
        "report_note": stay.report_note,
        "person_notes": person.notes,
        "stay_notes": stay.notes,
    }


def _save_room_person(room, form, stay=None):
    cd = form.cleaned_data
    start_date = _stay_start_date(cd)
    person, _ = Person.objects.update_or_create(
        id_number=cd["id_number"],
        defaults={
            "name": cd["person_name"],
            "phone": cd["phone"],
            "emergency_name": cd["emergency_name"],
            "emergency_phone": cd["emergency_phone"],
            "emergency_address": cd["emergency_address"],
            "notes": cd["person_notes"],
        },
    )
    tenancy = room.active_tenancy(start_date) if cd["stay_type"] == Stay.Type.PERMANENT else None
    if stay is None:
        stay = Stay(person=person, room=room)
    stay.person = person
    stay.room = room
    stay.tenancy = tenancy
    stay.stay_type = cd["stay_type"]
    stay.start_date = start_date
    stay.end_date = cd["end_date"]
    stay.is_active = True
    stay.report_note = cd["report_note"]
    stay.notes = cd["stay_notes"]
    stay.save()
    return stay


def room_person_create(request, pk):
    room = get_object_or_404(Room, pk=pk)
    if request.method == "POST":
        form = RoomPersonForm(request.POST)
        if form.is_valid():
            stay = _save_room_person(room, form)
            messages.success(request, f"已添加人员：{stay.person.name}。")
            return redirect("room_detail", pk=room.pk)
    else:
        form = RoomPersonForm(initial={"start_date": timezone.localdate()})
    return render(request, "core/form_page.html", {"form": form, "title": f"{room.number} 添加人员", "submit_label": "保存人员"})


def room_person_edit(request, room_pk, stay_pk):
    room = get_object_or_404(Room, pk=room_pk)
    stay = get_object_or_404(Stay.objects.select_related("person"), pk=stay_pk, room=room)
    if request.method == "POST":
        form = RoomPersonForm(request.POST)
        if form.is_valid():
            stay = _save_room_person(room, form, stay=stay)
            messages.success(request, f"已更新人员：{stay.person.name}。")
            return redirect("room_detail", pk=room.pk)
    else:
        form = RoomPersonForm(initial=_room_person_initial(stay))
    return render(request, "core/form_page.html", {"form": form, "title": f"{room.number} 修改人员", "submit_label": "保存修改"})


@require_POST
def room_person_remove(request, room_pk, stay_pk):
    room = get_object_or_404(Room, pk=room_pk)
    stay = get_object_or_404(Stay, pk=stay_pk, room=room)
    stay.is_active = False
    stay.end_date = stay.end_date or timezone.localdate()
    stay.save(update_fields=["is_active", "end_date"])
    messages.success(request, f"已移除当前人员：{stay.person.name}。")
    return redirect("room_detail", pk=room.pk)


def payment_create(request):
    if request.method == "POST":
        form = PaymentForm(request.POST)
        if form.is_valid():
            cd = form.cleaned_data
            room = cd["room"]
            tenancy = room.active_tenancy(cd["date"]) if room else None
            payment = record_payment(
                direction=cd["direction"],
                category=cd["category"],
                date=cd["date"],
                amount=cd["amount"],
                tenancy=tenancy,
                room=room,
                person=tenancy.primary_person if tenancy else None,
                memo=cd["memo"],
                auto_allocate=True,
            )
            messages.success(request, f"已记录{payment.get_direction_display()}款 {payment.amount}。")
            return redirect("room_detail", pk=room.pk) if room else redirect("dashboard")
    else:
        initial = {"date": timezone.localdate(), "direction": Payment.Direction.RECEIVE}
        if room_id := request.GET.get("room"):
            initial["room"] = room_id
        form = PaymentForm(initial=initial)
    return render(request, "core/form_page.html", {"form": form, "title": "记录收付款", "submit_label": "保存记录"})


def _refresh_charges(charge_ids):
    for charge in Charge.objects.filter(id__in=charge_ids):
        charge.refresh_status()


def payment_edit(request, pk):
    payment = get_object_or_404(Payment.objects.select_related("room", "person", "tenancy"), pk=pk)
    if request.method == "POST":
        form = PaymentForm(request.POST, instance=payment)
        if form.is_valid():
            old_charge_ids = list(payment.allocations.values_list("charge_id", flat=True))
            payment.allocations.all().delete()
            _refresh_charges(old_charge_ids)
            cd = form.cleaned_data
            payment = form.save(commit=False)
            room = cd["room"]
            tenancy = room.active_tenancy(cd["date"]) if room else None
            payment.room = room
            payment.tenancy = tenancy
            payment.person = tenancy.primary_person if tenancy else None
            payment.save()
            allocate_payment(payment)
            messages.success(request, "已更新收付款记录。")
            return redirect("room_detail", pk=room.pk) if room else redirect("dashboard")
    else:
        form = PaymentForm(instance=payment)
    return render(request, "core/form_page.html", {"form": form, "title": "修改收付款", "submit_label": "保存修改"})


@require_POST
def payment_delete(request, pk):
    payment = get_object_or_404(Payment, pk=pk)
    room_id = payment.room_id
    charge_ids = list(payment.allocations.values_list("charge_id", flat=True))
    payment.delete()
    _refresh_charges(charge_ids)
    messages.success(request, "已删除收付款记录。")
    if room_id:
        return _redirect_back(request, "room_detail", pk=room_id)
    return _redirect_back(request, "charge_list")


def _charge_allocated_amount(charge):
    return money(sum((allocation.amount for allocation in charge.allocations.all()), Decimal("0.00")))


def _charge_bill_month(charge):
    value = charge.period_start or charge.due_date
    return date(value.year, value.month, 1)


def _bill_status(balance, paid_amount, partial_label="部分已付"):
    if balance <= 0:
        return "paid", "已结清"
    if paid_amount > 0:
        return "open", partial_label
    return "open", "待处理"


def _latest_row_payment(charges):
    charge_ids = {charge.id for charge in charges}
    payments = {}
    for charge in charges:
        for allocation in charge.allocations.all():
            payments[allocation.payment_id] = allocation.payment
    editable = []
    for payment in payments.values():
        allocated_charge_ids = {allocation.charge_id for allocation in payment.allocations.all()}
        if allocated_charge_ids and allocated_charge_ids <= charge_ids:
            editable.append(payment)
    return max(editable, key=lambda item: (item.date, item.id), default=None)


def _receivable_bill_rows(charges):
    grouped = {}
    cumulative_due = {}
    for charge in charges:
        allocated = _charge_allocated_amount(charge)
        balance = money(charge.amount - allocated)
        tenant_key = (charge.tenancy_id, charge.room_id)
        if balance > 0:
            cumulative_due[tenant_key] = money(cumulative_due.get(tenant_key, Decimal("0.00")) + balance)
        key = (*tenant_key, _charge_bill_month(charge))
        if key not in grouped:
            grouped[key] = {
                "room": charge.room,
                "person": charge.person,
                "tenancy": charge.tenancy,
                "bill_month": key[-1],
                "charges": [],
                "charge_ids": [],
                "components": [],
                "rent_amount": Decimal("0.00"),
                "rent_paid": Decimal("0.00"),
                "rent_balance": Decimal("0.00"),
                "heating_amount": Decimal("0.00"),
                "heating_paid": Decimal("0.00"),
                "heating_balance": Decimal("0.00"),
                "amount": Decimal("0.00"),
                "paid_amount": Decimal("0.00"),
                "balance": Decimal("0.00"),
            }
        row = grouped[key]
        row["charges"].append(charge)
        row["charge_ids"].append(charge.id)
        row["components"].append(
            {
                "category": charge.category,
                "label": charge.get_category_display(),
                "period_start": charge.period_start,
                "period_end": charge.period_end,
                "amount": charge.amount,
                "paid_amount": allocated,
                "balance": balance,
            }
        )
        row["amount"] = money(row["amount"] + charge.amount)
        row["paid_amount"] = money(row["paid_amount"] + allocated)
        row["balance"] = money(row["balance"] + balance)
        if charge.category == Charge.Category.RENT:
            row["rent_amount"] = money(row["rent_amount"] + charge.amount)
            row["rent_paid"] = money(row["rent_paid"] + allocated)
            row["rent_balance"] = money(row["rent_balance"] + balance)
        elif charge.category == Charge.Category.HEATING:
            row["heating_amount"] = money(row["heating_amount"] + charge.amount)
            row["heating_paid"] = money(row["heating_paid"] + allocated)
            row["heating_balance"] = money(row["heating_balance"] + balance)
    rows = list(grouped.values())
    for row in rows:
        row["components"].sort(key=lambda item: 0 if item["category"] == Charge.Category.RENT else 1)
        row["latest_payment"] = _latest_row_payment(row["charges"])
        row["status"], row["status_label"] = _bill_status(
            row["balance"],
            row["paid_amount"],
            partial_label="部分已收",
        )
        row["cumulative_due"] = cumulative_due.get(
            (row["tenancy"].id if row["tenancy"] else None, row["room"].id if row["room"] else None),
            Decimal("0.00"),
        )
    return rows


def _payable_bill_rows(charges):
    rows = []
    for charge in charges:
        paid_amount = _charge_allocated_amount(charge)
        balance = money(charge.amount - paid_amount)
        status, status_label = _bill_status(balance, paid_amount)
        rows.append(
            {
                "room": charge.room,
                "person": charge.person,
                "tenancy": charge.tenancy,
                "bill_month": _charge_bill_month(charge),
                "charges": [charge],
                "charge_ids": [charge.id],
                "description": charge.description,
                "category_label": charge.get_category_display(),
                "due_date": charge.due_date,
                "amount": charge.amount,
                "paid_amount": paid_amount,
                "balance": balance,
                "status": status,
                "status_label": status_label,
                "latest_payment": _latest_row_payment([charge]),
            }
        )
    return rows


def bill_list(request):
    default_through = scheduled_due_through_date()
    default_start = date(default_through.year, default_through.month, 1)
    month_start, month_end = _month_bounds(request.GET.get("month"), default_start=default_start)
    direction = request.GET.get("direction", "income")
    scope = request.GET.get("scope", "month")
    status = request.GET.get("status", "all")
    through_date = month_end if scope == "month" else default_through
    _generate_due_charges_or_warn(request, through_date)

    if direction == "expense":
        charges = list(
            Charge.objects.select_related("room", "person", "tenancy")
            .prefetch_related("allocations__payment__allocations")
            .filter(
                direction=Charge.Direction.EXPENSE,
                category__in=[
                    Charge.Category.COMMISSION,
                    Charge.Category.DEPOSIT_REFUND,
                    Charge.Category.PROPERTY_RENT,
                ],
            )
            .exclude(status=Charge.Status.VOID)
        )
        rows = _payable_bill_rows(charges)
    else:
        direction = "income"
        charges = list(
            Charge.objects.select_related("room", "person", "tenancy")
            .prefetch_related("allocations__payment__allocations")
            .filter(
                direction=Charge.Direction.INCOME,
                category__in=[Charge.Category.RENT, Charge.Category.HEATING],
            )
            .exclude(status=Charge.Status.VOID)
        )
        rows = _receivable_bill_rows(charges)

    if scope == "month":
        rows = [row for row in rows if month_start <= row["bill_month"] <= month_end]
    else:
        scope = "all"
    all_rows = rows
    open_count = sum(1 for row in all_rows if row["status"] == "open")
    paid_count = sum(1 for row in all_rows if row["status"] == "paid")
    total_amount = money(sum((row["amount"] for row in all_rows), Decimal("0.00")))
    total_balance = money(sum((row["balance"] for row in all_rows), Decimal("0.00")))
    if status in {"open", "paid"}:
        rows = [row for row in rows if row["status"] == status]
    else:
        status = "all"

    if scope == "month":
        rows.sort(
            key=lambda row: (
                _room_number_sort_key(row["room"].number) if row["room"] else ("ZZZ",),
                row["bill_month"],
            )
        )
    else:
        rows.sort(
            key=lambda row: (
                -row["bill_month"].toordinal(),
                _room_number_sort_key(row["room"].number) if row["room"] else ("ZZZ",),
            )
        )
    return render(
        request,
        "core/bill_list.html",
        {
            "bill_rows": rows,
            "direction": direction,
            "scope": scope,
            "status": status,
            "month_value": f"{month_start:%Y-%m}",
            "month_start": month_start,
            "month_end": month_end,
            "open_count": open_count,
            "paid_count": paid_count,
            "total_amount": total_amount,
            "total_balance": total_balance,
        },
    )


def collection_list(request):
    return bill_list(request)


def _validate_bill_charge_group(charges):
    if not charges:
        raise ValueError("账单不存在或已经失效。")
    directions = {charge.direction for charge in charges}
    if directions == {Charge.Direction.INCOME}:
        if any(charge.category not in {Charge.Category.RENT, Charge.Category.HEATING} for charge in charges):
            raise ValueError("待收账单只支持房租和取暖费。")
        keys = {
            (charge.tenancy_id, charge.room_id, _charge_bill_month(charge))
            for charge in charges
        }
        if len(keys) != 1:
            raise ValueError("只能处理同一房间同一月份的账单。")
    elif directions == {Charge.Direction.EXPENSE}:
        if len(charges) != 1:
            raise ValueError("待付账单请逐笔处理。")
        if charges[0].category not in {
            Charge.Category.COMMISSION,
            Charge.Category.DEPOSIT_REFUND,
            Charge.Category.PROPERTY_RENT,
        }:
            raise ValueError("这笔费用不属于固定待付账单。")
    else:
        raise ValueError("应收和应付账单不能合并处理。")


@require_POST
@transaction.atomic
def bill_bulk_settle(request):
    groups = request.POST.getlist("bill_groups")
    if not groups:
        messages.error(request, "请先选择需要全额收款的账单。")
        return _redirect_back(request, "bill_list")
    prepared = []
    try:
        for value in groups:
            ids = [int(item) for item in value.split(",") if item]
            charges = list(
                Charge.objects.select_related("room", "person", "tenancy")
                .filter(id__in=ids)
                .exclude(status=Charge.Status.VOID)
            )
            if len(charges) != len(set(ids)):
                raise ValueError("部分账单不存在或已经失效。")
            _validate_bill_charge_group(charges)
            if {charge.direction for charge in charges} != {Charge.Direction.INCOME}:
                raise ValueError("批量操作只支持待收账单。")
            prepared.append(charges)
        total = Decimal("0.00")
        for charges in prepared:
            payment = settle_charges(charges, date=timezone.localdate())
            total = money(total + payment.amount)
    except (TypeError, ValueError) as exc:
        transaction.set_rollback(True)
        messages.error(request, str(exc))
    else:
        messages.success(request, f"已全额收款 {len(prepared)} 户，共 ¥{total}，并分别生成流水。")
    return _redirect_back(request, "bill_list")


@require_POST
def bill_settle(request):
    charge_ids = request.POST.getlist("charge_ids")
    charges = list(
        Charge.objects.select_related("room", "person", "tenancy")
        .filter(id__in=charge_ids)
        .exclude(status=Charge.Status.VOID)
    )
    try:
        if not charges or len(charges) != len(set(charge_ids)):
            raise ValueError("账单不存在或已经失效。")
        _validate_bill_charge_group(charges)
        payment = settle_charges(
            charges,
            amount=request.POST.get("amount"),
            date=timezone.localdate(),
            memo=request.POST.get("memo", ""),
        )
    except ValueError as exc:
        messages.error(request, str(exc))
    else:
        action = "收款" if payment.direction == Payment.Direction.RECEIVE else "付款"
        messages.success(request, f"已记录{action} ¥{payment.amount}，并自动核销本行账单。")
    return _redirect_back(request, "bill_list")


def bill_payment_edit(request, pk):
    payment = get_object_or_404(Payment.objects.prefetch_related("allocations__charge"), pk=pk)
    charges = [allocation.charge for allocation in payment.allocations.all()]
    try:
        _validate_bill_charge_group(charges)
    except ValueError as exc:
        messages.error(request, f"{exc} 请到流水页面修改。")
        return redirect("charge_list")
    maximum = money(sum((charge.balance for charge in charges), Decimal("0.00")) + payment.allocated_amount)
    if request.method == "POST":
        form = BillPaymentEditForm(request.POST, maximum=maximum)
        if form.is_valid():
            cd = form.cleaned_data
            try:
                revise_settlement_payment(
                    payment,
                    amount=cd["amount"],
                    date=cd["date"],
                    memo=cd["memo"],
                )
            except ValueError as exc:
                form.add_error(None, str(exc))
            else:
                messages.success(request, "已修改账单收付款，并重新核销原账单。")
                return _redirect_back(request, "bill_list")
    else:
        form = BillPaymentEditForm(
            initial={"date": payment.date, "amount": payment.amount, "memo": payment.memo},
            maximum=maximum,
        )
    return render(
        request,
        "core/form_page.html",
        {
            "form": form,
            "title": "修改账单收付款",
            "submit_label": "保存修改",
        },
    )


def property_rent_rule_list(request):
    rules = RecurringRule.objects.filter(
        direction=Charge.Direction.EXPENSE,
        category=Charge.Category.PROPERTY_RENT,
    )
    return render(request, "core/property_rent_rule_list.html", {"rules": rules})


def property_rent_rule_create(request):
    if request.method == "POST":
        form = PropertyRentRuleForm(request.POST)
        if form.is_valid():
            rule = form.save()
            generate_property_rent_charges_until(rule, scheduled_due_through_date())
            messages.success(request, "已新增产权方房租规则并生成对应待付账单。")
            return redirect("property_rent_rule_list")
    else:
        form = PropertyRentRuleForm(
            initial={
                "name": "产权方房租",
                "frequency": RecurringRule.Frequency.QUARTERLY,
                "day_of_month": 1,
                "start_date": timezone.localdate(),
                "active": True,
            }
        )
    return render(
        request,
        "core/form_page.html",
        {"form": form, "title": "新增产权方房租规则", "submit_label": "保存规则"},
    )


def property_rent_rule_edit(request, pk):
    rule = get_object_or_404(
        RecurringRule,
        pk=pk,
        direction=Charge.Direction.EXPENSE,
        category=Charge.Category.PROPERTY_RENT,
    )
    if request.method == "POST":
        form = PropertyRentRuleForm(request.POST, instance=rule)
        if form.is_valid():
            rule = form.save()
            clear_unpaid_property_rent_charges(rule)
            generate_property_rent_charges_until(rule, scheduled_due_through_date())
            messages.success(request, "已更新产权方房租规则和未付款账单。")
            return redirect("property_rent_rule_list")
    else:
        form = PropertyRentRuleForm(instance=rule)
    return render(
        request,
        "core/form_page.html",
        {"form": form, "title": "修改产权方房租规则", "submit_label": "保存修改"},
    )


@require_POST
def property_rent_rule_delete(request, pk):
    rule = get_object_or_404(
        RecurringRule,
        pk=pk,
        direction=Charge.Direction.EXPENSE,
        category=Charge.Category.PROPERTY_RENT,
    )
    clear_unpaid_property_rent_charges(rule)
    rule.delete()
    messages.success(request, "已删除产权方房租规则；已付款历史账单保留。")
    return redirect("property_rent_rule_list")


@require_POST
def collection_collect(request, pk):
    charge = get_object_or_404(
        Charge.objects.select_related("room", "person", "tenancy"),
        pk=pk,
        direction=Charge.Direction.INCOME,
        category__in=[Charge.Category.DEPOSIT, Charge.Category.RENT, Charge.Category.HEATING],
    )
    try:
        payment = collect_charge(
            charge,
            amount=request.POST.get("amount"),
            date=timezone.localdate(),
            memo=request.POST.get("memo", ""),
        )
    except ValueError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, f"已记录收款：{charge.room.number if charge.room else ''} {payment.amount}。")
    return _redirect_back(request, "bill_list")


def charge_list(request):
    status = request.GET.get("status", "all")
    charges = Charge.objects.select_related("room", "person", "tenancy").all()
    payments = Payment.objects.select_related("room", "person", "tenancy").all()
    if status == "open":
        charges = charges.exclude(status__in=[Charge.Status.PAID, Charge.Status.VOID])
        payments = Payment.objects.none()
    elif status == "paid":
        charges = charges.filter(status=Charge.Status.PAID)
        payments = Payment.objects.none()
    elif status == "payments":
        charges = Charge.objects.none()
    elif status not in {"", "all"}:
        charges = charges.filter(status=status)
        payments = Payment.objects.none()
    return render(
        request,
        "core/charge_list.html",
        {"ledger_rows": _ledger_rows(charges, payments, sort_by="room"), "status": status},
    )


def charge_create(request):
    if request.method == "POST":
        form = ManualChargeForm(request.POST)
        if form.is_valid():
            cd = form.cleaned_data
            room = cd["room"]
            tenancy = room.active_tenancy(cd["date"]) if room else None
            description = cd["description"] or f"{room.number + ' ' if room else ''}{dict(Charge.Category.choices).get(cd['category'], '款项')}"
            if cd["direction"] in {Payment.Direction.RECEIVE, Payment.Direction.PAY}:
                payment_category = Payment.Category.OTHER
                if cd["category"] in {Charge.Category.DEPOSIT, Charge.Category.DEPOSIT_REFUND}:
                    payment_category = Payment.Category.DEPOSIT
                elif cd["category"] == Charge.Category.RENT:
                    payment_category = Payment.Category.RENT
                payment = record_payment(
                    direction=cd["direction"],
                    category=payment_category,
                    date=cd["date"],
                    amount=cd["amount"],
                    tenancy=tenancy,
                    room=room,
                    person=tenancy.primary_person if tenancy else None,
                    memo=description if not cd["notes"] else f"{description}：{cd['notes']}",
                    auto_allocate=True,
                )
                messages.success(request, f"已记录{payment.get_direction_display()}款 {payment.amount}。")
                return redirect("room_detail", pk=room.pk) if room else redirect("dashboard")
            charge = Charge.objects.create(
                direction=cd["direction"],
                category=cd["category"],
                room=room,
                tenancy=tenancy,
                person=tenancy.primary_person if tenancy else None,
                due_date=cd["date"],
                amount=cd["amount"],
                description=description,
                notes=cd["notes"],
                source=Charge.Source.MANUAL,
            )
            allocate_unallocated_payments()
            charge.refresh_from_db()
            messages.success(request, "已新增待处理账单。")
            return redirect("room_detail", pk=charge.room_id) if charge.room_id else redirect("charge_list")
    else:
        initial = {"date": timezone.localdate(), "direction": Charge.Direction.INCOME}
        if room_id := request.GET.get("room"):
            initial["room"] = room_id
        form = ManualChargeForm(initial=initial)
    return render(request, "core/form_page.html", {"form": form, "title": "新增账单", "submit_label": "保存账单"})


def charge_edit(request, pk):
    charge = get_object_or_404(Charge.objects.select_related("room", "person", "tenancy"), pk=pk)
    if request.method == "POST":
        form = ChargeEditForm(request.POST, instance=charge)
        if form.is_valid():
            charge = form.save(commit=False)
            if charge.room_id:
                tenancy = charge.room.active_tenancy(charge.due_date)
                charge.tenancy = tenancy
                charge.person = tenancy.primary_person if tenancy else None
            else:
                charge.tenancy = None
                charge.person = None
            charge.save()
            charge.refresh_status()
            messages.success(request, "已更新账单。")
            return redirect("room_detail", pk=charge.room_id) if charge.room_id else redirect("charge_list")
    else:
        form = ChargeEditForm(instance=charge)
    return render(request, "core/form_page.html", {"form": form, "title": "修改账单", "submit_label": "保存账单"})


@require_POST
def charge_delete(request, pk):
    charge = get_object_or_404(Charge, pk=pk)
    room_id = charge.room_id
    if charge.allocations.exists():
        messages.error(request, "这个账单已有收付款核销，不能直接删除；可以把账单改为已作废。")
        if room_id:
            return _redirect_back(request, "room_detail", pk=room_id)
        return _redirect_back(request, "charge_list")
    charge.delete()
    messages.success(request, "已删除账单。")
    if room_id:
        return _redirect_back(request, "room_detail", pk=room_id)
    return _redirect_back(request, "charge_list")


def move_tenancy_view(request, pk):
    refresh_all_room_statuses()
    tenancy = get_object_or_404(Tenancy.objects.select_related("room", "primary_person"), pk=pk)
    if request.method == "POST":
        form = MoveRoomForm(request.POST)
        if form.is_valid():
            cd = form.cleaned_data
            move_tenancy(
                tenancy,
                new_room=cd["new_room"],
                move_date=cd["move_date"],
                new_monthly_rent=cd["new_monthly_rent"],
                manual_diff_amount=cd["manual_diff_amount"],
                note=cd["note"],
            )
            messages.success(request, "已记录换房并生成补差账单。")
            return redirect("tenancy_list")
    else:
        form = MoveRoomForm(initial={"move_date": timezone.localdate(), "new_monthly_rent": tenancy.monthly_rent})
    return render(
        request,
        "core/form_page.html",
        {"form": form, "title": f"{tenancy.room.number} {tenancy.primary_person.name} 换房", "submit_label": "保存换房"},
    )


def checkout_tenancy_view(request, pk):
    tenancy = get_object_or_404(Tenancy.objects.select_related("room", "primary_person"), pk=pk)
    planned_initial = {
        "planned_move_out_date": tenancy.planned_move_out_date or timezone.localdate(),
        "planned_deposit_refund_amount": tenancy.planned_deposit_refund_amount
        if tenancy.planned_deposit_refund_amount is not None
        else tenancy.deposit_amount,
        "note": tenancy.planned_move_out_note,
    }
    actual_initial = {
        "checkout_date": tenancy.planned_move_out_date or timezone.localdate(),
        "refund_deposit_amount": tenancy.planned_deposit_refund_amount
        if tenancy.planned_deposit_refund_amount is not None
        else Decimal("3500.00"),
        "note": tenancy.planned_move_out_note,
    }
    if request.method == "POST":
        action = request.POST.get("action", "plan")
        if action == "cancel_plan":
            cancel_planned_checkout(tenancy)
            messages.success(request, "已取消预计退租。")
            return redirect("room_detail", pk=tenancy.room_id)
        if action == "actual":
            actual_form = CheckoutForm(request.POST)
            plan_form = PlannedCheckoutForm(initial=planned_initial, tenancy=tenancy)
            if actual_form.is_valid():
                cd = actual_form.cleaned_data
                checkout_tenancy(
                    tenancy,
                    checkout_date=cd["checkout_date"],
                    refund_deposit_amount=cd["refund_deposit_amount"],
                    deposit_deduction_amount=0,
                    note=cd["note"],
                )
                messages.success(request, "已确认实际退租，人员会从警务报备导出中移除。")
                return redirect("room_detail", pk=tenancy.room_id)
        else:
            plan_form = PlannedCheckoutForm(request.POST, tenancy=tenancy)
            actual_form = CheckoutForm(initial=actual_initial)
            if plan_form.is_valid():
                cd = plan_form.cleaned_data
                set_planned_checkout(
                    tenancy,
                    planned_date=cd["planned_move_out_date"],
                    refund_deposit_amount=cd["planned_deposit_refund_amount"],
                    note=cd["note"],
                )
                messages.success(request, "已保存预计退租，合同仍保持在租，警务报备不会提前移除。")
                return redirect("room_detail", pk=tenancy.room_id)
    else:
        plan_form = PlannedCheckoutForm(initial=planned_initial, tenancy=tenancy)
        actual_form = CheckoutForm(initial=actual_initial)
    return render(
        request,
        "core/checkout_tenancy.html",
        {"tenancy": tenancy, "plan_form": plan_form, "actual_form": actual_form},
    )


def visitor_stay_create(request):
    if request.method == "POST":
        form = VisitorStayForm(request.POST)
        if form.is_valid():
            cd = form.cleaned_data
            stay = create_visitor_stay(
                room=cd["room"],
                person_data={"name": cd["person_name"], "id_number": cd["id_number"], "phone": cd["phone"]},
                start_date=cd["start_date"],
                end_date=cd["end_date"],
                report_note=cd["report_note"],
                notes=cd["notes"],
            )
            messages.success(request, f"已记录暂住：{stay.room.number} {stay.person.name}。")
            return redirect("person_list")
    else:
        initial = {"start_date": timezone.localdate(), "end_date": timezone.localdate()}
        if room_id := request.GET.get("room"):
            initial["room"] = room_id
        form = VisitorStayForm(initial=initial)
    return render(request, "core/form_page.html", {"form": form, "title": "记录暂住/探望", "submit_label": "保存入住"})


def police_export_view(request):
    return export_police_report()


def agent_export_view(request):
    return export_agent_room_status()


def template_export_view(request):
    return export_import_template()
