"""Fitness Phase 2 - nutrition. The USDA API is always mocked (FakeUsda counts every request): search, details and
the Food cache (a second lookup makes no request), retries, the key in a header, and the "USDA key not set" message;
the targets calculator and its guardrails; gram conversions; the diary (totals, what's left, over shown neutrally,
quick add, My foods, Recent, copy yesterday, USDA portions); recipe nutrition (matching, ignored lines, overrides,
per serving, manual, shown on cards and the page, following the recipe's visibility); logging a planned meal or a
recipe; the Meals week's planned macros; the dashboard and launcher line; and privacy between two people in the
same household."""
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from albright_labs.testing import kitchen, with_apps
from albright_meals import recipe_nutrition
from albright_meals.models import MealPlanEntry, Recipe, RecipeNutrition

from . import nutrition, stats, usda
from .models import CustomFood, Food, FoodLogEntry, NutritionProfile

PASSWORD = "pw-not-real-12345"
D = Decimal
KEY = "test-usda-key-not-real"

CHICKEN = {"fdcId": 171477, "description": "Chicken, broiler, breast, meat only, cooked, roasted", "dataType": "SR Legacy",
           "foodNutrients": [{"nutrientId": 1008, "nutrientNumber": "208", "unitName": "KCAL", "value": 165},
                             {"nutrientId": 1003, "unitName": "G", "value": 31.0}, {"nutrientId": 1004, "unitName": "G", "value": 3.57},
                             {"nutrientId": 1005, "unitName": "G", "value": 0}, {"nutrientId": 1093, "unitName": "MG", "value": 74}]}
RICE_SEARCH = {"fdcId": 168878, "description": "Rice, white, long-grain, cooked", "dataType": "SR Legacy",
               "foodNutrients": [{"nutrientId": 1008, "unitName": "KCAL", "value": 130}, {"nutrientId": 1003, "unitName": "G", "value": 2.69},
                                 {"nutrientId": 1005, "unitName": "G", "value": 28.17}, {"nutrientId": 1004, "unitName": "G", "value": 0.28}]}
RICE_DETAIL = {"fdcId": 168878, "description": "Rice, white, long-grain, cooked", "dataType": "SR Legacy",
               "foodNutrients": [{"nutrient": {"id": 1008, "number": "208", "unitName": "kcal"}, "amount": 130},
                                 {"nutrient": {"id": 1003, "number": "203", "unitName": "g"}, "amount": 2.69},
                                 {"nutrient": {"id": 1005, "number": "205", "unitName": "g"}, "amount": 28.17},
                                 {"nutrient": {"id": 1004, "number": "204", "unitName": "g"}, "amount": 0.28},
                                 {"nutrient": {"id": 1079, "number": "291", "unitName": "g"}, "amount": 0.4}],
               "foodPortions": [{"amount": 1.0, "modifier": "cup", "gramWeight": 158.0, "measureUnit": {"name": "undetermined"}}]}
EGG_DETAIL = {"fdcId": 748967, "description": "Eggs, Grade A, Large, egg whole", "dataType": "Foundation",
              "foodNutrients": [{"nutrient": {"id": 2047, "number": "957", "unitName": "kcal"}, "amount": 148},
                                {"nutrient": {"id": 1003, "number": "203", "unitName": "g"}, "amount": 12.4}],
              "foodPortions": [{"amount": 1, "modifier": "large", "gramWeight": 50.3, "measureUnit": {"name": "undetermined"}}]}
YOGURT = {"fdcId": 2001, "description": "Greek yogurt, plain", "dataType": "Branded", "brandOwner": "Example Dairy",
          "servingSize": 170, "servingSizeUnit": "g", "householdServingFullText": "1 container",
          "foodNutrients": [{"nutrientId": 1008, "unitName": "KCAL", "value": 59}, {"nutrientId": 1003, "unitName": "G", "value": 10.3}]}


class FakeResponse:
    def __init__(self, status, data=None, headers=None):
        self.status_code, self._data, self.headers = status, data, headers or {}

    def json(self):
        return self._data


