"""Meals moves from households to spaces (1 of 3 - schema): a nullable `space` next to `household` on recipes, the
plan, groceries, ideas and settings. 0010 fills it; 0011 makes it required and drops `household`."""
import django.db.models.deletion
from django.db import migrations, models


def space_fk():
    return models.ForeignKey(null=True, on_delete=django.db.models.deletion.CASCADE, related_name="+", to="albright_labs.space")


class Migration(migrations.Migration):

    dependencies = [
        ("albright_labs", "0008_circles"),
        ("albright_meals", "0008_nutrition"),
    ]

    operations = [
        migrations.AddField(model_name="recipe", name="space", field=space_fk()),
        migrations.AddField(model_name="mealplanentry", name="space", field=space_fk()),
        migrations.AddField(model_name="mealidea", name="space", field=space_fk()),
        migrations.AddField(model_name="groceryitem", name="space", field=space_fk()),
        migrations.AddField(model_name="mealssettings", name="space", field=models.OneToOneField(
            null=True, on_delete=django.db.models.deletion.CASCADE, related_name="meals_settings", to="albright_labs.space")),
    ]
