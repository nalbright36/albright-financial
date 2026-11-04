"""Meals moves from households to spaces (2 of 3 - data): every row's space is the space of the circle its household
became (albright_labs 0008 - Circle.legacy_household). Every Meals row has a household (0007), so every row gets a
space; the circle's members are exactly the household's, opted in to Meals - so each person sees exactly what they
saw before. Backwards: the household again, from the circle."""
from django.db import migrations

MODELS = ("Recipe", "MealPlanEntry", "MealIdea", "GroceryItem", "MealsSettings")


def forwards(apps, schema_editor):
    Space = apps.get_model("albright_labs", "Space")
    by_household = {s.circle.legacy_household: s.pk for s in Space.objects.filter(circle__legacy_household__isnull=False)
                    .select_related("circle")}
    for name in MODELS:
        model = apps.get_model("albright_meals", name)
        for household_id, space_id in by_household.items():
            model.objects.filter(household_id=household_id).update(space_id=space_id)
        missing = model.objects.filter(space__isnull=True).count()
        if missing:
            raise RuntimeError(f"{missing} {name} rows have a household with no circle - nothing was changed.")


def backwards(apps, schema_editor):
    Space = apps.get_model("albright_labs", "Space")
    for space in Space.objects.filter(circle__legacy_household__isnull=False).select_related("circle"):
        for name in MODELS:
            apps.get_model("albright_meals", name).objects.filter(space_id=space.pk).update(household_id=space.circle.legacy_household)


class Migration(migrations.Migration):

    dependencies = [
        ("albright_meals", "0009_spaces"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
