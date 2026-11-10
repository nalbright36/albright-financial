"""Idea tasks get a main task and subtasks: ScheduleItem.parent. Existing Idea tasks: per Idea and owner, the
lowest-numbered one with no parent is the main task; every other one becomes its subtask. Going back drops the
field (the links are the only thing lost)."""
import django.db.models.deletion
from django.db import migrations, models


def link_subtasks(apps, schema_editor):
    ScheduleItem = apps.get_model("albright_planner", "ScheduleItem")
    pairs = ScheduleItem.objects.filter(idea__isnull=False).values_list("idea_id", "owner_id").distinct()
    for idea_id, owner_id in pairs:
        items = ScheduleItem.objects.filter(idea_id=idea_id, owner_id=owner_id)
        main = items.filter(parent__isnull=True).order_by("pk").first()
        if main is not None:
            items.exclude(pk=main.pk).update(parent=main)


class Migration(migrations.Migration):

    dependencies = [
        ("albright_planner", "0015_plannersettings_timezone"),
    ]

    operations = [
        migrations.AddField(
            model_name="scheduleitem",
            name="parent",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                                    related_name="subtasks", to="albright_planner.scheduleitem"),
        ),
        migrations.RunPython(link_subtasks, migrations.RunPython.noop),
    ]
