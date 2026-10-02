from datetime import date
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse

from .deposit_collection import deposit_rows
from .models import Charge, Payment, Person, Room, Tenancy
from .services import record_payment, settle_charges, tenancy_finances


class DepositCollectionTests(TestCase):
    def setUp(self):
        clock = patch("django.utils.timezone.localdate", return_value=date(2026, 10, 1))
        clock.start()
        self.addCleanup(clock.stop)
        self.url = reverse("deposit_collection")
        self.tenancy = self.lease("A01")

    def lease(self, number, **overrides):
        values = dict(room=Room.objects.create(number=number), primary_person=Person.objects.create(name=f"{number}租客"),
                      start_date=date(2026, 1, 1), end_date=date(2026, 12, 31), monthly_rent=3000,
                      deposit_amount=None, billing_enabled=False)
        values.update(overrides)
        return Tenancy.objects.create(**values)

    def payload(self, *tenancies, **overrides):
        data = dict(date="2026-09-30", action="bulk", rows=[str(t.pk) for t in tenancies or [self.tenancy]])
        data.update(overrides)
        return data

    def charge(self, tenancy=None, **overrides):
        tenancy = tenancy or self.tenancy
        values = dict(tenancy=tenancy, room=tenancy.room, person=tenancy.primary_person,
                      direction="income", category="deposit", amount=3500, due_date=date(2026, 1, 1), description="押金")
        values.update(overrides)
        return Charge.objects.create(**values)

    def test_preview_defaults_to_3500_without_writing(self):
        response = self.client.get(self.url)
        self.assertContains(response, "批量登记押金")
        self.assertEqual(response.context["deposit_rows"][0]["amount"], 3500)
        self.assertFalse(Charge.objects.exists())
        self.assertFalse(Payment.objects.exists())
        self.tenancy.refresh_from_db()
        self.assertIsNone(self.tenancy.deposit_amount)

    def test_batch_creates_separate_receipts_without_enabling_other_billing(self):
        other = self.lease("B01")
        response = self.client.post(self.url, self.payload(self.tenancy, other))
        self.assertRedirects(response, self.url)
        self.assertEqual(Payment.objects.count(), 2)
        self.assertEqual(Charge.objects.count(), 2)
        self.assertEqual(sum(p.amount for p in Payment.objects.all()), 7000)
        for receipt in Payment.objects.all():
            self.assertEqual(receipt.category, "deposit")
            self.assertEqual(receipt.date, date(2026, 9, 30))
            self.assertEqual(receipt.unallocated_amount, 0)
        self.tenancy.refresh_from_db()
        self.assertEqual(self.tenancy.deposit_amount, 3500)
        self.assertFalse(self.tenancy.billing_enabled)
        self.assertEqual(tenancy_finances(self.tenancy)["deposit_held"], 3500)

    def test_partial_then_bulk_only_records_remainder_and_repeats_are_skipped(self):
        key = str(self.tenancy.pk)
        self.client.post(self.url, self.payload(single_row=key, **{f"partial_{key}": "1000"}))
        self.client.post(self.url, self.payload(single_row=key, **{f"partial_{key}": "500"}))
        self.assertEqual(deposit_rows()[0]["balance"], 2000)
        self.client.post(self.url, self.payload(rows=[key, key]))
        self.client.post(self.url, self.payload())
        self.assertEqual(list(Payment.objects.order_by("id").values_list("amount", flat=True)), [1000, 500, 2000])
        self.assertEqual(Charge.objects.count(), 1)
        self.assertEqual(deposit_rows()[0]["status"], "paid")

    def test_existing_deposit_receipts_are_used_but_rent_prepay_is_not(self):
        rent = self.charge(category="rent", amount=3000)
        heating = self.charge(category="heating", amount=400)
        deposit_receipt = record_payment(direction="receive", date=date(2026, 9, 1), amount=1000,
                                         tenancy=self.tenancy, category="deposit")
        rent_receipt = record_payment(direction="receive", date=date(2026, 9, 1), amount=500,
                                      tenancy=self.tenancy, category="rent", auto_allocate=False)
        self.assertEqual(deposit_rows()[0]["paid"], 1000)
        self.client.post(self.url, self.payload())
        deposit_receipt.refresh_from_db()
        rent_receipt.refresh_from_db()
        self.assertEqual(deposit_receipt.unallocated_amount, 0)
        self.assertEqual(rent_receipt.unallocated_amount, 500)
        self.assertEqual(rent.balance, 3000)
        self.assertEqual(heating.balance, 400)
        self.assertEqual(Payment.objects.order_by("id").last().amount, 2500)

    def test_fully_recorded_unallocated_deposit_does_not_create_more_money(self):
        record_payment(direction="receive", date=date(2026, 9, 1), amount=3500,
                       tenancy=self.tenancy, category="deposit")
        self.assertEqual(deposit_rows()[0]["balance"], 0)
        self.client.post(self.url, self.payload())
        self.assertEqual(Payment.objects.count(), 1)
        self.assertEqual(tenancy_finances(self.tenancy)["deposit_held"], 3500)

    def test_multiple_existing_receipts_never_over_allocate_deposit_bill(self):
        deposit = self.charge()
        for amount in [2000, 2000]:
            record_payment(direction="receive", date=date(2026, 9, 1), amount=amount,
                           tenancy=self.tenancy, category="deposit", auto_allocate=False)
        self.client.post(self.url, self.payload())
        self.assertEqual(Payment.objects.count(), 2)
        self.assertEqual(deposit.allocated_amount, 3500)
        self.assertEqual(sum(p.unallocated_amount for p in Payment.objects.all()), 500)

    def test_batch_respects_overpaid_deposit_receipts_without_an_existing_bill(self):
        record_payment(direction="receive", date=date(2026, 9, 1), amount=4000,
                       tenancy=self.tenancy, category="deposit", auto_allocate=False)
        response = self.client.post(self.url, self.payload())
        self.assertRedirects(response, self.url)
        self.assertEqual(Payment.objects.count(), 1)
        self.assertEqual(Charge.objects.get().allocated_amount, 3500)
        self.assertEqual(Payment.objects.get().unallocated_amount, 500)

    def test_existing_amount_and_renewal_deposit_are_preserved(self):
        deposit = self.charge(amount=3000)
        settle_charges([deposit], amount=1000)
        self.tenancy.status = "ended"
        self.tenancy.end_date = date(2026, 9, 30)
        self.tenancy.save()
        renewed = Tenancy.objects.create(previous_tenancy=self.tenancy, room=self.tenancy.room,
                  primary_person=self.tenancy.primary_person, start_date=date(2026, 10, 1),
                  end_date=date(2027, 9, 30), monthly_rent=3000, deposit_amount=3000, billing_enabled=False)
        self.assertEqual(deposit_rows()[0]["balance"], 2000)
        self.client.post(self.url, self.payload(renewed))
        self.assertEqual(Charge.objects.count(), 1)
        self.assertEqual(deposit.balance, 0)
        self.assertEqual(tenancy_finances(renewed)["deposit_held"], 3000)

    def test_invalid_single_amount_rolls_back_new_bill_and_contract_change(self):
        key = str(self.tenancy.pk)
        for value in ["-1", "0", "abc", "4000"]:
            response = self.client.post(self.url, self.payload(single_row=key, **{f"partial_{key}": value}))
            self.assertEqual(response.status_code, 200)
            self.assertFalse(Payment.objects.exists())
            self.assertFalse(Charge.objects.exists())
            self.tenancy.refresh_from_db()
            self.assertIsNone(self.tenancy.deposit_amount)

    def test_invalid_later_row_rolls_back_entire_batch(self):
        other = self.lease("B01")
        self.charge(other, status="void")
        response = self.client.post(self.url, self.payload(self.tenancy, other))
        self.assertContains(response, "已作废")
        self.assertFalse(Payment.objects.exists())
        self.assertFalse(Charge.objects.filter(tenancy=self.tenancy).exists())

    def test_custom_deposit_and_delete_receipt_reopen_balance(self):
        self.tenancy.deposit_amount = 3500
        self.tenancy.save(update_fields=["deposit_amount"])
        self.client.post(self.url, self.payload(**{f"deposit_amount_{self.tenancy.pk}": "3200"}))
        self.assertEqual(Charge.objects.get().amount, 3200)
        self.tenancy.refresh_from_db()
        self.assertEqual(self.tenancy.deposit_amount, 3200)
        receipt = Payment.objects.get()
        self.client.post(reverse("payment_delete", args=[receipt.pk]))
        self.assertEqual(deposit_rows()[0]["balance"], 3200)

    def test_future_and_ended_contracts_are_not_offered_and_partial_is_not_ignored(self):
        self.lease("B01", status="upcoming", start_date=date(2026, 10, 10))
        self.lease("B02", status="ended")
        self.assertEqual(len(deposit_rows()), 1)
        response = self.client.post(self.url, self.payload(**{f"partial_{self.tenancy.pk}": "1000"}))
        self.assertContains(response, "已填写本次实收")
        self.assertFalse(Payment.objects.exists())
