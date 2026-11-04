"""Meals moves from households to spaces (3 of 3 - schema): `space` required, `household` gone, and one meal per
space, day and slot (was per household)."""
import django.db.models.deletion
from django.db import migrations, models


def space_fk():
    return models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="+", to="albright_labs.space")


class Migration(migrations.Migration):

    dependencies = [
        ("albright_meals", "0010_household_to_space"),
    ]

    operations = [
        migrations.RemoveConstraint(model_name="mealplanentry", name="meals_one_entry_per_slot"),
        migrations.RemoveField(model_name="recipe", name="household"),
        migrations.RemoveField(model_name="mealplanentry", name="household"),
        migrations.RemoveField(model_name="mealidea", name="household"),
        migrations.RemoveField(model_name="groceryitem", name="household"),
        migrations.RemoveField(model_name="mealssettings", name="household"),
        migrations.AlterField(model_name="recipe", name="space", field=space_fk()),
        migrations.AlterField(model_name="mealplanentry", name="space", field=space_fk()),
        migrations.AlterField(model_name="mealidea", name="space", field=space_fk()),
        migrations.AlterField(model_name="groceryitem", name="space", field=space_fk()),
        migrations.AlterField(model_name="mealssettings", name="space", field=models.OneToOneField(
            on_delete=django.db.models.deletion.CASCADE, related_name="meals_settings", to="albright_labs.space")),
        migrations.AlterField(model_name="recipe", name="visibility", field=models.CharField(
            choices=[("household", "This space only"), ("library", "Shared library")], db_index=True, default="household",
            max_length=10)),
        migrations.AddConstraint(model_name="mealplanentry", constraint=models.UniqueConstraint(
            fields=("space", "date", "slot"), name="meals_one_entry_per_slot")),
    ]
