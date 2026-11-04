"""Fitness Phase 3: body metrics (weigh-ins in lb / kg, the 7-day moving average, 2- and 4-week changes, the goal
projection only with a supportive trend, measurements, the weekly weigh-in reminder, hiding weight), Insights (volume
and its change, sets per muscle, 1RM trends, exercises not done lately, food averages, streaks and the calendar),
the Fitness Bot API (every endpoint with and without its scope, always as the token owner; text workouts check off
the Planner occurrence; food by recipe / my food / quick macros, with suggestions and nothing logged for unmatched
names), and privacy (404 for anyone else, household members included)."""
import json
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from albright_bot_api.models import BotRequestLog, BotToken
from albright_labs.testing import allow_api_keys, kitchen, with_apps
from albright_meals import recipe_nutrition
from albright_meals.models import Recipe
from albright_planner.models import ItemCompletion, ScheduleItem

from . import body, insights, programs
from .models import (CustomFood, Exercise, FoodLogEntry, Measurement, NutritionProfile, PersonalRecord, Program, ProgramDay,
                     ProgramExercise, WeightEntry, Workout, WorkoutExercise, WorkoutSet)

PASSWORD = "pw-not-real-12345"
API = "/api/bot/v1/"
D = Decimal


def url(name, *args):
    return reverse(f"albright_fitness:{name}", args=args)


@override_settings(BOT_API_REQUIRE_HTTPS=False, USDA_API_KEY="")
class Base(TestCase):
    def setUp(self):
        cache.clear()
        outlook = mock.patch("albright_trading_app.views.get_market_outlook", return_value=None)
        outlook.start()
        self.addCleanup(outlook.stop)
        self.me = allow_api_keys(with_apps(User.objects.create_user(username="nate", password=PASSWORD, first_name="Nate"),
                                           ["fitness", "meals", "planner"]))
        self.wife = allow_api_keys(with_apps(User.objects.create_user(username="sam", password=PASSWORD, first_name="Sam"),
                                             ["fitness", "meals", "planner"]))
        self.household = kitchen(self.me, self.wife)
        self.client.force_login(self.me)
        self.today = timezone.localdate()

    def as_user(self, user):
        c = Client()
        c.force_login(user)
        return c

    def weigh(self, days_ago, lb, user=None):
        return body.log_weight(user or self.me, self.today - timedelta(days=days_ago), D(str(lb)), "lb")[0]

    def ex(self, name):
        return Exercise.objects.get(owner__isnull=True, name=name)

    def workout(self, days_ago=0, sets=(), exercise="Barbell Back Squat", owner=None):
        w = Workout.objects.create(owner=owner or self.me, date=self.today - timedelta(days=days_ago), finished_at=timezone.now())
        we = WorkoutExercise.objects.create(workout=w, exercise=self.ex(exercise))
        for n, (weight, reps) in enumerate(sets):
            WorkoutSet.objects.create(workout_exercise=we, order=n, weight=D(str(weight)), reps=reps, done=True)
        return w


# ---------------------------------------------------------------------------
# Body metrics
# ---------------------------------------------------------------------------

