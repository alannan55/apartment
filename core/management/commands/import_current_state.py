from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "旧列位置导入已停用，请在网页“更多 → 批量导入”使用项目模板。"

    def add_arguments(self, parser):
        # Recognize old invocations, and fail before any data is read or cleared.
        parser.add_argument("--data-dir")
        parser.add_argument("--clear", action="store_true")

    def handle(self, *args, **options):
        raise CommandError(
            "旧导入命令已停用：它不支持粉红退租、主租客识别和独立报备期间。"
            "请在“更多 → 批量导入”使用项目模板；原悦山四表先经 yueshan_check 核对，"
            "再按身份增量同步，不要清空正式库。"
        )
