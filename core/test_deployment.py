import hashlib
import sqlite3
import tempfile
from contextlib import closing
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, TestCase

from .test_support import configure_test_account


class RuntimePreflightTests(TestCase):
    def setUp(self):
        configure_test_account(self, login=False)

    def test_preflight_requires_accounts_and_valid_fonts_without_writes(self):
        original = self.account_file.read_bytes()
        call_command("check_runtime", stdout=StringIO())
        self.assertEqual(self.account_file.read_bytes(), original)
        with patch("core.management.commands.check_runtime.chinese_font_path", return_value=None):
            with self.assertRaisesMessage(CommandError, "缺少中文字体"):
                call_command("check_runtime", stdout=StringIO())
        self.account_file.unlink()
        with self.assertRaisesMessage(CommandError, "首次使用"):
            call_command("check_runtime", stdout=StringIO())

    def test_production_preflight_refuses_missing_database_instead_of_creating_one(self):
        missing = self.account_file.parent / "missing.sqlite3"
        with patch.dict(settings.DATABASES["default"], NAME=str(missing)):
            with self.assertRaisesMessage(CommandError, "数据库不存在"):
                call_command("check_runtime", require_db=True, stdout=StringIO())
        self.assertFalse(missing.exists())


class DatabaseBackupTests(SimpleTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.source = self.root / "source.sqlite3"
        self.output = self.root / "backups" / "snapshot.sqlite3"
        with closing(sqlite3.connect(self.source)) as connection:
            connection.execute("CREATE TABLE rooms (number TEXT)")
            connection.execute("INSERT INTO rooms VALUES ('A01')")
            connection.commit()
        self.original_hash = hashlib.sha256(self.source.read_bytes()).hexdigest()

    def backup(self, **options):
        with patch.dict(settings.DATABASES["default"], NAME=str(self.source)):
            call_command("backup_database", output=self.output, stdout=StringIO(), **options)

    def test_backup_is_readable_and_preserves_source_database(self):
        self.backup()
        with closing(sqlite3.connect(self.output)) as connection:
            self.assertEqual(connection.execute("SELECT number FROM rooms").fetchall(), [("A01",)])
            self.assertEqual(connection.execute("PRAGMA quick_check").fetchone(), ("ok",))
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(), self.original_hash)
        self.assertEqual(list(self.output.parent.glob(".backup-*")), [])

    def test_dry_run_creates_no_files_and_existing_backup_is_never_overwritten(self):
        self.backup(dry_run=True)
        self.assertFalse(self.output.parent.exists())
        self.output.parent.mkdir()
        self.output.write_bytes(b"existing-backup")
        with self.assertRaises(CommandError):
            self.backup()
        self.assertEqual(self.output.read_bytes(), b"existing-backup")

    def test_failed_backup_removes_partial_file(self):
        with patch("core.management.commands.backup_database.os.replace", side_effect=OSError("blocked")):
            with self.assertRaises(CommandError):
                self.backup()
        self.assertFalse(self.output.exists())
        self.assertEqual(list(self.output.parent.glob(".backup-*")), [])
