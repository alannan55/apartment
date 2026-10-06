from datetime import date
from decimal import Decimal
from io import BytesIO
from itertools import product
from unittest.mock import patch

from django.test import TestCase
from PIL import Image

from .exports import agent_room_status_data, agent_room_status_image
from .models import ApartmentSettings, Charge, Person, Room, Tenancy


from .test_support import configure_test_account


class AgentDisplayTests(TestCase):
    def setUp(self):
        configure_test_account(self)
        self.clock = patch("django.utils.timezone.localdate", return_value=date(2026, 10, 1))
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.room = Room.objects.create(number="A01", listing_price=3000, room_password="123456#")

    def payload(self, **overrides):
        data = {
            "agent_water_fee": "10元/吨", "agent_electricity_fee": "1.5元/度",
            "agent_property_fee": "免", "agent_internet_fee": "60元/月",
            "agent_heating_fee": "400", "agent_parking_fee": "180",
            "contact-name": "张小姐", "contact-phone": "18518699513",
            "prices-TOTAL_FORMS": "1", "prices-INITIAL_FORMS": "1",
            "prices-0-id": str(self.room.pk), "prices-0-listing_price": "3250.50",
            "prices-0-commission_base": "1850.50",
        }
        data.update(overrides)
        return data

    def lease(self, room=None, **overrides):
        values = dict(room=room or self.room, primary_person=Person.objects.create(name="租客"),
                      start_date=date(2026, 1, 1), end_date=date(2026, 10, 20),
                      monthly_rent=3000, billing_enabled=False)
        values.update(overrides)
        return Tenancy.objects.create(**values)

    def test_configuration_saves_all_fees_and_price_without_changing_billing(self):
        tenancy = self.lease()
        charge = Charge.objects.create(room=self.room, tenancy=tenancy, direction="income",
                                       category="rent", due_date=date(2026, 10, 1), amount=3000)
        ApartmentSettings.objects.create(pk=1, fee_defaults={"heating_fee": "380"})
        response = self.client.post("/exports/agent-settings/", self.payload())
        self.assertRedirects(response, "/exports/agent-preview/")
        self.room.refresh_from_db()
        tenancy.refresh_from_db()
        charge.refresh_from_db()
        self.assertEqual(self.room.listing_price, Decimal("3250.50"))
        self.assertEqual(self.room.commission_base, Decimal("1850.50"))
        self.assertEqual(self.room.heating_fee, 380)
        self.assertEqual(self.room.parking_fee, 150)
        self.assertEqual(tenancy.monthly_rent, 3000)
        self.assertEqual(tenancy.commission_base, 2000)
        self.assertEqual(charge.amount, 3000)
        defaults = ApartmentSettings.objects.get(pk=1).fee_defaults
        self.assertEqual(defaults["heating_fee"], "380")
        self.assertEqual(len(defaults["agent_fees"]), 7)
        response = self.client.get("/exports/agent-settings/")
        self.assertEqual(response.context["form"].initial["agent_internet_fee"], "60元/月")
        self.assertContains(response, "3250.50")
        fees = {f["label"]: f["value"] for f in agent_room_status_data()["fees"]}
        self.assertEqual(fees["水费"], "10元/吨")
        self.assertEqual(fees["停车费"], "180元/月")

    def test_invalid_price_or_fee_saves_neither_settings_nor_prices(self):
        for field in ("agent_heating_fee", "agent_parking_fee", "prices-0-listing_price", "prices-0-commission_base"):
            with self.subTest(field=field):
                response = self.client.post("/exports/agent-settings/", self.payload(**{field: "-1"}))
                self.assertEqual(response.status_code, 200)
                self.assertFalse(ApartmentSettings.objects.exists())
                self.room.refresh_from_db()
                self.assertEqual(self.room.listing_price, 3000)
                self.assertEqual(self.room.commission_base, 2000)
        response = self.client.post("/exports/agent-settings/", {"agent_water_fee": "10元/吨"})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(ApartmentSettings.objects.exists())

    def test_multiple_room_prices_are_saved_together(self):
        other = Room.objects.create(number="B01", listing_price=2000)
        response = self.client.post("/exports/agent-settings/", self.payload(**{
            "prices-TOTAL_FORMS": "2", "prices-INITIAL_FORMS": "2",
            "prices-1-id": str(other.pk), "prices-1-listing_price": "",
            "prices-1-commission_base": "0",
        }))
        self.assertRedirects(response, "/exports/agent-preview/")
        self.room.refresh_from_db()
        other.refresh_from_db()
        self.assertEqual(self.room.listing_price, Decimal("3250.50"))
        self.assertIsNone(other.listing_price)
        self.assertEqual(other.commission_base, 0)
        self.assertEqual(agent_room_status_data()["room_rows"][1]["commission"], "佣金基数 ¥0")

    def test_blank_fee_uses_room_standard_and_zero_is_kept(self):
        self.client.post("/exports/agent-settings/", self.payload(agent_water_fee="", agent_heating_fee="0", **{"prices-0-listing_price": ""}))
        row = agent_room_status_data()["room_rows"][0]
        self.assertEqual(row["price"], "价格面议")
        fees = {f["label"]: f["value"] for f in row["fees"]}
        self.assertEqual(fees["水费"], "9.5/吨")
        self.assertEqual(fees["取暖费"], "0元/月")
        self.client.post("/exports/agent-settings/", self.payload(**{"prices-0-listing_price": "0"}))
        self.assertEqual(agent_room_status_data()["room_rows"][0]["price"], "¥0")

    def test_actual_fee_editor_preserves_other_display_overrides(self):
        self.client.post("/exports/agent-settings/", self.payload())
        self.client.post("/more/fees/", {"water_fee": "9.5/吨", "electricity_fee": "1.2/度",
                          "property_fee": "免", "internet_fee": "免", "heating_fee": "380",
                          "parking_fee": "150", "agent_electricity_fee": "1.6/度", "agent_heating_fee": "410"})
        fees = ApartmentSettings.objects.get(pk=1).fee_defaults["agent_fees"]
        self.assertEqual(fees["water_fee"], "10元/吨")
        self.assertEqual(fees["parking_fee"], "180")
        self.assertEqual(fees["electricity_fee"], "1.6/度")

    def test_zero_means_free_and_parking_supports_monthly_and_annual_rates(self):
        response = self.client.post("/exports/agent-settings/", self.payload(
            agent_property_fee="0", agent_internet_fee="0", agent_parking_fee="150", agent_parking_annual_fee="1440",
        ))
        self.assertRedirects(response, "/exports/agent-preview/")
        fees = {f["label"]: f["value"] for f in agent_room_status_data()["fees"]}
        self.assertEqual(fees["物业费"], "免费")
        self.assertEqual(fees["网费"], "免费")
        self.assertEqual(fees["停车费"], "150元/月 · 1440元/包年")
        self.assertEqual(self.client.get("/exports/agent-settings/").context["form"].initial["agent_parking_annual_fee"], "1440")
        self.client.post("/exports/agent-settings/", self.payload(agent_parking_annual_fee=""))
        self.assertEqual(self.client.get("/exports/agent-settings/").context["form"].initial["agent_parking_annual_fee"], "")
        self.assertNotIn("包年", str(agent_room_status_data()["fees"]))

    def test_price_editor_shows_room_features_and_share_button_is_always_present(self):
        self.room.orientation = "东北"
        self.room.area = Decimal("42.50")
        self.room.floor = "2层"
        self.room.save()
        response = self.client.get("/exports/agent-settings/")
        for feature in ["东北", "2层", "42.50㎡"]:
            self.assertContains(response, feature)
        response = self.client.get("/exports/agent-preview/")
        self.assertContains(response, "分享图片到微信")
        self.assertContains(response, "复制房态图片")
        self.assertNotContains(response, "disabled hidden")
        self.lease()
        self.assertContains(self.client.get("/exports/agent-settings/"), "合同即将到期")
        self.room.refresh_from_db()
        self.assertEqual(self.room.status, "vacant")

    def test_publication_excludes_expired_unchecked_out_and_reserved_rooms(self):
        self.lease(end_date=date(2026, 9, 30))
        expiring = Room.objects.create(number="A02", room_password="private-password")
        tenancy = self.lease(room=expiring)
        vacant = Room.objects.create(number="B01", room_password="public-password")
        reserved = Room.objects.create(number="B02")
        self.lease(room=reserved, start_date=date(2026, 10, 10), end_date=date(2027, 10, 9), status="upcoming")
        Room.objects.create(number="B03", status="maintenance")
        data = agent_room_status_data()
        self.assertEqual([r["number"] for r in data["room_rows"]], [vacant.number, expiring.number])
        self.assertEqual(data["vacant_count"], 1)
        self.assertEqual(data["expiring_count"], 1)
        self.assertIn("public-password", data["room_rows"][0]["note"])
        self.assertNotIn("private-password", str(data))
        self.lease(room=expiring, start_date=date(2026, 10, 21), end_date=date(2027, 10, 20),
                   status="upcoming", previous_tenancy=tenancy)
        self.assertEqual([r["number"] for r in agent_room_status_data()["room_rows"]], [vacant.number])

    def test_different_fees_use_overrides_in_each_room(self):
        Room.objects.create(number="A02", water_fee="20元/吨", parking_fee=250)
        ApartmentSettings.objects.create(pk=1, fee_defaults={"agent_fees": {"internet_fee": "80元/月", "parking_fee": "200"}})
        data = agent_room_status_data()
        self.assertFalse(data["uniform_fees"])
        for row in data["room_rows"]:
            fees = {f["label"]: f["value"] for f in row["fees"]}
            self.assertEqual(fees["网费"], "80元/月")
            self.assertEqual(fees["停车费"], "200元/月")
        self.assertEqual(Image.open(BytesIO(agent_room_status_image())).width, 1080)

    def test_preview_is_inline_download_is_attachment_and_empty_state_renders(self):
        self.assertContains(self.client.get("/exports/agent-preview/"), "费用与挂牌价")
        self.assertContains(self.client.get("/exports/agent-settings/"), "prices-0-listing_price")
        preview = self.client.get("/exports/agent-room-status.png?preview=1")
        self.assertEqual(preview["Content-Disposition"], "inline")
        self.assertEqual(preview["Cache-Control"], "no-store")
        download = self.client.get("/exports/agent-room-status.png")
        self.assertTrue(download["Content-Disposition"].startswith("attachment"))
        self.room.delete()
        self.assertContains(self.client.get("/exports/agent-preview/"), "0间空房")
        image = Image.open(BytesIO(agent_room_status_image()))
        image.verify()

    def test_image_options_control_rendered_content_in_all_combinations(self):
        for commission, password, contact in product((False, True), repeat=3):
            with self.subTest(commission=commission, password=password, contact=contact):
                options = dict(include_commission=commission, include_password=password, include_contact=contact)
                data = agent_room_status_data(**options)
                self.assertEqual(bool(data["room_rows"][0]["commission"]), commission)
                self.assertEqual("123456#" in str(data), password)
                self.assertEqual(data["contact_text"], "张小姐 18518699513" if contact else "")
                with patch("core.exports.ImageDraw.ImageDraw.text") as draw_text:
                    image = Image.open(BytesIO(agent_room_status_image(**options)))
                    image.verify()
                rendered_text = "\n".join(str(call.args[1]) for call in draw_text.call_args_list)
                self.assertEqual("佣金基数 ¥2000" in rendered_text, commission)
                self.assertEqual("看房密码：123456#" in rendered_text, password)
                self.assertEqual("联系看房：张小姐 18518699513" in rendered_text, contact)
                self.assertIn("¥3000", rendered_text)

    def test_preview_download_and_share_use_same_options(self):
        for commission, password, contact in product((0, 1), repeat=3):
            with self.subTest(commission=commission, password=password, contact=contact):
                query = f"include_commission={commission}&include_password={password}&include_contact={contact}"
                response = self.client.get(f"/exports/agent-preview/?{query}")
                self.assertEqual(response.context["include_commission"], bool(commission))
                self.assertEqual(response.context["include_password"], bool(password))
                self.assertEqual(response.context["include_contact"], bool(contact))
                image_url = f"/exports/agent-room-status.png?{query}"
                self.assertEqual(response.context["agent_image_url"], image_url)
                html_url = image_url.replace("&", "&amp;")
                self.assertContains(response, f'href="{html_url}"')
                self.assertContains(response, f'src="{html_url}&amp;preview=1"')
                self.assertContains(response, f'data-image-url="{html_url}&amp;preview=1"')
                with patch("core.exports.agent_room_status_image", return_value=b"png") as renderer:
                    exported = self.client.get(image_url)
                    renderer.assert_called_once_with(None, include_commission=bool(commission), include_password=bool(password), include_contact=bool(contact))
                    self.assertEqual(exported.content, b"png")
        default = self.client.get("/exports/agent-preview/")
        self.assertTrue(default.context["include_commission"])
        self.assertTrue(default.context["include_password"])
        self.assertTrue(default.context["include_contact"])

    def test_contact_defaults_customization_and_blank_values(self):
        response = self.client.get("/exports/agent-settings/")
        self.assertContains(response, 'value="张小姐"')
        self.assertContains(response, 'value="18518699513"')
        response = self.client.post("/exports/agent-settings/", self.payload(**{
            "contact-name": "李先生", "contact-phone": "13800138000",
        }))
        self.assertRedirects(response, "/exports/agent-preview/")
        self.assertEqual(agent_room_status_data()["contact_text"], "李先生 13800138000")
        self.assertEqual(agent_room_status_data(include_contact=False)["contact_text"], "")
        response = self.client.get("/exports/agent-settings/")
        self.assertContains(response, 'value="李先生"')
        self.assertContains(response, 'value="13800138000"')
        self.client.post("/more/fees/", {"water_fee": "9.5/吨", "electricity_fee": "1.2/度",
                          "property_fee": "免", "internet_fee": "免", "heating_fee": "380", "parking_fee": "150"})
        self.assertEqual(agent_room_status_data()["contact_text"], "李先生 13800138000")
        self.client.post("/exports/agent-settings/", self.payload(**{"contact-name": "", "contact-phone": ""}))
        self.assertEqual(agent_room_status_data()["contact_text"], "")
        with patch("core.exports.ImageDraw.ImageDraw.text") as draw_text:
            agent_room_status_image()
        self.assertNotIn("联系看房", "\n".join(str(call.args[1]) for call in draw_text.call_args_list))

    def test_invalid_contact_does_not_save_partial_settings_or_prices(self):
        response = self.client.post("/exports/agent-settings/", self.payload(**{"contact-name": "张" * 81}))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["contact_form"].errors)
        self.assertFalse(ApartmentSettings.objects.exists())
        self.room.refresh_from_db()
        self.assertEqual(self.room.listing_price, 3000)
