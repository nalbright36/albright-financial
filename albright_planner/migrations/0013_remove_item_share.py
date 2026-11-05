"""Planner items' shares move to the one Share system (schema): the old ItemShare table is gone - ShareGrant has its rows (0012_shares_to_grants). PlannerSettings.default_shares too (now SharingDefault)."""
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("albright_planner", "0012_shares_to_grants"),
    ]

    operations = [
        migrations.RemoveField(model_name="scheduleitem", name="shared_with"),
        migrations.DeleteModel(name="ItemShare"),
        migrations.RemoveField(model_name="plannersettings", name="default_shares"),
    ]
