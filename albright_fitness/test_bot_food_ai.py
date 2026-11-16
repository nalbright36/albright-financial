"""A person's AI logging food for them (Bot API fitness/food-entries, fitness:food:read / fitness:food:write): entries
for the key owner only; a missing scope is a 403 naming it; someone else's entry is a 404; items save all or none
with field errors; an idempotency key doesn't duplicate; a macro mismatch warns but saves; the meal and date come
from the person's own time zone; the day's totals and targets come back; the "AI estimate" badge, editing it ("AI ·
edited"), the instructions and the OpenAPI spec."""
import json
from datetime import datetime, timezone as dt_timezone
from decimal import Decimal as D
from unittest import mock

from django.test import Client
from django.urls import reverse

from albright_bot_api import fitness_api
from albright_bot_api.models import BotRequestLog, BotToken
from albright_planner.models import PlannerSettings

from .models import FoodLogEntry, NutritionProfile
from .test_body import API, Base

BOWL = {"name": "Chipotle chicken burrito bowl", "quantity": 1, "unit": "bowl", "grams": 550, "calories": 720,
        "protein_g": 48, "carbs_g": 70, "fat_g": 24, "fiber_g": 12, "sodium_mg": 1800, "confidence": "high",
        "notes": "white rice, black beans, mild salsa, cheese"}
EGGS = {"name": "Two fried eggs", "calories": 180, "protein_g": 12, "carbs_g": 1, "fat_g": 14, "confidence": "low"}


class AiFoodBase(Base):
    def setUp(self):
        super().setUp()
        self.raw = BotToken.issue(self.me, "My AI", ["fitness:food:read", "fitness:food:write"])[1]

    def call(self, method, path, body=None, raw=None):
        headers = {"HTTP_AUTHORIZATION": f"Bearer {raw or self.raw}"}
        if method == "GET":
            return Client().get(API + path, **headers)
        send = {"POST": Client().post, "PATCH": Client().patch, "DELETE": Client().delete}[method]
        return send(API + path, data=json.dumps(body or {}), content_type="application/json", **headers)

    def log(self, body, raw=None):
        return self.call("POST", "fitness/food-entries", body, raw)


class LoggingTests(AiFoodBase):
    def test_logging_creates_entries_for_the_key_owner_only_with_the_days_totals(self):
        NutritionProfile.objects.update_or_create(user=self.me, defaults={"target_calories": 2200, "target_protein": 150})
        r = self.log({"date": self.today.isoformat(), "meal": "lunch", "items": [BOWL, EGGS],
                      "source_text": "chipotle bowl and two eggs", "idempotency_key": "k1"})
        self.assertEqual(r.status_code, 201, r.content)
        data = r.json()
        self.assertEqual([e["name"] for e in data["entries"]], [BOWL["name"], EGGS["name"]])
        self.assertEqual(data["totals"], {"calories": 900.0, "protein_g": 60.0, "carbs_g": 71.0, "fat_g": 38.0})
        self.assertEqual((data["targets"]["calories"], data["targets"]["protein_g"]), (2200, 150))
        self.assertEqual(data["warnings"], [])
        rows = FoodLogEntry.objects.order_by("pk")
        self.assertEqual({e.owner for e in rows}, {self.me})
        self.assertFalse(FoodLogEntry.objects.filter(owner=self.wife).exists())
        bowl = rows[0]
        self.assertEqual((bowl.source, bowl.slot, bowl.confidence, bowl.portion_label, bowl.grams, bowl.calories, bowl.sodium_mg),
                         ("ai", "lunch", "high", "1 bowl", D("550.0"), D("720.00"), D("1800.00")))
        self.assertEqual((bowl.source_text, bowl.idempotency_key, bowl.bot_key.name), ("chipotle bowl and two eggs", "k1", "My AI"))
        log = BotRequestLog.objects.filter(endpoint="/api/bot/v1/fitness/food-entries", status=201).get()
        self.assertIn(f"#{rows[0].pk}, #{rows[1].pk}", log.summary)
        self.assertEqual(log.token_name, "My AI")

    def test_get_a_day_and_recent(self):
        self.log({"items": [BOWL]})
        self.log({"items": [BOWL]})
        day = self.call("GET", f"fitness/food-entries?date={self.today}").json()
        self.assertEqual((len(day["entries"]), day["totals"]["calories"], day["targets"]), (2, 1440.0, None))
        recent = self.call("GET", "fitness/food-entries/recent?limit=5").json()["foods"]
        self.assertEqual(recent[0]["times_logged"], 2)
        self.assertEqual(recent[0]["item"]["name"], BOWL["name"])
        self.assertEqual(recent[0]["item"]["calories"], 720.0)

    def test_one_bad_item_saves_nothing_and_says_which_field(self):
        bad = dict(EGGS, calories=-5)
        del bad["protein_g"]
        r = self.log({"items": [BOWL, bad, dict(BOWL, calories=6000, grams=9000)], "meal": "brunch"})
        self.assertEqual(r.status_code, 400)
        fields = {e["field"]: e["message"] for e in r.json()["errors"]}
        self.assertEqual(fields["items[1].calories"], "can't be negative")
        self.assertEqual(fields["items[1].protein_g"], "is required")
        self.assertIn("items[2].calories", fields)
        self.assertIn("items[2].grams", fields)
        self.assertIn("meal", fields)
        self.assertFalse(FoodLogEntry.objects.exists())
        self.assertEqual(self.log({"items": []}).status_code, 400)
        self.assertEqual(self.log({"items": [EGGS] * 21}).status_code, 400)
        self.assertFalse(FoodLogEntry.objects.exists())

    def test_a_save_that_fails_halfway_leaves_nothing(self):
        real_save = FoodLogEntry.save
        calls = []

        def save(entry, *args, **kwargs):
            calls.append(1)
            if len(calls) == 2:
                raise RuntimeError("database hiccup")
            return real_save(entry, *args, **kwargs)
        with mock.patch.object(FoodLogEntry, "save", save), self.assertRaises(RuntimeError):
            self.log({"items": [BOWL, EGGS]})
        self.assertFalse(FoodLogEntry.objects.exists())

    def test_the_same_idempotency_key_doesnt_duplicate(self):
        first = self.log({"items": [BOWL, EGGS], "idempotency_key": "meal-123"})
        again = self.log({"items": [BOWL, EGGS], "idempotency_key": "meal-123"})
        self.assertEqual((first.status_code, again.status_code), (201, 200))
        self.assertTrue(again.json()["replayed"])
        self.assertEqual([e["id"] for e in again.json()["entries"]], [e["id"] for e in first.json()["entries"]])
        self.assertEqual(FoodLogEntry.objects.count(), 2)
        self.assertEqual(self.log({"items": [EGGS], "idempotency_key": "meal-456"}).status_code, 201)
        self.assertEqual(FoodLogEntry.objects.count(), 3)

    def test_a_macro_mismatch_warns_but_saves(self):
        r = self.log({"items": [dict(EGGS, name="Mystery", calories=900)]})      # 4*12 + 4*1 + 9*14 = 178 kcal
        self.assertEqual(r.status_code, 201)
        self.assertEqual(len(r.json()["warnings"]), 1)
        self.assertIn("Mystery", r.json()["warnings"][0])
        self.assertEqual(FoodLogEntry.objects.get().calories, D("900.00"))
        close = self.log({"items": [dict(EGGS, calories=215)]})                 # 37 kcal off: within 50
        self.assertEqual(close.json()["warnings"], [])


