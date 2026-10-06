from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.forms import DateField, formset_factory, modelformset_factory
from django.db import OperationalError, transaction
from django.db.models import Exists, F, OuterRef, Prefetch, Q, Sum, Value, CharField
from django.shortcuts import get_object_or_404, redirect, render as django_render
from django.urls import reverse
from django.http import JsonResponse
import hashlib
import json
from datetime import date, timedelta
from decimal import Decimal
import re
from urllib.parse import urlencode
from uuid import UUID, uuid4

from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from .exports import DEFAULT_AGENT_CONTACT, agent_room_status_data, export_agent_room_status, export_import_template, export_police_report, police_report_rows, police_report_snapshot
from .forms import (
    AgentContactForm,
    AgentFeesForm,
    DepositCollectionForm,
    RoomListingPriceForm,
    MonthlyRentCollectionForm,
    CommonFeesForm,
    RoommateForm,
    RenewalForm,
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
    PoliceReportRowForm,
    PropertyRentRuleForm,
    RoomPersonForm,
    RoomForm,
    SignContractForm,
    TenancyEditForm,
    VisitorStayForm,
)
from .models import ApartmentSettings, Charge, Payment, Person, PoliceReportExport, RecurringRule, Room, Stay, Tenancy
from .date_utils import money, long_term_first_rent, contract_months
from .services import (
    validate_deposit_receipt_change,
    allocation_candidates,
    renew_tenancy,
    tenancy_finances,
    tenancy_family_ids,
    cancel_planned_checkout,
    checkout_tenancy,
    clear_unpaid_property_rent_charges,
    collect_charge,
    create_visitor_stay,
    create_charge,
    generate_due_charges,
    generate_property_rent_charges_until,
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
from .rent_collection import monthly_rent_rows
from .deposit_collection import collect_deposit, deposit_rows
from .accounting import AccountingEntryForm, save_entry
from .maintenance import ensure_billing
from .queries import charge_totals, room_finance_summaries


def _return_url(request):
    target = request.POST.get("next") or request.GET.get("next")
    if target and url_has_allowed_host_and_scheme(target, {request.get_host()}, require_https=request.is_secure()):
        return target
    return ""


def _finish(request, name, **kwargs):
    return redirect(_return_url(request) or reverse(name, kwargs=kwargs))


def render(request, template, context=None, **kwargs):
    context = dict(context or {})
    fallback = "/rooms/"
    if request.path.startswith(("/people/", "/stays/")): fallback = "/people/"
    elif request.path.startswith(("/payments/", "/charges/", "/bills/")): fallback = "/bills/"
    elif request.path.startswith("/tenancies/"): fallback = "/tenancies/"
    context["return_url"] = _return_url(request) or fallback
    query = request.GET.copy()
    query.pop("page", None)
    context["page_query"] = query.urlencode()
    return django_render(request, template, context, **kwargs)


def _redirect_back(request, fallback_name, **fallback_kwargs):
    if _return_url(request):
        return redirect(_return_url(request))
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
            through_date = scheduled_due_through_date(through_date)
        return ensure_billing(through_date)
    except OperationalError as exc:
        if "locked" not in str(exc).lower():
            raise
        messages.warning(
            request,
            "数据库暂时被占用，本次未刷新自动待收；页面仍展示现有记录。稍后重新打开即可。",
        )
        return []


def dashboard(request):
    return room_list(request)

@require_POST
def generate_charges_view(request):
    through = scheduled_due_through_date()
    created = generate_due_charges(through)
    messages.success(request, f"已刷新到 {through:%Y-%m-%d} 的待收待付，生成或更新 {len(created)} 条记录。")
    return _finish(request, "dashboard")


def _sum_balances(charges):
    return money(sum((charge.balance for charge in charges), Decimal("0.00")))


def _room_finance_summary(room):
    if hasattr(room, "finance_summary"):
        return room.finance_summary
    today = timezone.localdate()
    related = Q(tenancy__room=room) | Q(room=room, tenancy__isnull=True)
    charges = getattr(room, "finance_charges", None)
    if charges is None:
        charges = list(Charge.objects.filter(related).prefetch_related("allocations", "adjustments"))
    payments = getattr(room, "finance_payments", None)
    if payments is None:
        payments = list(Payment.objects.filter(related).prefetch_related("allocations"))
    current = room.current_tenancies[0] if getattr(room, "current_tenancies", None) else room.active_tenancy(today)
    current_ids = set(tenancy_family_ids(current)) if current else set()
    has_receivables = any(c.direction == Charge.Direction.INCOME and c.status != Charge.Status.VOID and (not current or c.tenancy_id in current_ids) for c in charges)
    charges = [c for c in charges if c.status not in {Charge.Status.PAID, Charge.Status.VOID}]
    income = [c for c in charges if c.direction == Charge.Direction.INCOME]
    due = [c for c in income if c.due_date <= today]
    categories = {"rent_due": Charge.Category.RENT, "deposit_due": Charge.Category.DEPOSIT, "heating_due": Charge.Category.HEATING}
    summary = {key: _sum_balances([c for c in due if c.category == category]) for key, category in categories.items()}
    summary.update({
        "has_receivables": has_receivables,
        "other_due": _sum_balances([c for c in due if c.category not in categories.values()]),
        "tenant_due": _sum_balances(due),
        "current_due": _sum_balances([c for c in due if c.tenancy_id in current_ids]),
        "historical_due": _sum_balances([c for c in due if c.tenancy_id and c.tenancy_id not in current_ids]),
        "overdue": _sum_balances([c for c in due if c.due_date < today]),
        "future_due": _sum_balances([c for c in income if c.due_date > today]),
        "prepaid": money(sum((p.unallocated_amount for p in payments if p.direction == Payment.Direction.RECEIVE and p.auto_allocate and p.tenancy_id), Decimal("0.00"))),
        "expense_due": _sum_balances([c for c in charges if c.direction == Charge.Direction.EXPENSE]),
    })
    return summary

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
                "tenancy": charge.tenancy,
                "deposit_offset": charge.deposit_offset,
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
        if unallocated > 0 and payment.auto_allocate and payment.tenancy_id:
            status = "预收" if payment.direction == Payment.Direction.RECEIVE else "预付"
        rows.append(
            {
                "kind": "payment",
                "date": payment.date,
                "direction": payment.get_direction_display(),
                "category": "、".join(dict.fromkeys(a.charge.get_category_display() for a in payment.allocations.all())) or payment.get_category_display(),
                "room": payment.room,
                "person": payment.person,
                "tenancy": payment.tenancy,
                "amount": payment.amount,
                "balance": unallocated if payment.auto_allocate and payment.tenancy_id else Decimal("0.00"),
                "status": status,
                "description": payment.memo,
                "period_start": None,
                "period_end": None,
                "obj": payment,
                "open": False,
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


def _ledger_page(request, charges, payments):
    charge_keys = charges.order_by().annotate(ledger_date=F("due_date"), kind=Value("charge", output_field=CharField())).values("pk", "ledger_date", "kind")
    payment_keys = payments.order_by().annotate(ledger_date=F("date"), kind=Value("payment", output_field=CharField())).values("pk", "ledger_date", "kind")
    page = Paginator(charge_keys.union(payment_keys).order_by("-ledger_date", "-kind", "-pk"), 50).get_page(request.GET.get("page"))
    keys = list(page.object_list)
    visible_charges = charges.filter(pk__in=[r["pk"] for r in keys if r["kind"] == "charge"])
    visible_payments = payments.filter(pk__in=[r["pk"] for r in keys if r["kind"] == "payment"])
    return page, _ledger_rows(visible_charges, visible_payments, reverse=True)


def room_list(request):
    today = timezone.localdate()
    _generate_due_charges_or_warn(request, today, scheduled=True)
    refresh_all_room_statuses(today)
    room_rows = []
    rooms = Room.objects.all().prefetch_related(
        Prefetch(
            "tenancies",
            queryset=Tenancy.objects.filter(
                status=Tenancy.Status.ACTIVE,
                start_date__lte=today,
            )
            .select_related("primary_person", "renewal")
            .order_by("-start_date"),
            to_attr="current_tenancies",
        ),
        Prefetch(
            "stays",
            queryset=Stay.objects.current(today).select_related("person").order_by("person__name"),
            to_attr="current_stays",
        ),
    )
    rooms = list(rooms)
    room_finance_summaries(rooms, today)
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
    q = request.GET.get("q", "").strip()
    selected_status = request.GET.get("status", "all")
    total_rooms = len(room_rows)
    overdue_rooms = sum(1 for row in room_rows if row["finance"]["overdue"] > 0)
    for row in room_rows:
        row["expired"] = bool(row["tenancy"] and row["tenancy"].end_date < today)
        row["renewal"] = getattr(row["tenancy"], "renewal", None) if row["tenancy"] else None
    if q:
        room_rows = [row for row in room_rows if q.casefold() in row["room"].number.casefold() or any(q in stay.person.name or q in stay.person.phone for stay in row["people"])]
    if selected_status == "debt":
        room_rows = [row for row in room_rows if row["finance"]["tenant_due"] > 0]
    elif selected_status in Room.Status.values:
        room_rows = [row for row in room_rows if row["room"].status == selected_status]
    expiring_count = sum(item["count"] for item in status_summaries if item["value"] == Room.Status.EXPIRING)
    payable_count = Charge.objects.filter(direction=Charge.Direction.EXPENSE, due_date__lte=today).exclude(status__in=[Charge.Status.PAID, Charge.Status.VOID]).count()
    return render(
        request,
        "core/room_list.html",
        {
            "room_rows": room_rows,
            "q": q, "selected_status": selected_status, "total_rooms": total_rooms,
            "overdue_rooms": overdue_rooms, "expiring_count": expiring_count, "payable_count": payable_count,
            "status_summaries": status_summaries,
            "pending_billing_count": Tenancy.objects.exclude(status=Tenancy.Status.ENDED).filter(billing_enabled=False).count(),
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
            return _finish(request, "room_detail", pk=room.pk)
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
            return _finish(request, "room_detail", pk=room.pk)
    else:
        form = RoomForm(instance=room)
    return render(request, "core/form_page.html", {"form": form, "title": f"{room.number} 修改房间", "submit_label": "保存修改"})


@require_POST
def room_delete(request, pk):
    room = get_object_or_404(Room, pk=pk)
    if room.tenancies.exists() or room.stays.exists() or room.charges.exists() or room.payments.exists():
        messages.error(request, "这个房间已有合同、人员、账单或收付款记录，不能直接删除；可以改为自用或维修中。")
        return _finish(request, "room_detail", pk=room.pk)
    number = room.number
    room.delete()
    messages.success(request, f"已删除房间：{number}。")
    return _finish(request, "room_list")


def room_detail(request, pk):
    refresh_all_room_statuses()
    today = timezone.localdate()
    room = get_object_or_404(Room, pk=pk)
    room.current_tenancies = list(room.tenancies.filter(status="active", start_date__lte=today).select_related("primary_person", "broker", "renewal").order_by("-start_date"))
    active_tenancy = room.current_tenancies[0] if room.current_tenancies else None
    room_finance_summaries([room], today)
    latest_tenancy = room.tenancies.select_related("primary_person", "broker").order_by("-start_date").first()
    open_charges = charge_totals(room.charges.select_related("room", "person", "tenancy").exclude(status__in=[Charge.Status.PAID, Charge.Status.VOID]))
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
    all_charges = charge_totals(room.charges.exclude(status=Charge.Status.VOID).select_related("room", "person", "tenancy"))
    payments = room.payments.select_related("room", "person", "tenancy").prefetch_related("allocations__charge").order_by("-date", "-id")
    page, ledger_rows = _ledger_page(request, all_charges, payments)
    context = {
        "today": today,
        "room": room,
        "finance": _room_finance_summary(room),
        "tenancy_finance": tenancy_finances(active_tenancy) if active_tenancy else None,
        "active_tenancy": active_tenancy,
        "latest_tenancy": latest_tenancy,
        "active_stays": room.stays.select_related("person", "tenancy").current(today),
        "open_charges": open_charges.order_by("due_date", "id"),
        "collection_charges": collection_charges,
        "ledger_charges": all_charges.select_related("person", "tenancy").order_by("due_date", "id"),
        "ledger_rows": ledger_rows, "page_obj": page,
        "recent_payments": payments[:10],
    }
    return render(request, "core/room_detail.html", context)


def person_list(request):
    refresh_all_room_statuses()
    today = timezone.localdate()
    q = request.GET.get("q", "").strip()
    scope = request.GET.get("scope", "active")
    room_id = request.GET.get("room", "")
    current_stays = Stay.objects.current(today).filter(person_id=OuterRef("pk"))
    people = Person.objects.annotate(has_current_stay=Exists(current_stays))
    if scope == "active":
        people = people.filter(has_current_stay=True)
    elif scope == "history":
        people = people.filter(has_current_stay=False)
    if room_id:
        room_stays = Stay.objects.filter(person_id=OuterRef("pk"), room_id=room_id) if room_id.isdigit() else Stay.objects.none()
        people = people.annotate(in_current_room=Exists(room_stays.current(today)), in_past_room=Exists(room_stays)).filter(
            Q(has_current_stay=True, in_current_room=True) | Q(has_current_stay=False, in_past_room=True))
    stays_query = Stay.objects.current(today) if scope == "active" else Stay.objects.all()
    people = people.prefetch_related(Prefetch("stays", queryset=stays_query.select_related("room")))
    if q:
        people = people.filter(Q(name__icontains=q) | Q(phone__icontains=q) | Q(id_number__icontains=q) | Q(stays__room__number__icontains=q)).distinct()
    rows = []
    for person in people:
        all_stays = list(person.stays.all())
        stays = [stay for stay in all_stays if stay.active_on(today)]
        display_stays = stays or all_stays
        if scope == "active" and not stays: continue
        if scope == "history" and stays: continue
        if room_id and not any(str(stay.room_id) == room_id for stay in display_stays): continue
        rooms = sorted({stay.room.number for stay in display_stays}, key=_room_number_sort_key)
        rows.append({"person": person, "stays": stays, "rooms": "、".join(rooms), "types": "、".join(sorted({stay.get_stay_type_display() for stay in stays})), "sort_room": rooms[0] if rooms else "ZZZ", "visitor_due": any(stay.stay_type == Stay.Type.VISITOR and stay.end_date and stay.end_date <= today for stay in stays)})
    rows.sort(key=lambda row: (_room_number_sort_key(row["sort_room"]), row["person"].name))
    fingerprint = police_fingerprint()
    return render(request, "core/person_list.html", {"person_rows": rows, "person_count": len(rows), "q": q, "scope": scope, "rooms": Room.objects.all(), "room_filter": room_id, "report_changed": request.session.get("police_fingerprint") != fingerprint, "last_export": request.session.get("police_export_time")})

def person_detail(request, pk):
    person = get_object_or_404(Person, pk=pk)
    stays = person.stays.select_related("room", "tenancy").order_by("-is_active", "room__number", "-start_date")
    charges = charge_totals(person.charges.select_related("room", "person", "tenancy").exclude(status=Charge.Status.VOID))
    payments = person.payments.select_related("room", "person", "tenancy").prefetch_related("allocations__charge").order_by("-date", "-id")
    page, ledger_rows = _ledger_page(request, charges, payments)
    return render(
        request,
        "core/person_detail.html",
        {
            "person": person,
            "stays": stays,
            "charges": charges,
            "payments": payments[:10],
            "ledger_rows": ledger_rows, "page_obj": page,
        },
    )


def _stay_start_date(cleaned_data):
    return cleaned_data.get("start_date")


@transaction.atomic
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
            tenancy = room.active_tenancy() if cd["stay_type"] == Stay.Type.PERMANENT else None
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
            if request.POST.get("action") == "continue":
                query = urlencode({"room": room.pk, "stay_type": cd["stay_type"], "next": _return_url(request)})
                return redirect(f"{reverse('person_create')}?{query}")
            return _finish(request, "person_detail", pk=person.pk)
    else:
        initial = {"start_date": timezone.localdate()}
        if request.GET.get("stay_type") in Stay.Type.values:
            initial["stay_type"] = request.GET["stay_type"]
            if initial["stay_type"] != Stay.Type.PERMANENT:
                initial["start_date"] = None
        if room_id := request.GET.get("room"):
            initial["room"] = room_id
        if person_id := request.GET.get("person"):
            person = get_object_or_404(Person, pk=person_id)
            initial.update({field: getattr(person, field) for field in ["name", "id_number", "phone", "emergency_name", "emergency_phone", "emergency_address", "notes"]})
        form = PersonCreateForm(initial=initial)
    return render(request, "core/form_page.html", {"form": form, "title": "新增人员", "submit_label": "保存人员", "continue_adding": True})


def person_edit(request, pk):
    person = get_object_or_404(Person, pk=pk)
    if request.method == "POST":
        form = PersonForm(request.POST, instance=person)
        if form.is_valid():
            person = form.save()
            messages.success(request, f"已更新人员：{person.name}。")
            return _finish(request, "person_detail", pk=person.pk)
    else:
        form = PersonForm(instance=person)
    return render(request, "core/form_page.html", {"form": form, "title": f"{person.name} 修改人员", "submit_label": "保存修改"})


@require_POST
def person_delete(request, pk):
    person = get_object_or_404(Person, pk=pk)
    if person.stays.exists() or person.primary_tenancies.exists() or person.charges.exists() or person.payments.exists():
        messages.error(request, "这个人员已有入住、合同、账单或收付款记录，不能直接删除；可以先结束入住并保留历史。")
        return _finish(request, "person_detail", pk=person.pk)
    name = person.name
    person.delete()
    messages.success(request, f"已删除人员：{name}。")
    return _finish(request, "person_list")


def _save_person_stay(person, form, stay=None):
    cd = form.cleaned_data
    room = cd["room"]
    start_date = _stay_start_date(cd)
    tenancy = room.active_tenancy() if cd["stay_type"] == Stay.Type.PERMANENT else None
    if stay and not cd["is_active"] and stay.room_id == room.pk:
        tenancy = stay.tenancy
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
        form = PersonStayForm(request.POST, person=person)
        if form.is_valid():
            stay = _save_person_stay(person, form)
            messages.success(request, f"已添加入住记录：{stay.room.number}。")
            return _finish(request, "person_detail", pk=person.pk)
    else:
        form = PersonStayForm(initial={"start_date": timezone.localdate(), "is_active": True}, person=person)
    return render(request, "core/form_page.html", {"form": form, "title": f"{person.name} 添加入住", "submit_label": "保存入住"})


def person_stay_edit(request, person_pk, stay_pk):
    person = get_object_or_404(Person, pk=person_pk)
    stay = get_object_or_404(Stay, pk=stay_pk, person=person)
    if request.method == "POST":
        form = PersonStayForm(request.POST, person=person, stay=stay)
        if form.is_valid():
            _save_person_stay(person, form, stay=stay)
            messages.success(request, "已更新入住记录。")
            return _finish(request, "person_detail", pk=person.pk)
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
    if not stay.is_active:
        return _finish(request, "person_detail", pk=person.pk)
    stay.is_active = False
    stay.end_date = timezone.localdate()
    stay.save(update_fields=["is_active", "end_date"])
    messages.success(request, "已结束该入住记录。")
    return _finish(request, "person_detail", pk=person.pk)


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
                return _finish(request, "dashboard")
    else:
        form = ImportWorkbookForm()
    return render(request, "core/form_page.html", {"form": form, "title": "导入标准模板", "submit_label": "开始导入"})


def tenancy_list(request):
    sort = request.GET.get("sort", "room")
    refresh_all_room_statuses()
    q = request.GET.get("q", "").strip()
    scope = request.GET.get("scope", "current")
    queryset = Tenancy.objects.select_related("room", "primary_person", "broker", "renewal")
    if scope == "current": queryset = queryset.exclude(status=Tenancy.Status.ENDED)
    elif scope == "ended": queryset = queryset.filter(status=Tenancy.Status.ENDED)
    elif scope == "expiring": queryset = queryset.filter(status=Tenancy.Status.ACTIVE, end_date__lte=timezone.localdate() + timedelta(days=30))
    if q: queryset = queryset.filter(Q(room__number__icontains=q) | Q(primary_person__name__icontains=q))
    tenancies = list(queryset)
    balances = charge_totals(Charge.objects.filter(tenancy__in=tenancies, direction="income", due_date__lte=timezone.localdate()).exclude(status="void"))
    balances = dict(balances.order_by().values("tenancy_id").annotate(total=Sum("_balance")).values_list("tenancy_id", "total"))
    for tenancy in tenancies:
        tenancy.due_balance = balances.get(tenancy.pk, Decimal("0.00"))
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
    return render(request, "core/tenancy_list.html", {"tenancies": tenancies, "sort": sort, "q": q, "scope": scope})


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
            return _finish(request, "room_detail", pk=tenancy.room_id)
    else:
        form = TenancyEditForm(instance=tenancy)
    return render(request, "core/form_page.html", {"form": form, "title": f"{tenancy.room.number} 修改合同", "submit_label": "保存合同"})


@require_POST
def tenancy_delete(request, pk):
    tenancy = get_object_or_404(Tenancy, pk=pk)
    room_id = tenancy.room_id
    if tenancy.charges.exists() or tenancy.payments.exists():
        messages.error(request, "这个合同已有账单或收付款记录，不能直接删除；可以退租或改为已退租。")
        return _finish(request, "room_detail", pk=room_id)
    tenancy.stays.update(tenancy=None)
    tenancy.delete()
    Room.objects.get(pk=room_id).refresh_status(save=True)
    messages.success(request, "已删除合同。")
    return _finish(request, "room_detail", pk=room_id)


def sign_contract_view(request):
    return _sign_contract_page(request)


def room_contract_create(request, pk):
    room = get_object_or_404(Room, pk=pk)
    room.refresh_status()
    if room.status != Room.Status.VACANT or room.active_tenancy():
        messages.error(request, "只有空房可以办理入住，请先确认房态。")
        return _finish(request, "room_detail", pk=room.pk)
    return _sign_contract_page(request, room)


def _sign_contract_page(request, room=None):
    refresh_all_room_statuses()
    vacant = Room.objects.filter(status=Room.Status.VACANT).order_by("number")
    data = request.POST if request.method == "POST" else None
    initial = {"start_date": timezone.localdate()}
    if room: initial.update({"monthly_rent": room.listing_price, "room": room})
    form = SignContractForm(data, fixed_room=room, room_queryset=vacant, initial=initial)
    Roommates = formset_factory(RoommateForm, extra=1, max_num=10, validate_max=True)
    # Old clients can still submit a contract without the optional roommate formset.
    roommates = Roommates(data if data and "roommates-TOTAL_FORMS" in data else None, prefix="roommates")
    if request.method == "POST" and form.is_valid() and (not roommates.is_bound or roommates.is_valid()):
        cd = form.cleaned_data
        roommate_data = [row for row in roommates.cleaned_data if row] if roommates.is_bound else []
        ids = [cd["id_number"], *[row["id_number"] for row in roommate_data]]
        if len(ids) != len(set(ids)):
            form.add_error(None, "主租客与同住人的身份证不能重复。")
        else:
            try:
                tenancy = sign_contract(
                    room=cd["room"], person_data={"name": cd["person_name"], **{key: cd[key] for key in ["id_number", "phone", "emergency_name", "emergency_phone", "emergency_address"]}},
                    start_date=cd["start_date"], end_date=cd["end_date"], monthly_rent=cd["monthly_rent"], payment_cycle=cd["payment_cycle"], deposit_amount=cd["deposit_amount"], broker_name=cd["broker_name"], commission_manual_amount=cd["commission_manual_amount"], first_month_discount=cd["first_month_discount"] or 0, notes=cd["notes"], received_amount=cd.get("received_amount"), roommates=roommate_data, fee_terms=form.cleaned_fee_terms,
                )
            except ValueError as exc:
                form.add_error(None, str(exc))
            else:
                messages.success(request, f"已办理入住：{tenancy.room.number} {tenancy.primary_person.name}，合同、人员和首期费用已同步保存。")
                return _finish(request, "room_detail", pk=tenancy.room_id)
    return render(request, "core/sign_contract.html", {"form": form, "roommates": roommates, "title": f"{room.number} 办理入住" if room else "办理入住", "submit_label": "保存入住", "contract_room": room})


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
    tenancy = room.active_tenancy() if cd["stay_type"] == Stay.Type.PERMANENT else None
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
        form = RoomPersonForm(request.POST, room=room)
        if form.is_valid():
            stay = _save_room_person(room, form)
            messages.success(request, f"已添加人员：{stay.person.name}。")
            return _finish(request, "room_detail", pk=room.pk)
    else:
        form = RoomPersonForm(initial={"start_date": timezone.localdate()})
    return render(request, "core/form_page.html", {"form": form, "title": f"{room.number} 添加人员", "submit_label": "保存人员"})


def room_person_edit(request, room_pk, stay_pk):
    room = get_object_or_404(Room, pk=room_pk)
    stay = get_object_or_404(Stay.objects.select_related("person"), pk=stay_pk, room=room)
    if request.method == "POST":
        form = RoomPersonForm(request.POST, room=room, stay=stay)
        if form.is_valid():
            stay = _save_room_person(room, form, stay=stay)
            messages.success(request, f"已更新人员：{stay.person.name}。")
            return _finish(request, "room_detail", pk=room.pk)
    else:
        form = RoomPersonForm(initial=_room_person_initial(stay))
    return render(request, "core/form_page.html", {"form": form, "title": f"{room.number} 修改人员", "submit_label": "保存修改"})


@require_POST
def room_person_remove(request, room_pk, stay_pk):
    room = get_object_or_404(Room, pk=room_pk)
    stay = get_object_or_404(Stay, pk=stay_pk, room=room)
    if not stay.is_active:
        return _finish(request, "room_detail", pk=room.pk)
    stay.is_active = False
    stay.end_date = timezone.localdate()
    stay.save(update_fields=["is_active", "end_date"])
    messages.success(request, f"已移除当前人员：{stay.person.name}。")
    return _finish(request, "room_detail", pk=room.pk)


def payment_create(request):
    if request.method == "GET":
        return accounting_entry(request)
    refresh_all_room_statuses()
    initial = {"date": timezone.localdate(), "direction": request.GET.get("direction", Payment.Direction.RECEIVE), "room": request.GET.get("room"), "category": Payment.Category.DEPOSIT if request.GET.get("category") == Payment.Category.DEPOSIT else Payment.Category.OTHER}
    form = PaymentForm(request.POST if request.method == "POST" else None, initial=initial)
    if request.method == "POST" and form.is_valid():
        cd = form.cleaned_data
        room = cd["room"] or (cd["tenancy"].room if cd.get("tenancy") else None)
        tenancy = cd.get("tenancy") or (room.active_tenancy(cd["date"]) if room else None)
        payment = record_payment(direction=cd["direction"], category=cd["category"], date=cd["date"], amount=cd["amount"], tenancy=tenancy, room=room, person=tenancy.primary_person if tenancy else None, memo=cd["memo"], auto_allocate=cd["direction"] == Payment.Direction.RECEIVE)
        message = f"已记录{payment.get_direction_display()}款 ¥{payment.amount}。"
        if tenancy and payment.direction == Payment.Direction.RECEIVE:
            finance = tenancy_finances(tenancy)
            message += f" 当前到期未收 ¥{finance['due']}，预存余额 ¥{finance['prepaid']}。"
        messages.success(request, message)
        return _finish(request, "charge_list")
    return render(request, "core/form_page.html", {"form": form, "title": "收款 / 记支出", "submit_label": "确认保存", "payment_preview": True})


def accounting_entry(request):
    initial = {"date": timezone.localdate(), "token": uuid4(), "category": request.GET.get("category", "other"),
        "room": request.GET.get("room"), "tenancy": request.GET.get("tenancy"),
        "direction": "expense" if request.GET.get("direction") in {"expense", "pay"} else "income",
        "state": request.GET.get("state", "pending" if request.resolver_match.url_name == "charge_create" else "paid"), "charge": request.GET.get("charge")}
    if request.method == "POST":
        try:
            token = UUID(request.POST.get("token", ""))
        except (ValueError, TypeError, AttributeError):
            token = None
        if token and (Payment.objects.filter(entry_token=token).exists() or Charge.objects.filter(entry_token=token).exists()):
            messages.info(request, "这笔记录已经保存，没有重复记账。")
            return _finish(request, "bill_list")
    form = AccountingEntryForm(request.POST if request.method == "POST" else None, initial=initial)
    if request.method == "POST" and form.is_valid():
        try:
            charge, payment = save_entry(form.cleaned_data)
        except ValueError as exc:
            form.add_error(None, str(exc))
        else:
            if charge:
                messages.success(request, f"已保存{charge.description}。" + (f"本次实际收付 ¥{payment.amount}，未结 ¥{charge.balance}。" if payment else f"待收付 ¥{charge.balance}。"))
            else:
                messages.success(request, f"已收租金 ¥{payment.amount}，其中 ¥{payment.allocated_amount} 用于原欠款，余款 ¥{payment.unallocated_amount} 作为预存。")
            return _finish(request, "bill_list")
    bills = [{"id": c.pk, "direction": c.direction, "category": c.category, "room": c.room_id,
        "tenancy": c.tenancy_id, "balance": str(c.balance), "description": c.description,
        "person": c.person.name if c.person else "公共/房间费用",
        "period": f"{c.period_start:%Y-%m-%d} 至 {c.period_end:%Y-%m-%d}" if c.period_start and c.period_end else ""}
        for c in form.fields["charge"].queryset]
    contracts = [{"id": t.pk, "room": t.room_id, "status": t.status, "label": str(t),
        "start": t.start_date.isoformat(), "end": t.end_date.isoformat(), "person": t.primary_person.name,
        "checkout_url": reverse("checkout_tenancy", args=[t.pk]) if t.status == Tenancy.Status.ACTIVE else ""} for t in form.fields["tenancy"].queryset]
    return render(request, "core/accounting_entry.html", {"form": form, "entry_bills": bills, "entry_contracts": contracts})


def payment_preview(request):
    form = PaymentForm(request.GET)
    if not form.is_valid():
        return JsonResponse({"error": "请选择房间并填写有效金额和日期。"}, status=400)
    cd = form.cleaned_data
    room = cd["room"] or (cd["tenancy"].room if cd.get("tenancy") else None)
    tenancy = cd.get("tenancy") or (room.active_tenancy(cd["date"]) if room else None)
    payment = Payment(direction=cd["direction"], category=cd["category"], amount=cd["amount"], date=cd["date"], room=room, tenancy=tenancy, auto_allocate=cd["direction"] == Payment.Direction.RECEIVE)
    remaining = cd["amount"]
    items = []
    for charge in allocation_candidates(payment):
        amount = min(remaining, charge.balance)
        if amount > 0:
            items.append(f"{charge.description}：抵扣 ¥{amount:.2f}")
            remaining -= amount
    label = "预存余额" if tenancy and payment.direction == Payment.Direction.RECEIVE else "独立收支（不抵扣其他账单）"
    finance = tenancy_finances(tenancy) if tenancy else None
    return JsonResponse({"items": items, "remaining": str(remaining), "remaining_label": label, "person": tenancy.primary_person.name if tenancy else "公共或无合同收支", "current_due": str(finance["due"]) if finance else None})

def _refresh_charges(charge_ids):
    for charge in Charge.objects.filter(id__in=charge_ids):
        charge.refresh_status()


@transaction.atomic
def payment_edit(request, pk):
    payment = get_object_or_404(Payment.objects.select_related("room", "person", "tenancy"), pk=pk)
    charges = [allocation.charge for allocation in payment.allocations.select_related("charge")]
    if charges:
        maximum = money(sum((charge.balance for charge in charges), Decimal("0.00")) + payment.allocated_amount)
        form = BillPaymentEditForm(request.POST if request.method == "POST" else None, initial={"date": payment.date, "amount": payment.amount, "memo": payment.memo}, maximum=maximum)
        if request.method == "POST" and form.is_valid():
            try:
                revise_settlement_payment(payment, **form.cleaned_data)
            except ValueError as exc:
                form.add_error(None, str(exc))
            else:
                messages.success(request, "已修改这笔收付款，仅重新抵扣原账单。")
                return _finish(request, "charge_list")
    else:
        form = PaymentForm(request.POST if request.method == "POST" else None, instance=payment)
        if request.method == "POST" and form.is_valid():
            cd = form.cleaned_data
            payment = form.save(commit=False)
            room = cd["room"] or (cd["tenancy"].room if cd.get("tenancy") else None)
            tenancy = cd.get("tenancy") or (room.active_tenancy(cd["date"]) if room else None)
            payment.room, payment.tenancy = room, tenancy
            payment.person = tenancy.primary_person if tenancy else None
            payment.auto_allocate = cd["direction"] == Payment.Direction.RECEIVE
            payment.save()
            allocate_payment(payment)
            messages.success(request, "已更新收付款记录。")
            return _finish(request, "charge_list")
    return render(request, "core/form_page.html", {"form": form, "title": "修改收付款", "submit_label": "保存修改", "form_note": "已有抵扣记录时，只调整原账单的金额；更换对象请先撤销原流水，再重新登记。" if charges else ""})


@require_POST
def payment_delete(request, pk):
    payment = get_object_or_404(Payment, pk=pk)
    try:
        validate_deposit_receipt_change(payment)
    except ValueError as exc:
        messages.error(request, str(exc))
        return _redirect_back(request, "charge_list")
    room_id = payment.room_id
    charge_ids = list(payment.allocations.values_list("charge_id", flat=True))
    payment.delete()
    _refresh_charges(charge_ids)
    messages.success(request, "已删除收付款记录。")
    if room_id:
        return _redirect_back(request, "room_detail", pk=room_id)
    return _redirect_back(request, "charge_list")


def _charge_allocated_amount(charge):
    return charge.allocated_amount


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
        if balance > 0 and charge.due_date <= timezone.localdate():
            cumulative_due[tenant_key] = money(cumulative_due.get(tenant_key, Decimal("0.00")) + balance)
        key = (*tenant_key, _charge_bill_month(charge), None if charge.category in {Charge.Category.RENT, Charge.Category.HEATING} and charge.tenancy_id else charge.pk)
        if key not in grouped:
            grouped[key] = {
                "room": charge.room,
                "person": charge.person,
                "tenancy": charge.tenancy,
                "bill_month": key[2],
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
                "id": charge.id,
                "category": charge.category,
                "due_date": charge.due_date,
                "label": charge.get_category_display(),
                "period_start": charge.period_start,
                "period_end": charge.period_end,
                "amount": charge.amount,
                "paid_amount": allocated,
                "deposit_offset": charge.deposit_offset,
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
    scope = request.GET.get("scope", "month" if request.GET.get("month") else "all")
    status = request.GET.get("status", "all")
    category = request.GET.get("category", "all")
    q = request.GET.get("q", "").strip()
    timing = request.GET.get("timing", "all")
    through_date = month_end if scope == "month" else default_through
    _generate_due_charges_or_warn(request, through_date)

    if direction == "expense":
        charges = list(
            Charge.objects.select_related("room", "person", "tenancy")
            .prefetch_related("allocations__payment__allocations", "adjustments")
            .filter(
                direction=Charge.Direction.EXPENSE,

            )
            .exclude(status=Charge.Status.VOID)
        )
        charges = _filter_bills(charges, category, q, timing, request.GET.get("room"))
        rows = _payable_bill_rows(charges)
    else:
        direction = "income"
        charges = list(
            Charge.objects.select_related("room", "person", "tenancy")
            .prefetch_related("allocations__payment__allocations", "adjustments")
            .filter(
                direction=Charge.Direction.INCOME,

            )
            .exclude(status=Charge.Status.VOID)
        )
        cumulative = {}
        for charge in charges:
            if charge.due_date <= timezone.localdate():
                key = (charge.tenancy_id, charge.room_id)
                cumulative[key] = cumulative.get(key, Decimal("0.00")) + charge.balance
        charges = _filter_bills(charges, category, q, timing, request.GET.get("room"))
        rows = _receivable_bill_rows(charges)
        for row in rows:
            row["cumulative_due"] = money(cumulative.get((row["tenancy"].pk if row["tenancy"] else None, row["room"].pk if row["room"] else None), Decimal("0.00")))

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
            "q": q, "category": category, "timing": timing, "room_filter": request.GET.get("room", ""),
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


def monthly_rent_collection(request):
    today = timezone.localdate()
    source = request.POST if request.method == "POST" else request.GET
    collection_scope = source.get("collection_scope", "all")
    if collection_scope not in {"all", "unpaid", "draft", "paid"}:
        collection_scope = "all"
    month_field = MonthlyRentCollectionForm.base_fields["month"]
    try:
        month = month_field.clean(source.get("month") or f"{scheduled_due_through_date(today):%Y-%m}")
    except ValidationError:
        messages.error(request, "请选择有效的房租月份。")
        month = date(today.year, today.month, 1)
        if request.method == "POST":
            return redirect(f"{reverse('monthly_rent_collection')}?" + urlencode({"month": f"{month:%Y-%m}", "include_heating": source.get("include_heating", "0"), "collection_scope": collection_scope}))
    include_heating = source.get("include_heating") == "1"
    rows = monthly_rent_rows(month, include_heating=include_heating)
    data = request.POST.copy() if request.method == "POST" else None
    if data is not None and data.get("single_row"):
        key = data["single_row"]
        data["action"] = "single"
        data.setlist("rows", [key])
        data["amount"] = data.get(f"partial_{key}", "")
    form = MonthlyRentCollectionForm(
        data, rent_rows=rows,
        initial={"month": month, "date": today, "action": "bulk"},
    )
    if request.method == "POST" and form.is_valid():
        cd = form.cleaned_data
        try:
            if not cd["rows"]:
                raise ValueError("请勾选已实际交租的房间，或粘贴房号带入选择。")
            if cd["action"] == "single" and len(cd["rows"]) != 1:
                raise ValueError("单笔收租请选择一间房。")
            if cd["action"] == "bulk" and any(data.get(f"partial_{key}") for key in cd["rows"]):
                raise ValueError("已填写单笔实收金额，请先用该房间的“记录收款”保存，或清空金额后再批量收齐。")
            with transaction.atomic():
                current = {row["key"]: row for row in monthly_rent_rows(month, include_heating=include_heating)}
                count, skipped, total = 0, 0, Decimal("0.00")
                for key in dict.fromkeys(cd["rows"]):
                    row = current.get(key)
                    if row is None:
                        raise ValueError("部分房租已变更或作废，请刷新页面后重新选择。")
                    if row["balance"] <= 0:
                        skipped += 1
                        continue
                    charges = row["charges"]
                    if row["draft"]:
                        spec = dict(row["draft"])
                        spec["amount"] = cd.get(f"rent_amount_{key}") or row["draft"]["amount"]
                        if spec["amount"] <= 0:
                            raise ValueError("本期应收金额必须大于 0。")
                        charges = charges + [create_charge(**spec, source=Charge.Source.MANUAL)]
                    if row["tenancy"]:
                        existing_receipts = Payment.objects.filter(
                            tenancy_id__in=tenancy_family_ids(row["tenancy"]),
                            direction=Payment.Direction.RECEIVE, auto_allocate=True,
                        ).prefetch_related("allocations").order_by("date", "id")
                        for receipt in existing_receipts:
                            if receipt.unallocated_amount > 0:
                                allocate_payment(receipt)
                    # Read fresh balances; a repeated submission must not create another receipt.
                    charges = list(Charge.objects.filter(pk__in=[charge.pk for charge in charges]).exclude(status=Charge.Status.VOID))
                    if not charges:
                        raise ValueError("房租账单已经失效，请刷新页面。")
                    if sum((charge.balance for charge in charges), Decimal("0.00")) <= 0:
                        skipped += 1
                        continue
                    payment = settle_charges(
                        charges, amount=cd["amount"] if cd["action"] == "single" else None,
                        date=cd["date"], memo=f"{month:%Y年%m月}房租 · {'单笔收租' if cd['action'] == 'single' else '批量收租'}",
                    )
                    count += 1
                    total = money(total + payment.amount)
        except ValueError as exc:
            form.add_error(None, str(exc))
        else:
            messages.success(request, f"已记录 {count} 户房租，共 ¥{total}。" + (f"跳过 {skipped} 户已交齐的房间。" if skipped else ""))
            return redirect(f"{reverse('monthly_rent_collection')}?" + urlencode({"month": f"{month:%Y-%m}", "include_heating": source.get("include_heating", "0"), "collection_scope": collection_scope}))
    selected_numbers = {number.upper() for number in re.split(r"[\s,，、;；]+", request.GET.get("room_numbers", "").strip()) if number}
    for row in rows:
        row["selected"] = row["key"] in data.getlist("rows") if data is not None else row["room"].number.upper() in selected_numbers and row["balance"] > 0
        row["amount_field"] = form[f"rent_amount_{row['key']}"] if row["draft"] else None
        row["partial_amount"] = request.POST.get(f"partial_{row['key']}", "")
    if selected_numbers:
        available = {row["room"].number.upper(): row for row in rows}
        missing = sorted(selected_numbers - available.keys())
        paid = sorted(number for number in selected_numbers & available.keys() if available[number]["balance"] <= 0)
        if missing:
            messages.warning(request, "这些房号没有当月可登记房租，请核对：" + "、".join(missing))
        if paid:
            messages.info(request, "已交齐，已跳过：" + "、".join(paid))
    all_rows = rows
    if request.method == "GET" and not selected_numbers:
        if collection_scope == "unpaid":
            rows = [row for row in rows if row["other_balance"] > 0]
        elif collection_scope == "draft":
            rows = [row for row in rows if row["draft"]]
        elif collection_scope == "paid":
            rows = [row for row in rows if row["status"] == "paid"]
    rows = sorted(rows, key=lambda row: row["status"] == "paid")
    return render(request, "core/monthly_rent_collection.html", {
        "form": form, "include_heating": include_heating, "rent_rows": rows, "month": month, "month_value": f"{month:%Y-%m}",
        "collection_scope": collection_scope,
        "room_numbers": request.GET.get("room_numbers", ""),
        "paid_count": sum(row["status"] == "paid" for row in all_rows),
        "open_count": sum(row["status"] != "paid" for row in all_rows),
        "booked_count": sum(row["other_balance"] > 0 for row in all_rows),
        "draft_count": sum(bool(row["draft"]) for row in all_rows),
        "booked_balance": money(sum((row["other_balance"] for row in all_rows), Decimal("0.00"))),
        "draft_balance": money(sum((row["draft"]["amount"] for row in all_rows if row["draft"]), Decimal("0.00"))),
        "total_paid": money(sum((row["paid"] for row in all_rows), Decimal("0.00"))),
        "total_balance": money(sum((row["balance"] for row in all_rows), Decimal("0.00"))),
    })


def deposit_collection_view(request):
    rows = deposit_rows()
    data = request.POST.copy() if request.method == "POST" else None
    if data is not None and data.get("single_row"):
        key = data["single_row"]
        data["action"] = "single"
        data.setlist("rows", [key])
        data["amount"] = data.get(f"partial_{key}", "")
    form = DepositCollectionForm(data, deposit_rows=rows, initial={"date": timezone.localdate(), "action": "bulk"})
    if request.method == "POST" and form.is_valid():
        cd = form.cleaned_data
        try:
            if not cd["rows"]:
                raise ValueError("请勾选已实际收到押金的房间。")
            if cd["action"] == "single" and len(cd["rows"]) != 1:
                raise ValueError("单笔登记请选择一间房。")
            if cd["action"] == "bulk" and any(data.get(f"partial_{key}") for key in cd["rows"]):
                raise ValueError("已填写本次实收，请先用该房间的“记录本笔押金”保存，或清空金额再批量登记。")
            count, skipped, total = 0, 0, Decimal("0.00")
            with transaction.atomic():
                for key in dict.fromkeys(cd["rows"]):
                    payment = collect_deposit(
                        key, date=cd["date"], amount=cd["amount"], single=cd["action"] == "single",
                        deposit_amount=cd.get(f"deposit_amount_{key}"),
                    )
                    if payment is None:
                        skipped += 1
                    else:
                        count += 1
                        total = money(total + payment.amount)
        except (ValueError, Tenancy.DoesNotExist) as exc:
            form.add_error(None, str(exc) if isinstance(exc, ValueError) else "合同已变更，请刷新页面。")
        else:
            messages.success(request, f"已登记 {count} 户押金实收，共 ¥{total}。" + (f"另有 {skipped} 户无需新增收款。" if skipped else ""))
            return redirect("deposit_collection")
    for row in rows:
        row["amount_field"] = form[f"deposit_amount_{row['key']}"] if row["draft"] else None
        row["selected"] = data is not None and row["key"] in data.getlist("rows")
        row["partial_amount"] = request.POST.get(f"partial_{row['key']}", "")
    return render(request, "core/deposit_collection.html", {
        "form": form, "deposit_rows": rows,
        "paid_count": sum(row["status"] == "paid" for row in rows),
        "open_count": sum(row["status"] in {"open", "partial"} for row in rows),
        "total_paid": money(sum((row["paid"] for row in rows), Decimal("0.00"))),
        "total_balance": money(sum((row["balance"] for row in rows if not row["void"]), Decimal("0.00"))),
    })


def collection_list(request):
    return bill_list(request)


def _validate_bill_charge_group(charges):
    if not charges:
        raise ValueError("账单不存在或已经失效。")
    directions = {charge.direction for charge in charges}
    if directions == {Charge.Direction.INCOME}:
        keys = {
            (charge.tenancy_id, charge.room_id, _charge_bill_month(charge))
            for charge in charges
        }
        if len(keys) != 1:
            raise ValueError("只能处理同一房间同一月份的账单。")
    elif directions == {Charge.Direction.EXPENSE}:
        if len(charges) != 1:
            raise ValueError("待付账单请逐笔处理。")
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
        paid_date = DateField().clean(request.POST["date"]) if request.POST.get("date") else timezone.localdate()
        payment = settle_charges(
            charges,
            amount=request.POST.get("amount"),
            date=paid_date,
            memo=request.POST.get("memo", ""),
        )
    except (ValueError, ValidationError) as exc:
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
        return _finish(request, "charge_list")
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
            return _finish(request, "property_rent_rule_list")
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
            return _finish(request, "property_rent_rule_list")
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
    return _finish(request, "property_rent_rule_list")


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
    status = request.GET.get("status", "payments")
    q = request.GET.get("q", "").strip()
    month = request.GET.get("month", f"{timezone.localdate():%Y-%m}" if status == "payments" else "")
    charges = charge_totals(Charge.objects.select_related("room", "person", "tenancy"))
    payments = Payment.objects.select_related("room", "person", "tenancy").prefetch_related("allocations__charge").all()
    if q:
        charges = charges.filter(Q(room__number__icontains=q) | Q(person__name__icontains=q) | Q(description__icontains=q))
        payments = payments.filter(Q(room__number__icontains=q) | Q(person__name__icontains=q) | Q(memo__icontains=q))
    if month:
        start, end = _month_bounds(month)
        payments = payments.filter(date__range=(start, end))
        charges = charges.filter(due_date__range=(start, end))
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
    totals = payments.aggregate(income=Sum("amount", filter=Q(direction="receive"), default=0), expense=Sum("amount", filter=Q(direction="pay"), default=0))
    page, ledger_rows = _ledger_page(request, charges, payments)
    return render(
        request,
        "core/charge_list.html",
        {"ledger_rows": ledger_rows, "page_obj": page, "status": status, "q": q, "month": month, "income_total": money(totals["income"]), "expense_total": money(totals["expense"])},
    )


def charge_create(request):
    if request.method == "GET":
        return accounting_entry(request)
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
                    auto_allocate=cd["direction"] == Payment.Direction.RECEIVE,
                )
                messages.success(request, f"已记录{payment.get_direction_display()}款 {payment.amount}。")
                return _finish(request, "room_detail", pk=room.pk) if room else _finish(request, "dashboard")
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
            return _finish(request, "room_detail", pk=charge.room_id) if charge.room_id else _finish(request, "charge_list")
    else:
        initial = {"date": timezone.localdate(), "direction": request.GET.get("direction", Charge.Direction.INCOME)}
        if room_id := request.GET.get("room"):
            initial["room"] = room_id
        form = ManualChargeForm(initial=initial)
    return render(request, "core/form_page.html", {"form": form, "title": "增加待收 / 待付", "submit_label": "保存待收付", "form_note": "用于尚未实际收付的钱。已经发生的收支请使用“收款”或“记支出”。"})


def charge_edit(request, pk):
    charge = get_object_or_404(Charge.objects.select_related("room", "person", "tenancy"), pk=pk)
    if request.method == "POST":
        form = ChargeEditForm(request.POST, instance=charge)
        if form.is_valid():
            charge = form.save(commit=False)
            if charge.room_id and (not charge.tenancy_id or charge.tenancy.room_id != charge.room_id):
                tenancy = charge.room.active_tenancy(charge.due_date)
                charge.tenancy = tenancy
                charge.person = tenancy.primary_person if tenancy else None
            elif not charge.room_id:
                charge.tenancy = None
                charge.person = None
            charge.source = Charge.Source.MANUAL
            charge.save()
            charge.refresh_status()
            messages.success(request, "已更新账单。")
            return _finish(request, "room_detail", pk=charge.room_id) if charge.room_id else _finish(request, "charge_list")
    else:
        form = ChargeEditForm(instance=charge)
    return render(request, "core/form_page.html", {"form": form, "title": "修改账单", "submit_label": "保存账单"})


@require_POST
def charge_delete(request, pk):
    charge = get_object_or_404(Charge, pk=pk)
    room_id = charge.room_id
    if charge.allocations.exists() or charge.deposit_offset:
        messages.error(request, "这个账单已有收付款或押金抵扣，不能直接删除或作废；请先核对相应结算。")
        if room_id:
            return _redirect_back(request, "room_detail", pk=room_id)
        return _redirect_back(request, "charge_list")
    if charge.generated_key:
        charge.status = Charge.Status.VOID
        charge.save(update_fields=["status"])
        messages.success(request, "已作废自动账单，刷新后不会重新生成。")
    else:
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
            try:
                move_tenancy(
                    tenancy,
                    new_room=cd["new_room"],
                    move_date=cd["move_date"],
                    new_monthly_rent=cd["new_monthly_rent"],
                    manual_diff_amount=cd["manual_diff_amount"],
                    note=cd["note"],
                )
            except ValueError as exc:
                form.add_error(None, str(exc))
            else:
                messages.success(request, "已记录换房并生成补差账单。")
                return _finish(request, "tenancy_list")
    else:
        form = MoveRoomForm(initial={"move_date": timezone.localdate(), "new_monthly_rent": tenancy.monthly_rent})
    return render(
        request,
        "core/form_page.html",
        {"form": form, "title": f"{tenancy.room.number} {tenancy.primary_person.name} 换房", "submit_label": "保存换房", "move_rent": tenancy.monthly_rent},
    )


def checkout_tenancy_view(request, pk):
    tenancy = get_object_or_404(Tenancy.objects.select_related("room", "primary_person"), pk=pk)
    if tenancy.status != Tenancy.Status.ACTIVE or Tenancy.objects.filter(previous_tenancy=tenancy).exists():
        messages.error(request, "合同已结束或已安排续租；如需退租，请先取消未生效的续租合同。")
        return _finish(request, "tenancy_list")
    finance = tenancy_finances(tenancy)
    planned_initial = {
        "planned_move_out_date": tenancy.planned_move_out_date or timezone.localdate(),
        "planned_deposit_refund_amount": tenancy.planned_deposit_refund_amount
        if tenancy.planned_deposit_refund_amount is not None
        else finance["deposit_held"],
        "note": tenancy.planned_move_out_note,
    }
    actual_initial = {
        "checkout_date": tenancy.planned_move_out_date or timezone.localdate(),
        "refund_deposit_amount": tenancy.planned_deposit_refund_amount
        if tenancy.planned_deposit_refund_amount is not None
        else finance["deposit_held"],
        "note": tenancy.planned_move_out_note,
    }
    if request.method == "POST":
        action = request.POST.get("action", "plan")
        if action == "cancel_plan":
            cancel_planned_checkout(tenancy)
            messages.success(request, "已取消预计退租。")
            return _finish(request, "room_detail", pk=tenancy.room_id)
        if action == "actual":
            actual_form = CheckoutForm(request.POST)
            plan_form = PlannedCheckoutForm(initial=planned_initial, tenancy=tenancy)
            actual_form.is_valid()
            if actual_form.cleaned_data.get("checkout_date") and actual_form.cleaned_data["checkout_date"] < tenancy.start_date:
                actual_form.add_error("checkout_date", "退租日期不能早于合同开始日期。")
            if actual_form.is_valid():
                cd = actual_form.cleaned_data
                try:
                    checkout_tenancy(
                        tenancy, checkout_date=cd["checkout_date"],
                        refund_deposit_amount=cd["refund_deposit_amount"],
                        deposit_deduction_amount=cd.get("deposit_deduction_amount") or 0,
                        deposit_offset_amount=cd.get("deposit_offset_amount") or 0,
                        refund_paid=cd.get("refund_paid", False), note=cd["note"],
                    )
                except ValueError as exc:
                    actual_form.add_error(None, str(exc))
                else:
                    messages.success(request, "已保存退租结算，未实际退还的押金继续列在待付。")
                    return _finish(request, "room_detail", pk=tenancy.room_id)
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
                return _finish(request, "room_detail", pk=tenancy.room_id)
    else:
        plan_form = PlannedCheckoutForm(initial=planned_initial, tenancy=tenancy)
        actual_form = CheckoutForm(initial=actual_initial)
    return render(
        request,
        "core/checkout_tenancy.html",
        {"tenancy": tenancy, "plan_form": plan_form, "actual_form": actual_form, "finance": finance},
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
            return _finish(request, "person_list")
    else:
        initial = {}
        if room_id := request.GET.get("room"):
            initial["room"] = room_id
        form = VisitorStayForm(initial=initial)
    return render(request, "core/form_page.html", {"form": form, "title": "记录暂住/探望", "submit_label": "保存入住"})


@transaction.atomic
def _create_police_export(request):
    refresh_all_room_statuses()
    today = timezone.localdate()
    rows = police_report_rows(today)
    snapshot = police_report_snapshot(rows)
    report = PoliceReportExport.objects.create(report_date=today, rows=snapshot)
    # Failed serialization rolls back the batch; departures remain pending.
    response = export_police_report(today, snapshot=snapshot, report_id=report.pk)
    request.session["police_fingerprint"] = police_fingerprint(rows)
    request.session["police_export_time"] = timezone.localtime().strftime("%Y-%m-%d %H:%M")
    response["Cache-Control"] = "no-store"
    return report, response


def police_export_view(request):
    try:
        _, response = _create_police_export(request)
    except ValidationError:
        messages.error(request, "报备内容有待修正，请在预览中查看提示并修改对应人员。")
        return redirect("police_preview")
    return response


@transaction.atomic
def police_preview(request):
    refresh_all_room_statuses()
    rows = police_report_rows(include_excluded=True)
    initial = [{"stay_id": row["stay"].pk,
                "departure_token": str(row["stay"].police_departure_token or ""),
                "text": row["text"],
                "report_departure": "yes" if row["included"] else "no"} for row in rows]
    RowFormSet = formset_factory(PoliceReportRowForm, extra=0, min_num=len(rows), max_num=len(rows),
                                validate_min=True, validate_max=True)
    editing = request.method == "POST" and ("form-TOTAL_FORMS" in request.POST or "action" in request.POST)
    formset = RowFormSet(request.POST if editing else None, initial=initial)
    ready = True
    if editing:
        ready = formset.is_valid()
        if ready:
            ready = all(form.cleaned_data["stay_id"] == item["stay_id"]
                        and form.cleaned_data["departure_token"] == item["departure_token"]
                        and (not row["departed"] or form.cleaned_data["report_departure"] in {"yes", "no"})
                        for form, item, row in zip(formset, initial, rows))
            if not ready:
                messages.error(request, "人员或退租状态已变化，请重新打开预览后调整并保存。")
        if ready:
            for form, row in zip(formset, rows):
                stay = row["stay"]
                text = form.cleaned_data["text"]
                override = stay.police_report_text_override if text == row["text"] else text
                Stay.objects.filter(pk=stay.pk).update(
                    police_report_text_override=override,
                    police_departure_required=form.cleaned_data["report_departure"] == "yes" if row["departed"] else True,
                )
            messages.success(request, "已保存报备内容和退租报备选择。")
            if request.POST.get("action") == "save":
                return redirect("police_preview")
    if request.method == "POST" and ready:
        try:
            report, _ = _create_police_export(request)
        except ValidationError:
            messages.error(request, "报备内容有待修正，请修改下方提示。")
        else:
            return redirect(f"{reverse('police_report_detail', args=[report.pk])}?download=1")
    for row, form in zip(rows, formset):
        row["form"] = form
    selected_rows = [row for row in rows if row["included"]]
    return render(request, "core/police_preview.html", {
        "report_rows": rows, "report_count": len(selected_rows), "formset": formset,
        "current_count": sum(not r["departed"] for r in rows),
        "departure_count": sum(r["departed"] for r in selected_rows),
        "excluded_count": sum(not r["included"] for r in rows),
        "can_export": not any(r["errors"] for r in selected_rows),
        "recent_reports": PoliceReportExport.objects.all()[:10],
    })


def police_report_detail(request, pk):
    report = get_object_or_404(PoliceReportExport, pk=pk)
    return render(request, "core/police_report_detail.html", {
        "report": report, "auto_download": request.GET.get("download") == "1",
    })


def police_report_download(request, pk):
    report = get_object_or_404(PoliceReportExport, pk=pk)
    response = export_police_report(report.report_date, snapshot=report.rows, report_id=report.pk)
    response["Cache-Control"] = "no-store"
    return response


@require_POST
@transaction.atomic
def police_report_confirm(request, pk):
    report = get_object_or_404(PoliceReportExport.objects.select_for_update(), pk=pk)
    if not report.sent_at:
        report.sent_at = timezone.now()
        for row in report.rows:
            if row["departure_token"]:
                Stay.objects.filter(pk=row["stay_id"], is_active=False,
                                    police_departure_token=row["departure_token"],
                                    police_departure_reported_at__isnull=True).update(police_departure_reported_at=report.sent_at)
        report.save(update_fields=["sent_at"])
        messages.success(request, "已确认本次报备已发送；本次已报备的退租事件不再重复，之后新增或修正的退租仍会保留。")
    return redirect("police_report_detail", pk=report.pk)

def _agent_image_options(request):
    return {name: request.GET.get(name, "1") == "1" for name in ("include_commission", "include_password", "include_contact")}


def agent_export_view(request):
    response = export_agent_room_status(**_agent_image_options(request))
    if request.GET.get("preview") == "1":
        response["Content-Disposition"] = "inline"
    response["Cache-Control"] = "no-store"
    return response


def template_export_view(request):
    return export_import_template()


def _filter_bills(charges, category, q, timing, room_id):
    today = timezone.localdate()
    fixed = {Charge.Category.COMMISSION, Charge.Category.DEPOSIT_REFUND, Charge.Category.PROPERTY_RENT}
    if category == "regular": charges = [c for c in charges if c.category in {Charge.Category.RENT, Charge.Category.HEATING}]
    elif category == "fixed": charges = [c for c in charges if c.category in fixed]
    elif category in Charge.Category.values: charges = [c for c in charges if c.category == category]
    if room_id: charges = [c for c in charges if str(c.tenancy.room_id if c.tenancy else c.room_id) == room_id]
    if q: charges = [c for c in charges if q.casefold() in f"{c.room.number if c.room else ''} {c.person.name if c.person else ''} {c.description}".casefold()]
    if timing == "overdue": charges = [c for c in charges if c.due_date < today and c.balance > 0]
    elif timing == "due": charges = [c for c in charges if c.due_date <= today and c.balance > 0]
    elif timing == "future": charges = [c for c in charges if c.due_date > today and c.balance > 0]
    return charges


def renewal_view(request, pk):
    tenancy = get_object_or_404(Tenancy, pk=pk, status=Tenancy.Status.ACTIVE)
    form = RenewalForm(request.POST if request.method == "POST" else None, initial={"monthly_rent": tenancy.monthly_rent, "payment_cycle": tenancy.payment_cycle})
    if request.method == "POST" and form.is_valid():
        try:
            renewed = renew_tenancy(tenancy, **{key: form.cleaned_data[key] for key in ("end_date", "monthly_rent", "payment_cycle", "note")}, fee_terms=form.cleaned_fee_terms)
        except ValueError as exc:
            form.add_error(None, str(exc))
        else:
            messages.success(request, f"已办理续租，{renewed.start_date:%Y-%m-%d} 起执行新合同；押金和原欠款继续保留。")
            return _finish(request, "tenancy_list")
    return render(request, "core/form_page.html", {"form": form, "title": f"{tenancy.room.number} {tenancy.primary_person.name} 续租", "submit_label": "确认续租", "form_note": f"新合同从 {tenancy.end_date + timedelta(days=1):%Y-%m-%d} 开始。原合同保留，押金沿用，不重复收费。"})


def police_fingerprint(report_rows=None):
    rows = [(r["stay"].pk, r["stay"].person.name, r["stay"].person.id_number,
             r["stay"].room.number, r["stay"].person.phone, r["stay"].person.emergency_name,
             r["stay"].person.emergency_phone, r["text"], r["errors"], r["stay"].police_departure_token)
            for r in (police_report_rows() if report_rows is None else report_rows)]
    return hashlib.sha256(json.dumps(rows, default=str, ensure_ascii=False).encode("utf-8")).hexdigest()


def more_view(request):
    return render(request, "core/more.html")


def agent_preview(request):
    options = _agent_image_options(request)
    context = agent_room_status_data(**options)
    context.update(options)
    query = "&".join(f"{name}={int(value)}" for name, value in options.items())
    context["agent_image_url"] = f"{reverse('export_agent_rooms')}?{query}"
    return render(request, "core/agent_preview.html", context)


@transaction.atomic
def agent_settings_view(request):
    settings = ApartmentSettings.objects.filter(pk=1).first()
    defaults = dict(settings.fee_defaults) if settings else {}
    public_fees = defaults.get("agent_fees", {})
    initial = {f"agent_{name}": value for name, value in public_fees.items()}
    initial.setdefault("agent_parking_fee", "150")
    initial.setdefault("agent_parking_annual_fee", "1440")
    data = request.POST if request.method == "POST" else None
    form = AgentFeesForm(data, initial=initial)
    contact_form = AgentContactForm(data, initial=defaults.get("agent_contact", DEFAULT_AGENT_CONTACT), prefix="contact")
    PriceFormSet = modelformset_factory(Room, form=RoomListingPriceForm, extra=0, edit_only=True)
    prices = PriceFormSet(data, queryset=Room.objects.order_by("number"), prefix="prices")
    if request.method == "POST":
        fees_valid = form.is_valid()
        contact_valid = contact_form.is_valid()
        prices_valid = prices.is_valid()
        if fees_valid and contact_valid and prices_valid:
            defaults["agent_fees"] = {name.removeprefix("agent_"): str(value) for name, value in form.cleaned_data.items() if value not in (None, "")}
            defaults["agent_fees"].setdefault("parking_annual_fee", "")
            defaults["agent_contact"] = contact_form.cleaned_data
            ApartmentSettings.objects.update_or_create(pk=1, defaults={"fee_defaults": defaults})
            prices.save()
            messages.success(request, "已保存中介展示费用、联系方式、房间挂牌价和佣金基数。")
            return redirect("agent_preview")
    for price_form in prices:
        price_form.room_status = Room.Status(price_form.instance.refresh_status(save=False)).label
    return render(request, "core/agent_settings.html", {"form": form, "contact_form": contact_form, "prices": prices})


def person_lookup(request):
    q = request.GET.get("q", "").strip()
    if len(q) < 2: return JsonResponse({"people": []})
    people = Person.objects.filter(Q(name__icontains=q) | Q(phone__icontains=q) | Q(id_number__icontains=q)).prefetch_related(
        Prefetch("stays", queryset=Stay.objects.current().select_related("room"), to_attr="current_stays"))[:8]
    return JsonResponse({"people": [{"id": p.pk, "name": p.name, "phone": p.phone, "id_number": p.id_number or "", "emergency_name": p.emergency_name, "emergency_phone": p.emergency_phone, "emergency_address": p.emergency_address,
        "occupancy": "当前在住：" + "、".join(s.room.number for s in p.current_stays) if p.current_stays else "当前未在住"} for p in people]})


@transaction.atomic
def common_fees_view(request):
    settings = ApartmentSettings.objects.filter(pk=1).first()
    initial = dict(settings.fee_defaults) if settings else {}
    public_fees = initial.get("agent_fees", {})
    initial.update({"agent_electricity_fee": public_fees.get("electricity_fee", ""), "agent_heating_fee": public_fees.get("heating_fee")})
    form = CommonFeesForm(request.POST if request.method == "POST" else None, initial=initial)
    if request.method == "POST" and form.is_valid():
        values = {name: form.cleaned_data[name] for name in form.Meta.fields}
        defaults = {name: str(value) for name, value in values.items()}
        if "agent_contact" in initial:
            defaults["agent_contact"] = initial["agent_contact"]
        defaults["agent_fees"] = dict(public_fees)
        for name in ("electricity_fee", "heating_fee"):
            value = form.cleaned_data[f"agent_{name}"]
            defaults["agent_fees"].pop(name, None)
            if value not in (None, ""):
                defaults["agent_fees"][name] = str(value)
        ApartmentSettings.objects.update_or_create(pk=1, defaults={"fee_defaults": defaults})
        count = Room.objects.update(**values) if form.cleaned_data["apply_existing"] else 0
        messages.success(request, f"已保存新房间默认费用，并更新 {count} 间现有房间。已生成账单保持原金额。")
        return _finish(request, "more")
    return render(request, "core/form_page.html", {"form": form, "title": "统一房间费用", "submit_label": "保存费用标准", "form_note": "默认用于以后新增的房间；勾选后也应用到全部现有房间。"})


def contract_preview(request):
    form = SignContractForm(request.GET, room_queryset=Room.objects.all())
    form.is_valid()
    cd = form.cleaned_data
    if any(name not in cd for name in ["room", "start_date", "end_date", "monthly_rent", "deposit_amount", "payment_cycle"]) or cd["end_date"] < cd["start_date"]:
        return JsonResponse({"error": "请填写有效的租期、月租与押金。"}, status=400)
    months = {"monthly": 1, "quarterly": 3, "half_year": 6, "yearly": 12}[cd["payment_cycle"]]
    short = contract_months(cd["start_date"], cd["end_date"]) < 6
    rent = cd["monthly_rent"] * months if short else long_term_first_rent(cd["monthly_rent"], cd["start_date"])
    rent = max(money(rent - (cd.get("first_month_discount") or 0)), Decimal("0.00"))
    return JsonResponse({"rent": str(rent), "deposit": str(cd["deposit_amount"]), "subtotal": str(money(rent + cd["deposit_amount"])), "note": "房租和押金小计；取暖费按入住日期另计，保存后可查看完整账单。"})
