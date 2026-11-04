"""Recipe ratings - a Rating drop-down at the top of a recipe, blank by default, each person's own (one stamp per
recipe and person; re-rating updates it, blank removes it) - and the shared recipe tags (models.STANDARD_TAGS)."""
import pathlib
import re
from unittest import mock

from django.contrib.auth.models import User
from django.contrib.staticfiles import finders
from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.test import Client, TestCase
from django.urls import reverse

from albright_labs.testing import kitchen, with_apps

from .models import STANDARD_TAGS, Recipe, RecipeRating

PASSWORD = "pw-not-real-12345"


class Base(TestCase):
    def setUp(self):
        cache.clear()
        outlook = mock.patch("albright_trading_app.views.get_market_outlook", return_value=None)
        outlook.start()
        self.addCleanup(outlook.stop)
        self.me = with_apps(User.objects.create_user(username="nate", password=PASSWORD, first_name="Nate"), ["meals"])
        self.wife = with_apps(User.objects.create_user(username="kim", password=PASSWORD, first_name="Kim"), ["meals"])
        self.space = kitchen(self.me, self.wife)
        self.client.force_login(self.me)
        self.recipe = Recipe.objects.create(space=self.space, title="Sheet-pan fajitas", created_by=self.me)

    def page(self, client=None):
        return (client or self.client).get(reverse("albright_meals:recipe", args=[self.recipe.pk])).content.decode()

    def rate(self, level, note="", client=None, ajax=False):
        extra = {"HTTP_X_REQUESTED_WITH": "XMLHttpRequest"} if ajax else {}
        return (client or self.client).post(reverse("albright_meals:recipe_rate", args=[self.recipe.pk]),
                                            {"level": level, "note": note}, **extra)

    @staticmethod
    def dropdown(page):
        return page.split('id="recipe-rating-level"', 1)[1].split("</select>", 1)[0]


class LevelTests(Base):
    def test_six_ordered_levels_with_emoji_color_and_meaning(self):
        self.assertEqual([(c["value"], c["emoji"], c["label"], c["color"]) for c in RecipeRating.choices()], [
            (1, "\U0001F6AB", "Won't eat again", "red"), (2, "\U0001F610", "Not for me", "muted"),
            (3, "\U0001F527", "Needs tweaks", "amber"), (4, "\U0001F44D", "Will eat again", "neutral"),
            (5, "❤️", "Favorite", "green"), (6, "⭐", "Staple", "gold")])
        self.assertEqual(RecipeRating.INFO[RecipeRating.STAPLE][2], "Part of the regular rotation; our go-to.")

    def test_sorts_and_filters_by_level(self):
        other = Recipe.objects.create(space=self.space, title="Tofu bowls")
        RecipeRating.objects.create(recipe=self.recipe, user=self.me, level=RecipeRating.STAPLE)
        RecipeRating.objects.create(recipe=other, user=self.me, level=RecipeRating.NEEDS_TWEAKS)
        self.assertEqual(list(RecipeRating.objects.filter(level__gte=RecipeRating.FAVORITE).values_list("recipe__title", flat=True)),
                         ["Sheet-pan fajitas"])
        self.assertEqual([r.level for r in RecipeRating.objects.all()], [6, 3])       # best first

    def test_one_stamp_per_person_and_levels_1_to_6_only(self):
        RecipeRating.objects.create(recipe=self.recipe, user=self.me, level=4)
        with self.assertRaises(IntegrityError), transaction.atomic():
            RecipeRating.objects.create(recipe=self.recipe, user=self.me, level=5)
        with self.assertRaises(IntegrityError), transaction.atomic():
            RecipeRating.objects.create(recipe=self.recipe, user=self.wife, level=7)