class FakeUsda:
    """Stands in for requests.get against FoodData Central: search answers by query words, details by id."""
    def __init__(self, searches=None, details=None, queue=None):
        self.searches = searches or {}
        self.details = details or {}
        self.queue = list(queue or [])
        self.calls = []

    def __call__(self, url, params=None, headers=None, timeout=None):
        self.calls.append({"url": url, "params": dict(params or {}), "headers": dict(headers or {})})
        if self.queue:
            return self.queue.pop(0)
        if url.endswith("foods/search"):
            query = params["query"].lower()
            types = params["dataType"].split(",")
            foods = [f for words, found in self.searches.items() if words in query for f in found
                     if f["dataType"] in types or (f["dataType"] == "Survey (FNDDS)" and "Survey (FNDDS)" in types)]
            return FakeResponse(200, {"foods": foods})
        fdc_id = int(url.rstrip("/").rsplit("/", 1)[1])
        return FakeResponse(200, self.details[fdc_id]) if fdc_id in self.details else FakeResponse(404, {})


def fake_usda(**kw):
    fake = FakeUsda(**kw)
    patcher = mock.patch("albright_fitness.usda.requests.get", side_effect=fake)
    return fake, patcher


@override_settings(USDA_API_KEY=KEY)
class Base(TestCase):
    def setUp(self):
        cache.clear()
        Food.objects.all().delete()               # a clean cache: "no request" checks count from zero
        outlook = mock.patch("albright_trading_app.views.get_market_outlook", return_value=None)
        outlook.start()
        self.addCleanup(outlook.stop)
        self.me = with_apps(User.objects.create_user(username="nate", password=PASSWORD, first_name="Nate"),
                            ["fitness", "meals"])
        self.wife = with_apps(User.objects.create_user(username="sam", password=PASSWORD, first_name="Sam"),
                              ["fitness", "meals"])
        self.household = kitchen(self.me, self.wife)
        self.client.force_login(self.me)
        self.today = timezone.localdate()
        self.fake, patcher = fake_usda(searches={"chicken": [CHICKEN], "rice": [RICE_SEARCH], "egg": [EGG_DETAIL],
                                                 "yogurt": [YOGURT]},
                                       details={168878: RICE_DETAIL, 748967: EGG_DETAIL, 171477: CHICKEN, 2001: YOGURT})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.sleep = mock.patch("albright_fitness.usda.time.sleep")
        self.sleep.start()
        self.addCleanup(self.sleep.stop)

    def as_user(self, user):
        c = Client()
        c.force_login(user)
        return c

    def entry(self, owner=None, day=None, slot="lunch", label="Toast", **amounts):
        return FoodLogEntry.objects.create(owner=owner or self.me, date=day or self.today, slot=slot, source="quick",
                                           label=label, **{n: D(str(v)) for n, v in amounts.items()})

    def targets(self, user=None, **values):
        profile = NutritionProfile.load(user or self.me)
        for n, v in values.items():
            setattr(profile, f"target_{n}", v)
        profile.save()
        return profile


def url(name, *args):
    return reverse(f"albright_fitness:{name}", args=args)


# ---------------------------------------------------------------------------
# The USDA client
# ---------------------------------------------------------------------------

