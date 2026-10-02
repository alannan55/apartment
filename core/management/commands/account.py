import getpass

from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import make_password
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.conf import settings

from core.accounts import AccountFileError, load_accounts, save_accounts


class Command(BaseCommand):
    help = "在 JSON 文件中创建/重设账号密码，或禁用账号；密码交互输入，不写入命令行。"
    requires_system_checks = []

    def add_arguments(self, parser):
        parser.add_argument("username")
        parser.add_argument("--disable", action="store_true", help="禁用账号及其已登录会话")
        parser.add_argument("--admin", action="store_true", help="允许访问 Django 管理后台")
        parser.add_argument("--dry-run", action="store_true", help="只验证账号文件和用户名，不写入")

    def handle(self, *args, **options):
        username = options["username"]
        user = get_user_model()(username=username)
        try:
            user._meta.get_field("username").clean(username, user)
            if username != username.strip():
                raise CommandError("用户名不能带首尾空格。")
            accounts = load_accounts() if settings.APARTMENT_ACCOUNTS_FILE.exists() else {}
            if options["disable"] and username not in accounts:
                raise CommandError("账号不存在，无法禁用。")
            if options["disable"] and options["admin"]:
                raise CommandError("--disable 和 --admin 不能同时使用。")
            if options["dry_run"]:
                self.stdout.write(f"检查通过：{settings.APARTMENT_ACCOUNTS_FILE}，未写入。")
                return
            if options["disable"]:
                accounts[username]["is_active"] = False
            else:
                password = getpass.getpass("密码：")
                confirmation = getpass.getpass("再次输入密码：")
                if password != confirmation:
                    raise CommandError("两次密码不一致，未写入。")
                validate_password(password, user)
                accounts[username] = {
                    "username": username,
                    "password_hash": make_password(password),
                    "is_active": True,
                    "is_superuser": options["admin"] or accounts.get(username, {}).get("is_superuser", False),
                }
            save_accounts(accounts)
        except (AccountFileError, ValidationError, OSError) as exc:
            raise CommandError(str(exc)) from exc
        except (KeyboardInterrupt, EOFError) as exc:
            self.stderr.write("已取消，账号文件未写入。")
            raise SystemExit(130) from exc
        self.stdout.write(self.style.SUCCESS(f"账号 {username} 已{'禁用' if options['disable'] else '保存'}：{settings.APARTMENT_ACCOUNTS_FILE}"))
