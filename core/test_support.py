import json
import tempfile
from pathlib import Path

from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import make_password
from django.test import Client, override_settings


_password_hash = None


def configure_test_account(test_case, *, login=True):
    """Exercise the real file backend using a temporary account and test database."""
    global _password_hash
    if _password_hash is None:
        _password_hash = make_password("test-only-password-2026!")
    directory = tempfile.TemporaryDirectory()
    test_case.addCleanup(directory.cleanup)
    test_case.account_file = Path(directory.name) / "accounts.json"
    test_case.account_file.write_text(json.dumps({"users": [{
        "username": "test-operator", "password_hash": _password_hash,
        "is_active": True, "is_superuser": False,
    }]}), encoding="utf-8")
    overrides = override_settings(
        APARTMENT_ACCOUNTS_FILE=test_case.account_file,
        STORAGES={
            "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
            "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
        },
        WHITENOISE_USE_FINDERS=True,
        STATIC_ROOT=None,
    )
    overrides.enable()
    test_case.addCleanup(overrides.disable)
    test_case.account_user = get_user_model().objects.create(username="test-operator", password=_password_hash)
    if login:
        test_case.client.force_login(test_case.account_user, backend="core.accounts.FileAccountBackend")


def authenticated_client(test_case):
    client = Client()
    client.force_login(test_case.account_user, backend="core.accounts.FileAccountBackend")
    return client
