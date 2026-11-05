"""Workouts' shares move to the one Share system (data): WorkoutShare (person, view only) -> albright_labs.ShareGrant, checked per person (counts in the output). """
from django.db import migrations

from albright_labs.share_copy import copy_back, copy_shares


def forwards(apps, schema_editor):
    copy_shares(apps, app_label="albright_fitness", model="Workout", share_model="WorkoutShare", item_field="workout", user_field="user",
                permission=None, created_by=lambda share, item: item.owner_id, seen_field=None, label="Workouts")


def backwards(apps, schema_editor):
    copy_back(apps, app_label="albright_fitness", model="Workout", share_model="WorkoutShare", item_field="workout", user_field="user",
              permission=None, seen_field=None)


class Migration(migrations.Migration):

    dependencies = [
        ("albright_fitness", "0004_body_metrics"),
        ("albright_labs", "0010_sharing"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
