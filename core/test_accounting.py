from datetime import date
from decimal import Decimal
from uuid import uuid4
from unittest.mock import patch

from django.test import TestCase

from .models import Adjustment, Charge, Payment, Person, Room, Tenancy
from .services import checkout_tenancy, generate_heating_charges_until, generate_rent_charges_until, renew_tenancy, settle_charges, tenancy_finances
from .test_support import configure_test_account


class AccountingWorkflowTests(TestCase):
    def setUp(self):
        configure_test_account(self)
        clock = patch("django.utils.timezone.localdate", return_value=date(2026, 10, 5))
        clock.start()
        self.addCleanup(clock.stop)
        self.room = Room.objects.create(number="A01", heating_fee=380)
        self.person = Person.objects.create(name="原租客")
        self.lease = Tenancy.objects.create(room=self.room, primary_person=self.person, start_date=date(2026, 1, 1), end_date=date(2026, 12, 31), monthly_rent=3000, billing_enabled=False, deposit_amount=None)
        self.url = "/bills/entry/new/"

    def bill(self, **kwargs):
        values = dict(tenancy=self.lease, room=self.room, person=self.person, category="rent", direction="income", amount=3000, due_date=date(2026, 9, 30), period_start=date(2026, 10, 1), period_end=date(2026, 10, 31), description="10月房租", source="manual")
        values.update(kwargs)
        return Charge.objects.create(**values)

    def entry(self, **kwargs):
        data = dict(token=str(uuid4()), direction="expense", category="repair", state="paid", amount="300", date="2026-10-05", description="修水管")
        data.update(kwargs)
        return data

    def deposit(self):
        charge = self.bill(category="deposit", amount=3500, description="押金")
        return settle_charges([charge], date=date(2026, 1, 1))

    def test_paid_room_expense_keeps_category_and_does_not_charge_tenant(self):
        rent = self.bill()
        data = self.entry(room=self.room.pk)
        self.assertEqual(self.client.post(self.url, data).status_code, 302)
        payment = Payment.objects.get()
        self.assertIsNone(payment.tenancy_id)
        self.assertEqual(payment.allocations.get().charge.category, "repair")
        self.assertEqual(rent.balance, 3000)
        self.assertContains(self.client.get("/charges/"), "维修")
        self.client.post(self.url, data)
        self.assertEqual(Payment.objects.count(), 1)

    def test_partial_entry_then_settle_same_bill_and_replay(self):
        data = self.entry(state="partial", paid_amount="100", due_date="2026-10-01")
        self.assertEqual(self.client.post(self.url, data).status_code, 302)
        bill = Charge.objects.get()
        self.assertEqual(bill.balance, 200)
        self.assertEqual(bill.due_date, date(2026, 10, 1))
        data = self.entry(charge=bill.pk, amount="50")
        self.assertEqual(self.client.post(self.url, data).status_code, 302)
        self.client.post(self.url, data)
        self.assertEqual(bill.balance, 150)
        self.assertEqual(Charge.objects.count(), 1)
        self.assertEqual(Payment.objects.count(), 2)
        self.client.post(self.url, self.entry(charge=bill.pk, amount="150"))
        bill.refresh_from_db()
        self.assertEqual(bill.status, "paid")

    def test_pending_entry_records_no_payment_and_uses_due_date(self):
        data = self.entry(state="pending", due_date="2026-09-01")
        self.client.post(self.url, data)
        self.client.post(self.url, data)
        self.assertEqual(Charge.objects.count(), 1)
        self.assertEqual(Charge.objects.get().due_date, date(2026, 10, 5))
        self.assertFalse(Payment.objects.exists())

    def test_overpayment_and_partial_errors_do_not_write(self):
        response = self.client.post(self.url, self.entry(state="partial", paid_amount="400"))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["form"].errors)
        self.assertFalse(Charge.objects.exists())
        bill = self.bill(amount=50)
        response = self.client.post(self.url, self.entry(charge=bill.pk, amount=60))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Payment.objects.exists())

    def test_previous_tenant_bill_is_settled_without_touching_new_tenant(self):
        old = self.bill(amount=500)
        self.lease.status = "ended"
        self.lease.save()
        new = Tenancy.objects.create(room=self.room, primary_person=Person.objects.create(name="新租客"), start_date=date(2026, 10, 1), end_date=date(2027, 9, 30), monthly_rent=3500, billing_enabled=False)
        current = self.bill(tenancy=new, person=new.primary_person, amount=3500)
        self.client.post(self.url, self.entry(charge=old.pk, amount=500))
        self.assertEqual(old.balance, 0)
        self.assertEqual(current.balance, 3500)
        self.assertEqual(Payment.objects.get().tenancy_id, self.lease.pk)

    def test_previous_paid_contract_does_not_make_new_contract_look_initialized(self):
        from .views import _room_finance_summary
        settle_charges([self.bill()])
        self.lease.status = "ended"
        self.lease.save()
        Tenancy.objects.create(room=self.room, primary_person=Person.objects.create(name="下一位租客"), start_date=date(2026, 10, 1), end_date=date(2027, 9, 30), monthly_rent=3500, billing_enabled=False, deposit_amount=None)
        self.assertFalse(_room_finance_summary(self.room)["has_receivables"])
        self.assertContains(self.client.get("/rooms/"), "当前合同账务待登记")

    def test_tenant_fees_require_contract_and_mismatched_room_is_rejected(self):
        response = self.client.post(self.url, self.entry(direction="income", category="deposit", room=self.room.pk))
        self.assertIn("tenancy", response.context["form"].errors)
        other = Room.objects.create(number="B01")
        response = self.client.post(self.url, self.entry(room=other.pk, tenancy=self.lease.pk))
        self.assertIn("tenancy", response.context["form"].errors)
        self.assertFalse(Charge.objects.exists())

    def test_manual_rent_is_not_generated_twice(self):
        self.lease.billing_enabled = True
        self.lease.billing_start_date = date(2026, 10, 1)
        self.lease.save()
        data = self.entry(direction="income", category="rent", room=self.room.pk, tenancy=self.lease.pk, amount=2800, period_start="2026-10-01", period_end="2026-10-31")
        self.assertEqual(self.client.post(self.url, data).status_code, 302)
        generate_rent_charges_until(self.lease, date(2026, 10, 31))
        self.assertEqual(Charge.objects.filter(category="rent").count(), 1)
        self.assertEqual(Charge.objects.get().amount, 2800)

    def test_duplicate_deposit_is_rejected_even_on_renewal(self):
        self.deposit()
        new = renew_tenancy(self.lease, end_date=date(2027, 12, 31), monthly_rent=3200, payment_cycle="monthly")
        response = self.client.post(self.url, self.entry(direction="income", category="deposit", tenancy=new.pk, amount=3500))
        self.assertContains(response, "已有押金账单")
        self.assertEqual(Charge.objects.filter(category="deposit").count(), 1)

    def test_room_fee_changes_do_not_change_old_contract_and_renewal_copies_terms(self):
        self.room.heating_fee = 400
        self.room.save()
        self.lease.refresh_from_db()
        self.assertEqual(self.lease.heating_amount, 380)
        renewed = renew_tenancy(self.lease, end_date=date(2027, 12, 31), monthly_rent=3200, payment_cycle="monthly", fee_terms={"heating_fee": "420"})
        self.assertEqual(renewed.heating_amount, 420)
        self.assertEqual(self.lease.heating_amount, 380)
        self.lease.billing_enabled = True
        self.lease.save()
        charges = generate_heating_charges_until(self.lease, date(2026, 11, 30))
        self.assertEqual(charges[-1].amount, 380)

    def test_checkout_deduction_offset_and_refund_do_not_invent_cash_receipts(self):
        receipt = self.deposit()
        rent = self.bill(amount=1000)
        response = self.client.post(f"/tenancies/{self.lease.pk}/checkout/", dict(action="actual", checkout_date="2026-10-05", deposit_deduction_amount="500", deposit_offset_amount="1000", refund_deposit_amount="", refund_paid="on", note="茶几损坏"))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(rent.balance, 0)
        self.assertEqual(rent.deposit_offset, 1000)
        self.assertEqual(tenancy_finances(self.lease)["deposit_held"], 0)
        self.assertEqual(Payment.objects.filter(direction="receive").count(), 1)
        self.assertEqual(Payment.objects.get(direction="pay").amount, 2000)
        self.assertEqual(Adjustment.objects.count(), 2)
        self.client.post(f"/payments/{receipt.pk}/delete/")
        self.assertTrue(Payment.objects.filter(pk=receipt.pk).exists())
        self.client.post(f"/charges/{rent.pk}/delete/")
        self.assertTrue(Charge.objects.filter(pk=rent.pk).exists())
        self.assertContains(self.client.get("/bills/"), "押金抵扣")

    def test_checkout_overdraw_rolls_back_and_waiting_refund_is_not_paid(self):
        self.deposit()
        with self.assertRaises(ValueError):
            checkout_tenancy(self.lease, checkout_date=date(2026, 10, 5), deposit_deduction_amount=4000, note="损坏")
        self.lease.refresh_from_db()
        self.assertEqual(self.lease.status, "active")
        self.assertFalse(Adjustment.objects.exists())
        checkout_tenancy(self.lease, checkout_date=date(2026, 10, 5), deposit_deduction_amount=500, note="损坏")
        self.assertEqual(Charge.objects.get(category="deposit_refund").balance, 3000)
        self.assertFalse(Payment.objects.filter(direction="pay").exists())
        self.assertEqual(tenancy_finances(self.lease)["deposit_held"], 3000)

    def test_checkout_offset_cannot_pay_another_tenant_or_future_debt(self):
        self.deposit()
        self.bill(amount=1000, due_date=date(2026, 11, 1))
        with self.assertRaises(ValueError):
            checkout_tenancy(self.lease, checkout_date=date(2026, 10, 5), deposit_offset_amount=1000)
        self.assertFalse(Adjustment.objects.exists())

    def test_monthly_collection_can_explicitly_combine_heating_with_rent_draft(self):
        heating = self.bill(category="heating", amount=380)
        data = dict(month="2026-10", date="2026-10-05", action="bulk", rows=[f"tenancy-{self.lease.pk}"], include_heating="1")
        response = self.client.post("/bills/monthly-rent/", data)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Payment.objects.get().amount, 3380)
        self.assertEqual(heating.balance, 0)
        self.client.post("/bills/monthly-rent/", data)
        self.assertEqual(Payment.objects.count(), 1)

    def test_all_accounting_pages_offer_the_same_entry(self):
        for path in ("/bills/", "/bills/monthly-rent/", "/bills/deposits/", "/charges/"):
            response = self.client.get(path)
            self.assertContains(response, 'href="/bills/entry/new/"')
            self.assertNotContains(response, 'href="/payments/new/')
            self.assertNotContains(response, 'href="/charges/new/')
        self.assertEqual(self.client.get(self.url).status_code, 200)
        self.assertTemplateUsed(self.client.get("/payments/new/"), "core/accounting_entry.html")
        legacy_pending = self.client.get("/charges/new/")
        self.assertTemplateUsed(legacy_pending, "core/accounting_entry.html")
        self.assertEqual(legacy_pending.context["form"].initial["state"], "pending")

    def test_prepaid_rent_uses_own_debt_and_keeps_remainder_without_double_receipt(self):
        rent = self.bill(amount=3000)
        data = self.entry(direction="income", category="prepaid", tenancy=self.lease.pk, amount=3500)
        self.assertEqual(self.client.post(self.url, data).status_code, 302)
        self.client.post(self.url, data)
        self.assertEqual(rent.balance, 0)
        self.assertEqual(Payment.objects.count(), 1)
        self.assertEqual(tenancy_finances(self.lease)["prepaid"], 500)

    def test_refund_partly_paid_before_checkout_is_not_refunded_twice(self):
        from .services import set_planned_checkout
        self.deposit()
        set_planned_checkout(self.lease, planned_date=date(2026, 10, 5), refund_deposit_amount=3500)
        planned = Charge.objects.get(category="deposit_refund")
        settle_charges([planned], amount=500)
        checkout_tenancy(self.lease, checkout_date=date(2026, 10, 5), refund_deposit_amount=3500, refund_paid=True)
        self.assertEqual(sum(p.amount for p in Payment.objects.filter(direction="pay")), 3500)
        self.assertEqual(tenancy_finances(self.lease)["deposit_held"], 0)

    def test_group_partial_settlement_retains_date_and_rejects_invalid_date(self):
        rent = self.bill(amount=3000)
        heating = self.bill(category="heating", amount=380)
        payload = {"charge_ids": [rent.pk, heating.pk], "amount": "3100", "date": "2026-10-03"}
        self.client.post("/bills/settle/", payload)
        self.assertEqual(rent.balance, 0)
        self.assertEqual(heating.balance, 280)
        self.assertEqual(Payment.objects.get().date, date(2026, 10, 3))
        payload.update(amount="100", date="invalid")
        response = self.client.post("/bills/settle/", payload)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Payment.objects.count(), 1)

    def test_expense_cannot_reuse_income_rent_generation_key(self):
        rent = self.bill(generated_key=f"tenancy:{self.lease.pk}:rent:202610")
        response = self.client.post(self.url, self.entry(category="rent", tenancy=self.lease.pk, amount=100, period_start="2026-10-01", period_end="2026-10-31"))
        self.assertEqual(response.status_code, 200)
        self.assertIn("category", response.context["form"].errors)
        self.assertEqual(rent.balance, 3000)
        self.assertFalse(Payment.objects.exists())
