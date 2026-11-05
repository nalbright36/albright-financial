"""Planner items' shares move to the one Share system (data): ItemShare (person, view / edit) -> albright_labs.ShareGrant, checked per person (counts in the output). Also the "Remember as my default" people -> SharingDefault(app=planner)."""
from django.db import migrations

from albright_labs.share_copy import copy_back, copy_shares


def forwards(apps, schema_editor):
    copy_shares(apps, app_label="albright_planner", model="ScheduleItem", share_model="ItemShare", item_field="item", user_field="user",
                permission="permission", created_by=lambda share, item: item.owner_id, seen_field=None, label="Planner items")
    # "Remember as my default" (PlannerSettings.default_shares {user id: permission}) -> the Planner's sharing default.
    Settings = apps.get_model("albright_planner", "PlannerSettings")
    SharingDefault = apps.get_model("albright_labs", "SharingDefault")
    moved = 0
    for row in Settings.objects.exclude(default_shares={}):
        people = {str(k): v for k, v in (row.default_shares or {}).items() if v in ("view", "edit")}
        if people:
            SharingDefault.objects.update_or_create(user_id=row.user_id, app="planner",
                                                    defaults={"mode": "people", "people": people})
            moved += 1
    print(f"  Planner sharing defaults moved: {moved}")


def backwards(apps, schema_editor):
    Settings = apps.get_model("albright_planner", "PlannerSettings")
    SharingDefault = apps.get_model("albright_labs", "SharingDefault")
    for d in SharingDefault.objects.filter(app="planner", mode="people"):
        Settings.objects.update_or_create(user_id=d.user_id, defaults={"default_shares": d.people})
    SharingDefault.objects.filter(app="planner").delete()
    copy_back(apps, app_label="albright_planner", model="ScheduleItem", share_model="ItemShare", item_field="item", user_field="user",
              permission="permission", seen_field=None)


class Migration(migrations.Migration):

    dependencies = [
        ("albright_planner", "0011_view_options"),
        ("albright_labs", "0010_sharing"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
