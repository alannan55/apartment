import json
import re
from datetime import date

from django.db import migrations, models
from django.db.models import Count, Q


def restore_undated_visitors(apps, schema_editor):
    alias = schema_editor.connection.alias if schema_editor else "default"
    Person = apps.get_model("core", "Person")
    Room = apps.get_model("core", "Room")
    Stay = apps.get_model("core", "Stay")
    rooms = dict(Room.objects.using(alias).values_list("number", "pk"))
    linked = set(Stay.objects.using(alias).values_list("person_id", flat=True))
    marker = "[悦山资料同步 2026-10-01]"
    for p in Person.objects.using(alias).filter(notes__contains=marker):
        if p.pk in linked:
            continue
        try:
            info = json.loads(p.notes.split(marker, 1)[1].strip())
        except (ValueError, TypeError):
            continue
        if info.get("missing_date") is True and info.get("status") == "active" and info.get("room") in rooms:
            Stay.objects.using(alias).create(person_id=p.pk, room_id=rooms[info["room"]], stay_type="visitor",
                start_date=None, end_date=None, is_active=True,
                report_note=info.get("reporting_period") or info.get("residents_period") or "探望、暂住",
                notes="原入住表明确未退租，未提供具体入住日期；保留原暂住说明。")
    duplicate = Stay.objects.using(alias).filter(is_active=True).values("person_id").annotate(n=Count("id")).filter(n__gt=1)
    if duplicate.exists():
        raise RuntimeError("存在同一人员多条未结束的入住记录；请先核对重复入住，再执行迁移。未自动删除或改退租状态。")


def restore_report_starts(apps, schema_editor):
    alias = schema_editor.connection.alias
    Stay = apps.get_model("core", "Stay")
    Tenancy = apps.get_model("core", "Tenancy")
    pattern = re.compile(r"(?<!\d)(\d{4})[./年-](\d{1,2})[./月-](\d{1,2})(?:日)?(?!\d)")
    for stay in Stay.objects.using(alias).filter(is_active=True, tenancy__isnull=False).select_related("tenancy"):
        tenancy = stay.tenancy
        if stay.person_id != tenancy.primary_person_id or tenancy.police_report_start_date:
            continue
        matches = pattern.findall(stay.report_note or "")
        if len(matches) != 2:
            continue
        try:
            start, end = (date(*map(int, parts)) for parts in matches)
        except ValueError:
            continue
        if start <= end:
            Tenancy.objects.using(alias).filter(pk=tenancy.pk).update(police_report_start_date=start)


class Migration(migrations.Migration):
    dependencies = [("core", "0008_allow_incomplete_import")]
    operations = [
        migrations.AddField(model_name="tenancy", name="police_report_start_date", field=models.DateField(blank=True, null=True, verbose_name="报备开始日期")),
        migrations.AlterField(model_name="tenancy", name="police_report_end_date", field=models.DateField(blank=True, null=True, verbose_name="报备结束日期")),
        migrations.AlterField(model_name="stay", name="start_date", field=models.DateField(blank=True, null=True, verbose_name="实际入住日期")),
        migrations.AlterField(model_name="stay", name="report_note", field=models.CharField(blank=True, max_length=200, verbose_name="个人报备内容", help_text="留空自动生成；可填写独立期间或“假期暂住”等原文。")),
        migrations.RunPython(restore_undated_visitors, migrations.RunPython.noop),
        migrations.RunPython(restore_report_starts, migrations.RunPython.noop),
        migrations.AddConstraint(model_name="stay", constraint=models.UniqueConstraint(condition=Q(is_active=True), fields=("person",), name="one_active_stay_per_person")),
    ]
