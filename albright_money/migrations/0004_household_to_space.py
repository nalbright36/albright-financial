"""Money moves to spaces (2 of 3 - data):
  - shared (household set) -> the space of the circle that household became (albright_labs 0008);
  - personal (household empty) -> its owner's Personal space (made here when they don't have one yet).
The circle's members are exactly the household's, opted in to Money, and a Personal space is its person's alone - so
each person sees exactly the accounts, categories, rules and budgets they saw before. Backwards: the household again
for circle spaces, empty for Personal ones."""
from django.db import migrations

MODELS = ("Account", "Category", "Rule", "Budget")


def forwards(apps, schema_editor):
    Space = apps.get_model("albright_labs", "Space")
    by_household = {s.circle.legacy_household: s.pk for s in Space.objects.filter(circle__legacy_household__isnull=False)
                    .select_related("circle")}
    personal = {}

    def personal_space(user_id):
        if user_id not in personal:
            personal[user_id] = Space.objects.get_or_create(user_id=user_id, circle=None)[0].pk
        return personal[user_id]

    for name in MODELS:
        model = apps.get_model("albright_money", name)
        for household_id, space_id in by_household.items():
            model.objects.filter(household_id=household_id).update(space_id=space_id)
        for owner_id in model.objects.filter(household__isnull=True).values_list("owner_id", flat=True).distinct():
            model.objects.filter(household__isnull=True, owner_id=owner_id).update(space_id=personal_space(owner_id))
        missing = model.objects.filter(space__isnull=True).count()
        if missing:
            raise RuntimeError(f"{missing} {name} rows have a household with no circle - nothing was changed.")


def backwards(apps, schema_editor):
    Space = apps.get_model("albright_labs", "Space")
    for space in Space.objects.filter(circle__legacy_household__isnull=False).select_related("circle"):
        for name in MODELS:
            apps.get_model("albright_money", name).objects.filter(space_id=space.pk).update(household_id=space.circle.legacy_household)


class Migration(migrations.Migration):

    dependencies = [
        ("albright_money", "0003_spaces"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
