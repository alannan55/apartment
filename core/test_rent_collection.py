from datetime import date
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse

from .models import Charge, Payment, Person, Room, Tenancy
from .rent_collection import monthly_rent_rows
from .services import generate_rent_charges_until, record_payment, settle_charges


from .test_support import configure_test_account


class MonthlyRentCollectionTests(TestCase):
    def setUp(self):
        configure_test_account(self)
        clock = patch("django.utils.timezone.localdate", return_value=date(2026, 10, 1))
        clock.start()
        self.addCleanup(clock.stop)
        self.url = reverse("monthly_rent_collection")
        self.tenancy = self.lease("A04")

    def lease(self, number, **overrides):
        room = Room.objects.create(number=number)
        person = Person.objects.create(name=f"{number}测试租客")
        values = dict(
            room=room, primary_person=person, start_date=date(2026, 1, 1),
            end_date=date(2026, 12, 31), monthly_rent=Decimal("3000"),
            deposit_amount=None, billing_enabled=False,
        )
        values.update(overrides)
        return Tenancy.objects.create(**values)

    def payload(self, *tenancies, **overrides):
        data = dict(month="2026-10", date="2026-09-30", action="bulk", rows=[f"tenancy-{t.pk}" for t in tenancies or [self.tenancy]])
        data.update(overrides)
        return data

    def rent(self, tenancy=None, **overrides):
        tenancy = tenancy or self.tenancy
        values = dict(
            tenancy=tenancy, room=tenancy.room, person=tenancy.primary_person,
            direction="income", category="rent", due_date=date(2026, 9, 30),
            period_start=date(2026, 10, 1), period_end=date(2026, 10, 31),
            amount=3000, description="10月房租",
        )
        values.update(overrides)
        return Charge.objects.create(**values)

    def test_preview_of_imported_contract_does_not_write_or_enable_billing(self):
        response = self.client.get(self.url, {"month": "2026-10"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "待登记")
        row = response.context["rent_rows"][0]
        self.assertEqual(row["balance"], 3000)
        self.assertFalse(Charge.objects.exists())
        self.assertFalse(Payment.objects.exists())
        self.tenancy.refresh_from_db()
        self.assertFalse(self.tenancy.billing_enabled)
        self.assertIsNone(self.tenancy.deposit_amount)

    def test_paste_user_room_list_and_collect_18_rooms_in_one_request(self):
        numbers = "A04,A05,A08,A09,A11,A12,A13,A14,A15,A16,A17,A19,A22,A24,B01,B02,B05,B06".split(",")
        tenancies = [self.tenancy] + [self.lease(number) for number in numbers[1:]]
        response = self.client.get(self.url, {"month": "2026-10", "room_numbers": "，".join(numbers).lower()})
        self.assertEqual(sum(row["selected"] for row in response.context["rent_rows"]), 18)
        response = self.client.post(self.url, self.payload(*tenancies))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Payment.objects.count(), 18)
        self.assertEqual(Charge.objects.count(), 18)
        self.assertEqual(sum(p.amount for p in Payment.objects.all()), 54000)
        for payment in Payment.objects.all():
            self.assertEqual(payment.category, "rent")
            self.assertEqual(payment.date, date(2026, 9, 30))
            self.assertEqual(payment.unallocated_amount, 0)
        self.assertFalse(Tenancy.objects.filter(billing_enabled=True).exists())
        self.assertFalse(Charge.objects.exclude(category="rent").exists())

    def test_single_partial_receipt_then_bulk_only_collects_balance(self):
        key = f"tenancy-{self.tenancy.pk}"
        response = self.client.post(self.url, self.payload(single_row=key, **{f"partial_{key}": "1000"}))
        self.assertEqual(response.status_code, 302)
        charge = Charge.objects.get()
        self.assertEqual(charge.balance, 2000)
        self.assertEqual(monthly_rent_rows(date(2026, 10, 1))[0]["status"], "partial")
        self.client.post(self.url, self.payload())
        self.assertEqual(list(Payment.objects.order_by("id").values_list("amount", flat=True)), [1000, 2000])
        self.assertEqual(charge.balance, 0)

    def test_repeating_bulk_request_skips_paid_rooms_and_deduplicates_selection(self):
        data = self.payload()
        data["rows"] *= 2
        self.client.post(self.url, data)
        self.client.post(self.url, data)
        self.assertEqual(Payment.objects.count(), 1)
        self.assertEqual(Charge.objects.count(), 1)

    def test_batch_does_not_settle_deposit_heating_or_other_months(self):
        october = self.rent()
        other_month = self.rent(period_start=date(2026, 9, 1))
        deposit = self.rent(category="deposit", amount=3500)
        heating = self.rent(category="heating", amount=380)
        settle_charges([october], amount=1000)
        self.client.post(self.url, self.payload())
        self.assertEqual(october.balance, 0)
        for charge, balance in [(other_month, 3000), (deposit, 3500), (heating, 380)]:
            self.assertEqual(charge.balance, balance)
        self.assertEqual(Payment.objects.latest("id").amount, 2000)

    def test_draft_amount_can_be_corrected_before_collection(self):
        key = f"tenancy-{self.tenancy.pk}"
        self.client.post(self.url, self.payload(**{f"rent_amount_{key}": "2750.50"}))
        charge = Charge.objects.get()
        self.assertEqual(charge.amount, Decimal("2750.50"))
        self.tenancy.billing_enabled = True
        self.tenancy.billing_start_date = date(2026, 10, 1)
        self.tenancy.save()
        generate_rent_charges_until(self.tenancy, date(2026, 10, 31))
        self.assertEqual(Charge.objects.count(), 1)
        charge.refresh_from_db()
        self.assertEqual(charge.amount, Decimal("2750.50"))

    def test_invalid_amount_or_overpayment_rolls_back_new_bill(self):
        key = f"tenancy-{self.tenancy.pk}"
        for amount in ["3000.01", "-1", "NaN", "Infinity", "invalid"]:
            with self.subTest(amount=amount):
                response = self.client.post(self.url, self.payload(single_row=key, **{f"partial_{key}": amount}))
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.context["form"].errors)
                self.assertFalse(Payment.objects.exists())
                self.assertFalse(Charge.objects.exists())

    def test_invalid_month_date_or_row_does_not_write(self):
        for overrides in [{"month": "invalid"}, {"date": "invalid"}, {"rows": ["tenancy-9999"]}, {"rows": []}]:
            with self.subTest(overrides=overrides):
                self.client.post(self.url, self.payload(**overrides))
                self.assertFalse(Payment.objects.exists())
                self.assertFalse(Charge.objects.exists())

    def test_batch_rejects_partial_input_to_avoid_accidental_full_receipt(self):
        key = f"tenancy-{self.tenancy.pk}"
        response = self.client.post(self.url, self.payload(**{f"partial_{key}": "1000"}))
        self.assertContains(response, "已填写单笔实收金额")
        self.assertFalse(Payment.objects.exists())

    def test_entire_batch_rolls_back_if_a_later_settlement_fails(self):
        other = self.lease("A05")
        def settle_or_fail(charges, **kwargs):
            if charges[0].tenancy_id == other.pk:
                raise ValueError("模拟第二户金额变化")
            return settle_charges(charges, **kwargs)
        with patch("core.views.settle_charges", side_effect=settle_or_fail):
            response = self.client.post(self.url, self.payload(self.tenancy, other))
        self.assertContains(response, "模拟第二户金额变化")
        self.assertFalse(Payment.objects.exists())
        self.assertFalse(Charge.objects.exists())

    def test_void_bill_is_not_recreated_and_room_paste_explains_missing_or_paid(self):
        self.rent(status="void")
        paid = self.lease("A05")
        settle_charges([self.rent(paid)])
        response = self.client.get(self.url, {"month": "2026-10", "room_numbers": "A04 A05 A99"})
        self.assertContains(response, "这些房号没有当月可登记房租")
        self.assertContains(response, "已交齐，已跳过：A05")
        self.assertEqual(len(response.context["rent_rows"]), 1)
        self.assertFalse(response.context["rent_rows"][0]["selected"])

    def test_first_month_and_short_term_payment_cycle_match_existing_generator(self):
        first = self.lease("A05", start_date=date(2026, 10, 10), end_date=date(2027, 10, 31))
        short = self.lease("B01", start_date=date(2026, 8, 7), end_date=date(2026, 12, 6), payment_cycle="quarterly")
        october = {row["tenancy"].pk: row for row in monthly_rent_rows(date(2026, 10, 1))}
        self.assertEqual(october[first.pk]["amount"], Decimal("2032.26"))
        self.assertNotIn(short.pk, october)
        november = {row["tenancy"].pk: row for row in monthly_rent_rows(date(2026, 11, 1))}
        self.assertEqual(november[short.pk]["amount"], 9000)
        self.assertEqual(november[short.pk]["draft"]["period_start"], date(2026, 11, 7))

    def test_checkout_and_billing_start_limit_drafts(self):
        self.tenancy.planned_move_out_date = date(2026, 9, 30)
        self.tenancy.save()
        self.lease("A05", billing_start_date=date(2026, 11, 1))
        self.lease("A06", status="ended", move_out_date=date(2026, 9, 30))
        self.assertEqual(monthly_rent_rows(date(2026, 10, 1)), [])

    def test_deleting_receipt_restores_rent_balance(self):
        self.client.post(self.url, self.payload())
        payment = Payment.objects.get()
        self.client.post(reverse("payment_delete", args=[payment.pk]))
        self.assertEqual(monthly_rent_rows(date(2026, 10, 1))[0]["balance"], 3000)

    def test_existing_single_receipt_is_allocated_before_batch_records_remainder(self):
        record_payment(direction="receive", date=date(2026, 9, 30), amount=1000, tenancy=self.tenancy)
        self.client.post(self.url, self.payload())
        self.assertEqual(list(Payment.objects.order_by("id").values_list("amount", flat=True)), [1000, 2000])
        self.assertEqual(Charge.objects.get().balance, 0)

    def test_prepaid_full_rent_does_not_create_second_receipt_or_consume_deposit(self):
        prepaid = record_payment(direction="receive", date=date(2026, 9, 30), amount=3000, tenancy=self.tenancy)
        deposit = record_payment(direction="receive", date=date(2026, 9, 30), amount=3500, tenancy=self.tenancy, category="deposit")
        self.client.post(self.url, self.payload())
        self.assertEqual(Payment.objects.count(), 2)
        self.assertEqual(Charge.objects.get().balance, 0)
        self.assertEqual(prepaid.unallocated_amount, 0)
        self.assertEqual(deposit.unallocated_amount, 3500)

    def test_home_distinguishes_unrecorded_import_from_paid_rent(self):
        response = self.client.get("/")
        self.assertContains(response, "房租记录")
        self.assertContains(response, "待登记")
        self.assertNotContains(response, '<strong class="amount-good">已交齐</strong>')

    def test_midmonth_billing_start_uses_same_period_and_key_as_generator(self):
        tenancy = self.lease("A05", start_date=date(2026, 10, 1), end_date=date(2027, 10, 31), billing_start_date=date(2026, 10, 15))
        self.client.post(self.url, self.payload(tenancy))
        charge = tenancy.charges.get()
        self.assertEqual(charge.generated_key, f"tenancy:{tenancy.pk}:rent:202610")
        tenancy.billing_enabled = True
        tenancy.save()
        generate_rent_charges_until(tenancy, date(2026, 10, 31))
        self.assertEqual(tenancy.charges.count(), 1)
