"""Bot suggestions get status_changed_at, as Admin > Ideas has. Existing rows have no history, so it is filled
with the best guess: created_at for ones still New, updated_at (the last change of any kind) for the rest."""
from django.db import migrations, models


def backfill(apps, schema_editor):
    SiteSuggestion = apps.get_model("albright_bot_api", "SiteSuggestion")
    SiteSuggestion.objects.filter(status="new").update(status_changed_at=models.F("created_at"))
    SiteSuggestion.objects.exclude(status="new").update(status_changed_at=models.F("updated_at"))


class Migration(migrations.Migration):

    dependencies = [
        ('albright_bot_api', '0004_sitesuggestion_idea_statuses'),
    ]

    operations = [
        migrations.AddField(
            model_name='sitesuggestion',
            name='status_changed_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.RunPython(backfill, migrations.RunPython.noop),
    ]
