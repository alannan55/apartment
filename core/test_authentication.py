import json
from io import StringIO
from unittest.mock import patch

from django.contrib.auth import SESSION_KEY, authenticate, get_user_model
from django.contrib.auth.hashers import check_password, make_password
from django.core.cache import cache
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from .accounts import AccountFileError, load_accounts, save_accounts
from .models import Payment, Room
from .test_support import configure_test_account
from .urls import urlpatterns as business_urls


PASSWORD = "test-only-password-2026!"


class AuthenticationTests(TestCase):
    def setUp(self):
        configure_test_account(self, login=False)
        cache.clear()
        self.addCleanup(cache.clear)

    def login(self, client=None, **extra):
        return (client or self.client).post("/login/", {
            "username": "test-operator", "password": PASSWORD, **extra,
        })

    def test_all_business_urls_require_login_including_exports_and_post_actions(self):
        for route in business_urls:
            args = [1 if converter.__class__.__name__ == "IntConverter" else "test" for converter in route.pattern.converters.values()]
            url = reverse(route.name, args=args)
            for method in ("get", "post"):
                with self.subTest(url=url, method=method):
                    response = getattr(self.client, method)(url)
                    self.assertEqual(response.status_code, 302)
                    self.assertTrue(response.url.startswith("/login/?next="))
        self.assertFalse(Room.objects.exists())
        self.assertFalse(Payment.objects.exists())

    def test_login_returns_to_original_page_and_displays_account(self):
        response = self.login(next="/rooms/?q=A01")
        self.assertEqual(response.url, "/rooms/?q=A01")
        self.assertIn(SESSION_KEY, self.client.session)
        page = self.client.get("/rooms/")
        self.assertContains(page, "test-operator")
        self.assertContains(page, 'action="/logout/"')
        self.assertEqual(page["Cache-Control"], "private, no-store")

    def test_wrong_password_and_unknown_username_do_not_login(self):
        for data in ({"username": "test-operator", "password": "wrong"},
                     {"username": "unknown", "password": PASSWORD}):
            response = self.client.post("/login/", data)
            self.assertEqual(response.status_code, 200)
            self.assertNotIn(SESSION_KEY, self.client.session)
            self.assertContains(response, "账号或密码不正确")

    def test_external_next_is_rejected(self):
        for target in ("https://example.com/", "//example.com/", "http://testserver/rooms/"):
            self.client.logout()
            response = self.client.post("/login/", {"username": "test-operator", "password": PASSWORD, "next": target}, secure=True)
            self.assertEqual(response.url, "/")

    def test_already_logged_in_login_page_redirects(self):
        self.login()
        self.assertRedirects(self.client.get("/login/"), "/")

    def test_logout_is_post_only_and_removes_access(self):
        self.login()
        self.assertEqual(self.client.get("/logout/").status_code, 405)
        self.assertRedirects(self.client.post("/logout/"), "/login/")
        self.assertEqual(self.client.get("/exports/police.xlsx").status_code, 302)

    def test_login_and_logout_enforce_csrf(self):
        client = Client(enforce_csrf_checks=True)
        self.assertEqual(client.post("/login/", {"username": "test-operator", "password": PASSWORD}).status_code, 403)
        client.get("/login/")
        token = client.cookies["csrftoken"].value
        self.assertEqual(client.post("/login/", {"username": "test-operator", "password": PASSWORD, "csrfmiddlewaretoken": token}).status_code, 302)
        self.assertEqual(client.post("/logout/").status_code, 403)
        token = client.cookies["csrftoken"].value
        self.assertEqual(client.post("/logout/", {"csrfmiddlewaretoken": token}).status_code, 302)

    @override_settings(
        ALLOWED_HOSTS=["alan-stream.duckdns.org"],
        APARTMENT_TRUST_PROXY=True,
        SECURE_PROXY_SSL_HEADER=("HTTP_X_FORWARDED_PROTO", "https"),
        USE_X_FORWARDED_HOST=True,
        SECURE_SSL_REDIRECT=True,
        SESSION_COOKIE_SECURE=True,
        CSRF_COOKIE_SECURE=True,
        CSRF_TRUSTED_ORIGINS=["https://alan-stream.duckdns.org:8443"],
    )
    def test_https_gateway_preserves_nonstandard_port_and_csrf_origin(self):
        client = Client(enforce_csrf_checks=True)
        headers = {
            "HTTP_X_FORWARDED_PROTO": "https",
            "HTTP_X_FORWARDED_HOST": "alan-stream.duckdns.org:8443",
            "HTTP_X_FORWARDED_FOR": "198.51.100.10",
        }
        response = client.get("/login/", **headers)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(client.cookies["csrftoken"]["secure"])
        response = client.post("/login/", {
            "username": "test-operator", "password": PASSWORD,
            "csrfmiddlewaretoken": client.cookies["csrftoken"].value,
        }, HTTP_ORIGIN="https://alan-stream.duckdns.org:8443", **headers)
        self.assertEqual(response.status_code, 302)
        self.assertTrue(client.cookies["sessionid"]["secure"])
        self.assertEqual(client.get("/rooms/", **headers).status_code, 200)
        self.assertEqual(client.get("/rooms/", HTTP_X_FORWARDED_HOST="wrong.example", HTTP_X_FORWARDED_PROTO="https").status_code, 400)

    def test_disabling_or_removing_account_revokes_session_without_restart(self):
        for remove in (False, True):
            configure = {"users": [{"username": "test-operator", "password_hash": self.account_user.password}]}
            self.account_file.write_text(json.dumps(configure), encoding="utf-8")
            self.login()
            accounts = load_accounts()
            if remove:
                accounts.pop("test-operator")
            else:
                accounts["test-operator"]["is_active"] = False
            save_accounts(accounts)
            self.assertEqual(self.client.get("/rooms/").status_code, 302)
            self.assertNotIn(SESSION_KEY, self.client.session)
            self.assertIsNone(authenticate(username="test-operator", password=PASSWORD))

    def test_password_change_revokes_old_session_and_old_password(self):
        self.login()
        accounts = load_accounts()
        accounts["test-operator"]["password_hash"] = make_password("New-account-password-2026!")
        save_accounts(accounts)
        self.assertEqual(self.client.get("/rooms/").status_code, 302)
        self.assertNotIn(SESSION_KEY, self.client.session)
        self.assertIsNone(authenticate(username="test-operator", password=PASSWORD))
        self.assertIsNotNone(authenticate(username="test-operator", password="New-account-password-2026!"))

    def test_password_change_revokes_other_sessions_after_new_login(self):
        other = Client()
        self.login(other)
        accounts = load_accounts()
        accounts["test-operator"]["password_hash"] = make_password("Other-new-password-2026!")
        save_accounts(accounts)
        self.client.post("/login/", {"username": "test-operator", "password": "Other-new-password-2026!"})
        self.assertEqual(other.get("/rooms/").status_code, 302)
        self.assertNotIn(SESSION_KEY, other.session)

    def test_missing_or_broken_file_fails_closed_even_for_existing_database_user(self):
        self.login()
        self.account_file.unlink()
        with self.assertLogs("core.accounts", level="ERROR"):
            self.assertEqual(self.client.get("/rooms/").status_code, 302)
            self.assertIsNone(authenticate(username="test-operator", password=PASSWORD))
        self.account_file.write_text("{broken", encoding="utf-8")
        with self.assertLogs("core.accounts", level="ERROR"):
            self.assertIsNone(authenticate(username="test-operator", password=PASSWORD))

    def test_superuser_privilege_comes_from_file_and_can_be_revoked(self):
        self.login()
        self.assertEqual(self.client.get("/admin/").status_code, 302)
        accounts = load_accounts()
        accounts["test-operator"]["is_superuser"] = True
        save_accounts(accounts)
        self.assertEqual(self.client.get("/admin/").status_code, 200)
        accounts["test-operator"]["is_superuser"] = False
        save_accounts(accounts)
        self.assertEqual(self.client.get("/admin/").status_code, 302)

    def test_login_limits_attempts_before_password_hashing(self):
        with patch("core.accounts.FileAccountBackend.authenticate", return_value=None) as backend:
            for _ in range(10):
                self.client.post("/login/", {"username": "test-operator", "password": "wrong"})
            response = self.client.post("/login/", {"username": "test-operator", "password": "wrong"})
            self.assertEqual(response.status_code, 429)
            self.assertEqual(response["Retry-After"], "300")
            self.assertEqual(backend.call_count, 10)

    def test_forwarded_ip_is_only_used_when_proxy_trust_is_enabled(self):
        with patch("core.accounts.FileAccountBackend.authenticate", return_value=None):
            for _ in range(10):
                self.client.post("/login/", HTTP_X_FORWARDED_FOR="198.51.100.1")
            self.assertEqual(self.client.post("/login/", HTTP_X_FORWARDED_FOR="198.51.100.2").status_code, 429)
            cache.clear()
            with override_settings(APARTMENT_TRUST_PROXY=True):
                for _ in range(10):
                    self.client.post("/login/", HTTP_X_FORWARDED_FOR="198.51.100.1")
                self.assertEqual(self.client.post("/login/", HTTP_X_FORWARDED_FOR="198.51.100.1").status_code, 429)
                self.assertEqual(self.client.post("/login/", HTTP_X_FORWARDED_FOR="198.51.100.2").status_code, 200)

    def test_media_is_authenticated_and_cannot_escape_upload_directory(self):
        root = self.account_file.parent / "media"
        root.mkdir()
        (root / "qr.png").write_bytes(b"test-image")
        with override_settings(MEDIA_ROOT=root):
            self.assertEqual(self.client.get("/media/qr.png").status_code, 302)
            self.login()
            response = self.client.get("/media/qr.png")
            self.assertEqual(b"".join(response.streaming_content), b"test-image")
            response.close()
            for path in ("/media/../accounts.json", "/media/%2e%2e/accounts.json", "/media/missing.png"):
                self.assertEqual(self.client.get(path).status_code, 404)

    def test_health_is_read_only_and_does_not_expose_information(self):
        response = self.client.get("/health/")
        self.assertEqual(response.status_code, 204)
        self.assertEqual(response.content, b"")
        save_accounts({})
        self.assertEqual(self.client.get("/health/").status_code, 503)

    def test_invalid_account_documents_are_rejected(self):
        record = {"username": "test", "password_hash": self.account_user.password}
        for document in ([], {"users": [record, record]}, {"users": [{**record, "is_active": "false"}]},
                         {"users": [{**record, "password_hash": "plaintext"}]},
                         {"users": [{**record, "username": " invalid "}]},
                         {"users": [{**record, "password": "secret"}]}):
            with self.subTest(document_type=type(document).__name__):
                self.account_file.write_text(json.dumps(document), encoding="utf-8")
                with self.assertRaises(AccountFileError):
                    load_accounts()
        self.account_file.write_text('{"users": [], "users": []}', encoding="utf-8")
        with self.assertRaises(AccountFileError):
            load_accounts()

    def test_account_command_stores_hash_and_does_not_modify_database(self):
        count = get_user_model().objects.count()
        with patch("core.management.commands.account.getpass.getpass", side_effect=["Strong-local-pass-2026!", "Strong-local-pass-2026!"]):
            call_command("account", "owner", admin=True, stdout=StringIO())
        account = load_accounts()["owner"]
        self.assertTrue(check_password("Strong-local-pass-2026!", account["password_hash"]))
        self.assertNotIn("Strong-local-pass-2026!", self.account_file.read_text(encoding="utf-8"))
        self.assertTrue(account["is_superuser"])
        self.assertEqual(get_user_model().objects.count(), count)
        call_command("account", "owner", disable=True, stdout=StringIO())
        self.assertFalse(load_accounts()["owner"]["is_active"])

    def test_account_command_dry_run_and_failed_input_preserve_file(self):
        original = self.account_file.read_bytes()
        call_command("account", "owner", dry_run=True, stdout=StringIO())
        self.assertEqual(self.account_file.read_bytes(), original)
        with patch("core.management.commands.account.getpass.getpass", side_effect=["Strong-pass-2026!", "different"]):
            with self.assertRaises(CommandError):
                call_command("account", "owner", stdout=StringIO())
        self.assertEqual(self.account_file.read_bytes(), original)
        self.assertEqual(list(self.account_file.parent.glob(".accounts-*")), [])

    def test_account_command_initializes_missing_file_and_preserves_existing_accounts(self):
        self.account_file.unlink()
        with patch("core.management.commands.account.getpass.getpass", side_effect=["Initial-account-2026!", "Initial-account-2026!"]):
            call_command("account", "owner", stdout=StringIO())
        with patch("core.management.commands.account.getpass.getpass", side_effect=["Second-account-2026!", "Second-account-2026!"]):
            call_command("account", "manager", stdout=StringIO())
        self.assertEqual(set(load_accounts()), {"owner", "manager"})

    def test_atomic_account_write_failure_preserves_original(self):
        original = self.account_file.read_bytes()
        with patch("core.accounts.os.replace", side_effect=OSError("blocked")):
            with self.assertRaises(OSError):
                save_accounts(load_accounts())
        self.assertEqual(self.account_file.read_bytes(), original)
        self.assertEqual(list(self.account_file.parent.glob(".accounts-*")), [])