class UsdaClientTests(Base):
    def test_search_caches_foods_and_the_same_search_makes_no_second_request(self):
        foods = usda.search("roast chicken")
        self.assertEqual([f.fdc_id for f in foods], [171477])
        chicken = Food.objects.get(fdc_id=171477)
        self.assertEqual((chicken.calories, chicken.protein, chicken.fat, chicken.carbs, chicken.sodium),
                         (D("165.00"), D("31.00"), D("3.57"), D("0.00"), D("74.00")))
        self.assertFalse(chicken.detailed)
        self.assertEqual(len(self.fake.calls), 1)
        call = self.fake.calls[0]
        self.assertTrue(call["url"].startswith("https://api.nal.usda.gov/fdc/v1/foods/search"))
        self.assertEqual(call["params"]["dataType"], "Foundation,SR Legacy,Survey (FNDDS),Branded")
        self.assertEqual(call["headers"]["X-Api-Key"], KEY)                       # in a header ...
        self.assertNotIn(KEY, str(call["params"]) + call["url"])                  # ... never in the URL
        usda.search("roast chicken")
        self.assertEqual(len(self.fake.calls), 1)

    def test_details_are_cached_after_the_first_lookup(self):
        rice = usda.food(168878)
        self.assertEqual(rice.portions, [{"label": "1 cup", "grams": 158.0}])
        self.assertEqual((rice.fiber, rice.detailed), (D("0.40"), True))
        usda.food(168878)
        usda.food(168878)
        self.assertEqual(len(self.fake.calls), 1)

    def test_a_search_never_overwrites_a_full_record(self):
        usda.food(168878)
        usda.search("white rice")
        rice = Food.objects.get(fdc_id=168878)
        self.assertTrue(rice.detailed)
        self.assertEqual(rice.portions, [{"label": "1 cup", "grams": 158.0}])

    def test_foundation_energy_and_branded_servings(self):
        egg = usda.food(748967)
        self.assertEqual(egg.calories, D("148.00"))                              # Atwater energy when 1008 is missing
        self.assertEqual(egg.portions, [{"label": "1 large", "grams": 50.3}])
        yogurt = usda.search("yogurt")[0]
        self.assertEqual((yogurt.brand, yogurt.portions), ("Example Dairy", [{"label": "1 container (170 g)", "grams": 170.0}]))

    def test_busy_api_is_retried_politely(self):
        self.fake.queue = [FakeResponse(429, headers={"Retry-After": "3"}), FakeResponse(200, CHICKEN)]
        self.assertEqual(usda.food(171477).name, CHICKEN["description"])
        self.assertEqual(len(self.fake.calls), 2)
        usda.time.sleep.assert_called_once_with(3)
        self.fake.queue = [FakeResponse(503)] * 3
        with self.assertRaises(usda.UsdaError):
            usda.food(999)
        self.assertEqual(len(self.fake.calls), 5)                                # at most two retries

    @override_settings(USDA_API_KEY="")
    def test_missing_key_message_and_everything_else_works(self):
        with self.assertRaises(usda.NotConfigured):
            usda.search("chicken")
        page = self.client.get(url("food_add")).content.decode()
        self.assertIn("USDA key not set", page)
        self.assertNotIn(KEY, page)
        self.assertEqual(self.client.get(url("food")).status_code, 200)
        self.client.post(url("food_log"), {"kind": "quick", "label": "Apple", "calories": "95", "slot": "snack"})
        self.assertTrue(FoodLogEntry.objects.filter(label="Apple").exists())
        self.assertEqual(self.fake.calls, [])


# ---------------------------------------------------------------------------
# Targets
# ---------------------------------------------------------------------------

