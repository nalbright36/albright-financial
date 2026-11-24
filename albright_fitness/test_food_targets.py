"""Food diary vs targets with exercise (burn.py, nutrition.allowance): the (MET - 1) x kg x hours estimate, weight
from weigh-ins, a typed watch number winning, goal = target + exercise (carbs + kcal / 4), the toggle; the Set-targets
banner and "Don't show me again"; typed targets out of range are an error; the macros-vs-calories note; one rounding;
the person's own day (Planner time zone); copies never keep the AI's idempotency key."""
from datetime import date, datetime, time, timedelta, timezone as dt_tz
from decimal import Decimal as D
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from albright_labs.testing import with_apps

from . import burn, nutrition
from .models import Exercise, FoodLogEntry, NutritionProfile, WeightEntry, Workout, WorkoutExercise, WorkoutSet

PASSWORD = "pw-not-real-12345"


class FoodTargetsTests(TestCase):
    def setUp(self):
        self.me = with_apps(User.objects.create_user(username="nate", password=PASSWORD), ["fitness"])
        self.client.login(username="nate", password=PASSWORD)
        self.day = nutrition.today(self.me)
        self.profile = NutritionProfile.load(self.me)

    def targets(self, **kw):
        values = {"target_calories": 2230, "target_protein": 148, "target_carbs": 247, "target_fat": 62, "weight_kg": D("84")}
        values.update(kw)
        NutritionProfile.objects.filter(pk=self.profile.pk).update(**values)
        self.profile.refresh_from_db()

    def run_and_lift(self):
        run = Workout.objects.create(owner=self.me, date=self.day, name="Run", finished_at=timezone.now())
        we = WorkoutExercise.objects.create(workout=run, exercise=Exercise.objects.get(name="Running", owner__isnull=True))
        WorkoutSet.objects.create(workout_exercise=we, duration_seconds=32 * 60, distance=D("3.5"), done=True)
        lift = Workout.objects.create(owner=self.me, date=self.day, name="Upper A", start_time=time(17, 30),
                                      end_time=time(18, 20), finished_at=timezone.now())
        we = WorkoutExercise.objects.create(workout=lift, exercise=Exercise.objects.get(name="Barbell Bench Press", owner__isnull=True))
        for i in range(4):
            WorkoutSet.objects.create(workout_exercise=we, order=i, reps=8, weight=185, done=True)
        return run, lift

    def test_estimate_weight_and_override(self):
        run, _lift = self.run_and_lift()
        self.assertEqual(burn.day(self.me, self.day)["weight_from"], "default")          # 75 kg
        self.targets()
        got = burn.day(self.me, self.day)
        self.assertEqual((got["weight_from"], [r["kcal"] for r in got["rows"]], got["total"]), ("targets", [394, 280], 674))
        WeightEntry.objects.create(owner=self.me, date=self.day, weight_kg=D("90"))
        self.assertEqual(burn.day(self.me, self.day)["weight_from"], "weigh-in")
        run.calories_burned = 410
        run.save()
        self.assertEqual(burn.day(self.me, self.day)["rows"][0]["kcal"], 410)

    def test_goal_adds_exercise_and_carbs_only(self):
        self.targets()
        self.run_and_lift()
        FoodLogEntry.objects.create(owner=self.me, date=self.day, label="Lunch", calories=D("1610"), carbs=D("168"))
        page = self.client.get(reverse("albright_fitness:food"))
        self.assertEqual(page.context["allowance"]["goal"], 2904)
        self.assertEqual(page.context["allowance"]["left"], 1294)
        rows = {r["name"]: r for r in page.context["progress"]}
        self.assertEqual((rows["carbs"]["target"], rows["protein"]["target"]), (416, 148))
        self.assertContains(page, "1294")
        self.targets(add_exercise=False)
        self.assertEqual(self.client.get(reverse("albright_fitness:food")).context["allowance"]["goal"], 2230)

    def test_banner_dismiss_and_calorie_target(self):
        page = self.client.get(reverse("albright_fitness:food"))
        self.assertContains(page, 'id="fx-targets-banner"')
        self.client.post(reverse("albright_fitness:hide_targets_banner"))
        page = self.client.get(reverse("albright_fitness:food"))
        self.assertNotContains(page, 'id="fx-targets-banner"')
        self.assertContains(page, "Set targets</a>")
        self.targets(hide_targets_banner=False, target_calories=2000)
        page = self.client.get(reverse("albright_fitness:food"))
        self.assertNotContains(page, 'id="fx-targets-banner"')
        self.assertContains(page, 'id="fx-left"')

    def test_workout_calories_typed_reset_and_private(self):
        run, _lift = self.run_and_lift()
        url = reverse("albright_fitness:workout_calories", args=[run.pk])
        self.client.post(url, {"calories": "410"})
        run.refresh_from_db()
        self.assertEqual(run.calories_burned, 410)
        self.client.post(url, {"calories": "99999"})
        run.refresh_from_db()
        self.assertEqual(run.calories_burned, 410)
        self.client.post(url, {"reset": "1"})
        run.refresh_from_db()
        self.assertIsNone(run.calories_burned)
        with_apps(User.objects.create_user(username="kim", password=PASSWORD), ["fitness"])
        other = self.client_class()
        other.login(username="kim", password=PASSWORD)
        self.assertEqual(other.post(url, {"calories": "1"}).status_code, 404)

    def test_targets_errors_notes_and_nudge(self):
        self.targets(activity="moderate")
        url = reverse("albright_fitness:targets")
        page = self.client.post(url, {"action": "targets", "calories": "12000", "protein": "148", "track_calories": "1"}, follow=True)
        self.assertContains(page, "Calories: enter a whole number")
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.target_calories, 2230)                          # not wiped
        self.assertContains(page, 'id="fx-rerun-note"')
        self.targets(target_protein=200, target_carbs=300, target_fat=100, target_calories=2000)
        self.assertContains(self.client.get(url), "add up to about 2,900 calories")
        self.client.post(url, {"action": "targets", "calories": "2000", "track_calories": "1", "exercise_shown": "1"})
        self.profile.refresh_from_db()
        self.assertFalse(self.profile.add_exercise)

    def test_one_rounding(self):
        self.targets(tracked=["protein"])
        FoodLogEntry.objects.create(owner=self.me, date=self.day, label="A", protein=D("91.6"))
        self.assertEqual(nutrition.status_line(self.me), "Protein 92/148 g")
        self.assertEqual(nutrition.whole(D("0.5")), 1)

    def test_diary_uses_the_persons_time_zone(self):
        from albright_planner.models import PlannerSettings
        PlannerSettings.objects.update_or_create(user=self.me, defaults={"timezone": "America/Los_Angeles", "auto_timezone": False})
        late = datetime(2026, 11, 24, 5, 30, tzinfo=dt_tz.utc)                           # 9:30 PM Nov 23 in LA
        FoodLogEntry.objects.create(owner=self.me, date=date(2026, 11, 23), label="Dinner", calories=D("500"))
        with mock.patch("django.utils.timezone.now", return_value=late):
            page = self.client.get(reverse("albright_fitness:food"))
        self.assertEqual(page.context["on"], date(2026, 11, 23))
        self.assertEqual(page.context["totals"]["calories"], D("500"))

    def test_copy_yesterday_drops_the_ai_key(self):
        FoodLogEntry.objects.create(owner=self.me, date=self.day - timedelta(days=1), slot="lunch", label="AI lunch",
                                    calories=D("400"), source="ai", idempotency_key="k-1")
        self.client.post(reverse("albright_fitness:food_copy"), {"date": self.day.isoformat(), "slot": "lunch"})
        self.assertEqual(FoodLogEntry.objects.get(date=self.day).idempotency_key, "")
