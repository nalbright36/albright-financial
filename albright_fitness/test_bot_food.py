"""Bot API: the one food search, a USDA pick by fdc_id with any quantity + unit, My foods by unit, and a day's
entries - never a guessed food or unit."""
import json
from decimal import Decimal as D
from unittest import mock

from django.test import Client

from albright_bot_api.models import BotToken

from . import usda
from .models import CustomFood, Food, FoodLogEntry
from .test_body import API, Base
from .test_food_amounts import YOGURT


class BotFoodTests(Base):
    def call(self, method, path, raw, body=None):
        headers = {"HTTP_AUTHORIZATION": f"Bearer {raw}"}
        if method == "GET":
            return Client().get(API + path, **headers)
        return Client().post(API + path, data=json.dumps(body or {}), content_type="application/json", **headers)

    def setUp(self):
        super().setUp()
        self.raw = BotToken.issue(self.me, "Coach", ["fitness:read", "fitness:write"])[1]
        self.food = Food.objects.create(**YOGURT)

    def test_usda_pick_with_a_fraction_and_a_unit(self):
        r = self.call("POST", "fitness/food", self.raw, {"fdc_id": 1, "quantity": "1/4", "unit": "1 container (170 g)", "slot": "breakfast"})
        self.assertEqual(r.status_code, 201, r.content)
        data = r.json()
        self.assertEqual((data["quantity"], data["unit"], data["amount"], data["grams"], data["calories"], data["fdc_id"]),
                         ("0.25", "p:1 container (170 g)", "¼ container (170 g)", "42.5", "25.1", 1))
        self.assertEqual(self.call("POST", "fitness/food", self.raw, {"fdc_id": 1, "quantity": 3, "unit": "oz"}).json()["grams"], "85.0")

    def test_unknown_unit_or_none_lists_the_units_and_logs_nothing(self):
        r = self.call("POST", "fitness/food", self.raw, {"fdc_id": 1, "quantity": 1, "unit": "slice"})
        self.assertEqual(r.status_code, 422)
        self.assertIn({"unit": "g", "label": "g"}, r.json()["units"])
        self.assertEqual(self.call("POST", "fitness/food", self.raw, {"fdc_id": 1}).status_code, 422)
        self.assertEqual(self.call("POST", "fitness/food", self.raw, {"fdc_id": 1, "quantity": "lots", "unit": "g"}).status_code, 400)
        self.assertEqual(self.call("POST", "fitness/food", self.raw, {"fdc_id": 999, "quantity": 1, "unit": "g"}).status_code, 422)
        self.assertEqual(self.call("POST", "fitness/food", self.raw, {"food": "yogurt"}).status_code, 422)   # by name: never
        self.assertFalse(FoodLogEntry.objects.exists())

    def test_my_food_by_unit_and_the_old_servings_still_work(self):
        CustomFood.objects.create(owner=self.me, name="Shake", serving_label="1 bottle", serving_grams=D(330), calories=D(160))
        r = self.call("POST", "fitness/food", self.raw, {"my_food": "shake", "quantity": 165, "unit": "g"})
        self.assertEqual((r.status_code, r.json()["calories"]), (201, "80.0"))
        old = self.call("POST", "fitness/food", self.raw, {"my_food": "Shake", "servings": 2}).json()
        self.assertEqual((old["calories"], old["quantity"], old["unit"]), ("320.0", "2", "serving"))

    def test_search_gives_ids_units_and_a_ready_body(self):
        CustomFood.objects.create(owner=self.me, name="Greek yogurt bowl", calories=D(300))
        with mock.patch.object(usda, "configured", return_value=True), mock.patch.object(usda, "search", return_value=[self.food]):
            data = self.call("GET", "fitness/foods/search?q=greek", self.raw).json()
        groups = {g["group"]: g for g in data["groups"]}
        self.assertEqual(groups["mine"]["items"][0]["log"], {"my_food": "Greek yogurt bowl", "quantity": 1, "unit": "serving"})
        usda_item = groups["usda"]["items"][0]
        self.assertEqual((usda_item["fdc_id"], usda_item["units"][0]["unit"]), (1, "p:1 container (170 g)"))
        logged = self.call("POST", "fitness/food", self.raw, usda_item["log"])
        self.assertEqual(logged.status_code, 201)
        self.assertEqual(self.call("GET", "fitness/foods/search", self.raw).status_code, 400)
        self.assertIsNotNone(self.call("GET", "fitness/foods/search?q=greek", self.raw).json()["note"])   # no key here

    def test_a_days_entries_are_mine_only(self):
        self.call("POST", "fitness/food", self.raw, {"fdc_id": 1, "quantity": 1, "unit": "1 cup", "slot": "lunch"})
        self.call("POST", "fitness/food", self.raw, {"label": "Latte", "calories": 190, "slot": "breakfast"})
        FoodLogEntry.objects.create(owner=self.wife, date=self.today, source="quick", label="Sam's", calories=D(1))
        data = self.call("GET", "fitness/food/entries", self.raw).json()
        self.assertEqual([e["label"] for e in data["entries"]], ["Latte", "Greek yogurt, plain"])
        self.assertEqual(data["totals"]["calories"], "334.6")
        self.assertEqual(self.call("GET", "fitness/food/entries?date=nope", self.raw).status_code, 400)
        write_only = BotToken.issue(self.me, "W", ["fitness:write"])[1]
        self.assertEqual(self.call("GET", "fitness/food/entries", write_only).status_code, 403)
