"""Stamp a stable id on every saved ingredient line so a photo can stay attached across recipe edits."""
from django.db import migrations


def stamp_ids(apps, schema_editor):
    Recipe = apps.get_model("albright_meals", "Recipe")
    from albright_meals.ingredients import normalize_list
    for recipe in Recipe.objects.iterator():
        stamped = normalize_list(recipe.ingredients)
        if stamped != (recipe.ingredients or []):
            Recipe.objects.filter(pk=recipe.pk).update(ingredients=stamped)


def unstamp(apps, schema_editor):
    Recipe = apps.get_model("albright_meals", "Recipe")
    from albright_meals.ingredients import without_ids
    for recipe in Recipe.objects.iterator():
        Recipe.objects.filter(pk=recipe.pk).update(ingredients=without_ids(recipe.ingredients or []))


class Migration(migrations.Migration):

    dependencies = [
        ("albright_meals", "0018_recipe_attachments"),
    ]

    operations = [
        migrations.RunPython(stamp_ids, unstamp),
    ]
