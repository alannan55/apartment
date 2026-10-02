"""Validate mounts/accounts before any schema writes; forward the service's signals."""

import os
import subprocess
import sys


def main():
    command = sys.argv[1:]
    if not command:
        raise SystemExit("缺少启动命令。")
    # Explicit management commands must work before the first account/database exists.
    if command[0] == "gunicorn":
        for label, arguments in (
            ("检查账号、硬盘挂载和中文字体", ["check_runtime", "--require-db"]),
            ("检查 Django 部署配置", ["check", "--deploy", "--fail-level", "ERROR"]),
            ("更新数据库结构", ["migrate", "--noinput"]),
        ):
            print(f"[启动] {label}...", flush=True)
            subprocess.run([sys.executable, "manage.py", *arguments], check=True)
    print("[启动] 执行服务命令。", flush=True)
    os.execvp(command[0], command)


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as exc:
        raise SystemExit(exc.returncode) from exc
    except KeyboardInterrupt:
        print("\n启动已取消。", file=sys.stderr, flush=True)
        raise SystemExit(130)
