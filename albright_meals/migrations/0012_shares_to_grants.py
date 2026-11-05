"""Recipes' shares move to the one Share system (data): RecipeShare (person, read-only, with its notice) -> albright_labs.ShareGrant, checked per person (counts in the output). Read-only shares become view grants; who shared it and when the notice was seen are kept."""
from django.db import migrations

from albright_labs.share_copy import copy_back, copy_shares


def forwards(apps, schema_editor):
    copy_shares(apps, app_label="albright_meals", model="Recipe", share_model="RecipeShare", item_field="recipe", user_field="shared_with",
                permission=None, created_by=lambda share, item: share.shared_by_id, seen_field="seen_at", label="Recipes")


def backwards(apps, schema_editor):
    copy_back(apps, app_label="albright_meals", model="Recipe", share_model="RecipeShare", item_field="recipe", user_field="shared_with",
              permission=None, seen_field="seen_at", extra=lambda g: {"shared_by_id": g.created_by_id})


class Migration(migrations.Migration):

    dependencies = [
        ("albright_meals", "0011_spaces_required"),
        ("albright_labs", "0010_sharing"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
