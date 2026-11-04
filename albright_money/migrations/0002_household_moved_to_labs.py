"""The Household now lives in albright_labs (its migration 0007 took over the tables and renamed them). This only
updates Django's state - Money's household foreign keys now name albright_labs.Household, and Money's own copies of
the two models go away. No SQL: the columns and their foreign keys already point at the (renamed) tables."""
import django.db.models.deletion
from django.db import migrations, models

FK = dict(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name="+",
          to="albright_labs.household", help_text="Empty: personal (the owner only). Set: shared with the household.")


class Migration(migrations.Migration):

    dependencies = [
        ("albright_money", "0001_initial"),
        ("albright_labs", "0007_household"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.AlterField(model_name="account", name="household", field=models.ForeignKey(**FK)),
                migrations.AlterField(model_name="category", name="household", field=models.ForeignKey(**FK)),
                migrations.AlterField(model_name="rule", name="household", field=models.ForeignKey(**FK)),
                migrations.AlterField(model_name="budget", name="household", field=models.ForeignKey(**FK)),
                migrations.DeleteModel(name="HouseholdMember"),
                migrations.DeleteModel(name="Household"),
            ],
            database_operations=[],
        ),
    ]
