"""Money moves from "personal or shared with the household" to spaces (1 of 3 - schema): a nullable `space` next to
`household` on accounts, categories, rules and budgets. 0004 fills it; 0005 makes it required and drops `household`."""
import django.db.models.deletion
from django.db import migrations, models


def space_fk():
    return models.ForeignKey(null=True, on_delete=django.db.models.deletion.CASCADE, related_name="+", to="albright_labs.space")


class Migration(migrations.Migration):

    dependencies = [
        ("albright_labs", "0008_circles"),
        ("albright_money", "0002_household_moved_to_labs"),
    ]

    operations = [
        migrations.AddField(model_name="account", name="space", field=space_fk()),
        migrations.AddField(model_name="category", name="space", field=space_fk()),
        migrations.AddField(model_name="rule", name="space", field=space_fk()),
        migrations.AddField(model_name="budget", name="space", field=space_fk()),
    ]
