"""Meals gets households (step 3 of 3 - schema): every row now has its household (0006), so it's required; and
"one meal per day and slot" is back - per household."""
import django.db.models.deletion
from django.db import migrations, models


def household_fk():
    return models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="+", to="albright_labs.household")


class Migration(migrations.Migration):

    dependencies = [
        ("albright_meals", "0006_assign_household"),
    ]

    operations = [
        migrations.AlterField(model_name="recipe", name="household", field=household_fk()),
        migrations.AlterField(model_name="mealplanentry", name="household", field=household_fk()),
        migrations.AlterField(model_name="groceryitem", name="household", field=household_fk()),
        migrations.AlterField(model_name="mealidea", name="household", field=household_fk()),
        migrations.AlterField(
            model_name="mealssettings", name="household",
            field=models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name="meals_settings",
                                       to="albright_labs.household")),
        migrations.AddConstraint(
            model_name="mealplanentry",
            constraint=models.UniqueConstraint(fields=("household", "date", "slot"), name="meals_one_entry_per_slot")),
    ]
