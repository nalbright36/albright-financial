"""Every recipe's ingredients in the one stored shape (albright_meals.ingredients.normalize_list): lines that kept
the whole text in "name" ("2 lb beef stew meat, trimmed...") split into amount / unit / name / note, each with its
original line kept ("original") so nothing is lost, and a category. Then the grocery list's lines filed by the same
categories: Pantry, Bakery and Other lines are placed again from their name (Meat, Produce, Dairy, Frozen and
Household keep theirs). Going back leaves the data as it is - nothing was removed."""
from django.db import migrations

KEEP = {"meat", "produce", "dairy", "frozen", "household"}


def forwards(apps, schema_editor):
    from albright_meals.ingredients import aisle_for, normalize_list
    Recipe = apps.get_model("albright_meals", "Recipe")
    for recipe in Recipe.objects.all().only("pk", "ingredients").iterator():
        new = normalize_list(recipe.ingredients)
        if new != recipe.ingredients:
            Recipe.objects.filter(pk=recipe.pk).update(ingredients=new)
    GroceryItem = apps.get_model("albright_meals", "GroceryItem")
    for item in GroceryItem.objects.exclude(aisle__in=KEEP).only("pk", "name", "aisle").iterator():
        aisle = aisle_for(item.name)
        if aisle != item.aisle:
            GroceryItem.objects.filter(pk=item.pk).update(aisle=aisle)


class Migration(migrations.Migration):
    dependencies = [("albright_meals", "0016_grocery_categories")]
    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
