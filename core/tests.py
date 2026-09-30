from datetime import date
from io import BytesIO
from decimal import Decimal
from unittest.mock import patch

from django.db import OperationalError, connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from .exports import agent_room_status_image, import_template_workbook, police_report_workbook
from .forms import PaymentForm, PersonCreateForm, SignContractForm
from .models import Charge, Payment, Person, RecurringRule, Room, Stay, Tenancy
from .services import (
    checkout_tenancy,
    collect_charge,
    generate_due_charges,
    generate_rent_charges_until,
    generate_heating_charges_until,
    generate_scheduled_due_charges,
    move_tenancy,
    record_payment,
    cancel_planned_checkout,
    set_planned_checkout,
    sign_contract,
)
from .spreadsheet_import import import_template


class BillingServiceTests(TestCase):
    def setUp(self):
        self.room = Room.objects.create(number="A01", listing_price=Decimal("3500.00"), orientation="南", floor=Room.Floor.SECOND)
        self.person_data = {
            "name": "张三",
            "id_number": "110101199001011234",
            "phone": "13800000000",
            "emergency_name": "李四",
            "emergency_phone": "13900000000",
            "emergency_address": "北京",
        }

    def test_long_term_first_month_uses_expected_proration(self):
        tenancy = sign_contract(
            room=self.room,
            person_data=self.person_data,
            start_date=date(2026, 4, 10),
            end_date=date(2027, 4, 9),
            monthly_rent=Decimal("3000.00"),
        )
        rent = tenancy.charges.get(category=Charge.Category.RENT)
        self.assertEqual(rent.amount, Decimal("2000.00"))
        self.assertEqual(rent.period_start, date(2026, 4, 10))
        self.assertEqual(rent.period_end, date(2026, 4, 30))

    def test_sign_contract_form_only_lists_vacant_rooms(self):
        Room.objects.create(number="A02", status=Room.Status.OCCUPIED)
        form = SignContractForm()
        self.assertEqual(list(form.fields["room"].queryset), [self.room])

    def test_short_term_uses_contract_day_cycle(self):
        tenancy = sign_contract(
            room=self.room,
            person_data=self.person_data,
            start_date=date(2026, 4, 10),
            end_date=date(2026, 7, 9),
            monthly_rent=Decimal("3000.00"),
        )
        rent = tenancy.charges.get(category=Charge.Category.RENT)
        self.assertTrue(tenancy.is_short_term)
        self.assertEqual(rent.amount, Decimal("3000.00"))
        self.assertEqual(rent.period_start, date(2026, 4, 10))
        self.assertEqual(rent.period_end, date(2026, 5, 9))
        self.assertEqual(tenancy.police_end_date, date(2026, 10, 10))

    def test_billing_start_skips_historical_rent_generation(self):
        tenancy = sign_contract(
            room=self.room,
            person_data=self.person_data,
            start_date=date(2025, 9, 20),
            end_date=date(2026, 9, 30),
            monthly_rent=Decimal("3200.00"),
        )
        Charge.objects.filter(tenancy=tenancy, category=Charge.Category.RENT).delete()
        tenancy.billing_start_date = date(2026, 6, 1)
        tenancy.save(update_fields=["billing_start_date"])
        generate_rent_charges_until(tenancy, date(2026, 7, 12))
        periods = list(
            tenancy.charges.filter(category=Charge.Category.RENT).order_by("period_start").values_list("period_start", flat=True)
        )
        self.assertEqual(periods, [date(2026, 6, 1), date(2026, 7, 1)])

    def test_scheduled_generation_creates_next_month_on_penultimate_day(self):
        tenancy = sign_contract(
            room=self.room,
            person_data=self.person_data,
            start_date=date(2026, 6, 1),
            end_date=date(2027, 5, 31),
            monthly_rent=Decimal("3000.00"),
        )
        generate_scheduled_due_charges(date(2026, 6, 28))
        self.assertFalse(
            tenancy.charges.filter(category=Charge.Category.RENT, period_start=date(2026, 7, 1)).exists()
        )
        generate_scheduled_due_charges(date(2026, 6, 29))
        july_rent = tenancy.charges.get(category=Charge.Category.RENT, period_start=date(2026, 7, 1))
        self.assertEqual(july_rent.due_date, date(2026, 6, 30))

    def test_sign_contract_creates_deposit_due_and_collection_payment(self):
        tenancy = sign_contract(
            room=self.room,
            person_data=self.person_data,
            start_date=date(2026, 6, 1),
            end_date=date(2027, 5, 31),
            monthly_rent=Decimal("3000.00"),
        )
        deposit = tenancy.charges.get(category=Charge.Category.DEPOSIT)
        response = self.client.get("/collections/?month=2026-06&category=deposit")
        self.assertContains(response, self.room.number)
        self.client.post(f"/collections/{deposit.id}/collect/", HTTP_REFERER="/collections/?month=2026-06&category=deposit")
        deposit.refresh_from_db()
        payment = Payment.objects.get(category=Payment.Category.DEPOSIT)
        self.assertEqual(payment.amount, Decimal("3500.00"))
        self.assertEqual(deposit.balance, Decimal("0.00"))
        self.assertEqual(deposit.status, Charge.Status.PAID)

    def test_scheduled_generation_creates_heating_cycle_due(self):
        tenancy = sign_contract(
            room=self.room,
            person_data=self.person_data,
            start_date=date(2026, 10, 1),
            end_date=date(2027, 9, 30),
            monthly_rent=Decimal("3000.00"),
        )
        self.assertFalse(tenancy.charges.filter(category=Charge.Category.HEATING).exists())
        generate_scheduled_due_charges(date(2026, 10, 30))
        heating = tenancy.charges.get(category=Charge.Category.HEATING, period_start=date(2026, 11, 15))
        self.assertEqual(heating.amount, Decimal("380.00"))
        self.assertEqual(heating.due_date, date(2026, 11, 15))

    def test_partial_payment_allocates_oldest_charges_first(self):
        tenancy = sign_contract(
            room=self.room,
            person_data=self.person_data,
            start_date=date(2026, 1, 1),
            end_date=date(2026, 12, 31),
            monthly_rent=Decimal("3000.00"),
        )
        Charge.objects.filter(tenancy=tenancy).delete()
        jan = Charge.objects.create(
            direction=Charge.Direction.INCOME,
            category=Charge.Category.RENT,
            tenancy=tenancy,
            room=self.room,
            person=tenancy.primary_person,
            due_date=date(2026, 1, 1),
            amount=Decimal("3000.00"),
            description="1月租金",
        )
        feb = Charge.objects.create(
            direction=Charge.Direction.INCOME,
            category=Charge.Category.RENT,
            tenancy=tenancy,
            room=self.room,
            person=tenancy.primary_person,
            due_date=date(2026, 2, 1),
            amount=Decimal("3000.00"),
            description="2月租金",
        )
        mar = Charge.objects.create(
            direction=Charge.Direction.INCOME,
            category=Charge.Category.RENT,
            tenancy=tenancy,
            room=self.room,
            person=tenancy.primary_person,
            due_date=date(2026, 3, 1),
            amount=Decimal("3000.00"),
            description="3月租金",
        )
        payment = record_payment(
            direction=Payment.Direction.RECEIVE,
            date=date(2026, 3, 20),
            amount=Decimal("7000.00"),
            tenancy=tenancy,
        )
        jan.refresh_from_db()
        feb.refresh_from_db()
        mar.refresh_from_db()
        self.assertEqual(payment.allocated_amount, Decimal("7000.00"))
        self.assertEqual(jan.balance, Decimal("0.00"))
        self.assertEqual(feb.balance, Decimal("0.00"))
        self.assertEqual(mar.balance, Decimal("2000.00"))

    def test_general_payment_prioritizes_rent_before_heating(self):
        tenancy = sign_contract(
            room=self.room,
            person_data=self.person_data,
            start_date=date(2026, 1, 1),
            end_date=date(2026, 12, 31),
            monthly_rent=Decimal("3000.00"),
        )
        Charge.objects.filter(tenancy=tenancy).delete()
        heating = Charge.objects.create(
            direction=Charge.Direction.INCOME,
            category=Charge.Category.HEATING,
            tenancy=tenancy,
            room=self.room,
            person=tenancy.primary_person,
            due_date=date(2026, 1, 1),
            amount=Decimal("720.00"),
            description="取暖费",
        )
        rent = Charge.objects.create(
            direction=Charge.Direction.INCOME,
            category=Charge.Category.RENT,
            tenancy=tenancy,
            room=self.room,
            person=tenancy.primary_person,
            due_date=date(2026, 1, 1),
            amount=Decimal("7000.00"),
            description="租金",
        )
        record_payment(
            direction=Payment.Direction.RECEIVE,
            category=Payment.Category.OTHER,
            date=date(2026, 1, 5),
            amount=Decimal("5000.00"),
            tenancy=tenancy,
        )
        rent.refresh_from_db()
        heating.refresh_from_db()
        self.assertEqual(rent.balance, Decimal("2000.00"))
        self.assertEqual(heating.balance, Decimal("720.00"))

    def test_collect_charge_targets_selected_charge(self):
        tenancy = sign_contract(
            room=self.room,
            person_data=self.person_data,
            start_date=date(2026, 1, 1),
            end_date=date(2026, 12, 31),
            monthly_rent=Decimal("3000.00"),
        )
        Charge.objects.filter(tenancy=tenancy).delete()
        rent = Charge.objects.create(
            direction=Charge.Direction.INCOME,
            category=Charge.Category.RENT,
            tenancy=tenancy,
            room=self.room,
            person=tenancy.primary_person,
            due_date=date(2026, 1, 1),
            amount=Decimal("7000.00"),
            description="租金",
        )
        heating = Charge.objects.create(
            direction=Charge.Direction.INCOME,
            category=Charge.Category.HEATING,
            tenancy=tenancy,
            room=self.room,
            person=tenancy.primary_person,
            due_date=date(2026, 1, 1),
            amount=Decimal("720.00"),
            description="取暖费",
        )
        collect_charge(heating, amount=Decimal("300.00"), date=date(2026, 1, 5))
        rent.refresh_from_db()
        heating.refresh_from_db()
        self.assertEqual(rent.balance, Decimal("7000.00"))
        self.assertEqual(heating.balance, Decimal("420.00"))

    def test_heating_generates_partial_and_full_cycle(self):
        tenancy = sign_contract(
            room=self.room,
            person_data=self.person_data,
            start_date=date(2026, 11, 20),
            end_date=date(2027, 12, 31),
            monthly_rent=Decimal("3000.00"),
        )
        Charge.objects.filter(tenancy=tenancy, category=Charge.Category.HEATING).delete()
        charges = generate_heating_charges_until(tenancy, date(2026, 12, 20))
        amounts = {charge.period_start: charge.amount for charge in charges}
        self.assertEqual(amounts[date(2026, 11, 20)], Decimal("318.71"))
        self.assertEqual(amounts[date(2026, 12, 15)], Decimal("380.00"))

    def test_commission_prorates_by_room_base_and_due_after_one_month(self):
        self.room.commission_base = Decimal("2400.00")
        self.room.save(update_fields=["commission_base"])
        tenancy = sign_contract(
            room=self.room,
            person_data=self.person_data,
            start_date=date(2026, 4, 1),
            end_date=date(2026, 6, 30),
            monthly_rent=Decimal("3000.00"),
            broker_name="乘风",
        )
        commission = tenancy.charges.get(category=Charge.Category.COMMISSION)
        self.assertEqual(commission.amount, Decimal("600.00"))
        self.assertEqual(commission.due_date, date(2026, 5, 1))

    def test_move_room_creates_manual_difference_charge(self):
        new_room = Room.objects.create(number="A02", listing_price=Decimal("3800.00"))
        tenancy = sign_contract(
            room=self.room,
            person_data=self.person_data,
            start_date=date(2026, 4, 1),
            end_date=date(2027, 3, 31),
            monthly_rent=Decimal("3000.00"),
        )
        move_tenancy(
            tenancy,
            new_room=new_room,
            move_date=date(2026, 4, 16),
            new_monthly_rent=Decimal("3500.00"),
            manual_diff_amount=Decimal("200.00"),
            note="优惠补差",
        )
        tenancy.refresh_from_db()
        charge = tenancy.charges.get(category=Charge.Category.MOVE_DIFF)
        self.assertEqual(tenancy.room, new_room)
        self.assertEqual(tenancy.monthly_rent, Decimal("3500.00"))
        self.assertEqual(charge.amount, Decimal("200.00"))
        self.assertEqual(tenancy.adjustments.count(), 1)

    def test_checkout_removes_person_from_police_export(self):
        tenancy = sign_contract(
            room=self.room,
            person_data=self.person_data,
            start_date=date(2026, 1, 1),
            end_date=date(2026, 12, 31),
            monthly_rent=Decimal("3000.00"),
        )
        checkout_tenancy(tenancy, checkout_date=date(2026, 6, 1), refund_deposit_amount=Decimal("3500.00"))
        stay = Stay.objects.get(tenancy=tenancy)
        self.assertFalse(stay.is_active)
        workbook = police_report_workbook(today=date(2026, 6, 2))
        values = [cell.value for row in workbook.active.iter_rows() for cell in row]
        self.assertNotIn("张三", values)

    def test_planned_checkout_keeps_police_report_and_creates_refund_due(self):
        tenancy = sign_contract(
            room=self.room,
            person_data=self.person_data,
            start_date=date(2026, 5, 4),
            end_date=date(2027, 5, 31),
            monthly_rent=Decimal("3000.00"),
        )
        set_planned_checkout(
            tenancy,
            planned_date=date(2026, 6, 30),
            refund_deposit_amount=Decimal("3500.00"),
            note="计划提前退租",
        )
        tenancy.refresh_from_db()
        stay = Stay.objects.get(tenancy=tenancy)
        refund = Charge.objects.get(generated_key=f"tenancy:{tenancy.id}:planned_deposit_refund")
        self.assertEqual(tenancy.status, Tenancy.Status.ACTIVE)
        self.assertIsNone(tenancy.move_out_date)
        self.assertTrue(stay.is_active)
        self.assertEqual(refund.direction, Charge.Direction.EXPENSE)
        self.assertEqual(refund.category, Charge.Category.DEPOSIT_REFUND)
        self.assertEqual(refund.due_date, date(2026, 6, 30))
        self.assertEqual(refund.amount, Decimal("3500.00"))
        self.assertEqual(self.room.refresh_status(today=date(2026, 6, 16), save=False), Room.Status.EXPIRING)
        workbook = police_report_workbook(today=date(2026, 6, 16))
        values = [cell.value for row in workbook.active.iter_rows() for cell in row]
        self.assertIn("张三", values)
        set_planned_checkout(
            tenancy,
            planned_date=date(2026, 7, 30),
            refund_deposit_amount=Decimal("3500.00"),
            note="改为七月底退租",
        )
        refund.refresh_from_db()
        self.assertEqual(refund.due_date, date(2026, 7, 30))
        self.assertEqual(self.room.refresh_status(today=date(2026, 6, 16), save=False), Room.Status.EXPIRING)

    def test_planned_checkout_removes_unpaid_future_rent_due(self):
        tenancy = sign_contract(
            room=self.room,
            person_data=self.person_data,
            start_date=date(2026, 6, 1),
            end_date=date(2027, 5, 31),
            monthly_rent=Decimal("3000.00"),
        )
        generate_scheduled_due_charges(date(2026, 6, 29))
        self.assertTrue(tenancy.charges.filter(category=Charge.Category.RENT, period_start=date(2026, 7, 1)).exists())
        set_planned_checkout(
            tenancy,
            planned_date=date(2026, 6, 30),
            refund_deposit_amount=Decimal("3500.00"),
            note="预计月底退租",
        )
        self.assertFalse(tenancy.charges.filter(category=Charge.Category.RENT, period_start=date(2026, 7, 1)).exists())
        cancel_planned_checkout(tenancy)
        generate_scheduled_due_charges(date(2026, 6, 29))
        self.assertTrue(tenancy.charges.filter(category=Charge.Category.RENT, period_start=date(2026, 7, 1)).exists())

    def test_cancel_planned_checkout_removes_refund_due_and_restores_room_status(self):
        tenancy = sign_contract(
            room=self.room,
            person_data=self.person_data,
            start_date=date(2026, 5, 4),
            end_date=date(2027, 5, 31),
            monthly_rent=Decimal("3000.00"),
        )
        set_planned_checkout(
            tenancy,
            planned_date=date(2026, 6, 30),
            refund_deposit_amount=Decimal("3500.00"),
            note="计划提前退租",
        )
        cancel_planned_checkout(tenancy)
        tenancy.refresh_from_db()
        self.assertIsNone(tenancy.planned_move_out_date)
        self.assertFalse(Charge.objects.filter(generated_key=f"tenancy:{tenancy.id}:planned_deposit_refund").exists())
        self.assertEqual(self.room.refresh_status(today=date(2026, 6, 16), save=False), Room.Status.OCCUPIED)

    def test_checkout_view_saves_planned_checkout(self):
        tenancy = sign_contract(
            room=self.room,
            person_data=self.person_data,
            start_date=date(2026, 5, 4),
            end_date=date(2027, 5, 31),
            monthly_rent=Decimal("3000.00"),
        )
        response = self.client.post(
            f"/tenancies/{tenancy.id}/checkout/",
            {
                "action": "plan",
                "planned_move_out_date": "2026-06-30",
                "planned_deposit_refund_amount": "3500",
                "note": "计划提前退租",
            },
        )
        self.assertEqual(response.status_code, 302)
        tenancy.refresh_from_db()
        self.assertEqual(tenancy.planned_move_out_date, date(2026, 6, 30))
        self.assertEqual(tenancy.status, Tenancy.Status.ACTIVE)

    def test_checkout_view_renders_plan_and_actual_actions(self):
        tenancy = sign_contract(
            room=self.room,
            person_data=self.person_data,
            start_date=date(2026, 5, 4),
            end_date=date(2027, 5, 31),
            monthly_rent=Decimal("3000.00"),
        )
        response = self.client.get(f"/tenancies/{tenancy.id}/checkout/")
        self.assertContains(response, "保存预计退租")
        self.assertContains(response, "确认实际退租")

    def test_manager_police_report_uses_manager_text(self):
        person = Person.objects.create(name="管理员", id_number="110101198001010000", phone="13600000000")
        Stay.objects.create(
            person=person,
            room=self.room,
            stay_type=Stay.Type.MANAGER,
            start_date=date(2026, 1, 1),
            report_note="管理员",
        )
        workbook = police_report_workbook(today=date(2026, 6, 2))
        rows = list(workbook.active.iter_rows(values_only=True))
        manager_row = [row for row in rows if row and row[0] == "管理员"][0]
        self.assertEqual(manager_row[-1], "管理员")

    def test_agent_room_status_export_is_png(self):
        self.room.status = Room.Status.VACANT
        self.room.room_password = "123456#"
        self.room.save(update_fields=["status", "room_password"])
        png = agent_room_status_image(today=date(2026, 6, 11))
        self.assertTrue(png.startswith(b"\x89PNG"))
        self.assertIn(b"PNG", png[:16])

    def test_standard_import_template_can_round_trip_sample_rows(self):
        workbook = import_template_workbook()
        data = BytesIO()
        workbook.save(data)
        data.seek(0)
        counts = import_template(data, clear=True)
        self.assertEqual(counts["房间"], 1)
        self.assertEqual(counts["人员"], 3)
        self.assertEqual(counts["合同"], 1)
        self.assertEqual(counts["入住"], 3)
        self.assertEqual(counts["账单"], 0)
        self.assertEqual(counts["收付款"], 0)
        self.assertEqual(Room.objects.count(), 1)
        self.assertEqual(Person.objects.count(), 3)
        self.assertEqual(Tenancy.objects.count(), 1)
        self.assertEqual(Payment.objects.count(), 0)
        self.assertEqual(Stay.objects.filter(stay_type=Stay.Type.MANAGER).count(), 1)

    def test_person_create_form_validates_id_and_phone_lengths(self):
        form = PersonCreateForm(
            data={
                "name": "王五",
                "id_number": "123",
                "phone": "1380000000",
                "room": self.room.id,
                "stay_type": Stay.Type.PERMANENT,
                "start_date": "2026-06-01",
            }
        )
        self.assertFalse(form.is_valid())
        self.assertIn("id_number", form.errors)
        self.assertIn("phone", form.errors)

    def test_payment_form_has_payment_category(self):
        form = PaymentForm()
        self.assertIn("category", form.fields)
        self.assertEqual([label for _, label in form.fields["category"].choices], ["押金", "租金", "其它"])

    def test_person_create_view_defaults_room_from_querystring(self):
        response = self.client.get(f"/people/new/?room={self.room.id}")
        html = response.content.decode("utf-8")
        self.assertIn(f'value="{self.room.id}" selected', html)

    def test_manager_person_create_form_does_not_require_dates(self):
        form = PersonCreateForm(
            data={
                "name": "管理员",
                "id_number": "110101198001010000",
                "phone": "13600000000",
                "room": self.room.id,
                "stay_type": Stay.Type.MANAGER,
            }
        )
        self.assertTrue(form.is_valid(), form.errors)
        self.assertIsNone(form.cleaned_data["end_date"])
        self.assertEqual(form.cleaned_data["report_note"], "管理员")

    def test_ledger_page_includes_manual_payments(self):
        record_payment(
            direction=Payment.Direction.RECEIVE,
            category=Payment.Category.OTHER,
            date=date(2026, 6, 1),
            amount=Decimal("123.00"),
            room=self.room,
            memo="手动收款测试",
        )
        response = self.client.get("/charges/?status=payments")
        self.assertContains(response, "手动收款测试")
        self.assertContains(response, "流水")

    def test_collection_page_lists_rent_due(self):
        tenancy = sign_contract(
            room=self.room,
            person_data=self.person_data,
            start_date=date(2026, 6, 1),
            end_date=date(2027, 5, 31),
            monthly_rent=Decimal("3000.00"),
        )
        response = self.client.get("/collections/?month=2026-06")
        self.assertContains(response, self.room.number)
        self.assertContains(response, tenancy.primary_person.name)
        self.assertContains(response, "全额收")

    def test_monthly_bill_combines_rent_and_heating(self):
        tenancy = sign_contract(
            room=self.room,
            person_data=self.person_data,
            start_date=date(2026, 11, 1),
            end_date=date(2027, 10, 31),
            monthly_rent=Decimal("3000.00"),
        )
        response = self.client.get("/bills/?direction=income&scope=month&month=2026-11")
        rows = response.context["bill_rows"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["rent_amount"], Decimal("3000.00"))
        self.assertEqual(rows[0]["heating_amount"], Decimal("380.00"))
        self.assertEqual(rows[0]["amount"], Decimal("3380.00"))
        self.assertContains(response, "房租")
        self.assertContains(response, "取暖费")
        self.assertEqual(tenancy.charges.filter(category=Charge.Category.HEATING).count(), 1)

    def test_partial_monthly_bill_payment_prioritizes_rent(self):
        tenancy = sign_contract(
            room=self.room,
            person_data=self.person_data,
            start_date=date(2026, 11, 1),
            end_date=date(2027, 10, 31),
            monthly_rent=Decimal("3000.00"),
        )
        rent = tenancy.charges.get(category=Charge.Category.RENT)
        heating = tenancy.charges.get(category=Charge.Category.HEATING)
        response = self.client.post(
            "/bills/settle/",
            {"charge_ids": [str(rent.id), str(heating.id)], "amount": "3200"},
            HTTP_REFERER="/bills/?direction=income&scope=month&month=2026-11",
        )
        self.assertEqual(response.status_code, 302)
        rent.refresh_from_db()
        heating.refresh_from_db()
        self.assertEqual(rent.balance, Decimal("0.00"))
        self.assertEqual(heating.balance, Decimal("180.00"))
        payment = Payment.objects.get(amount=Decimal("3200.00"))
        self.assertEqual(payment.direction, Payment.Direction.RECEIVE)
        self.assertEqual(payment.allocations.count(), 2)

    def test_paid_monthly_bill_remains_visible_and_blue(self):
        tenancy = sign_contract(
            room=self.room,
            person_data=self.person_data,
            start_date=date(2026, 6, 1),
            end_date=date(2027, 5, 31),
            monthly_rent=Decimal("3000.00"),
        )
        rent = tenancy.charges.get(category=Charge.Category.RENT)
        self.client.post("/bills/settle/", {"charge_ids": [str(rent.id)]})
        response = self.client.get("/bills/?direction=income&scope=month&month=2026-06&status=paid")
        self.assertContains(response, self.room.number)
        self.assertContains(response, "已结清")
        self.assertContains(response, "bill-row-paid")

    def test_payable_bill_creates_payment_and_stays_visible(self):
        charge = Charge.objects.create(
            direction=Charge.Direction.EXPENSE,
            category=Charge.Category.COMMISSION,
            room=self.room,
            due_date=date(2026, 6, 20),
            amount=Decimal("800.00"),
            description="A01 中介佣金",
        )
        response = self.client.get("/bills/?direction=expense&scope=month&month=2026-06")
        self.assertContains(response, "中介佣金")
        self.client.post("/bills/settle/", {"charge_ids": [str(charge.id)]})
        charge.refresh_from_db()
        payment = Payment.objects.get(direction=Payment.Direction.PAY)
        self.assertEqual(payment.amount, Decimal("800.00"))
        self.assertEqual(charge.status, Charge.Status.PAID)
        response = self.client.get("/bills/?direction=expense&scope=month&month=2026-06&status=paid")
        self.assertContains(response, "bill-row-paid")

    def test_bulk_full_collection_creates_one_payment_per_room(self):
        first = sign_contract(
            room=self.room,
            person_data=self.person_data,
            start_date=date(2026, 6, 1),
            end_date=date(2027, 5, 31),
            monthly_rent=Decimal("3000.00"),
        )
        second_room = Room.objects.create(number="A02", listing_price=Decimal("3200.00"))
        second = sign_contract(
            room=second_room,
            person_data={
                **self.person_data,
                "name": "李四",
                "id_number": "110101199202021234",
                "phone": "13900000000",
            },
            start_date=date(2026, 6, 1),
            end_date=date(2027, 5, 31),
            monthly_rent=Decimal("3200.00"),
        )
        first_rent = first.charges.get(category=Charge.Category.RENT)
        second_rent = second.charges.get(category=Charge.Category.RENT)
        response = self.client.post(
            "/bills/bulk-settle/",
            {"bill_groups": [str(first_rent.id), str(second_rent.id)]},
            HTTP_REFERER="/bills/?direction=income&month=2026-06",
        )
        self.assertEqual(response.status_code, 302)
        first_rent.refresh_from_db()
        second_rent.refresh_from_db()
        self.assertEqual(first_rent.status, Charge.Status.PAID)
        self.assertEqual(second_rent.status, Charge.Status.PAID)
        self.assertEqual(Payment.objects.filter(direction=Payment.Direction.RECEIVE).count(), 2)
        self.assertSetEqual(
            set(Payment.objects.values_list("room__number", flat=True)),
            {"A01", "A02"},
        )

    def test_bill_payment_can_be_revised_after_settlement(self):
        tenancy = sign_contract(
            room=self.room,
            person_data=self.person_data,
            start_date=date(2026, 6, 1),
            end_date=date(2027, 5, 31),
            monthly_rent=Decimal("3000.00"),
        )
        rent = tenancy.charges.get(category=Charge.Category.RENT)
        self.client.post("/bills/settle/", {"charge_ids": [str(rent.id)]})
        payment = Payment.objects.get(direction=Payment.Direction.RECEIVE)
        response = self.client.post(
            f"/bills/payments/{payment.id}/edit/",
            {"date": "2026-06-20", "amount": "1200", "memo": "点错后调整"},
            HTTP_REFERER="/bills/?direction=income&month=2026-06",
        )
        self.assertEqual(response.status_code, 302)
        payment.refresh_from_db()
        rent.refresh_from_db()
        self.assertEqual(payment.amount, Decimal("1200.00"))
        self.assertEqual(payment.memo, "点错后调整")
        self.assertEqual(rent.balance, Decimal("1800.00"))
        self.assertEqual(rent.status, Charge.Status.PARTIAL)

    def test_settled_bill_payment_can_be_revoked(self):
        tenancy = sign_contract(
            room=self.room,
            person_data=self.person_data,
            start_date=date(2026, 6, 1),
            end_date=date(2027, 5, 31),
            monthly_rent=Decimal("3000.00"),
        )
        rent = tenancy.charges.get(category=Charge.Category.RENT)
        self.client.post("/bills/settle/", {"charge_ids": [str(rent.id)]})
        payment = Payment.objects.get(direction=Payment.Direction.RECEIVE)
        response = self.client.post(
            f"/payments/{payment.id}/delete/",
            HTTP_REFERER="/bills/?direction=income&month=2026-06",
        )
        self.assertEqual(response.status_code, 302)
        rent.refresh_from_db()
        self.assertEqual(rent.balance, Decimal("3000.00"))
        self.assertEqual(rent.status, Charge.Status.OPEN)

    def test_payable_bill_only_lists_fixed_categories(self):
        included = Charge.objects.create(
            direction=Charge.Direction.EXPENSE,
            category=Charge.Category.PROPERTY_RENT,
            due_date=date(2026, 6, 10),
            amount=Decimal("10000.00"),
            description="产权方房租",
        )
        Charge.objects.create(
            direction=Charge.Direction.EXPENSE,
            category=Charge.Category.REPAIR,
            due_date=date(2026, 6, 10),
            amount=Decimal("500.00"),
            description="临时维修",
        )
        response = self.client.get("/bills/?direction=expense&scope=month&month=2026-06")
        charge_ids = {charge_id for row in response.context["bill_rows"] for charge_id in row["charge_ids"]}
        self.assertEqual(charge_ids, {included.id})
        self.assertContains(response, "产权方房租")
        self.assertNotContains(response, "临时维修")

    def test_property_rent_rule_generates_quarterly_payables(self):
        rule = RecurringRule.objects.create(
            name="产权方季度房租",
            direction=Charge.Direction.EXPENSE,
            category=Charge.Category.PROPERTY_RENT,
            amount=Decimal("30000.00"),
            frequency=RecurringRule.Frequency.QUARTERLY,
            day_of_month=10,
            start_date=date(2026, 1, 1),
            active=True,
        )
        generate_due_charges(date(2026, 10, 31))
        due_dates = list(
            Charge.objects.filter(category=Charge.Category.PROPERTY_RENT)
            .order_by("due_date")
            .values_list("due_date", flat=True)
        )
        self.assertEqual(
            due_dates,
            [
                date(2026, 1, 10),
                date(2026, 4, 10),
                date(2026, 7, 10),
                date(2026, 10, 10),
            ],
        )
        first = Charge.objects.get(
            generated_key=f"recurring:{rule.id}:property_rent:20260110"
        )
        self.client.post("/bills/settle/", {"charge_ids": [str(first.id)]})
        rule.amount = Decimal("35000.00")
        rule.save(update_fields=["amount"])
        generate_due_charges(date(2026, 10, 31))
        first.refresh_from_db()
        self.assertEqual(first.amount, Decimal("30000.00"))

    def test_repeated_charge_generation_does_not_update_unchanged_records(self):
        tenancy = sign_contract(
            room=self.room,
            person_data=self.person_data,
            start_date=date(2026, 6, 1),
            end_date=date(2027, 5, 31),
            monthly_rent=Decimal("3000.00"),
        )
        generate_due_charges(date(2026, 6, 30))
        with CaptureQueriesContext(connection) as queries:
            generate_due_charges(date(2026, 6, 30))
        business_updates = [
            query["sql"]
            for query in queries
            if query["sql"].lstrip().upper().startswith("UPDATE")
            and any(table in query["sql"] for table in ['"core_charge"', '"core_room"', '"core_tenancy"'])
        ]
        self.assertEqual(business_updates, [])

    @patch("core.views.generate_due_charges", side_effect=OperationalError("database is locked"))
    def test_collection_page_survives_database_lock(self, _generate):
        response = self.client.get("/collections/?month=2026-06")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "数据库暂时被占用")

    def test_sign_contract_page_uses_grouped_contract_layout(self):
        response = self.client.get("/tenancies/new/")
        self.assertContains(response, "主租客")
        self.assertContains(response, "合同周期")
        self.assertContains(response, "租金与押金")
        self.assertContains(response, 'class="form-panel contract-form"')

    def test_collection_collect_post_marks_charge_paid(self):
        tenancy = sign_contract(
            room=self.room,
            person_data=self.person_data,
            start_date=date(2026, 6, 1),
            end_date=date(2027, 5, 31),
            monthly_rent=Decimal("3000.00"),
        )
        rent = tenancy.charges.get(category=Charge.Category.RENT)
        response = self.client.post(
            f"/collections/{rent.id}/collect/",
            HTTP_REFERER="/collections/?month=2026-06",
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/collections/?month=2026-06")
        rent.refresh_from_db()
        self.assertEqual(rent.balance, Decimal("0.00"))
        self.assertEqual(rent.status, Charge.Status.PAID)

    def test_collection_collect_post_accepts_partial_amount(self):
        tenancy = sign_contract(
            room=self.room,
            person_data=self.person_data,
            start_date=date(2026, 6, 1),
            end_date=date(2027, 5, 31),
            monthly_rent=Decimal("3000.00"),
        )
        rent = tenancy.charges.get(category=Charge.Category.RENT)
        response = self.client.post(
            f"/collections/{rent.id}/collect/",
            {"amount": "1000"},
            HTTP_REFERER="/collections/?month=2026-06",
        )
        self.assertEqual(response.status_code, 302)
        rent.refresh_from_db()
        self.assertEqual(rent.balance, Decimal("2000.00"))
        self.assertEqual(rent.status, Charge.Status.PARTIAL)

    def test_payment_delete_returns_to_ledger_referer(self):
        payment = record_payment(
            direction=Payment.Direction.RECEIVE,
            category=Payment.Category.OTHER,
            date=date(2026, 6, 1),
            amount=Decimal("123.00"),
            room=self.room,
            memo="manual payment delete",
        )
        response = self.client.post(
            f"/payments/{payment.id}/delete/",
            HTTP_REFERER="/charges/?status=payments",
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/charges/?status=payments")

    def test_charge_delete_returns_to_ledger_referer(self):
        charge = Charge.objects.create(
            direction=Charge.Direction.INCOME,
            category=Charge.Category.OTHER,
            room=self.room,
            due_date=date(2026, 6, 1),
            amount=Decimal("123.00"),
            description="manual charge delete",
        )
        response = self.client.post(
            f"/charges/{charge.id}/delete/",
            HTTP_REFERER="/charges/?status=open",
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/charges/?status=open")

# Create your tests here.
