import os
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from PIL import ImageFont

from core.accounts import AccountFileError, load_accounts
from core.exports import chinese_font_path


class Command(BaseCommand):
    help = "只读检查账号、数据目录和中文字体；不会安装依赖、迁移或初始化数据库。"
    requires_system_checks = []

    def add_arguments(self, parser):
        parser.add_argument("--require-db", action="store_true", help="要求数据库与上传目录已存在，防止挂载错误后启动空库")

    def handle(self, *args, **options):
        try:
            accounts = load_accounts()
        except AccountFileError as exc:
            raise CommandError(f"{exc} 首次使用请运行 python manage.py account 你的账号。") from exc
        active_count = sum(account["is_active"] for account in accounts.values())
        if not active_count:
            raise CommandError("账号文件没有启用的账号，请先使用 account 命令创建账号。")
        db_path = Path(settings.DATABASES["default"]["NAME"])
        if not db_path.parent.is_dir() or not os.access(db_path.parent, os.W_OK):
            raise CommandError(f"数据库目录不存在或不可写：{db_path.parent}。请检查硬盘挂载和目录权限。")
        if options["require_db"]:
            if not db_path.is_file():
                raise CommandError(f"数据库不存在：{db_path}。请复制已有数据库；新安装需明确运行 migrate 初始化。")
            if not os.access(db_path, os.R_OK | os.W_OK):
                raise CommandError(f"数据库不可读写：{db_path}。请检查文件权限。")
            with db_path.open("rb") as stream:
                if stream.read(16) != b"SQLite format 3\x00":
                    raise CommandError(f"不是有效的 SQLite 数据库：{db_path}。")
            if not settings.MEDIA_ROOT.is_dir() or not os.access(settings.MEDIA_ROOT, os.W_OK):
                raise CommandError(f"上传目录不存在或不可写：{settings.MEDIA_ROOT}。请检查挂载和目录权限。")
        for bold in (False, True):
            font = chinese_font_path(bold)
            if font is None:
                raise CommandError("缺少中文字体。Linux 请安装 fonts-noto-cjk，Docker 镜像已包含。")
            try:
                ImageFont.truetype(str(font), 24)
            except OSError as exc:
                raise CommandError(f"中文字体无法加载：{font}。") from exc
        self.stdout.write(self.style.SUCCESS(f"运行检查通过：{active_count} 个启用账号；数据库 {db_path}；中文字体可用。"))
