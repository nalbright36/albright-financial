"""Bot suggestions use the same statuses as Admin > Ideas: new, considering,
planned, in progress, done, declined. Existing rows: new -> new,
accepted -> planned, declined -> declined, done -> done.

Going back: considering / planned / in progress all become accepted."""
from django.db import migrations, models

FORWARD = {"accepted": "planned"}
BACKWARD = {"considering": "accepted", "planned": "accepted", "in_progress": "accepted"}


def remap(mapping):
    def run(apps, schema_editor):
        SiteSuggestion = apps.get_model("albright_bot_api", "SiteSuggestion")
        for old, new in mapping.items():
            SiteSuggestion.objects.filter(status=old).update(status=new)
    return run


class Migration(migrations.Migration):

    dependencies = [
        ('albright_bot_api', '0003_tradingnote_strategy'),
    ]

    operations = [
        migrations.AlterField(
            model_name='sitesuggestion',
            name='status',
            field=models.CharField(choices=[('new', 'New'), ('considering', 'Considering'), ('planned', 'Planned'), ('in_progress', 'In progress'), ('done', 'Done'), ('declined', 'Declined')], db_index=True, default='new', max_length=11),
        ),
        # Forward: widen the column first, then remap. Backward runs in reverse order: remap back to
        # the old values while the column is still wide, then shrink it.
        migrations.RunPython(remap(FORWARD), remap(BACKWARD)),
    ]
