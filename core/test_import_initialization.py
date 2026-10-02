from datetime import date
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase
from PIL import ImageDraw

from .exports import agent_room_status_image
from .forms import PersonForm, TenancyEditForm
from .models import ApartmentSettings, Charge, Person, Room, Tenancy
from .services import generate_due_charges, generate_heating_charges_until, generate_rent_charges_until, renew_tenancy


from .test_support import configure_test_account


class ImportInitializationTests(TestCase):
    def setUp(self):
        configure_test_account(self)
        self.room = Room.objects.create(number="A02", listing_price=3500)
        self.person = Person.objects.create(name="张凯", id_number=None)
        self.tenancy = Tenancy.objects.create(
            room=self.room, primary_person=self.person,
            start_date=date(2026, 5, 1), end_date=date(2027, 4, 30),
            monthly_rent=3400, deposit_amount=None, billing_enabled=False,
        )

    def test_names_without_identity_stay_separate_and_can_be_completed(self):
        other = Person.objects.create(name="凌文菁", id_number=None)
        form = PersonForm({"name": "张凯", "id_number": "", "phone": ""}, instance=self.person)
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        form = PersonForm({"name": "张凯", "id_number": "110101199001011234"}, instance=self.person)
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        other.refresh_from_db()
        self.assertIsNone(other.id_number)

    def test_unconfirmed_finances_never_generate_bills(self):
        with patch("django.utils.timezone.localdate", return_value=date(2026, 11, 20)):
            generate_due_charges(date(2026, 11, 30))
            generate_rent_charges_until(self.tenancy, date(2026, 11, 30))
            generate_heating_charges_until(self.tenancy, date(2026, 11, 30))
            self.assertEqual(self.client.get("/rooms/").status_code, 200)
            self.assertContains(self.client.get("/tenancies/"), "账务待补")
        self.assertFalse(Charge.objects.exists())
        self.assertIsNone(self.tenancy.deposit_amount)

    def test_enabling_billing_requires_deposit_and_start_date(self):
        form = TenancyEditForm({
            "room": self.room.pk, "primary_person": self.person.pk,
            "start_date": "2026-05-01", "end_date": "2027-04-30",
            "monthly_rent": "3400", "payment_cycle": "monthly", "status": "active",
            "billing_enabled": "on",
        }, instance=self.tenancy)
        self.assertFalse(form.is_valid())
        self.assertIn("deposit_amount", form.errors)
        self.assertIn("billing_start_date", form.errors)
        self.tenancy.billing_enabled = True
        self.tenancy.deposit_amount = Decimal("3500")
        self.tenancy.billing_start_date = date(2026, 10, 1)
        self.tenancy.save()
        generate_rent_charges_until(self.tenancy, date(2026, 10, 31))
        self.assertEqual(list(Charge.objects.values_list("period_start", "amount")), [(date(2026, 10, 1), Decimal("3400"))])

    def test_future_booking_becomes_current_without_enabling_finances(self):
        self.tenancy.start_date = date(2026, 10, 1)
        self.tenancy.status = Tenancy.Status.UPCOMING
        self.tenancy.save()
        with patch("django.utils.timezone.localdate", return_value=date(2026, 9, 30)):
            generate_due_charges(date(2026, 10, 31))
            self.assertIsNone(self.room.active_tenancy(date(2026, 9, 30)))
            self.assertContains(self.client.get(f"/rooms/{self.room.pk}/"), "预订入住")
        with patch("django.utils.timezone.localdate", return_value=date(2026, 10, 1)):
            generate_due_charges(date(2026, 10, 31))
            self.tenancy.refresh_from_db()
            self.assertEqual(self.tenancy.status, Tenancy.Status.ACTIVE)
            self.assertEqual(self.room.active_tenancy(date(2026, 10, 1)), self.tenancy)
        self.assertFalse(Charge.objects.exists())

    def test_renewal_keeps_unconfirmed_finances_disabled(self):
        renewed = renew_tenancy(self.tenancy, end_date=date(2028, 4, 30), monthly_rent=3400, payment_cycle="monthly")
        self.assertFalse(renewed.billing_enabled)
        self.assertIsNone(renewed.deposit_amount)

    def test_public_new_rates_do_not_change_internal_old_rates(self):
        Room.objects.create(number="A09", electricity_fee="1.55/度", heating_fee=400)
        ApartmentSettings.objects.create(pk=1, fee_defaults={"agent_fees": {"electricity_fee": "1.55/度", "heating_fee": "400"}})
        original_text = ImageDraw.ImageDraw.text
        with patch.object(ImageDraw.ImageDraw, "text", autospec=True, side_effect=original_text) as drawn:
            image = agent_room_status_image(date(2026, 9, 30))
        texts = [str(call.args[2]) for call in drawn.call_args_list]
        self.assertTrue(image.startswith(b"\x89PNG"))
        self.assertIn("电费", texts)
        self.assertIn("1.55/度", texts)
        self.assertIn("取暖费", texts)
        self.assertIn("400元/月", texts)
        self.assertFalse(any("1.4/度" in text or "380/月" in text for text in texts))
        self.room.refresh_from_db()
        self.assertEqual(self.room.heating_fee, 380)
        self.assertEqual(self.room.electricity_fee, "1.4/度")

    def test_fee_settings_save_public_rates_without_changing_rooms(self):
        response = self.client.post("/more/fees/", {
            "water_fee": "9.5/吨", "electricity_fee": "1.55/度",
            "property_fee": "免", "internet_fee": "免", "heating_fee": "400", "parking_fee": "150",
            "agent_electricity_fee": "1.55/度", "agent_heating_fee": "400",
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Decimal(ApartmentSettings.objects.get(pk=1).fee_defaults["agent_fees"]["heating_fee"]), Decimal("400"))
        self.room.refresh_from_db()
        self.assertEqual(self.room.heating_fee, 380)
