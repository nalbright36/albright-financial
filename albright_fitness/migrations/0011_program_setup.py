from django.db import migrations, models
from django.utils import timezone


def backfill(apps, schema_editor):
    """Every existing program keeps working as it did: started the day it was scheduled (else made), and on the
    Planner choice remembered when it's on the Planner now."""
    Program = apps.get_model("albright_fitness", "Program")
    for p in Program.objects.all():
        p.start_date = p.scheduled_from or timezone.localtime(p.created_at).date()
        p.on_planner = p.scheduled_from is not None
        p.save(update_fields=["start_date", "on_planner"])


class Migration(migrations.Migration):

    dependencies = [("albright_fitness", "0010_scheduled_sessions")]

    operations = [
        migrations.AddField("program", "start_date", models.DateField(blank=True, null=True)),
        migrations.AddField("program", "on_planner", models.BooleanField(default=False)),
        migrations.AddField("program", "planner_time", models.TimeField(blank=True, null=True)),
        migrations.RunPython(backfill, migrations.RunPython.noop),
    ]