class TimeZoneTests(AiFoodBase):
    def at(self, utc):
        return mock.patch("django.utils.timezone.now", return_value=utc)

    def test_meal_and_date_come_from_the_persons_local_time(self):
        PlannerSettings.objects.update_or_create(user=self.me, defaults={"timezone": "America/New_York"})
        cases = [(datetime(2026, 11, 15, 14, 0, tzinfo=dt_timezone.utc), "breakfast", "2026-11-15"),   # 9:00 ET
                 (datetime(2026, 11, 15, 16, 0, tzinfo=dt_timezone.utc), "lunch", "2026-11-15"),       # 11:00 ET
                 (datetime(2026, 11, 15, 20, 30, tzinfo=dt_timezone.utc), "dinner", "2026-11-15"),     # 15:30 ET
                 (datetime(2026, 11, 16, 3, 30, tzinfo=dt_timezone.utc), "snack", "2026-11-15")]       # 22:30 ET
        for utc, meal, day in cases:
            with self.subTest(utc=utc), self.at(utc):
                r = self.log({"items": [EGGS]})
                self.assertEqual(r.status_code, 201, r.content)
                self.assertEqual((r.json()["entries"][0]["meal"], r.json()["date"]), (meal, day))

    def test_meal_for(self):
        self.assertEqual(fitness_api.meal_for(datetime(2026, 1, 1, 10, 29)), "breakfast")
        self.assertEqual(fitness_api.meal_for(datetime(2026, 1, 1, 10, 30)), "lunch")
        self.assertEqual(fitness_api.meal_for(datetime(2026, 1, 1, 15, 0)), "dinner")
        self.assertEqual(fitness_api.meal_for(datetime(2026, 1, 1, 21, 0)), "snack")


