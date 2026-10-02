"""The account file is authoritative; Django users only provide sessions/admin support."""

import json
import logging
import os
import tempfile
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.backends import ModelBackend
from django.contrib.auth.hashers import check_password, identify_hasher, make_password
from django.core.exceptions import ValidationError


logger = logging.getLogger(__name__)


class AccountFileError(ValueError):
    pass


def _unique_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise AccountFileError(f"账号文件存在重复字段：{key}。")
        result[key] = value
    return result


def validate_accounts(document):
    if not isinstance(document, dict) or set(document) != {"users"} or not isinstance(document["users"], list):
        raise AccountFileError('账号文件格式应为 {"users": [...]}。')
    accounts = {}
    username_field = get_user_model()._meta.get_field("username")
    for record in document["users"]:
        if not isinstance(record, dict) or set(record) - {"username", "password_hash", "is_active", "is_superuser"}:
            raise AccountFileError("账号只能包含 username、password_hash、is_active、is_superuser。")
        username = record.get("username")
        if not isinstance(username, str) or not username or username != username.strip():
            raise AccountFileError("账号名称不能为空或带首尾空格。")
        try:
            username_field.clean(username, None)
        except ValidationError as exc:
            raise AccountFileError("账号名称不符合 Django 的用户名规则。") from exc
        if username in accounts:
            raise AccountFileError(f"账号重复：{username}。")
        password_hash = record.get("password_hash")
        try:
            hasher = identify_hasher(password_hash)
            hasher.decode(password_hash)
        except (TypeError, ValueError, AttributeError, KeyError) as exc:
            raise AccountFileError(f"账号 {username} 的 password_hash 无效，请使用 account 命令设置密码。") from exc
        for flag in ("is_active", "is_superuser"):
            if flag in record and type(record[flag]) is not bool:
                raise AccountFileError(f"账号 {username} 的 {flag} 必须是 true 或 false。")
        accounts[username] = {
            "username": username,
            "password_hash": password_hash,
            "is_active": record.get("is_active", True),
            "is_superuser": record.get("is_superuser", False),
        }
    return accounts


def load_accounts():
    path = Path(settings.APARTMENT_ACCOUNTS_FILE)
    try:
        document = json.loads(path.read_text(encoding="utf-8-sig"), object_pairs_hook=_unique_keys)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AccountFileError(f"无法读取账号文件 {path}，请检查文件是否存在、权限及 JSON 格式。") from exc
    return validate_accounts(document)


def save_accounts(accounts):
    """Replace atomically so readers never observe a partially written JSON file."""
    path = Path(settings.APARTMENT_ACCOUNTS_FILE)
    document = {"users": list(accounts.values())}
    validate_accounts(document)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".accounts-", suffix=".json", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(document, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class FileAccountBackend(ModelBackend):
    def authenticate(self, request, username=None, password=None, **kwargs):
        if username is None or password is None:
            return None
        try:
            account = load_accounts().get(username)
        except AccountFileError as exc:
            logger.error("账号配置不可用：%s", exc)
            return None
        if account is None:
            # Use Django's normal password cost for unknown usernames too.
            make_password(password)
            return None
        try:
            valid = check_password(password, account["password_hash"])
        except (ValueError, TypeError):
            return None
        if not valid or not account["is_active"]:
            return None
        user, _ = get_user_model().objects.update_or_create(
            username=username,
            defaults={
                "password": account["password_hash"],
                "is_active": True,
                "is_staff": account["is_superuser"],
                "is_superuser": account["is_superuser"],
            },
        )
        return user

    def get_user(self, user_id):
        try:
            user = get_user_model().objects.get(pk=user_id)
            account = load_accounts().get(user.username)
        except get_user_model().DoesNotExist:
            return None
        except AccountFileError as exc:
            logger.error("账号配置不可用：%s", exc)
            return None
        if not account or not account["is_active"] or user.password != account["password_hash"]:
            return None
        # Check the file on every request so revocation does not need a restart.
        # No database writes are necessary for an ordinary authenticated request.
        user.is_active = True
        user.is_staff = user.is_superuser = account["is_superuser"]
        return user