class TargetTests(Base):
    def profile(self, **fields):
        profile = NutritionProfile.load(self.me)
        for k, v in fields.items():
            setattr(profile, k, v)
        profile.save()
        return profile

    def test_mifflin_st_jeor_and_macros(self):
        p = self.profile(sex="male", age=30, height_cm=D("180"), weight_kg=D("80"), activity="moderate", goal="maintain",
                         protein_per_lb=D("0.8"))
        s = nutrition.calculate(p)
        self.assertEqual((s["bmr"], s["tdee"]), (1780, 2759))
        self.assertEqual((s["calories"], s["protein"], s["fat"], s["carbs"]), (2760, 141, 77, 376))
        self.assertFalse(s["limited"])
        self.assertEqual(nutrition.calculate(self.profile(goal="lose"))["calories"], 2510)        # 250 a day ~ 0.5 lb a week
        self.assertEqual(nutrition.calculate(self.profile(goal="gain"))["calories"], 3010)

    def test_never_below_resting_rate_or_more_than_a_20_percent_deficit(self):
        p = self.profile(sex="female", age=60, height_cm=D("155"), weight_kg=D("50"), activity="sedentary", goal="lose")
        s = nutrition.calculate(p)
        self.assertTrue(s["limited"])
        self.assertGreaterEqual(s["calories"], s["bmr"])
        self.assertGreaterEqual(s["calories"], int(s["tdee"] * 0.8))
        self.assertEqual(nutrition.floor_calories(p), 1008)

    def test_low_typed_targets_get_a_gentle_note(self):
        p = self.profile(sex="female", age=60, height_cm=D("155"), weight_kg=D("50"), activity="sedentary", target_calories=900)
        self.assertIn("doctor or registered dietitian", nutrition.low_target_note(p))
        self.assertIn("doctor", self.client.get(url("targets")).content.decode())
        p.target_calories = 1600
        self.assertEqual(nutrition.low_target_note(p), "")
        bare = NutritionProfile.load(self.wife)
        bare.target_calories = 1000
        self.assertIn("dietitian", nutrition.low_target_note(bare))

    def test_calculator_page_in_lb_and_in_then_use_these(self):
        self.client.post(url("targets"), {"action": "units", "weight_unit": "lb", "height_unit": "in"})
        page = self.client.post(url("targets"), {"action": "calculate", "sex": "male", "age": "30", "height": "70.9",
                                                  "weight": "176.4", "activity": "moderate", "goal": "maintain",
                                                  "protein_per_lb": "0.8"}).content.decode()
        self.assertIn("Use these", page)
        p = NutritionProfile.objects.get(user=self.me)
        self.assertEqual((p.height_cm, p.weight_kg), (D("180.1"), D("80.0")))
        self.assertIsNone(p.target_calories)                                       # nothing saved until "Use these"
        self.client.post(url("targets"), {"action": "use", "sex": "male", "age": "30", "height": "70.9", "weight": "176.4",
                                          "activity": "moderate", "goal": "maintain", "protein_per_lb": "0.8"})
        p.refresh_from_db()
        self.assertEqual((p.target_calories, p.target_protein), (2760, 141))

    def test_track_protein_only_hides_calories(self):
        self.client.post(url("targets"), {"action": "targets", "protein": "150", "calories": "2400", "track_protein": "1"})
        p = NutritionProfile.objects.get(user=self.me)
        self.assertEqual((p.tracked, p.target_protein), (["protein"], 150))
        self.entry(calories=500, protein=40)
        page = self.client.get(url("food")).content.decode()
        self.assertIn('data-macro="protein"', page)
        self.assertNotIn('data-macro="calories"', page)
        self.assertNotIn("kcal", page)


# ---------------------------------------------------------------------------
# Grams
# ---------------------------------------------------------------------------

class GramTests(TestCase):
    CUP = [{"label": "1 cup", "grams": 158.0}]

    def test_portions_and_units(self):
        self.assertEqual(nutrition.to_grams(D("1.5"), "cup", self.CUP), D("237.0"))
        self.assertEqual(nutrition.to_grams(1, "cups", self.CUP), D("158.0"))
        self.assertEqual(nutrition.to_grams(8, "oz"), D("226.8"))
        self.assertEqual(nutrition.to_grams(1, "lb"), D("453.6"))
        self.assertEqual(nutrition.to_grams(2, "kg"), D("2000.0"))
        self.assertEqual(nutrition.to_grams(1, "tbsp", self.CUP), D("9.9"))        # scaled from the food's cup
        self.assertEqual(nutrition.to_grams(2, "cup", []), D("473.2"))             # water's density
        self.assertEqual(nutrition.to_grams(2, "", [{"label": "1 large", "grams": 50.3}], "eggs"), D("100.6"))
        self.assertEqual(nutrition.to_grams(3, "clove", [{"label": "1 clove", "grams": 3.0}]), D("9.0"))
        self.assertEqual(nutrition.to_grams(1, "", [{"label": "1 cup, chopped", "grams": 160}, {"label": "1 medium", "grams": 110}],
                                            "onion"), D("110.0"))
        self.assertIsNone(nutrition.to_grams(1, "pinch", []))

    def test_portion_labels(self):
        self.assertEqual(nutrition.portion_parts("1 cup, chopped"), (D(1), "cup", "chopped"))
        self.assertEqual(nutrition.portion_parts("2 large"), (D(2), "large", ""))
        self.assertEqual(nutrition.portion_parts("1/2 cup"), (D("0.5"), "cup", ""))
        self.assertEqual(nutrition.portion_parts("1 container (170 g)"), (D(1), "container", ""))
        self.assertEqual(nutrition.portion_parts("1 fl oz"), (D(1), "floz", ""))


# ---------------------------------------------------------------------------
# The diary
# ---------------------------------------------------------------------------