class DropdownTests(Base):
    def test_at_the_top_and_blank_by_default(self):
        page = self.page()
        self.assertLess(page.index('id="recipe-rating"'), page.index('<h1 class="meals-head__title">'))
        menu = self.dropdown(page)
        self.assertIn('<option value="" selected>&mdash;</option>', menu)
        self.assertEqual(re.findall(r'<option value="(\d)"', menu), ["1", "2", "3", "4", "5", "6"])
        self.assertNotIn(" selected>", menu.split("</option>", 1)[1])                 # nothing else picked
        self.assertIn('title="Really loved it; make it often."', menu)                # the meaning as a tooltip
        self.assertIn("⭐ Staple</option>", menu)

    def test_picking_saves_my_stamp_and_rerating_updates_it(self):
        response = self.rate(RecipeRating.FAVORITE, ajax=True)
        self.assertEqual(response.json(), {"ok": True, "level": 5, "label": "Favorite",
                                           "meaning": "Really loved it; make it often."})
        self.rate(RecipeRating.STAPLE)
        stamp = RecipeRating.objects.get()
        self.assertEqual((stamp.user, stamp.level), (self.me, RecipeRating.STAPLE))
        self.assertIn('<option value="6" title="Part of the regular rotation; our go-to." data-meaning="Part of the regular rotation; our go-to." selected>',
                      self.dropdown(self.page()))

    def test_each_person_sees_only_their_own(self):
        self.rate(RecipeRating.STAPLE)
        kim = Client()
        kim.force_login(self.wife)
        self.assertIn('<option value="" selected>', self.dropdown(self.page(kim)))     # still blank for Kim
        self.rate(RecipeRating.NOT_FOR_ME, client=kim)
        self.assertEqual(dict(RecipeRating.objects.values_list("user__username", "level")), {"nate": 6, "kim": 2})
        self.assertRegex(self.dropdown(self.page()), r'<option value="6"[^>]* selected>')    # mine, not Kim's
        self.assertNotIn("Not for me", self.page().split('id="recipe-rating-meaning"', 1)[1][:200])

    def test_blank_clears_it(self):
        self.rate(RecipeRating.WILL_EAT)
        self.rate("")
        self.assertFalse(RecipeRating.objects.exists())
        self.assertIn('<option value="" selected>', self.dropdown(self.page()))

    def test_needs_tweaks_asks_what_to_change(self):
        page = self.page()
        self.assertRegex(page, r'meals-rating__note"\s+hidden id="recipe-rating-note-field"')    # not asked yet
        self.rate(RecipeRating.NEEDS_TWEAKS, note="Less salt, more lime" + "x" * 600)
        stamp = RecipeRating.objects.get()
        self.assertEqual(len(stamp.note), 500)
        page = self.page()
        self.assertIn("What would you change?", page)
        self.assertNotIn('hidden id="recipe-rating-note-field"', page)
        self.assertIn('value="Less salt, more lime', page)

    def test_bad_values_and_access(self):
        self.assertEqual(self.rate("9", ajax=True).status_code, 400)
        self.assertFalse(RecipeRating.objects.exists())
        self.assertEqual(self.client.get(reverse("albright_meals:recipe_rate", args=[self.recipe.pk])).status_code, 405)
        no_meals = with_apps(User.objects.create_user(username="pat", password=PASSWORD), ["notes"])
        other = Client()
        other.force_login(no_meals)
        self.assertEqual(self.rate("5", client=other).status_code, 404)

    def test_saves_on_change_without_a_reload(self):
        js = pathlib.Path(finders.find("meals/recipe.js")).read_text(encoding="utf-8")
        self.assertIn('select.addEventListener("change"', js)
        self.assertIn('"X-Requested-With": "XMLHttpRequest"', js)
        self.assertIn("noteField.hidden = select.value !== tweaks;", js)


class StandardTagTests(Base):
    def test_the_seven_tags(self):
        self.assertEqual([label for _key, label in STANDARD_TAGS],
                         ["Quick weeknight", "Good leftovers", "Meal-prep friendly", "Special occasion", "Company-worthy",
                          "Kid-friendly", "Budget-friendly"])

    def test_anyone_who_can_edit_sets_them_and_they_show_as_chips(self):
        kim = Client()
        kim.force_login(self.wife)
        form = kim.get(reverse("albright_meals:recipe_edit", args=[self.recipe.pk])).content.decode()
        self.assertIn('name="standard_tags" value="kid_friendly"', form)
        kim.post(reverse("albright_meals:recipe_edit", args=[self.recipe.pk]), {
            "title": "Sheet-pan fajitas", "ingredients_text": "", "steps_text": "", "tags_text": "chicken",
            "standard_tags": ["quick_weeknight", "kid_friendly", "not-a-tag"]})
        self.recipe.refresh_from_db()
        self.assertEqual(self.recipe.standard_tags, [])                               # an unknown choice: not saved
        kim.post(reverse("albright_meals:recipe_edit", args=[self.recipe.pk]), {
            "title": "Sheet-pan fajitas", "ingredients_text": "", "steps_text": "", "tags_text": "chicken",
            "standard_tags": ["kid_friendly", "quick_weeknight"]})
        self.recipe.refresh_from_db()
        self.assertEqual(self.recipe.standard_tags, ["quick_weeknight", "kid_friendly"])
        self.assertEqual(self.recipe.tags, ["chicken"])                               # the free-form tags stay separate
        chips = self.page().split('id="recipe-standard-tags"', 1)[1].split("</div>", 1)[0]
        self.assertEqual(re.findall(r'meals-tag--standard">([^<]+)<', chips), ["Quick weeknight", "Kid-friendly"])
        self.assertFalse(RecipeRating.objects.exists())                               # separate from ratings
