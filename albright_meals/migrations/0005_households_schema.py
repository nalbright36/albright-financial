"""Meals gets households (step 1 of 3 - schema): a household column on recipes, plan entries, grocery lines, ideas
and the settings row (empty for now - 0006 fills it, 0007 makes it required); recipe visibility ("household" or the
shared library) and "saved from" credit; RecipeShare (a recipe shown to one person outside the household). The
plan's "one meal per day and slot" rule is dropped here and comes back per household in 0007."""
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


def household_fk(**extra):
    return models.ForeignKey(null=True, blank=True, on_delete=django.db.models.deletion.CASCADE, related_name="+",
                             to="albright_labs.household", **extra)


class Migration(migrations.Migration):

    dependencies = [
        ("albright_meals", "0004_recipe_ratings_and_tags"),
        ("albright_labs", "0007_household"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.RemoveConstraint(model_name="mealplanentry", name="meals_one_entry_per_slot"),
        migrations.AddField(model_name="recipe", name="household", field=household_fk()),
        migrations.AddField(model_name="mealplanentry", name="household", field=household_fk()),
        migrations.AddField(model_name="groceryitem", name="household", field=household_fk()),
        migrations.AddField(model_name="mealidea", name="household", field=household_fk()),
        migrations.AddField(
            model_name="mealssettings", name="household",
            field=models.OneToOneField(null=True, blank=True, on_delete=django.db.models.deletion.CASCADE,
                                       related_name="meals_settings", to="albright_labs.household")),
        migrations.AddField(
            model_name="recipe", name="visibility",
            field=models.CharField(choices=[("household", "Household"), ("library", "Shared library")], db_index=True,
                                   default="household", max_length=10)),
        migrations.AddField(
            model_name="recipe", name="saved_from",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                                    related_name="copies", to="albright_meals.recipe")),
        migrations.AddField(model_name="recipe", name="saved_from_name", field=models.CharField(blank=True, max_length=100)),
        migrations.CreateModel(
            name="RecipeShare",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("seen_at", models.DateTimeField(blank=True, null=True)),
                ("recipe", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="shares",
                                             to="albright_meals.recipe")),
                ("shared_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                                                related_name="+", to=settings.AUTH_USER_MODEL)),
                ("shared_with", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE,
                                                  related_name="meals_shared_recipes", to=settings.AUTH_USER_MODEL)),
            ],
            options={"ordering": ["-created_at", "-pk"],
                     "constraints": [models.UniqueConstraint(fields=("recipe", "shared_with"), name="meals_share_once")]},
        ),
    ]
