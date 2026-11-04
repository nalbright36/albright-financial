"""Meals gets households (step 2 of 3 - data): everything already in Meals goes to ONE household, so nobody loses
anything and nobody new sees it.

Until now every Meals row was shared by everyone who could open Meals (superusers, and anyone given Meals in
Admin > Users). So:
  - The household: the one the first superuser (by id) already has (e.g. from Money) - reused, never copied.
    Without one, a new household "<first name>'s household" created by them. (No superuser: the first Meals user.)
  - Its members: everyone who can open Meals today and isn't in another household yet - the same people who
    see this data now. Someone already in a different household is left out (and noted on stdout).
  - Every recipe, plan entry, grocery line, idea and the settings row gets that household.
Nothing to do on an empty database (no Meals rows and no Meals users). Reversing clears the household columns
(the household and its members stay - they may be Money's too)."""
from django.db import migrations

MEALS_MODELS = ("Recipe", "MealPlanEntry", "GroceryItem", "MealIdea", "MealsSettings")


def forwards(apps, schema_editor):
    User = apps.get_model("auth", "User")
    Preference = apps.get_model("albright_labs", "UserPreference")
    Household = apps.get_model("albright_labs", "Household")
    Member = apps.get_model("albright_labs", "HouseholdMember")
    tables = [apps.get_model("albright_meals", name) for name in MEALS_MODELS]

    granted = set(Preference.objects.filter(allowed_apps__contains=["meals"]).values_list("user_id", flat=True))
    users = [u for u in User.objects.filter(is_active=True).order_by("-is_superuser", "pk")
             if u.is_superuser or u.pk in granted]
    has_rows = any(t.objects.exists() for t in tables)
    if not users and not has_rows:
        return
    owner = users[0] if users else None
    membership = Member.objects.filter(user=owner).select_related("household").first() if owner else None
    if membership:
        household = membership.household
    else:
        name = f"{(owner.first_name or owner.username).strip()}'s household" if owner else "Household"
        household = Household.objects.create(name=name[:100], created_by=owner)
        if owner:
            Member.objects.create(household=household, user=owner)
    for user in users:
        mine = Member.objects.filter(user=user).first()
        if mine is None:
            Member.objects.create(household=household, user=user)
        elif mine.household_id != household.pk:
            print(f"\n  Meals: {user.username} is already in another household - not added to {household.name}.")
    for table in tables:
        table.objects.filter(household__isnull=True).update(household=household)


def backwards(apps, schema_editor):
    for name in MEALS_MODELS:
        apps.get_model("albright_meals", name).objects.update(household=None)


class Migration(migrations.Migration):

    dependencies = [
        ("albright_meals", "0005_households_schema"),
        ("albright_labs", "0007_household"),
    ]

    operations = [migrations.RunPython(forwards, backwards)]
