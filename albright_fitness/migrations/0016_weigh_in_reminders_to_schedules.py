"""The old weekly "Weigh in" reminder (NutritionProfile.weigh_in_reminder on weigh_in_weekday, Planner source_id
"weigh-in") becomes a weekly WeighInSchedule on that day. Its Planner task, check-offs and all, is kept: renamed
"Weigh-in" and linked as the schedule's task ("weighin-<pk>"). It stays all day, as it was. Reversing turns the
schedules back into reminders."""
from datetime import date

from django.db import migrations


def forward(apps, schema_editor):
    NutritionProfile = apps.get_model("albright_fitness", "NutritionProfile")
    WeighInSchedule = apps.get_model("albright_fitness", "WeighInSchedule")
    ScheduleItem = apps.get_model("albright_planner", "ScheduleItem")
    for profile in NutritionProfile.objects.filter(weigh_in_reminder=True):
        user_id = profile.user_id
        if WeighInSchedule.objects.filter(owner_id=user_id).exists():
            continue
        item = ScheduleItem.objects.filter(owner_id=user_id, source_app="fitness", source_id="weigh-in",
                                           archived=False).first()
        schedule = WeighInSchedule.objects.create(
            owner_id=user_id, frequency="weekly", days_of_week=[profile.weigh_in_weekday], time_of_day=None,
            start_date=item.date if item else date.today(), active=True)
        if item is not None:
            item.source_id, item.title = f"weighin-{schedule.pk}", "Weigh-in"
            item.save(update_fields=["source_id", "title"])
            schedule.planner_item = item
            schedule.save(update_fields=["planner_item"])
        profile.weigh_in_reminder = False
        profile.save(update_fields=["weigh_in_reminder"])


def backward(apps, schema_editor):
    NutritionProfile = apps.get_model("albright_fitness", "NutritionProfile")
    WeighInSchedule = apps.get_model("albright_fitness", "WeighInSchedule")
    for schedule in WeighInSchedule.objects.filter(active=True, frequency="weekly").select_related("planner_item"):
        NutritionProfile.objects.filter(user_id=schedule.owner_id).update(
            weigh_in_reminder=True, weigh_in_weekday=(schedule.days_of_week or [0])[0])
        if schedule.planner_item_id:
            item = schedule.planner_item
            item.source_id, item.title = "weigh-in", "Weigh in"
            item.save(update_fields=["source_id", "title"])


class Migration(migrations.Migration):

    dependencies = [
        ("albright_fitness", "0015_weigh_in_schedules"),
        ("albright_planner", "0019_show_weather"),
    ]

    operations = [migrations.RunPython(forward, backward)]
