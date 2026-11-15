"""Adding food the easy way (food_find_views): one search box, the MyFitnessPal-style amount panel, editing a logged
entry, "Enter it yourself" (saved to My foods + logged in one go), Undo - and it's all private."""
from decimal import Decimal as D
from unittest import mock

from django.test import override_settings

from . import usda
from .models import CustomFood, Food, FoodLogEntry
from .test_food_amounts import YOGURT
from .tests import Base, url

XHR = {"HTTP_X_REQUESTED_WITH": "XMLHttpRequest"}


@override_settings(USDA_API_KEY="")
class FindTests(Base):
    def test_one_box_over_recent_and_my_foods_and_a_plain_note_without_usda(self):
        mine = CustomFood.objects.create(owner=self.me, name="Greek yogurt cup", serving_label="1 cup", calories=D(120))
        CustomFood.objects.create(owner=self.friend, name="Greek yogurt secret", calories=D(1))
        data = self.client.get(url("food_find") + "?q=greek&format=json").json()
        self.assertEqual([g["key"] for g in data["groups"]], ["mine"])
        self.assertEqual(data["groups"][0]["items"][0]["url"], url("food_pick", "custom", mine.pk))
        self.assertIn("isn't set up", data["note"])
        page = self.client.get(url("food_find") + "?q=greek").content.decode()
        self.assertIn("Greek yogurt cup", page)
        self.assertNotIn("secret", page)
        self.assertIn('id="fx-food-new"', page)

    def test_usda_results_when_the_key_is_set(self):
        food = Food.objects.create(**YOGURT)
        with mock.patch.object(usda, "configured", return_value=True), mock.patch.object(usda, "search", return_value=[food]):
            data = self.client.get(url("food_find") + "?q=yogurt&format=json").json()
        self.assertEqual(data["groups"][-1]["items"][0]["url"], url("food_pick", "usda", food.fdc_id))
        with mock.patch.object(usda, "configured", return_value=True), \
                mock.patch.object(usda, "search", side_effect=usda.UsdaError("USDA is busy - try again in a minute.")):
            self.assertEqual(self.client.get(url("food_find") + "?q=yogurt&format=json").json()["note"], "USDA is busy - try again in a minute.")

    def test_quarter_of_a_container_then_undo(self):
        food = Food.objects.create(**YOGURT)
        panel = self.client.get(url("food_pick", "usda", food.fdc_id) + "?slot=breakfast", **XHR).content.decode()
        self.assertNotIn("<html", panel)                                  # the sheet's fragment
        self.assertIn('value="p:1 container (170 g)"', panel)
        self.assertIn('value="oz"', panel)
        self.assertIn("data-menu", panel)
        r = self.client.post(url("food_pick", "usda", food.fdc_id), {"quantity": "1/4", "unit": "p:1 container (170 g)",
                                                                      "slot": "breakfast", "date": self.today.isoformat()})
        self.assertEqual(r.status_code, 302)
        e = FoodLogEntry.objects.get(owner=self.me)
        self.assertEqual((e.quantity, e.grams, e.calories, e.portion_label, e.slot),
                         (D("0.25"), D("42.5"), D("25.08"), "¼ container (170 g)", "breakfast"))
        diary = self.client.get(url("food")).content.decode()
        self.assertIn('id="fx-undo"', diary)
        self.assertNotIn('id="fx-undo"', self.client.get(url("food")).content.decode())   # once
        again = self.client.get(url("food_pick", "usda", food.fdc_id)).content.decode()
        self.assertIn('value="1/4"', again)                              # starts at what I had last time
        self.client.post(url("food_pick", "usda", food.fdc_id), {"quantity": "2", "unit": "oz", "slot": "lunch"})
        self.client.post(url("food_undo"))
        self.assertEqual(list(FoodLogEntry.objects.values_list("pk", flat=True)), [e.pk])

    def test_bad_amount_or_unit_logs_nothing(self):
        mine = CustomFood.objects.create(owner=self.me, name="Bar", calories=D(200))
        r = self.client.post(url("food_pick", "custom", mine.pk), {"quantity": "a lot", "unit": "serving", "slot": "snack"})
        self.assertContains(r, "Enter an amount like 1, 1.5, .3 or 1/4.")
        r = self.client.post(url("food_pick", "custom", mine.pk), {"quantity": "1", "unit": "g", "slot": "snack"})
        self.assertContains(r, "Pick one of the units")
        self.assertFalse(FoodLogEntry.objects.exists())

    def test_tap_an_entry_to_change_amount_unit_and_meal_or_remove(self):
        food = Food.objects.create(**YOGURT)
        self.client.post(url("food_pick", "usda", food.fdc_id), {"quantity": "1", "unit": "p:1 container (170 g)", "slot": "breakfast"})
        e = FoodLogEntry.objects.get()
        self.assertIn(url("food_edit", e.pk), self.client.get(url("food")).content.decode())
        self.client.post(url("food_edit", e.pk), {"quantity": "100", "unit": "g", "slot": "dinner"})
        e.refresh_from_db()
        self.assertEqual((e.quantity, e.unit, e.grams, e.calories, e.slot), (D(100), "g", D("100.0"), D("59.00"), "dinner"))
        self.client.post(url("food_edit", e.pk), {"action": "remove"})
        self.assertFalse(FoodLogEntry.objects.exists())

    def test_quick_add_edits_its_numbers(self):
        e = FoodLogEntry.objects.create(owner=self.me, date=self.today, source="quick", label="Latte", calories=D(190))
        self.assertContains(self.client.get(url("food_edit", e.pk)), 'name="calories"')
        self.client.post(url("food_edit", e.pk), {"calories": "220", "protein": "12", "slot": "snack", "label": "Latte"})
        e.refresh_from_db()
        self.assertEqual((e.calories, e.protein), (D("220.00"), D("12.00")))

    def test_enter_it_yourself_saves_and_logs_in_one_go(self):
        self.assertContains(self.client.get(url("food_new") + "?q=chobani"), 'value="chobani"')
        self.client.post(url("food_new"), {"name": "Chobani", "serving_label": "1 container", "serving_grams": "150",
                                           "calories": "120", "protein": "14", "quantity": "1/2", "slot": "snack", "save": "1"})
        mine = CustomFood.objects.get(owner=self.me)
        self.assertEqual((mine.serving_label, mine.serving_grams), ("1 container", D("150.0")))
        e = FoodLogEntry.objects.get()
        self.assertEqual((e.custom_food, e.quantity, e.unit, e.calories, e.grams), (mine, D("0.5"), "serving", D("60.00"), D("75.0")))
        self.client.post(url("food_new"), {"name": "Office cake", "calories": "300", "quantity": "2", "slot": "snack"})
        cake = FoodLogEntry.objects.get(label="Office cake")
        self.assertEqual((cake.source, cake.calories, CustomFood.objects.count()), ("quick", D("600.00"), 1))
        r = self.client.post(url("food_new"), {"name": "", "quantity": "x"})
        self.assertContains(r, "Give it a name.")

    def test_my_foods_can_say_what_a_serving_weighs_or_holds(self):
        self.client.post(url("my_foods"), {"name": "Oat milk", "serving_label": "1 cup", "serving_ml": "240", "calories": "120"})
        self.assertEqual(CustomFood.objects.get(name="Oat milk").serving_ml, D("240.0"))
        self.assertContains(self.client.get(url("my_foods")), "1 cup = 240 ml")

    def test_private(self):
        theirs = CustomFood.objects.create(owner=self.friend, name="Theirs", calories=D(1))
        entry = FoodLogEntry.objects.create(owner=self.friend, date=self.today, source="quick", label="x", calories=D(1))
        self.assertEqual(self.client.get(url("food_pick", "custom", theirs.pk)).status_code, 404)
        self.assertEqual(self.client.get(url("food_pick", "entry", entry.pk)).status_code, 404)
        self.assertEqual(self.client.post(url("food_edit", entry.pk), {"action": "remove"}).status_code, 404)
        self.assertEqual(self.client.get(url("food_pick", "nope", 1)).status_code, 404)
        session = self.client.session
        session["fx_food_undo"] = {"ids": [entry.pk], "text": "x"}
        session.save()
        self.client.post(url("food_undo"))
        self.assertTrue(FoodLogEntry.objects.filter(pk=entry.pk).exists())