class WeightTests(Base):
    def test_entry_units_and_one_a_day(self):
        self.client.post(url("log_weight"), {"weight": "182.4", "unit": "lb", "date": self.today.isoformat()})
        entry = WeightEntry.objects.get(owner=self.me)
        self.assertEqual(entry.weight_kg, D("82.74"))
        self.assertEqual(body.shown(entry.weight_kg, "lb"), D("182.4"))
        self.client.post(url("log_weight"), {"weight": "82.5", "unit": "kg", "date": self.today.isoformat()})
        self.assertEqual(WeightEntry.objects.get(owner=self.me).weight_kg, D("82.50"))       # replaced, not doubled
        self.client.post(url("log_weight"), {"weight": "5", "unit": "lb"})
        self.assertEqual(WeightEntry.objects.count(), 1)
        NutritionProfile.objects.filter(user=self.me).update(weight_unit="kg")
        self.assertIn("82.5", self.client.get(url("body")).content.decode())

    def test_moving_average(self):
        for days_ago, lb in ((8, 210), (6, 200), (5, 202), (3, 198), (0, 204)):
            self.weigh(days_ago, lb)
        points = body.series(self.me)
        averages = [body.shown(a, "lb") for _d, _w, a in points]
        # each is the mean of the weigh-ins in the 7 days up to it: the 8-days-ago one drops out of the later ones
        self.assertEqual(averages, [D("210.0"), D("205.0"), D("204.0"), D("202.5"), D("201.0")])

    def test_two_and_four_week_changes_in_neutral_words(self):
        for days_ago in range(0, 35):
            self.weigh(days_ago, 200 - (34 - days_ago) * 0.05)           # slowly down 0.05 lb a day
        points = body.series(self.me)
        two, four = body.change(points, 2, "lb"), body.change(points, 4, "lb")
        self.assertEqual(two["text"], "down 0.7 lb over 2 weeks")
        self.assertEqual(four["text"], "down 1.4 lb over 4 weeks")
        page = self.client.get(url("body")).content.decode()
        self.assertIn("Down 1.4 lb over 4 weeks", page)
        WeightEntry.objects.all().delete()
        for days_ago in range(0, 30):
            self.weigh(days_ago, 180 + (0.3 if days_ago % 2 else -0.3))  # just daily swings
        self.assertEqual(body.change(body.series(self.me), 4, "lb")["text"], "about the same over 4 weeks")

    def test_projection_only_with_a_supportive_trend(self):
        profile = NutritionProfile.load(self.me)
        profile.goal_weight_kg = body.to_kg(190, "lb")
        profile.save()
        for days_ago in range(0, 28):
            self.weigh(days_ago, 200 - (27 - days_ago) * 0.1)            # down 0.7 lb a week
        projected = body.projection(self.me, profile)
        self.assertIsNotNone(projected)
        self.assertGreater(projected["date"], self.today + timedelta(days=60))
        self.assertLess(projected["date"], self.today + timedelta(days=110))
        self.assertIn("around", self.client.get(url("body")).content.decode())
        profile.goal_weight_kg = body.to_kg(210, "lb")                    # the trend heads away from it
        self.assertIsNone(body.projection(self.me, profile))
        WeightEntry.objects.all().delete()
        for days_ago in range(0, 28):
            self.weigh(days_ago, 200 + (0.2 if days_ago % 2 else 0))      # flat
        profile.goal_weight_kg = body.to_kg(190, "lb")
        self.assertIsNone(body.projection(self.me, profile))
        WeightEntry.objects.all().delete()
        for days_ago in (0, 1, 2):
            self.weigh(days_ago, 200 - days_ago)                          # too few, too recent
        self.assertIsNone(body.projection(self.me, profile))
        self.assertNotIn("around", self.client.get(url("body")).content.decode())

    def test_measurements_in_inches_with_trends(self):
        self.client.post(url("log_measurements"), {"waist": "34", "body_fat": "18.5", "date": (self.today - timedelta(days=30)).isoformat()})
        self.client.post(url("log_measurements"), {"waist": "33", "chest": "41"})
        first = Measurement.objects.get(owner=self.me, date=self.today - timedelta(days=30))
        self.assertEqual((first.waist, first.body_fat), (D("86.4"), D("18.5")))
        charts = {m["key"]: m for m in body.measurement_charts(self.me, "in")}
        self.assertEqual(charts["waist"]["latest"]["value"], D("33.0"))
        self.assertEqual(charts["waist"]["change"], D("-1.0"))
        self.assertIsNotNone(charts["waist"]["chart"])
        self.assertIsNone(charts["chest"]["chart"])                        # one value: no chart yet
        page = self.client.get(url("body")).content.decode()
        self.assertIn("Waist", page)
        self.assertIn("Body fat", page)

    def test_weekly_reminder_on_the_planner_off_by_default(self):
        profile = NutritionProfile.load(self.me)
        self.assertFalse(profile.weigh_in_reminder)
        self.client.post(url("body_settings"), {"weight_unit": "lb", "height_unit": "in", "weigh_in_reminder": "1", "weigh_in_weekday": "4"})
        item = ScheduleItem.objects.get(owner=self.me, source_app="fitness", source_id="weigh-in")
        self.assertEqual((item.title, item.recurrence, item.archived), ("Weigh in", "FREQ=WEEKLY;BYDAY=FR", False))
        self.assertFalse(item.shares.exists())
        self.client.post(url("body_settings"), {"weight_unit": "lb", "height_unit": "in"})
        item.refresh_from_db()
        self.assertTrue(item.archived or item.recurrence_until == self.today)

    def test_hide_weight_from_the_dashboard(self):
        self.weigh(0, 181)
        self.assertIn('id="fx-weigh-card"', self.client.get(url("dashboard")).content.decode())
        self.client.post(url("body_settings"), {"weight_unit": "lb", "height_unit": "in", "hide_weight": "1"})
        page = self.client.get(url("dashboard")).content.decode()
        self.assertNotIn('id="fx-weigh-card"', page)
        self.assertNotIn("181.0", page)

    def test_dashboard_quick_weigh_in_returns_to_the_dashboard(self):
        response = self.client.post(url("log_weight"), {"weight": "180", "unit": "lb", "next": url("dashboard")})
        self.assertRedirects(response, url("dashboard"), fetch_redirect_response=False)
        response = self.client.post(url("log_weight"), {"weight": "180", "unit": "lb", "next": "https://evil.example.com/"})
        self.assertEqual(response["Location"], url("body"))