class DiaryTests(Base):
    def test_totals_left_and_over_is_neutral(self):
        self.targets(calories=2000, protein=150)
        self.entry(calories=600, protein=50)
        self.entry(slot="dinner", calories=900, protein=42)
        rows = {r["name"]: r for r in nutrition.progress(NutritionProfile.load(self.me), nutrition.day(self.me, self.today)["totals"])}
        self.assertEqual((rows["protein"]["total"], rows["protein"]["left"], rows["protein"]["pct"]), (92, 58, 61))
        self.assertEqual(rows["calories"]["left"], 500)
        page = self.client.get(url("food")).content.decode()
        self.assertIn("58 g left", page)
        self.entry(calories=100, protein=70)
        page = self.client.get(url("food")).content.decode()
        self.assertIn("12 g over", page)
        self.assertIn("is-over", page)
        self.assertNotIn("fx-danger", page)
        self.assertNotIn("loss", page.split('id="fx-food-totals"')[1].split("</section>")[0])

    def test_quick_add(self):
        self.client.post(url("food_log"), {"kind": "quick", "label": "Latte", "calories": "190", "protein": "10",
                                           "slot": "breakfast", "date": self.today.isoformat()})
        e = FoodLogEntry.objects.get(label="Latte")
        self.assertEqual((e.slot, e.source, e.calories, e.protein, e.fat), ("breakfast", "quick", D("190.00"), D("10.00"), None))
        self.client.post(url("food_log"), {"kind": "quick", "label": "Nothing"})
        self.assertFalse(FoodLogEntry.objects.filter(label="Nothing").exists())

    def test_my_foods_by_servings(self):
        self.client.post(url("my_foods"), {"name": "Protein shake", "serving_label": "1 scoop", "serving_grams": "30",
                                           "calories": "120", "protein": "24"})
        shake = CustomFood.objects.get(owner=self.me)
        self.client.post(url("food_log"), {"kind": "custom", "food": shake.pk, "servings": "1.5", "slot": "snack"})
        e = FoodLogEntry.objects.get(custom_food=shake)
        self.assertEqual((e.calories, e.protein, e.grams, e.portion_label), (D("180.00"), D("36.00"), D("45.0"), "1.5 × 1 scoop"))

    def test_recent_foods_relog_in_one_tap(self):
        old = self.entry(day=self.today - timedelta(days=2), label="Oatmeal", calories=300, protein=10)
        self.entry(day=self.today - timedelta(days=1), label="Oatmeal", calories=300, protein=10)
        page = self.client.get(url("food_add") + "?tab=recent").content.decode()
        self.assertIn("Oatmeal", page)
        self.assertIn("&times;2", page)
        self.client.post(url("food_log"), {"kind": "recent", "entry": old.pk, "slot": "breakfast", "date": self.today.isoformat()})
        e = FoodLogEntry.objects.get(date=self.today, label="Oatmeal")
        self.assertEqual((e.slot, e.calories), ("breakfast", D("300.00")))

    def test_copy_yesterdays_meal(self):
        self.entry(day=self.today - timedelta(days=1), slot="breakfast", label="Eggs", calories=150)
        self.entry(day=self.today - timedelta(days=1), slot="breakfast", label="Toast", calories=80)
        self.entry(day=self.today - timedelta(days=1), slot="dinner", label="Pasta", calories=600)
        self.client.post(url("food_copy"), {"date": self.today.isoformat(), "slot": "breakfast"})
        self.assertEqual(sorted(FoodLogEntry.objects.filter(date=self.today).values_list("label", flat=True)), ["Eggs", "Toast"])

    def test_usda_food_by_portion_or_grams(self):
        page = self.client.get(url("food_usda", 168878) + "?slot=lunch").content.decode()
        self.assertIn("1 cup (158 g)", page)
        self.client.post(url("food_usda", 168878), {"portion": "0", "amount": "2", "slot": "lunch", "date": self.today.isoformat()})
        e = FoodLogEntry.objects.get(food__fdc_id=168878)
        self.assertEqual((e.grams, e.calories, e.portion_label), (D("316.0"), D("410.80"), "2 × 1 cup"))
        self.client.post(url("food_usda", 168878), {"portion": "g", "amount": "50", "slot": "dinner", "date": self.today.isoformat()})
        self.assertEqual(FoodLogEntry.objects.get(slot="dinner").calories, D("65.00"))
        self.assertEqual(len(self.fake.calls), 1)                                # one details request, then the cache

    def test_search_results_page(self):
        page = self.client.get(url("food_add") + "?q=chicken&slot=dinner").content.decode()
        self.assertIn("Chicken, broiler", page)
        self.assertIn(url("food_usda", 171477), page)


