from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0015_seed_data_export_permission"),
    ]

    operations = [
        migrations.AddField(
            model_name="tenantsettings",
            name="cashier_can_open_session",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="tenantsettings",
            name="cashier_can_close_session",
            field=models.BooleanField(default=False),
        ),
    ]
