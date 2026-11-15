"""Food amounts, MyFitnessPal-style (amounts.py): any quantity - decimals and fractions - of any unit the food
supports, kept on the entry (quantity + unit + grams) so it can be changed later; and the backfill that gives
every existing entry its amount without changing its numbers."""
import importlib
from datetime import date
from decimal import Decimal as D

from django.apps import apps as django_apps

from . import amounts
from .models import CustomFood, Food, FoodLogEntry
from .tests import Base, url

YOGURT = {"fdc_id": 1, "name": "Greek yogurt, plain", "calories": D("59"), "protein": D("10.3"), "carbs": D("3.6"),
          "fat": D("0.4"), "fiber": D("0"), "detailed": True,
          "portions": [{"label": "1 container (170 g)", "grams": 170.0}, {"label": "1 cup", "grams": 245.0}]}


class QuantityTests(Base):
    def test_decimals_and_fractions(self):
        cases = {"2": "2", "1.5": "1.5", ".3": "0.3", "1/4": "0.25", "1 1/2": "1.5", "1-1/2": "1.5", "½": "0.5",
                 "1½": "1.5", "2,5": "2.5", " 3 ": "3", "1/3": "0.333"}
        for text, want in cases.items():
            self.assertEqual(amounts.parse_quantity(text), D(want), text)
        for bad in ("", "0", "-1", "abc", "1/0", "5001", "1//2", "0.0001"):
            self.assertIsNone(amounts.parse_quantity(bad), bad)

    def test_shown_nicely(self):
        self.assertEqual([amounts.show_quantity(D(x)) for x in ("0.25", "1.5", "2", "0.3", "0.333")], ["¼", "1½", "2", "0.3", "⅓"])


class UnitTests(Base):
    def test_usda_food_has_its_portions_weights_and_volumes(self):
        food = Food.objects.create(**YOGURT)
        basis = amounts.for_food(food)
        keys = [u.key for u in basis.units]
        self.assertEqual(keys[:4], ["p:1 container (170 g)", "p:1 cup", "g", "oz"])
        self.assertIn("ml", keys)
        self.assertIn("tbsp", keys)
        self.assertNotIn("cup", keys)                                   # already a portion
        self.assertEqual(basis.unit("p:1 cup").label, "1 cup (245 g)")
        quarter = basis.amount(amounts.parse_quantity("1/4"), "p:1 container (170 g)")
        self.assertEqual(quarter["grams"], D("42.5"))
        self.assertEqual(quarter["totals"]["calories"], D("25.08"))        # 59 kcal / 100 g x 42.5 g
        self.assertEqual(quarter["portion_label"], "¼ container (170 g)")
        self.assertEqual(basis.amount(D(3), "oz")["grams"], D("85.0"))
        self.assertIsNone(basis.amount(D(1), "slice"))
        self.assertEqual(basis.unit("1 CUP").key, "p:1 cup")             # by label too (the Bot API)

    def test_my_food_serving_with_weight_and_without(self):
        mine = CustomFood.objects.create(owner=self.me, name="Shake", serving_label="1 bottle", serving_grams=D(330),
                                         serving_ml=D(325), calories=D(160), protein=D(30))
        basis = amounts.for_custom(mine)
        self.assertEqual(basis.units[0].label, "1 bottle (330 g, 325 ml)")
        self.assertEqual(basis.amount(D("0.5"), "serving")["totals"]["protein"], D("15.00"))
        self.assertEqual(basis.amount(D(165), "g")["totals"]["calories"], D("80.00"))
        self.assertEqual(basis.amount(D(1), "floz")["grams"], D("30.0"))
        bare = amounts.for_custom(CustomFood.objects.create(owner=self.me, name="Bar", calories=D(200)))
        self.assertEqual([u.key for u in bare.units], ["serving"])


class EntryAmountTests(Base):
    def test_logging_a_usda_portion_and_my_food_keep_quantity_and_unit(self):
        food = Food.objects.create(**YOGURT)
        self.client.post(url("food_usda", food.fdc_id), {"portion": "0", "amount": "2", "slot": "breakfast"})
        e = FoodLogEntry.objects.get(source="usda")
        self.assertEqual((e.quantity, e.unit, e.grams), (D(2), "p:1 container (170 g)", D("340.0")))
        mine = CustomFood.objects.create(owner=self.me, name="Bar", calories=D(200))
        self.client.post(url("food_log"), {"kind": "custom", "food": mine.pk, "servings": "1.5"})
        e = FoodLogEntry.objects.get(source="custom")
        self.assertEqual((e.quantity, e.unit), (D("1.5"), "serving"))

    def test_changing_an_entry_and_when_the_food_is_gone(self):
        food = Food.objects.create(**YOGURT)
        e = FoodLogEntry(owner=self.me, date=date.today(), source="usda", food=food, label=food.name)
        basis = amounts.basis_for_entry(e)
        self.assertTrue(amounts.apply(e, basis, D(1), "p:1 cup"))
        e.save()
        self.assertEqual((e.calories, e.portion_label), (D("144.55"), "1 cup (245 g)"))
        self.assertFalse(amounts.apply(e, basis, D(1), "slice"))
        self.assertFalse(amounts.apply(e, basis, D(5000), "p:1 cup"))      # over 5 kg
        food.delete()
        e.refresh_from_db()
        gone = amounts.basis_for_entry(e)
        self.assertEqual([u.key for u in gone.units], ["entry", "g", "oz"])
        amounts.apply(e, gone, D("0.5"), "entry")
        self.assertEqual(e.calories, D("72.28"))


class BackfillTests(Base):
    def test_existing_entries_learn_their_amount_numbers_unchanged(self):
        mig = importlib.import_module("albright_fitness.migrations.0012_food_amounts")
        food = Food.objects.create(**YOGURT)
        day = date.today()
        rows = [FoodLogEntry.objects.create(owner=self.me, date=day, source="usda", food=food, label="Y", grams=D("490"),
                                            portion_label="2 × 1 cup", calories=D("289.1")),
                FoodLogEntry.objects.create(owner=self.me, date=day, source="usda", food=food, label="Y", grams=D("170"),
                                            portion_label="1 container (170 g)", calories=D("100.3")),
                FoodLogEntry.objects.create(owner=self.me, date=day, source="usda", food=food, label="Y", grams=D("85"), calories=D("50")),
                FoodLogEntry.objects.create(owner=self.me, date=day, source="custom", label="Bar", servings=D("1.5"), calories=D("300")),
                FoodLogEntry.objects.create(owner=self.me, date=day, source="quick", label="Latte", calories=D("190"))]
        mig.backfill(django_apps, None)
        got = [(e.quantity, e.unit, e.calories) for e in FoodLogEntry.objects.filter(pk__in=[r.pk for r in rows]).order_by("pk")]
        self.assertEqual(got, [(D(2), "p:1 cup", D("289.1")), (D(1), "p:1 container (170 g)", D("100.3")), (D(85), "g", D("50")),
                               (D("1.5"), "serving", D("300")), (None, "", D("190"))])

    def test_existing_programs_start_when_made_and_remember_the_planner(self):
        from . import programs
        mig = importlib.import_module("albright_fitness.migrations.0011_program_setup")
        on = programs.copy_template(self.me, "full_body")
        programs.schedule(on)
        off = programs.copy_template(self.me, "running", active=False)
        mig.backfill(django_apps, None)
        on.refresh_from_db()
        off.refresh_from_db()
        self.assertEqual((on.on_planner, on.start_date), (True, on.scheduled_from))
        self.assertEqual((off.on_planner, off.is_draft), (False, False))  # an old "off" program is paused, not a draft