# ---------------------------------------------------------------------------
# Insights
# ---------------------------------------------------------------------------

class InsightTests(Base):
    def test_volume_change_records_and_food_in_the_week(self):
        monday = insights.stats.week_start(self.today)
        last_week = (self.today - monday).days + 3
        self.workout(last_week, [(200, 5)] * 2)                            # 2,000 lb last week
        w = self.workout(0, [(200, 5)] * 3)                                # 3,000 lb this week
        PersonalRecord.objects.create(owner=self.me, exercise=self.ex("Barbell Back Squat"), kind="weight", value=D(200),
                                      previous=D(190), workout=w, achieved_on=self.today)
        NutritionProfile.objects.filter(user=NutritionProfile.load(self.me).user).update(target_protein=150)
        FoodLogEntry.objects.create(owner=self.me, date=self.today, source="quick", label="Food", protein=D(140), calories=D(2000))
        week = insights.week(self.me, monday)
        self.assertEqual((week["volume"], week["volume_last"], week["volume_change"]), (3000, 2000, 50))
        self.assertEqual(insights.volume_change_text(week), "up 50% on last week")
        self.assertEqual((week["days_logged"], week["avg_protein"], week["target_protein"]), (1, 140, 150))
        self.assertEqual(len(week["records"]), 1)

    def test_sets_per_muscle_lift_trends_and_untrained(self):
        self.workout(21, [(200, 5)])
        self.workout(0, [(225, 5), (225, 5)])
        self.workout(20, [(150, 8)], exercise="Barbell Bench Press")
        muscles = insights.muscle_weeks(self.me, 8)
        quads = next(r for r in muscles["rows"] if r["muscle"] == "quads")
        self.assertEqual((quads["sets"][-1], quads["total"]), (2, 3))
        squat = next(t for t in insights.lift_trends(self.me) if t["name"] == "Barbell Back Squat")
        self.assertEqual((squat["first"], squat["last"]), (D("233.33"), D("262.50")))     # Epley
        stale = insights.untrained(self.me)
        self.assertEqual([r["exercise"].name for r in stale], ["Barbell Bench Press"])
        self.assertEqual(stale[0]["days"], 20)

    def test_nutrition_averages_over_logged_days_and_most_logged(self):
        NutritionProfile.load(self.me)
        for days_ago, kcal in ((0, 2000), (1, 2200), (10, 1800)):
            FoodLogEntry.objects.create(owner=self.me, date=self.today - timedelta(days=days_ago), source="quick", label="Oatmeal",
                                        calories=D(kcal), protein=D(100))
        food = insights.nutrition(self.me)
        self.assertEqual(food["days_logged"], 3)
        self.assertEqual(next(r for r in food["rows"] if r["name"] == "calories")["average"], 2000)
        self.assertEqual(food["foods"][0], {"label": "Oatmeal", "times": 3})

    def test_streak_calendar_and_neutral_page(self):
        self.workout(0, [(100, 5)])
        self.workout(2, [(100, 5)])
        data = insights.consistency(self.me, 12)
        self.assertEqual(data["active_days"], len({self.today, self.today - timedelta(days=2)}))
        on = [d["date"] for w in data["weeks"] for d in w["days"] if d["count"]]
        self.assertIn(self.today, on)
        page = self.client.get(url("insights")).content.decode().lower().split('id="fitness"', 1)[1].split("<script", 1)[0]   # the page's words
        for word in ("missed", "failed", "behind", "bad week", "lazy"):
            self.assertNotIn(word, page)
        self.assertIn("this week", self.client.get(url("dashboard")).content.decode().lower())


# ---------------------------------------------------------------------------
# The Bot API
# ---------------------------------------------------------------------------

