from django.core.management.base import BaseCommand
from django.db import transaction

from core.models import Adjustment, Allocation, Broker, Charge, Payment, Person, PoliceReportExport, RecurringRule, Room, Stay, Tenancy


class Command(BaseCommand):
    help = "清空所有公寓业务数据，保留系统账号和数据库结构。"

    def add_arguments(self, parser):
        parser.add_argument("--yes", action="store_true", help="确认清空，不加此参数不会执行")

    def handle(self, *args, **options):
        if not options["yes"]:
            self.stdout.write(self.style.WARNING("未执行。请加 --yes 确认清空业务数据。"))
            return
        with transaction.atomic():
            PoliceReportExport.objects.all().delete()
            Allocation.objects.all().delete()
            Payment.objects.all().delete()
            Adjustment.objects.all().delete()
            Charge.objects.all().delete()
            Stay.objects.all().delete()
            Tenancy.objects.all().delete()
            RecurringRule.objects.all().delete()
            Broker.objects.all().delete()
            Person.objects.all().delete()
            Room.objects.all().delete()
        self.stdout.write(self.style.SUCCESS("已清空所有业务数据。"))
