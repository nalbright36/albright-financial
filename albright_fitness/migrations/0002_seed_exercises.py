"""Adds the built-in exercise library (seed.EXERCISES) - only the ones not there yet, so it's safe to re-run.
Reversing it removes the built-in exercises no workout uses."""
from django.db import migrations


def forwards(apps, schema_editor):
    from albright_fitness.seed import seed
    seed(apps.get_model("albright_fitness", "Exercise"))


def backwards(apps, schema_editor):
    from albright_fitness.seed import EXERCISES
    Exercise = apps.get_model("albright_fitness", "Exercise")
    used = set(apps.get_model("albright_fitness", "WorkoutExercise").objects.values_list("exercise_id", flat=True))
    Exercise.objects.filter(owner__isnull=True, name__in=[e[0] for e in EXERCISES]).exclude(pk__in=used).delete()


class Migration(migrations.Migration):
    dependencies = [("albright_fitness", "0001_initial")]
    operations = [migrations.RunPython(forwards, backwards)]
