"""The Household is gone (schema): Meals and Money now use spaces (their 0011 / 0005), and household invites became
circle invites (0008). Drops Invitation.household, the Household tables, and Circle.legacy_household (only the data
migrations needed it)."""
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("albright_labs", "0008_circles"),
        ("albright_meals", "0011_spaces_required"),
        ("albright_money", "0005_spaces_required"),
    ]

    operations = [
        migrations.RemoveField(model_name="invitation", name="household"),
        migrations.RemoveField(model_name="circle", name="legacy_household"),
        migrations.DeleteModel(name="HouseholdMember"),
        migrations.DeleteModel(name="Household"),
    ]