class AccessTests(AiFoodBase):
    def test_missing_scope_is_a_403_naming_it(self):
        read_only = BotToken.issue(self.me, "Reader", ["fitness:food:read"])[1]
        r = self.log({"items": [EGGS]}, raw=read_only)
        self.assertEqual(r.status_code, 403)
        self.assertIn("fitness:food:write", r.json()["error"])
        old = BotToken.issue(self.me, "Old", ["fitness:read", "fitness:write"])[1]
        self.assertEqual(self.log({"items": [EGGS]}, raw=old).status_code, 403)
        self.assertEqual(self.call("GET", "fitness/food-entries", raw=old).status_code, 403)
        self.assertEqual(self.call("GET", "fitness/foods/search?q=egg", raw=old).status_code, 200)    # either scope
        self.assertEqual(self.call("GET", "fitness/foods/search?q=egg").status_code, 200)
        self.assertFalse(FoodLogEntry.objects.exists())

    def test_patch_and_delete_only_your_own_entries(self):
        theirs = FoodLogEntry.objects.create(owner=self.wife, date=self.today, slot="lunch", source="quick", label="Hers",
                                             calories=D(100))
        self.assertEqual(self.call("PATCH", f"fitness/food-entries/{theirs.pk}", {"calories": 1}).status_code, 404)
        self.assertEqual(self.call("DELETE", f"fitness/food-entries/{theirs.pk}").status_code, 404)
        theirs.refresh_from_db()
        self.assertEqual(theirs.calories, D("100.00"))
        mine_id = self.log({"items": [BOWL]}).json()["entries"][0]["id"]
        r = self.call("PATCH", f"fitness/food-entries/{mine_id}", {"calories": 650, "meal": "dinner", "quantity": 2})
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual((r.json()["entry"]["calories"], r.json()["entry"]["meal"], r.json()["totals"]["calories"]),
                         (650.0, "dinner", 650.0))
        entry = FoodLogEntry.objects.get(pk=mine_id)
        self.assertEqual((entry.portion_label, entry.source, entry.edited), ("2 bowl", "ai", False))
        bad = self.call("PATCH", f"fitness/food-entries/{mine_id}", {"protein_g": -1, "owner": 2})
        self.assertEqual({e["field"] for e in bad.json()["errors"]}, {"protein_g", "owner"})
        self.assertEqual(self.call("DELETE", f"fitness/food-entries/{mine_id}").json()["totals"]["calories"], 0.0)
        self.assertFalse(FoodLogEntry.objects.filter(pk=mine_id).exists())

    def test_writes_are_rate_limited_per_key(self):
        with mock.patch.object(fitness_api, "FOOD_WRITES_PER_HOUR", 2):
            self.assertEqual(self.log({"items": [EGGS]}).status_code, 201)
            self.assertEqual(self.log({"items": [EGGS]}).status_code, 201)
            self.assertEqual(self.log({"items": [EGGS]}).status_code, 429)
        self.assertEqual(FoodLogEntry.objects.count(), 2)

    def test_openapi_spec(self):
        spec = Client().get(API + "fitness/openapi.json").json()
        self.assertEqual(spec["openapi"], "3.1.0")
        self.assertIn("/fitness/food-entries", spec["paths"])
        self.assertIn("/fitness/food-entries/{id}", spec["paths"])
        self.assertTrue(spec["servers"][0]["url"].endswith("/api/bot/v1"))


class FoodPageTests(AiFoodBase):
    def test_ai_entries_show_the_badge_and_count_in_the_totals(self):
        self.log({"meal": "lunch", "items": [BOWL, EGGS], "source_text": "chipotle bowl"})
        page = self.client.get(reverse("albright_fitness:food")).content.decode()
        self.assertEqual(page.count(">AI estimate</span>"), 2)
        self.assertEqual(page.count(">low confidence</span>"), 1)
        self.assertIn("900", page)                                   # the day's calories include them

    def test_editing_an_ai_entry_keeps_ai_and_marks_it_edited(self):
        entry_id = self.log({"items": [BOWL], "source_text": "chipotle bowl, extra cheese"}).json()["entries"][0]["id"]
        panel = self.client.get(reverse("albright_fitness:food_edit", args=[entry_id])).content.decode()
        self.assertIn("chipotle bowl, extra cheese", panel)
        self.assertIn("white rice, black beans", panel)
        self.client.post(reverse("albright_fitness:food_edit", args=[entry_id]),
                         {"action": "save", "slot": "dinner", "label": "Burrito bowl", "calories": "650", "protein": "45",
                          "carbs": "60", "fat": "20", "fiber": ""})
        entry = FoodLogEntry.objects.get(pk=entry_id)
        self.assertEqual((entry.source, entry.edited, entry.calories, entry.label, entry.slot),
                         ("ai", True, D("650.00"), "Burrito bowl", "dinner"))
        page = self.client.get(reverse("albright_fitness:food")).content.decode()
        self.assertIn("AI &middot; edited", page)

    def test_connect_your_ai_instructions_never_hold_a_key(self):
        page = self.client.get(reverse("albright_fitness:food_ai")).content.decode()
        self.assertIn("/api/bot/v1/docs", page)
        self.assertIn("idempotency_key", page)
        self.assertIn("For food logging: estimate macros per item", page)
        self.assertNotIn(self.raw, page)
        self.assertNotIn("abt_", page)
        self.assertIn("Log food with your AI", self.client.get(reverse("albright_fitness:food")).content.decode())
        keys = self.client.get(reverse("labs:api_keys")).content.decode()
        self.assertIn('id="connect"', keys)
        self.assertIn(">Fitness</p>", keys)
        self.assertIn("Read your food log", keys)
        self.assertIn("Add and edit food entries", keys)