# ---------------------------------------------------------------------------
# Recipe nutrition
# ---------------------------------------------------------------------------

class RecipeNutritionTests(Base):
    def setUp(self):
        super().setUp()
        self.recipe = Recipe.objects.create(household=self.household, title="Chicken and rice", created_by=self.me, servings=4,
                                            ingredients=[{"name": "chicken breast", "quantity": "1", "unit": "lb"},
                                                         {"name": "white rice", "quantity": "2", "unit": "cup"},
                                                         {"name": "salt", "quantity": "", "unit": "", "original": "salt to taste"}])

    def calc(self):
        return self.client.post(reverse("albright_meals:recipe_nutrition_calculate", args=[self.recipe.pk]))

    def test_matching_grams_ignore_and_per_serving(self):
        self.calc()
        row = RecipeNutrition.objects.get(recipe=self.recipe)
        chicken, rice, salt = row.lines.all()
        self.assertEqual((chicken.food.fdc_id, chicken.grams), (171477, D("453.6")))
        self.assertEqual((rice.food.fdc_id, rice.grams), (168878, D("316.0")))           # the cup portion from the details
        self.assertTrue(salt.ignored)
        self.assertIsNone(salt.food)
        self.assertEqual(row.total_calories, D("1159.24"))
        self.assertEqual((row.serving_calories, row.serving_protein), (D("289.81"), D("37.28")))
        self.assertTrue(row.needs_review)
        page = self.client.get(reverse("albright_meals:recipe_nutrition", args=[self.recipe.pk])).content.decode()
        self.assertIn("Needs review", page)
        self.assertIn("453.6", page)

    def test_change_grams_match_and_ignore_then_confirm(self):
        self.calc()
        row = RecipeNutrition.objects.get(recipe=self.recipe)
        chicken, rice, salt = row.lines.all()
        line_url = lambda line: reverse("albright_meals:recipe_nutrition_line", args=[self.recipe.pk, line.pk])  # noqa: E731
        self.client.post(reverse("albright_meals:recipe_nutrition_confirm", args=[self.recipe.pk]))
        self.client.post(line_url(chicken), {"grams": "400"})
        row.refresh_from_db()
        self.assertEqual((row.status, row.total_calories), ("review", D("1070.80")))     # changes need review again
        self.client.post(line_url(rice), {"ignored": "1"})
        row.refresh_from_db()
        self.assertEqual(row.total_calories, D("660.00"))
        self.client.post(line_url(rice), {"ignored": "0"})
        self.client.post(line_url(chicken), {"fdc_id": "748967"})                        # a different food, same grams
        chicken.refresh_from_db()
        self.assertEqual((chicken.food.fdc_id, chicken.grams, chicken.calories), (748967, D("400.0"), D("592.00")))
        self.client.post(reverse("albright_meals:recipe_nutrition_confirm", args=[self.recipe.pk]))
        row.refresh_from_db()
        self.assertEqual(row.status, "confirmed")
        picker = self.client.get(reverse("albright_meals:recipe_nutrition", args=[self.recipe.pk]) + f"?line={rice.pk}&q=chicken")
        self.assertContains(picker, 'name="fdc_id" value="171477"')

    def test_manual_per_serving(self):
        self.client.post(reverse("albright_meals:recipe_nutrition_manual", args=[self.recipe.pk]), {"calories": "450", "protein": "35"})
        row = RecipeNutrition.objects.get(recipe=self.recipe)
        self.assertEqual((row.status, row.serving_calories, row.total_calories, row.serving_carbs), ("manual", D("450.00"), D("1800.00"), None))
        self.assertEqual(self.fake.calls, [])

    def test_shown_on_cards_and_the_page_and_follows_visibility(self):
        recipe_nutrition.set_manual(self.recipe, {"calories": D(450), "protein": D(35), "carbs": None, "fat": None, "fiber": None}, self.me)
        cards = self.client.get(reverse("albright_meals:recipes")).content.decode()
        self.assertIn("450 kcal &middot; 35 g protein", cards)
        page = self.client.get(reverse("albright_meals:recipe", args=[self.recipe.pk])).content.decode()
        self.assertIn('id="recipe-per-serving"', page)
        self.assertIn('id="recipe-log"', page)
        stranger = with_apps(User.objects.create_user(username="kim", password=PASSWORD), ["meals", "fitness"])
        kitchen(stranger, name="Elsewhere")
        kim = self.as_user(stranger)
        self.assertEqual(kim.get(reverse("albright_meals:recipe", args=[self.recipe.pk])).status_code, 404)
        self.assertEqual(kim.get(reverse("albright_meals:recipe_nutrition", args=[self.recipe.pk])).status_code, 404)
        self.assertEqual(kim.post(reverse("albright_meals:recipe_nutrition_calculate", args=[self.recipe.pk])).status_code, 404)
        self.assertEqual(kim.post(url("food_log"), {"kind": "recipe", "recipe": self.recipe.pk}).status_code, 404)
        Recipe.objects.filter(pk=self.recipe.pk).update(visibility=Recipe.LIBRARY)
        library_page = kim.get(reverse("albright_meals:recipe", args=[self.recipe.pk])).content.decode()
        self.assertIn('id="recipe-per-serving"', library_page)                               # it follows the recipe
        self.assertNotIn(reverse("albright_meals:recipe_nutrition", args=[self.recipe.pk]), library_page)   # read-only
        self.assertEqual(kim.get(reverse("albright_meals:recipe_nutrition", args=[self.recipe.pk])).status_code, 404)

    @override_settings(USDA_API_KEY="")
    def test_without_a_key_you_can_still_type_it_in(self):
        page = self.client.get(reverse("albright_meals:recipe_nutrition", args=[self.recipe.pk])).content.decode()
        self.assertIn("USDA key not set", page)
        response = self.calc()
        self.assertFalse(RecipeNutrition.objects.filter(recipe=self.recipe).exists())
        self.assertEqual(response.status_code, 302)