class BotApiTests(Base):
    def token(self, scopes, user=None):
        return BotToken.issue(user or self.me, "Coach bot", scopes)[1]

    def call(self, method, path, raw, body=None):
        headers = {"HTTP_AUTHORIZATION": f"Bearer {raw}"}
        if method == "GET":
            return Client().get(API + path, **headers)
        return Client().post(API + path, data=json.dumps(body or {}), content_type="application/json", **headers)

    def test_scopes_are_required_and_offered(self):
        none = self.token(["meals:read"])
        for method, path in (("POST", "fitness/workouts"), ("POST", "fitness/food"), ("POST", "fitness/weight"),
                             ("GET", "fitness/today"), ("GET", "fitness/summary")):
            response = self.call(method, path, none, {"text": "squat 100x5"})
            self.assertEqual(response.status_code, 403, path)
        read = self.token(["fitness:read"])
        self.assertEqual(self.call("GET", "fitness/today", read).status_code, 200)
        self.assertEqual(self.call("POST", "fitness/weight", read, {"weight": 180}).status_code, 403)
        page = self.as_user(self.wife).get(reverse("albright_bot_api:my_keys")).content.decode()
        self.assertIn('value="fitness:read"', page)
        self.assertIn("Log workouts, food and weigh-ins for me", page)
        no_fitness = allow_api_keys(with_apps(User.objects.create_user(username="lee", password=PASSWORD), ["meals"]))
        raw = BotToken.issue(no_fitness, "x", ["fitness:read"])[1]
        self.assertEqual(self.call("GET", "fitness/today", raw).status_code, 403)
        self.assertTrue(BotRequestLog.objects.filter(endpoint="/api/bot/v1/fitness/today", status=200).exists())

    def test_text_workout_parses_reports_unknowns_records_and_checks_off_the_planner(self):
        program = Program.objects.create(owner=self.me, name="Strength")
        day = ProgramDay.objects.create(program=program, name="Legs", weekdays=[self.today.weekday()])
        ProgramExercise.objects.create(day=day, exercise=self.ex("Barbell Back Squat"), target_sets=3, target_reps=5)
        programs.schedule(program, start=self.today)
        item = ScheduleItem.objects.get(owner=self.me, source_id=f"day-{day.pk}")
        self.workout(7, [(200, 5)])                                       # an earlier session: today's can be a record
        raw = self.token(["fitness:write"])
        response = self.call("POST", "fitness/workouts", raw, {"text": "squat 225x5x3, zorbing 10x3"})
        self.assertEqual(response.status_code, 201)
        data = response.json()
        self.assertEqual(data["exercises"], [{"exercise": "Barbell Back Squat", "sets": [{"reps": 5, "weight": "225.00", "unit": "lb"}] * 3}])
        self.assertEqual(data["unknown"][0]["text"], "zorbing 10x3")
        self.assertTrue(data["records"])
        self.assertEqual((data["program_day"], data["planner_completed"]), ("Legs", True))
        self.assertTrue(ItemCompletion.objects.filter(item=item, occurrence_date=self.today).exists())
        self.assertEqual(Workout.objects.get(pk=data["id"]).owner, self.me)

    def test_structured_workout_and_nothing_parsed(self):
        raw = self.token(["fitness:write"])
        response = self.call("POST", "fitness/workouts", raw, {"exercises": [
            {"exercise": "bench", "sets": [{"reps": 8, "weight": 185}, {"reps": 7, "weight": 185}]},
            {"exercise": "run", "sets": [{"distance": 3.1, "distance_unit": "mi", "duration_seconds": 1710}]}]})
        self.assertEqual(response.status_code, 201)
        names = [e["exercise"] for e in response.json()["exercises"]]
        self.assertEqual(names, ["Barbell Bench Press", "Running"])
        self.assertEqual(response.json()["planner_completed"], False)
        response = self.call("POST", "fitness/workouts", raw, {"text": "zorbing 10x3"})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["unknown"][0]["text"], "zorbing 10x3")

    def test_food_by_recipe_my_food_quick_and_suggestions(self):
        raw = self.token(["fitness:write"])
        chili = Recipe.objects.create(household=self.household, title="Turkey chili", created_by=self.me, servings=4)
        recipe_nutrition.set_manual(chili, {"calories": D(400), "protein": D(30), "carbs": None, "fat": None, "fiber": None}, self.me)
        r = self.call("POST", "fitness/food", raw, {"recipe": "turkey CHILI", "servings": 1.5, "slot": "dinner"})
        self.assertEqual(r.status_code, 201)
        self.assertEqual((r.json()["calories"], r.json()["slot"]), ("600.0", "dinner"))
        self.assertEqual(self.call("POST", "fitness/food", raw, {"recipe_id": chili.pk}).json()["calories"], "400.0")
        miss = self.call("POST", "fitness/food", raw, {"recipe": "Turkey stew"})
        self.assertEqual(miss.status_code, 422)
        self.assertEqual(miss.json()["suggestions"]["recipes"], [{"id": chili.pk, "title": "Turkey chili"}])
        CustomFood.objects.create(owner=self.me, name="Protein shake", serving_label="1 scoop", calories=D(120), protein=D(24))
        r = self.call("POST", "fitness/food", raw, {"my_food": "protein shake", "servings": 2})
        self.assertEqual((r.status_code, r.json()["protein"]), (201, "48.0"))
        miss = self.call("POST", "fitness/food", raw, {"my_food": "Protein bar"})
        self.assertEqual((miss.status_code, miss.json()["suggestions"]["my_foods"]), (422, ["Protein shake"]))
        r = self.call("POST", "fitness/food", raw, {"label": "Latte", "calories": 190, "protein": 10})
        self.assertEqual((r.status_code, r.json()["source"], r.json()["fat"]), (201, "quick", None))
        guess = self.call("POST", "fitness/food", raw, {"food": "banana"})
        self.assertEqual(guess.status_code, 422)                           # USDA foods are never guessed
        self.assertEqual(FoodLogEntry.objects.filter(owner=self.me).count(), 4)
        self.assertFalse(FoodLogEntry.objects.filter(owner=self.wife).exists())

    def test_weight_today_and_summary(self):
        raw = self.token(["fitness:read", "fitness:write"])
        r = self.call("POST", "fitness/weight", raw, {"weight": 182.4, "unit": "lb"})
        self.assertEqual((r.status_code, r.json()["weight"], r.json()["replaced"]), (201, "182.4", False))
        r = self.call("POST", "fitness/weight", raw, {"weight": 82.5, "unit": "kg"})
        self.assertEqual((r.status_code, r.json()["replaced"]), (200, True))
        self.assertEqual(self.call("POST", "fitness/weight", raw, {"weight": 182, "date": (self.today + timedelta(days=1)).isoformat()}).status_code, 400)
        NutritionProfile.objects.filter(user=self.me).update(target_protein=150)
        NutritionProfile.load(self.me)
        FoodLogEntry.objects.create(owner=self.me, date=self.today, source="quick", label="x", protein=D(92))
        today = self.call("GET", "fitness/today", raw).json()
        self.assertEqual((today["macros"]["protein"]["eaten"], today["macros"]["protein"]["remaining"]), ("92", 58))
        self.workout(0, [(100, 5)])
        summary = self.call("GET", "fitness/summary?weeks=2", raw).json()
        self.assertEqual(len(summary["weeks"]), 2)
        self.assertEqual((summary["weeks"][0]["workouts_done"], summary["weeks"][0]["volume_lb"]), (1, 500))
        self.assertEqual(self.call("GET", "fitness/summary?weeks=99", raw).status_code, 400)

    def test_always_acts_as_the_token_owner(self):
        self.weigh(0, 200)
        FoodLogEntry.objects.create(owner=self.me, date=self.today, source="quick", label="Mine", protein=D(50))
        hers = self.token(["fitness:read", "fitness:write"], user=self.wife)
        today = self.call("GET", "fitness/today", hers).json()
        self.assertEqual(today["macros"]["protein"]["eaten"], "0")
        self.call("POST", "fitness/weight", hers, {"weight": 140})
        self.assertEqual(WeightEntry.objects.get(owner=self.wife).weight_kg, body.to_kg(140, "lb"))
        self.assertEqual(body.shown(WeightEntry.objects.get(owner=self.me).weight_kg, "lb"), D("200.0"))   # mine untouched


# ---------------------------------------------------------------------------
# Privacy
# ---------------------------------------------------------------------------

class PrivacyTests(Base):
    def test_body_metrics_and_insights_are_private_within_the_household(self):
        mine = self.weigh(0, 203.7)
        Measurement.objects.create(owner=self.me, date=self.today, waist=D("91.3"))
        profile = NutritionProfile.load(self.me)
        profile.goal_weight_kg = D("85.1")
        profile.save()
        self.workout(0, [(317.5, 5)])
        sam = self.as_user(self.wife)
        for path in (url("body"), url("insights"), url("dashboard")):
            page = sam.get(path).content.decode()
            for secret in ("203.7", "35.9", "187.6", "317.5", "1588"):
                self.assertNotIn(secret, page, path)
        self.assertEqual(sam.post(url("delete_weight", mine.pk)).status_code, 404)
        self.assertTrue(WeightEntry.objects.filter(pk=mine.pk).exists())
