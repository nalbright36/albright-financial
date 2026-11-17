"""Fitness workout tasks already checked off on the Planner with no finished workout get a "Completed from Planner"
one, so they count in Fitness right away (today's included). The same as `manage.py sync_workout_task_completion`.
Backward: those auto-made workouts with no sets are removed."""
from django.db import migrations


def forward(apps, schema_editor):
    from albright_fitness.workout_sync import backfill
    backfill(apps.get_model)


def backward(apps, schema_editor):
    Workout = apps.get_model("albright_fitness", "Workout")
    Workout.objects.filter(from_planner=True, exercises__isnull=True).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("albright_fitness", "0017_workout_from_planner"),
        ("albright_planner", "0019_show_weather"),
    ]

    operations = [
        migrations.RunPython(forward, backward),
    ]