# ---------------------------------------------------------------------------
# Meals connection
# ---------------------------------------------------------------------------

class MealsConnectionTests(Base):
    def setUp(self):
        super().setUp()
        self.recipe = Recipe.objects.create(household=self.household, title="Turkey chili", created_by=self.me, servings=6)
        recipe_nutrition.set_manual(self.recipe, {"calories": D(400), "protein": D(30), "carbs": D(35), "fat": D(12), "fiber": None}, self.me)
        self.dinner = MealPlanEntry.objects.create(household=self.household, date=self.today, slot="dinner", recipe=self.recipe)

    def test_from_my_meal_plan_logs_with_servings(self):
        page = self.client.get(url("food")).content.decode()
        self.assertIn('id="fx-plan"', page)
        self.assertIn("Turkey chili", page)
        self.client.post(url("food_log"), {"kind": "plan", "entry": self.dinner.pk, "servings": "1.5", "date": self.today.isoformat()})
        e = FoodLogEntry.objects.get(owner=self.me)
        self.assertEqual((e.slot, e.servings, e.calories, e.protein, e.plan_entry, e.portion_label),
                         ("dinner", D("1.50"), D("600.00"), D("45.00"), self.dinner, "1.5 servings"))
        self.assertIn("Logged &#x2713;", self.client.get(url("food")).content.decode())
        self.assertFalse(FoodLogEntry.objects.filter(owner=self.wife).exists())          # hers is hers to log

    def test_log_to_my_diary_from_the_recipe_and_plan_entry_pages(self):
        recipe_page = reverse("albright_meals:recipe", args=[self.recipe.pk])
        response = self.client.post(url("food_log"), {"kind": "recipe", "recipe": self.recipe.pk, "servings": "0.5", "slot": "lunch",
                                                      "date": self.today.isoformat(), "next": recipe_page})
        self.assertRedirects(response, recipe_page, fetch_redirect_response=False)
        self.assertEqual(FoodLogEntry.objects.get(slot="lunch").calories, D("200.00"))
        entry_page = self.client.get(reverse("albright_meals:plan_entry", args=[self.dinner.pk])).content.decode()
        self.assertIn('id="entry-log"', entry_page)
        response = self.client.post(url("food_log"), {"kind": "recipe", "recipe": self.recipe.pk, "next": "https://evil.example.com/"})
        self.assertNotIn("evil", response["Location"])

    def test_meals_week_planned_macros(self):
        other = Recipe.objects.create(household=self.household, title="Mystery soup", created_by=self.me)
        MealPlanEntry.objects.create(household=self.household, date=self.today, slot="lunch", recipe=other)
        page = self.client.get(reverse("albright_meals:week")).content.decode()
        self.assertIn("Planned: 400 kcal &middot; 30 g protein", page)
        self.assertIn("(1 of 2 meals known)", page)

    def test_dashboard_macros_average_and_launcher_line(self):
        self.targets(protein=150, calories=2200)
        NutritionProfile.objects.filter(user=self.me).update(tracked=["calories", "protein", "carbs", "fat"])
        self.entry(protein=92, calories=1500)
        self.entry(day=self.today - timedelta(days=1), protein=108, calories=1700)
        page = self.client.get(url("dashboard")).content.decode()
        self.assertIn('id="fx-food-today"', page)
        self.assertIn("58 g left", page)
        self.assertIn("7-day average (2 days logged): 1600 kcal &middot; 100 g protein", page)
        self.assertEqual(stats.status_line(self.me), "Protein 92/150 g")                  # no workout planned today
        self.assertEqual(stats.status_line(self.wife), "Rest day")


