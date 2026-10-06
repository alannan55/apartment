from datetime import date

from django.core.management.base import BaseCommand

from core.maintenance import ensure_billing
from core.services import refresh_all_room_statuses, scheduled_due_through_date


class Command(BaseCommand):
    help = "刷新到期账务与房态；可每天运行，网页首次访问也会自动补跑。"

    def add_arguments(self, parser):
        parser.add_argument("--through", type=date.fromisoformat, help="生成截至日期 YYYY-MM-DD")
        parser.add_argument("--force", action="store_true", help="忽略检查点，重新核对缺失账单")

    def handle(self, *args, **options):
        self.stdout.write("正在检查到期账务…")
        self.stdout.flush()
        try:
            ensure_billing(options["through"] or scheduled_due_through_date(), force=options["force"])
            refresh_all_room_statuses()
        except KeyboardInterrupt:
            self.stderr.write("检查已取消，未完成的账务事务已回滚。")
            raise SystemExit(130)
        self.stdout.write(self.style.SUCCESS("账务与房态检查完成。"))
