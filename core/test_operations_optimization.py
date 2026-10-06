from datetime import date
from decimal import Decimal
from unittest.mock import patch
from io import StringIO

from django.core.management import call_command
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from .maintenance import ensure_billing
from .models import Adjustment, Allocation, BillingCheckpoint, Charge, Payment, Person, Room, Stay, Tenancy
from .queries import charge_totals, room_finance_summaries
from .rent_collection import monthly_rent_rows
from .services import refresh_all_room_statuses, settle_charges
from .test_support import configure_test_account
from .views import _room_finance_summary


class OperationsOptimizationTests(TestCase):
    def setUp(self):
        configure_test_account(self)
        clock = patch("django.utils.timezone.localdate", return_value=date(2026, 10, 6))
        self.clock = clock.start()
        self.addCleanup(clock.stop)
        self.room = Room.objects.create(number="A01")
        self.person = Person.objects.create(name="测试住户", id_number="110101199001011234")
        self.lease = Tenancy.objects.create(room=self.room, primary_person=self.person,
            start_date=date(2026, 1, 1), end_date=date(2026, 12, 31), monthly_rent=3000, billing_enabled=False)

    def bill(self, **kwargs):
        values = dict(room=self.room, tenancy=self.lease, person=self.person, direction="income", category="rent",
            due_date=date(2026, 10, 1), period_start=date(2026, 10, 1), period_end=date(2026, 10, 31), amount=3000, description="10月房租")
        values.update(kwargs)
        return Charge.objects.create(**values)

    def test_database_totals_do_not_multiply_multiple_receipts_and_offsets(self):
        bill = self.bill()
        settle_charges([bill], amount=100)
        settle_charges([bill], amount=200)
        for amount in (30, 40):
            Adjustment.objects.create(charge=bill, tenancy=self.lease, adjustment_type="deposit_deduction", amount=amount)
        total = charge_totals(Charge.objects.all()).get()
        self.assertEqual(total.cash_allocated_amount, 300)
        self.assertEqual(total.deposit_offset, 70)
        self.assertEqual(total.balance, 2630)
        with self.assertNumQueries(0):
            self.assertEqual(total.balance, 2630)

    def test_room_summary_matches_existing_semantics_for_history_renewal_and_move(self):
        old = self.bill(amount=700)
        self.lease.status = "ended"
        self.lease.save()
        current = Tenancy.objects.create(room=self.room, primary_person=self.person, previous_tenancy=self.lease,
            start_date=date(2026, 10, 1), end_date=date(2027, 9, 30), monthly_rent=3200, billing_enabled=False)
        rent = self.bill(tenancy=current, amount=3200)
        settle_charges([rent], amount=200)
        Adjustment.objects.create(charge=old, tenancy=self.lease, adjustment_type="deposit_deduction", amount=100)
        unrelated = Tenancy.objects.create(room=self.room, primary_person=Person.objects.create(name="前住户"),
            start_date=date(2025, 1, 1), end_date=date(2025, 12, 31), monthly_rent=2000, billing_enabled=False, status="ended")
        self.bill(tenancy=unrelated, amount=500)
        self.bill(tenancy=current, amount=800, due_date=date(2026, 11, 1))
        self.bill(tenancy=current, amount=1000, status="void")
        self.bill(tenancy=None, person=None, direction="expense", amount=150)
        self.bill(tenancy=None, person=None, category="repair", amount=50)
        Payment.objects.create(room=self.room, tenancy=current, direction="receive", amount=250)
        other = Room.objects.create(number="A02")
        self.bill(room=other, tenancy=current, amount=80)  # Contract's current room determines debt ownership.
        expected = _room_finance_summary(Room.objects.get(pk=self.room.pk))
        self.room.current_tenancies = [current]
        room_finance_summaries([self.room], date(2026, 10, 6))
        self.assertEqual(self.room.finance_summary, expected)
        self.assertEqual(expected["historical_due"], 500)
        self.assertEqual(expected["current_due"], 3680)
        self.assertEqual(expected["prepaid"], 250)

    def test_bulk_status_matches_individual_refresh_and_has_constant_queries(self):
        empty = Room.objects.create(number="A02")
        occupied = Room.objects.create(number="A03", status="occupied")
        repair = Room.objects.create(number="A04", status="maintenance")
        visitor = Person.objects.create(name="暂住")
        Stay.objects.create(room=empty, person=visitor, stay_type="visitor", start_date=None, is_active=True)
        expected = {room.pk: room.refresh_status(save=False) for room in Room.objects.all()}
        refresh_all_room_statuses()
        self.assertEqual(dict(Room.objects.values_list("pk", "status")), expected)
        with CaptureQueriesContext(connection) as first:
            refresh_all_room_statuses()
        Room.objects.bulk_create([Room(number=f"B{i:02}") for i in range(40)])
        with CaptureQueriesContext(connection) as larger:
            refresh_all_room_statuses()
        self.assertEqual(len(first), len(larger))

    def test_checkpoint_skips_repeat_and_rechecks_changes_and_missing_periods(self):
        self.lease.billing_enabled = True
        self.lease.billing_start_date = date(2026, 10, 1)
        self.lease.save()
        through = date(2026, 10, 31)
        ensure_billing(through)
        with self.assertNumQueries(1):
            self.assertEqual(ensure_billing(through), [])
        rent = Charge.objects.get(category="rent")
        rent.amount = 2800
        rent.save()
        self.assertIsNone(BillingCheckpoint.objects.get(pk=1).through_date)
        ensure_billing(through)
        rent.refresh_from_db()
        self.assertEqual(rent.amount, 2800)
        rent.status = "void"
        rent.save()
        ensure_billing(through)
        self.assertEqual(Charge.objects.filter(category="rent").count(), 1)
        rent.delete()
        ensure_billing(through)
        self.assertEqual(Charge.objects.get(category="rent").amount, 3000)

    def test_checkpoint_failure_rolls_back_and_next_call_retries(self):
        with patch("core.services.generate_due_charges", side_effect=ValueError("failed")):
            with self.assertRaises(ValueError):
                ensure_billing(date(2026, 10, 31))
        self.assertFalse(BillingCheckpoint.objects.exists())
        ensure_billing(date(2026, 10, 31))
        self.assertEqual(BillingCheckpoint.objects.get().through_date, date(2026, 10, 31))

    def test_cross_day_checkpoint_activates_renewal_without_losing_residents(self):
        self.lease.end_date = date(2026, 10, 31)
        self.lease.save()
        Stay.objects.create(room=self.room, person=self.person, tenancy=self.lease, start_date=date(2026, 1, 1))
        upcoming = Tenancy.objects.create(room=self.room, primary_person=self.person, previous_tenancy=self.lease,
            start_date=date(2026, 11, 1), end_date=date(2027, 10, 31), monthly_rent=3300, billing_enabled=False, status="upcoming")
        ensure_billing(date(2026, 11, 30))
        self.clock.return_value = date(2026, 11, 1)
        ensure_billing(date(2026, 11, 30))
        upcoming.refresh_from_db()
        self.lease.refresh_from_db()
        self.assertEqual(upcoming.status, "active")
        self.assertEqual(self.lease.status, "ended")
        self.assertEqual(Stay.objects.current().get().tenancy_id, upcoming.pk)

    def test_same_day_receipt_invalidates_checkpoint_and_is_allocated(self):
        bill = self.bill()
        ensure_billing(date(2026, 10, 31))
        payment = Payment.objects.create(room=self.room, tenancy=self.lease, direction="receive", amount=123)
        ensure_billing(date(2026, 10, 31))
        self.assertEqual(payment.allocations.get().amount, 123)
        self.assertEqual(bill.balance, 2877)

    def test_ledger_pagination_preserves_totals_and_explicit_all_months(self):
        Payment.objects.bulk_create([Payment(room=self.room, direction="receive", amount=Decimal("1.01"), date=date(2026, 10, 6), auto_allocate=False) for _ in range(60)])
        Payment.objects.create(direction="pay", amount=7, date=date(2026, 10, 6), auto_allocate=False)
        Payment.objects.create(direction="receive", amount=100, date=date(2026, 9, 1), auto_allocate=False)
        first = self.client.get("/charges/").context
        second = self.client.get("/charges/", {"page": 2}).context
        self.assertEqual(len(first["ledger_rows"]), 50)
        self.assertEqual(len(second["ledger_rows"]), 11)
        self.assertEqual(first["income_total"], Decimal("60.60"))
        self.assertEqual(second["income_total"], first["income_total"])
        self.assertEqual(first["expense_total"], 7)
        ids = {r["obj"].pk for r in first["ledger_rows"]}
        self.assertFalse(ids & {r["obj"].pk for r in second["ledger_rows"]})
        self.assertEqual(self.client.get("/charges/", {"month": ""}).context["income_total"], Decimal("160.60"))

    def test_mixed_ledger_keeps_bill_and_payment_with_same_id(self):
        bill = self.bill()
        payment = Payment.objects.create(pk=bill.pk, direction="receive", amount=100, date=bill.due_date)
        response = self.client.get("/charges/", {"status": "all", "month": ""})
        self.assertEqual({(r["kind"], r["obj"].pk) for r in response.context["ledger_rows"]}, {("charge", bill.pk), ("payment", payment.pk)})

    def test_collection_separates_confirmed_debt_and_drafts_and_filters(self):
        rent = self.bill()
        settle_charges([rent], amount=1000)
        other_room = Room.objects.create(number="A02")
        lease = Tenancy.objects.create(room=other_room, primary_person=Person.objects.create(name="待登记住户"),
            start_date=date(2026, 1, 1), end_date=date(2026, 12, 31), monthly_rent=3500, billing_enabled=False)
        self.bill(room=other_room, tenancy=lease, person=lease.primary_person, category="heating", amount=380)
        query = {"month": "2026-10", "include_heating": "1"}
        context = self.client.get("/bills/monthly-rent/", query).context
        self.assertEqual(context["booked_balance"], 2380)
        self.assertEqual(context["draft_balance"], 3500)
        self.assertEqual(context["total_balance"], 5880)
        rows = self.client.get("/bills/monthly-rent/", query | {"collection_scope": "draft"}).context["rent_rows"]
        self.assertEqual([r["tenancy"].pk for r in rows], [lease.pk])
        self.assertFalse(self.client.get("/bills/monthly-rent/", query | {"collection_scope": "paid"}).context["rent_rows"])

    def test_continue_person_keeps_context_but_not_identity_or_unknown_dates(self):
        response = self.client.post("/people/new/", dict(name="探望人员", id_number="110101199001011235", phone="13800000000",
            room=self.room.pk, stay_type="visitor", action="continue", next="/people/?scope=active"))
        self.assertEqual(response.status_code, 302)
        form = self.client.get(response.url).context["form"]
        self.assertEqual(str(form.initial["room"]), str(self.room.pk))
        self.assertEqual(form.initial["stay_type"], "visitor")
        self.assertIsNone(form.initial["start_date"])
        self.assertFalse(form["name"].value())
        self.assertFalse(form["id_number"].value())
        self.assertFalse(form["phone"].value())
        lookup = self.client.get("/people/lookup/", {"q": "探望人员"}).json()["people"][0]
        self.assertEqual(lookup["occupancy"], "当前在住：A01")

    def test_person_filter_does_not_show_previous_room_for_current_resident(self):
        other = Room.objects.create(number="A02")
        Stay.objects.create(person=self.person, room=self.room, start_date=date(2026, 1, 1), is_active=False)
        Stay.objects.create(person=self.person, room=other, start_date=date(2026, 10, 1))
        self.assertFalse(self.client.get("/people/", {"room": self.room.pk}).context["person_rows"])
        self.assertEqual(len(self.client.get("/people/", {"room": other.pk}).context["person_rows"]), 1)

    def test_monthly_queries_do_not_grow_per_bill(self):
        self.bill()
        with CaptureQueriesContext(connection) as small:
            monthly_rent_rows(date(2026, 10, 1))
        for number in range(20):
            self.bill(category="heating", amount=380)
        with CaptureQueriesContext(connection) as larger:
            monthly_rent_rows(date(2026, 10, 1), include_heating=True)
        self.assertEqual(len(small), len(larger))
        self.assertLessEqual(len(larger), 8)

    def test_collection_save_preserves_filter(self):
        self.bill()
        response = self.client.post("/bills/monthly-rent/", dict(month="2026-10", collection_scope="unpaid",
            date="2026-10-06", action="single", rows=[f"tenancy-{self.lease.pk}"], amount="100"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("collection_scope=unpaid", response.url)

    def test_forced_command_recovers_bulk_writes_and_interrupts_cleanly(self):
        bill = self.bill()
        ensure_billing(date(2026, 10, 31))
        Payment.objects.bulk_create([Payment(room=self.room, tenancy=self.lease, direction="receive", amount=100)])
        ensure_billing(date(2026, 10, 31))
        self.assertEqual(bill.balance, 3000)
        call_command("refresh_operations", through=date(2026, 10, 31), force=True, stdout=StringIO())
        self.assertEqual(bill.balance, 2900)
        with patch("core.management.commands.refresh_operations.ensure_billing", side_effect=KeyboardInterrupt):
            with self.assertRaises(SystemExit) as raised:
                call_command("refresh_operations", stdout=StringIO(), stderr=StringIO())
        self.assertEqual(raised.exception.code, 130)