# ---------------------------------------------------------------------------
# Privacy - even within a household
# ---------------------------------------------------------------------------

class PrivacyTests(Base):
    def test_diary_targets_and_my_foods_are_private_within_the_household(self):
        mine = self.entry(label="Secret snack", calories=300)
        self.targets(calories=1873, protein=140)
        NutritionProfile.objects.filter(user=self.me).update(weight_kg=D("90"), age=41)
        shake = CustomFood.objects.create(owner=self.me, name="My shake", calories=D(120))
        sam = self.as_user(self.wife)
        for page in (sam.get(url("food")), sam.get(url("food_day", self.today.isoformat())), sam.get(url("food_add") + "?tab=recent"),
                     sam.get(url("food_add") + "?tab=mine"), sam.get(url("my_foods")), sam.get(url("targets")), sam.get(url("dashboard"))):
            body = page.content.decode()
            for secret in ("Secret snack", "My shake", "1873", 'value="41"', 'value="198.4"'):
                self.assertNotIn(secret, body)
        self.assertEqual(sam.post(url("food_delete", mine.pk)).status_code, 404)
        self.assertEqual(sam.post(url("food_log"), {"kind": "recent", "entry": mine.pk}).status_code, 404)
        self.assertEqual(sam.post(url("food_log"), {"kind": "custom", "food": shake.pk}).status_code, 404)
        self.assertEqual(sam.post(url("my_food_delete", shake.pk)).status_code, 404)
        self.assertTrue(FoodLogEntry.objects.filter(pk=mine.pk).exists())
        self.assertIsNone(NutritionProfile.load(self.wife).target_calories)               # her own, empty
        self.assertEqual(nutrition.status_line(self.wife), "")

    def test_needs_fitness_access(self):
        meals_only = with_apps(User.objects.create_user(username="lee", password=PASSWORD), ["meals"])
        kitchen(self.me, meals_only)
        lee = self.as_user(meals_only)
        self.assertEqual(lee.get(url("food")).status_code, 404)
        recipe = Recipe.objects.create(household=self.household, title="Tacos", created_by=self.me)
        recipe_nutrition.set_manual(recipe, {"calories": D(500), "protein": D(25), "carbs": None, "fat": None, "fiber": None}, self.me)
        page = lee.get(reverse("albright_meals:recipe", args=[recipe.pk])).content.decode()
        self.assertIn('id="recipe-per-serving"', page)                                     # the recipe's, for its household
        self.assertNotIn('id="recipe-log"', page)                                          # no diary without Fitness
