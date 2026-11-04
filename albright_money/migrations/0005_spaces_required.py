"""Money moves to spaces (3 of 3 - schema): `space` required, `household` gone, and one budget per space and month
(was one per household and month, or per owner and month for personal ones)."""
import django.db.models.deletion
from django.db import migrations, models


def space_fk():
    return models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="+", to="albright_labs.space")


class Migration(migrations.Migration):

    dependencies = [
        ("albright_money", "0004_household_to_space"),
    ]

    operations = [
        migrations.RemoveConstraint(model_name="budget", name="money_one_household_budget_a_month"),
        migrations.RemoveConstraint(model_name="budget", name="money_one_personal_budget_a_month"),
        migrations.RemoveField(model_name="account", name="household"),
        migrations.RemoveField(model_name="category", name="household"),
        migrations.RemoveField(model_name="rule", name="household"),
        migrations.RemoveField(model_name="budget", name="household"),
        migrations.AlterField(model_name="account", name="space", field=space_fk()),
        migrations.AlterField(model_name="category", name="space", field=space_fk()),
        migrations.AlterField(model_name="rule", name="space", field=space_fk()),
        migrations.AlterField(model_name="budget", name="space", field=space_fk()),
        migrations.AddConstraint(model_name="budget", constraint=models.UniqueConstraint(
            fields=("space", "month"), name="money_one_budget_a_month")),
    ]
