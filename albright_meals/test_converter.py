"""Meals > Convert (front-end only: static/meals/convert_math.js + converter.js; the math's own tests are
js_tests/convert_math.test.js under `node --test`): the floating button and dialog on the recipe and Add / Edit recipe
pages, and a convert icon on each ingredient line that has an amount - carrying the parsed amount, unit and name."""
import pathlib

from django.contrib.auth.models import User
from django.contrib.staticfiles import finders
from django.test import TestCase
from django.urls import reverse

from albright_labs.testing import kitchen, with_apps

from .models import Recipe

PASSWORD = "pw-not-real-12345"


class ConverterPageTests(TestCase):
    def setUp(self):
        self.me = with_apps(User.objects.create_user(username="nate", password=PASSWORD), ["meals"])
        self.household = kitchen(self.me)
        self.client.force_login(self.me)
        self.recipe = Recipe.objects.create(household=self.household, title="Cookies", created_by=self.me, ingredients=[
            {"name": "all-purpose flour", "quantity": "2 1/4", "unit": "cup", "note": ""},
            {"name": "butter", "quantity": "1", "unit": "cup", "note": "softened"},
            {"name": "eggs", "quantity": "2", "unit": "", "note": ""},
            {"name": "salt", "quantity": "", "unit": "", "note": "", "original": "salt to taste"}])

    def test_recipe_page_has_the_button_dialog_and_an_icon_per_line(self):
        page = self.client.get(reverse("albright_meals:recipe", args=[self.recipe.pk])).content.decode()
        self.assertIn('id="meals-convert-open"', page)
        self.assertIn('<dialog class="meals-converter" id="meals-converter"', page)
        self.assertIn("meals/convert_math.js", page)
        self.assertIn("meals/converter.js", page)
        self.assertLess(page.index("meals/convert_math.js"), page.index("meals/converter.js"))
        self.assertIn('data-convert-qty="2.25" data-convert-unit="cup" data-convert-name="all-purpose flour"', page)
        self.assertIn('data-convert-qty="1" data-convert-unit="cup" data-convert-name="butter"', page)
        self.assertIn('data-convert-qty="2" data-convert-unit="" data-convert-name="eggs"', page)
        self.assertEqual(page.count('class="meals-convert-line"'), 3)        # not "salt to taste" (no amount)
        self.assertIn("Approximate: varies by brand and how it's packed.", page)
        for tab in ("Volume", "Weight", "Temperature"):
            self.assertIn(f">{tab}</button>", page)

    def test_icons_sit_outside_the_tick_box_labels(self):
        page = self.client.get(reverse("albright_meals:recipe", args=[self.recipe.pk])).content.decode()
        for label in page.split('<label class="meals-pick">')[1:]:
            self.assertNotIn("meals-convert-line", label.split("</label>", 1)[0])

    def test_add_and_edit_pages_have_it_too(self):
        for path in (reverse("albright_meals:recipe_add"), reverse("albright_meals:recipe_edit", args=[self.recipe.pk])):
            page = self.client.get(path).content.decode()
            self.assertIn('id="meals-convert-open"', page, path)
            self.assertIn('id="meals-converter"', page, path)

    def test_math_module_is_pure_and_tested(self):
        source = pathlib.Path(finders.find("meals/convert_math.js")).read_text(encoding="utf-8")
        for dom in ("document.", "window.", "localStorage"):
            self.assertNotIn(dom, source)
        tests = pathlib.Path(__file__).with_name("js_tests") / "convert_math.test.js"
        self.assertIn('require("../static/meals/convert_math.js")', tests.read_text(encoding="utf-8"))
