from datetime import date
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse
from .test_support import configure_test_account

from .exports import police_report_workbook
from .models import ApartmentSettings, Charge, Payment, Person, Room, Stay, Tenancy
from .services import (
    activate_renewals, allocate_unallocated_payments, generate_due_charges,
    generate_rent_charges_until, move_tenancy, record_payment,
    refresh_all_room_statuses, renew_tenancy, settle_charges, sign_contract,
    tenancy_finances,
)
from .views import _room_finance_summary


class DailyOperationsTests(TestCase):
    def setUp(self):
        configure_test_account(self)
        self.clock = patch("django.utils.timezone.localdate", return_value=date(2026, 6, 20))
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.room = Room.objects.create(number="A01", listing_price=3000)
        self.person_data = {"name": "测试租客", "id_number": "110101199001011234", "phone": "13800000000"}

    def lease(self, **kwargs):
        values = dict(room=self.room, person_data=self.person_data, start_date=date(2026, 1, 1), end_date=date(2026, 12, 31), monthly_rent=Decimal("3000"))
        values.update(kwargs)
        return sign_contract(**values)

    def charge(self, **kwargs):
        values = dict(room=self.room, direction=Charge.Direction.INCOME, category=Charge.Category.RENT, due_date=date(2026, 6, 1), amount=Decimal("100"), description="测试费用")
        values.update(kwargs)
        return Charge.objects.create(**values)

    def test_expired_contract_is_not_vacant_and_remains_in_report(self):
        tenancy = self.lease(end_date=date(2026, 6, 19))
        refresh_all_room_statuses()
        self.room.refresh_from_db()
        self.assertEqual(self.room.status, Room.Status.EXPIRING)
        self.assertEqual(self.room.active_tenancy(), tenancy)
        self.assertEqual(police_report_workbook().active.max_row, 3)
        response = self.client.get("/rooms/")
        self.assertContains(response, "合同已到期")
        self.assertContains(response, reverse("renew_tenancy", args=[tenancy.pk]))

    def test_maintenance_and_actual_residents_are_not_overwritten_as_vacant(self):
        self.room.status = Room.Status.MAINTENANCE
        self.room.save()
        self.room.refresh_status()
        self.assertEqual(self.room.status, Room.Status.MAINTENANCE)
        self.room.status = Room.Status.VACANT
        self.room.save()
        person = Person.objects.create(**self.person_data)
        Stay.objects.create(room=self.room, person=person, start_date=date(2026, 6, 1), end_date=date(2026, 6, 2), stay_type=Stay.Type.VISITOR)
        self.room.refresh_status()
        self.assertEqual(self.room.status, Room.Status.OCCUPIED)

    def test_generating_future_bills_does_not_advance_room_date(self):
        self.lease(end_date=date(2026, 9, 30))
        generate_due_charges(date(2026, 12, 31))
        self.room.refresh_from_db()
        self.assertEqual(self.room.status, Room.Status.OCCUPIED)

    def test_public_income_and_expense_never_settle_other_rooms(self):
        income = self.charge()
        expense = self.charge(direction=Charge.Direction.EXPENSE)
        for direction in [Payment.Direction.RECEIVE, Payment.Direction.PAY]:
            payment = record_payment(direction=direction, date=date(2026, 6, 20), amount=100)
            self.assertFalse(payment.allocations.exists())
        allocate_unallocated_payments()
        self.assertEqual(income.balance, 100)
        self.assertEqual(expense.balance, 100)

    def test_manual_expense_remains_independent_after_refresh(self):
        payable = self.charge(direction=Charge.Direction.EXPENSE, category=Charge.Category.COMMISSION)
        response = self.client.post("/payments/new/", {"direction": "pay", "category": "other", "room": self.room.pk, "date": "2026-06-20", "amount": "40", "memo": "修门"})
        self.assertEqual(response.status_code, 302)
        allocate_unallocated_payments()
        self.assertEqual(payable.balance, 100)
        self.assertFalse(Payment.objects.get().auto_allocate)

    def test_future_receivables_and_outgoing_payments_are_separate(self):
        self.charge(amount=100)
        self.charge(amount=200, due_date=date(2026, 7, 1))
        self.charge(direction=Charge.Direction.EXPENSE, amount=50)
        summary = _room_finance_summary(self.room)
        self.assertEqual(summary["tenant_due"], 100)
        self.assertEqual(summary["future_due"], 200)
        self.assertEqual(summary["expense_due"], 50)
        response = self.client.get(f"/rooms/{self.room.pk}/")
        self.assertContains(response, "到期未收")
        self.assertContains(response, "未来应收")

    def test_all_receivables_include_deposit_and_keep_rent_settlement_separate(self):
        tenancy = self.lease(start_date=date(2026, 6, 1))
        rent = tenancy.charges.get(category=Charge.Category.RENT)
        settle_charges([rent])
        response = self.client.get("/bills/?month=2026-06")
        self.assertEqual(len(response.context["bill_rows"]), 2)
        response = self.client.get("/bills/?month=2026-06&status=paid")
        self.assertContains(response, "A01")
        self.assertContains(response, "bill-row-paid")
        response = self.client.get("/bills/?month=2026-06&category=deposit")
        self.assertEqual(len(response.context["bill_rows"]), 1)
        self.assertEqual(response.context["bill_rows"][0]["balance"], 3500)

    def test_all_payables_include_manual_repair_and_can_be_settled(self):
        repair = self.charge(direction=Charge.Direction.EXPENSE, category=Charge.Category.REPAIR, description="修门")
        response = self.client.get("/bills/?direction=expense")
        self.assertContains(response, "修门")
        self.client.post("/bills/settle/", {"charge_ids": [repair.pk]})
        self.assertEqual(repair.balance, 0)

    def test_payment_preview_matches_actual_allocation_without_writing(self):
        tenancy = self.lease()
        tenancy.charges.all().delete()
        rent = self.charge(tenancy=tenancy, amount=7000)
        heating = self.charge(tenancy=tenancy, category=Charge.Category.HEATING, amount=720)
        data = {"room": self.room.pk, "amount": "5000", "category": "other", "direction": "receive", "date": "2026-06-20"}
        response = self.client.get("/payments/preview/", data)
        self.assertEqual(response.status_code, 200)
        self.assertIn("5000", response.json()["items"][0])
        self.assertFalse(Payment.objects.exists())
        self.client.post("/payments/new/", data)
        self.assertEqual(rent.balance, 2000)
        self.assertEqual(heating.balance, 720)

    def test_payment_edit_keeps_original_bills_and_rolls_back_invalid_amount(self):
        tenancy = self.lease()
        tenancy.charges.all().delete()
        charge = self.charge(tenancy=tenancy, amount=100)
        payment = settle_charges([charge], amount=80)
        unrelated = self.charge(tenancy=tenancy, amount=200)
        self.client.post(f"/payments/{payment.pk}/edit/", {"amount": "40", "date": "2026-06-20", "memo": "改正"})
        self.assertEqual(charge.balance, 60)
        self.assertEqual(unrelated.balance, 200)
        self.client.post(f"/payments/{payment.pk}/edit/", {"amount": "999", "date": "2026-06-20"})
        payment.refresh_from_db()
        self.assertEqual(payment.amount, 40)
        self.assertEqual(charge.balance, 60)

    def test_manual_bill_adjustment_and_discount_survive_regeneration(self):
        tenancy = self.lease(first_month_discount=200)
        rent = tenancy.charges.get(category=Charge.Category.RENT)
        self.assertEqual(rent.amount, 2800)
        generate_rent_charges_until(tenancy, date(2026, 6, 20))
        rent.refresh_from_db()
        self.assertEqual(rent.amount, 2800)
        self.client.post(f"/charges/{rent.pk}/edit/", {"direction": "income", "category": "rent", "room": self.room.pk, "due_date": "2026-01-01", "amount": "2500", "description": "协商优惠", "status": "open"})
        generate_due_charges(date(2026, 6, 20))
        rent.refresh_from_db()
        self.assertEqual(rent.amount, 2500)

    def test_receipt_has_safe_return_to_original_filter(self):
        data = {"direction": "receive", "category": "other", "date": "2026-06-20", "amount": "50", "next": "/charges/?q=A01"}
        self.assertRedirects(self.client.post("/payments/new/", data), "/charges/?q=A01", fetch_redirect_response=False)
        data["next"] = "https://example.com/"
        self.assertRedirects(self.client.post("/payments/new/", data), "/charges/", fetch_redirect_response=False)
        response = self.client.get("/payments/new/?next=/people/?scope=active")
        self.assertContains(response, 'name="next" value="/people/?scope=active"')

    def test_current_people_search_and_departure_keep_history(self):
        tenancy = self.lease()
        departed = Person.objects.create(name="已离开", id_number="110101199002022222")
        Stay.objects.create(room=self.room, person=departed, start_date=date(2026, 1, 1), is_active=False)
        response = self.client.get("/people/")
        self.assertContains(response, "测试租客")
        self.assertNotIn(departed.pk, [row["person"].pk for row in response.context["person_rows"]])
        response = self.client.get("/people/?q=找不到")
        self.assertEqual(len(response.context["person_rows"]), 0)
        stay = tenancy.stays.get()
        self.client.post(f"/people/{stay.person_id}/stays/{stay.pk}/remove/", {"next": "/people/"})
        stay.refresh_from_db()
        self.assertEqual(stay.end_date, date(2026, 6, 20))
        self.assertFalse(stay.is_active)
        self.assertTrue(Person.objects.filter(pk=stay.person_id).exists())

    def test_report_download_checkpoint_changes_after_person_edit(self):
        tenancy = self.lease()
        self.assertTrue(self.client.get("/people/").context["report_changed"])
        self.assertEqual(self.client.get("/exports/police.xlsx").status_code, 200)
        self.assertFalse(self.client.get("/people/").context["report_changed"])
        tenancy.primary_person.phone = "13900000000"
        tenancy.primary_person.save()
        self.assertTrue(self.client.get("/people/").context["report_changed"])

    def test_renewal_is_scheduled_without_duplicate_deposit_or_immediate_rent_change(self):
        tenancy = self.lease(end_date=date(2026, 6, 30))
        deposit = tenancy.charges.get(category=Charge.Category.DEPOSIT)
        settle_charges([deposit])
        renewed = renew_tenancy(tenancy, end_date=date(2027, 6, 30), monthly_rent=3500, payment_cycle="monthly")
        self.assertEqual(renewed.status, Tenancy.Status.UPCOMING)
        self.assertEqual(self.room.active_tenancy(), tenancy)
        self.assertFalse(renewed.charges.filter(category=Charge.Category.DEPOSIT).exists())
        activate_renewals(date(2026, 7, 1))
        renewed.refresh_from_db()
        tenancy.refresh_from_db()
        self.assertEqual(renewed.status, Tenancy.Status.ACTIVE)
        self.assertEqual(tenancy.status, Tenancy.Status.ENDED)
        self.assertIsNone(tenancy.move_out_date)
        self.assertEqual(renewed.stays.filter(is_active=True).count(), 1)
        self.assertEqual(tenancy_finances(renewed)["deposit_held"], 3500)

    def test_renewal_carries_old_debt_and_prepaid_without_crossing_to_new_tenant(self):
        tenancy = self.lease(end_date=date(2026, 6, 19))
        tenancy.charges.all().delete()
        old = self.charge(tenancy=tenancy, amount=100)
        renewed = renew_tenancy(tenancy, end_date=date(2027, 6, 19), monthly_rent=3000, payment_cycle="monthly")
        renewed.charges.all().delete()
        current = self.charge(tenancy=renewed, amount=200)
        other = Person.objects.create(name="其他人", id_number="110101199003033333")
        different = Tenancy.objects.create(room=self.room, primary_person=other, start_date=date(2025, 1, 1), end_date=date(2025, 12, 31), monthly_rent=1000, status="ended")
        unrelated = self.charge(tenancy=different, amount=400)
        record_payment(direction="receive", date=date(2026, 6, 20), amount=350, tenancy=renewed)
        self.assertEqual(old.balance, 0)
        self.assertEqual(current.balance, 0)
        self.assertEqual(unrelated.balance, 400)
        self.assertEqual(tenancy_finances(renewed)["prepaid"], 50)

    def test_renewal_cannot_be_submitted_twice(self):
        tenancy = self.lease(end_date=date(2026, 6, 30))
        data = {"end_date": "2027-06-30", "monthly_rent": "3200", "payment_cycle": "monthly"}
        self.assertEqual(self.client.post(f"/tenancies/{tenancy.pk}/renew/", data).status_code, 302)
        response = self.client.post(f"/tenancies/{tenancy.pk}/renew/", data)
        self.assertContains(response, "已经办理续租")
        self.assertEqual(Tenancy.objects.filter(previous_tenancy=tenancy).count(), 1)

    def test_checkout_defaults_to_received_deposit_and_does_not_record_refund_as_paid(self):
        tenancy = self.lease()
        deposit = tenancy.charges.get(category=Charge.Category.DEPOSIT)
        settle_charges([deposit], amount=2000)
        response = self.client.get(f"/tenancies/{tenancy.pk}/checkout/")
        self.assertEqual(response.context["actual_form"].initial["refund_deposit_amount"], 2000)
        response = self.client.post(f"/tenancies/{tenancy.pk}/checkout/", {"action": "actual", "checkout_date": "2026-06-20", "refund_deposit_amount": "1800"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(tenancy.charges.get(category=Charge.Category.DEPOSIT_REFUND).balance, 1800)
        self.assertFalse(Payment.objects.filter(direction="pay").exists())

    def test_lower_rent_move_creates_positive_payable_not_negative_receivable(self):
        tenancy = self.lease()
        new_room = Room.objects.create(number="A02")
        move_tenancy(tenancy, new_room=new_room, move_date=date(2026, 6, 20), new_monthly_rent=2000)
        diff = tenancy.charges.get(category=Charge.Category.MOVE_DIFF)
        self.assertEqual(diff.direction, Charge.Direction.EXPENSE)
        self.assertGreater(diff.amount, 0)

    def test_common_fees_only_change_existing_rooms_when_selected(self):
        data = {"water_fee": "10/吨", "electricity_fee": "1.5/度", "property_fee": "免", "internet_fee": "免", "heating_fee": "400", "parking_fee": "160"}
        self.client.post("/more/fees/", data)
        self.room.refresh_from_db()
        self.assertEqual(self.room.heating_fee, 380)
        self.assertEqual(ApartmentSettings.objects.get(pk=1).fee_defaults["heating_fee"], "400")
        self.assertEqual(self.client.get("/rooms/new/").context["form"].initial["heating_fee"], "400")
        data["apply_existing"] = "on"
        self.client.post("/more/fees/", data)
        self.room.refresh_from_db()
        self.assertEqual(self.room.heating_fee, 400)

    def test_all_main_pages_render_with_realistic_data(self):
        tenancy = self.lease()
        paths = ["/", "/rooms/", f"/rooms/{self.room.pk}/", "/people/", "/tenancies/", "/bills/", "/bills/?direction=expense", "/charges/", "/more/", "/more/fees/", "/exports/agent-preview/", "/payments/new/", f"/tenancies/{tenancy.pk}/renew/"]
        for path in paths:
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 200)

    def test_contract_preview_has_no_database_side_effects(self):
        response = self.client.get("/tenancies/preview/", {"room": self.room.pk, "start_date": "2026-06-10", "end_date": "2027-06-09", "monthly_rent": "3000", "deposit_amount": "3500", "payment_cycle": "monthly", "first_month_discount": "200"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["rent"], "1800.00")
        self.assertFalse(Tenancy.objects.exists())
        self.assertFalse(Charge.objects.exists())

    def test_browser_date_defaults_use_iso_format(self):
        response = self.client.get("/payments/new/")
        self.assertContains(response, 'value="2026-06-20"')
        self.assertNotContains(response, 'value="2026年6月20日"')

    def test_signing_with_roommate_and_receipt_is_one_operation(self):
        data = {
            "room": self.room.pk, "person_name": self.person_data["name"],
            "id_number": self.person_data["id_number"], "phone": self.person_data["phone"],
            "start_date": "2026-06-20", "end_date": "2027-06-19",
            "monthly_rent": "3000", "deposit_amount": "3500", "payment_cycle": "monthly",
            "received_amount": "4500", "roommates-TOTAL_FORMS": "1", "roommates-INITIAL_FORMS": "0",
            "roommates-0-name": "同住人员", "roommates-0-id_number": "110101199005055555",
        }
        response = self.client.post(f"/rooms/{self.room.pk}/contract/new/", data)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.room.stays.count(), 2)
        self.assertEqual(Payment.objects.get().amount, 4500)
        self.assertEqual(self.room.tenancies.count(), 1)

    def test_moved_tenants_debt_follows_current_room(self):
        tenancy = self.lease()
        tenancy.charges.all().delete()
        self.charge(tenancy=tenancy, amount=700)
        destination = Room.objects.create(number="B01")
        move_tenancy(tenancy, new_room=destination, move_date=date(2026, 6, 20), new_monthly_rent=3000)
        self.assertEqual(_room_finance_summary(self.room)["tenant_due"], 0)
        self.assertGreaterEqual(_room_finance_summary(destination)["tenant_due"], 700)

    def test_cannot_adjust_bill_below_received_amount(self):
        charge = self.charge(amount=100)
        settle_charges([charge], amount=80)
        response = self.client.post(f"/charges/{charge.pk}/edit/", {"direction": "income", "category": "rent", "room": self.room.pk, "due_date": "2026-06-01", "amount": "50", "description": "调整", "status": "open"})
        self.assertContains(response, "不能小于已收付金额")
        charge.refresh_from_db()
        self.assertEqual(charge.amount, 100)

    def test_removed_generated_bill_does_not_reappear(self):
        tenancy = self.lease()
        rent = tenancy.charges.get(category=Charge.Category.RENT)
        self.client.post(f"/charges/{rent.pk}/delete/")
        generate_due_charges(date(2026, 6, 20))
        rent.refresh_from_db()
        self.assertEqual(rent.status, Charge.Status.VOID)
