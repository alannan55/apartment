from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("core", "0012_accounting_workbench")]
    operations = [migrations.CreateModel(
        name="BillingCheckpoint",
        fields=[
            ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
            ("completed_on", models.DateField(null=True)),
            ("through_date", models.DateField(null=True)),
        ],
    )]
