import os
import sqlite3
import tempfile
import time
from contextlib import closing
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone


class Command(BaseCommand):
    help = "在线生成一致性的 SQLite 备份，不复制运行中的数据库原文件；上传文件和账号需另行备份。"
    requires_system_checks = []

    def add_arguments(self, parser):
        parser.add_argument("--output", type=Path, help="新备份文件的路径；默认保存到数据库旁的 backups 目录")
        parser.add_argument("--dry-run", action="store_true", help="只执行只读预检，不创建备份")

    def handle(self, *args, **options):
        source_path = Path(settings.DATABASES["default"]["NAME"]).resolve()
        output = options["output"] or source_path.parent / "backups" / f"apartment-{timezone.localtime():%Y%m%d-%H%M%S-%f}.sqlite3"
        output = output.resolve()
        if not source_path.is_file():
            raise CommandError(f"源数据库不存在：{source_path}。")
        if output == source_path or output.exists():
            raise CommandError("备份目标已存在或与源数据库相同，未覆盖。")
        ancestor = output.parent
        while not ancestor.exists():
            ancestor = ancestor.parent
        if not ancestor.is_dir() or not os.access(ancestor, os.W_OK):
            raise CommandError(f"备份目标目录不可写：{ancestor}。")
        temporary = None
        try:
            with closing(sqlite3.connect(source_path.as_uri() + "?mode=ro", uri=True, timeout=30)) as source:
                source.execute("SELECT name FROM sqlite_master LIMIT 1").fetchone()
                if options["dry_run"]:
                    self.stdout.write(f"预检通过：{source_path} -> {output}，未创建备份。")
                    return
                output.parent.mkdir(parents=True, exist_ok=True)
                descriptor, temporary = tempfile.mkstemp(prefix=".backup-", suffix=".sqlite3", dir=output.parent)
                os.close(descriptor)
                self.stdout.write("正在生成一致性数据库备份...", ending="\n")
                self.stdout.flush()
                last_report = time.monotonic()

                def progress(status, remaining, total):
                    nonlocal last_report
                    now = time.monotonic()
                    if now - last_report >= 5:
                        self.stdout.write(f"备份进度：{total - remaining}/{total} 页。")
                        self.stdout.flush()
                        last_report = now

                with closing(sqlite3.connect(temporary)) as target:
                    source.backup(target, pages=128, progress=progress)
                    if target.execute("PRAGMA quick_check").fetchone() != ("ok",):
                        raise CommandError("备份完整性检查失败，未生成最终文件。")
            os.replace(temporary, output)
            temporary = None
        except (sqlite3.Error, OSError) as exc:
            raise CommandError(f"备份失败：{exc}") from exc
        except KeyboardInterrupt:
            self.stderr.write("备份已取消。")
            raise SystemExit(130)
        finally:
            if temporary and os.path.exists(temporary):
                os.unlink(temporary)
        self.stdout.write(self.style.SUCCESS(f"备份完成：{output}（{output.stat().st_size} 字节）。请复制到另一台设备或异地。"))
