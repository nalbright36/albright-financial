"""Meals by household: the Household moved to albright_labs (Money unchanged), existing Meals data assigned to one
household (migration 0006), another household can't see or change anything of ours (pages and the Bot API), the
"Set up your kitchen" page, the shared library and "Save to my recipes", sharing one recipe with one person (exact
username or email only, read-only, removal, the notice), card ratings, the rating filters, and ratings never shown
outside the household. Plus My account > Household."""
import importlib
import json
import re
from unittest import mock

from django.apps import apps as django_apps
from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from albright_bot_api.models import BotToken
from albright_labs import households
from albright_labs.models import Household, HouseholdMember, Invitation, UserPreference
from albright_labs.testing import allow_api_keys, kitchen, with_apps

from .models import GroceryItem, MealIdea, MealPlanEntry, Recipe, RecipeRating, RecipeShare

PASSWORD = "pw-not-real-12345"
API = "/api/bot/v1/"
FAV, STAPLE, WONT = RecipeRating.FAVORITE, RecipeRating.STAPLE, RecipeRating.WONT_EAT


def url(name, *args):
    return reverse(f"albright_meals:{name}", args=args)


@override_settings(BOT_API_REQUIRE_HTTPS=False)
class Base(TestCase):
    def setUp(self):
        cache.clear()
        outlook = mock.patch("albright_trading_app.views.get_market_outlook", return_value=None)
        outlook.start()
        self.addCleanup(outlook.stop)
        self.nate = with_apps(User.objects.create_user(username="nate", password=PASSWORD, first_name="Nathan",
                                                       email="nate@example.com"), ["meals", "money"])
        self.wife = with_apps(User.objects.create_user(username="sam", password=PASSWORD, first_name="Sam"), ["meals"])
        self.kim = with_apps(User.objects.create_user(username="kim", password=PASSWORD, first_name="Kim",
                                                      email="kim@example.com"), ["meals"])
        self.ours = kitchen(self.nate, self.wife, name="Albrights")
        self.theirs = kitchen(self.kim, name="Kim's place")
        self.client.force_login(self.nate)
        self.today = timezone.localdate()

    def as_user(self, user):
        c = Client()
        c.force_login(user)
        return c

    def recipe(self, title="Family Meatballs", household=None, **kw):
        return Recipe.objects.create(household=household or self.ours, title=title, created_by=self.nate, **kw)

    def rate(self, user, recipe, level, note=""):
        return RecipeRating.objects.update_or_create(recipe=recipe, user=user, defaults={"level": level, "note": note})[0]


# ---------------------------------------------------------------------------
# The Household move, and the data migration
# ---------------------------------------------------------------------------

class HouseholdMoveTests(Base):
    def test_one_household_model_shared_by_money(self):
        from albright_money import models as money_models
        self.assertIs(money_models.Household, Household)
        self.assertEqual(Household._meta.db_table, "albright_labs_household")
        self.assertEqual(HouseholdMember._meta.get_field("user").remote_field.related_name, "household_membership")

    def test_money_keeps_working(self):
        from albright_money.models import Account
        joint = Account.objects.create(owner=self.nate, household=self.ours, name="Joint", starting_date=self.today)
        mine = Account.objects.create(owner=self.nate, name="Mine", starting_date=self.today)
        money = with_apps(self.wife, ["meals", "money"])
        sam = self.as_user(money)
        self.assertEqual(sam.get(reverse("albright_money:account", args=[joint.pk])).status_code, 200)
        self.assertEqual(sam.get(reverse("albright_money:account", args=[mine.pk])).status_code, 404)
        page = self.client.get(reverse("albright_money:household")).content.decode()
        self.assertIn("Albrights", page)

    def migration(self):
        return importlib.import_module("albright_meals.migrations.0006_assign_household")

    def test_data_migration_reuses_my_household_and_adds_everyone_who_uses_meals(self):
        HouseholdMember.objects.all().delete()
        Household.objects.all().delete()
        boss = User.objects.create_superuser("boss", "b@example.com", PASSWORD, first_name="Nathan")
        money_household = Household.objects.create(name="From Money", created_by=boss)
        HouseholdMember.objects.create(household=money_household, user=boss)
        elsewhere = Household.objects.create(name="Elsewhere", created_by=self.kim)
        HouseholdMember.objects.create(household=elsewhere, user=self.kim)
        no_meals = with_apps(User.objects.create_user(username="pat", password=PASSWORD), ["notes"])
        self.migration().forwards(django_apps, None)
        members = set(HouseholdMember.objects.filter(household=money_household).values_list("user__username", flat=True))
        self.assertEqual(members, {"boss", "nate", "sam"})                 # reused; the Meals users joined
        self.assertEqual(HouseholdMember.objects.get(user=self.kim).household, elsewhere)   # left in their own
        self.assertFalse(HouseholdMember.objects.filter(user=no_meals).exists())

    def test_data_migration_makes_one_when_there_is_none(self):
        HouseholdMember.objects.all().delete()
        Household.objects.all().delete()
        User.objects.create_superuser("boss", "b@example.com", PASSWORD, first_name="Nathan")
        self.migration().forwards(django_apps, None)
        household = Household.objects.get()
        self.assertEqual(household.name, "Nathan's household")
        self.assertEqual(set(household.members.values_list("user__username", flat=True)), {"boss", "nate", "sam", "kim"})

    def test_every_meals_row_has_a_household(self):
        for model in (Recipe, MealPlanEntry, GroceryItem, MealIdea):
            self.assertFalse(model._meta.get_field("household").null, model.__name__)
        self.assertIn(("household", "date", "slot"),
                      [tuple(c.fields) for c in MealPlanEntry._meta.constraints])


# ---------------------------------------------------------------------------
# Another household sees nothing of ours
# ---------------------------------------------------------------------------

class IsolationTests(Base):
    def setUp(self):
        super().setUp()
        self.meatballs = self.recipe()
        self.entry = MealPlanEntry.objects.create(household=self.ours, date=self.today, slot="dinner", recipe=self.meatballs)
        self.milk = GroceryItem.objects.create(household=self.ours, name="milk")
        self.idea = MealIdea.objects.create(household=self.ours, title="Try ramen")
        self.rate(self.nate, self.meatballs, STAPLE, "Double the garlic")
        self.kim_client = self.as_user(self.kim)

    def test_pages_404_and_lists_are_empty(self):
        kim = self.kim_client
        for path in (url("recipe", self.meatballs.pk), url("recipe_edit", self.meatballs.pk), url("plan_entry", self.entry.pk)):
            self.assertEqual(kim.get(path).status_code, 404, path)
        for path, data in ((url("plan_move", self.entry.pk), {"date": self.today, "slot": "lunch"}),
                           (url("plan_delete", self.entry.pk), {}), (url("plan_status", self.entry.pk), {"status": "cooked"}),
                           (url("grocery_toggle", self.milk.pk), {}), (url("grocery_delete", self.milk.pk), {}),
                           (url("grocery_merge", self.milk.pk), {}), (url("idea_plan", self.idea.pk), {"date": self.today, "slot": "dinner"}),
                           (url("idea_decline", self.idea.pk), {}), (url("recipe_rate", self.meatballs.pk), {"level": "1"}),
                           (url("recipe_plan", self.meatballs.pk), {"date": self.today, "slot": "dinner"}),
                           (url("recipe_groceries", self.meatballs.pk), {}), (url("recipe_share", self.meatballs.pk), {"who": "kim"}),
                           (url("recipe_save_copy", self.meatballs.pk), {}), (url("recipe_visibility", self.meatballs.pk), {"visibility": "library"})):
            self.assertEqual(kim.post(path, data).status_code, 404, path)
        for path in (url("week"), url("groceries"), url("ideas"), url("recipes")):
            page = kim.get(path).content.decode()
            for ours in ("Family Meatballs", "milk</", "Try ramen"):
                self.assertNotIn(ours, page, path)
        self.assertNotIn("milk", kim.get(url("grocery_list")).content.decode())
        self.milk.refresh_from_db()
        self.assertFalse(self.milk.checked)
        self.assertTrue(MealPlanEntry.objects.filter(pk=self.entry.pk).exists())

    def test_each_household_has_its_own_plan_slots(self):
        response = self.kim_client.post(url("plan_add"), {"date": self.today, "slot": "dinner", "title": "Pizza"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(MealPlanEntry.objects.filter(date=self.today, slot="dinner").count(), 2)
        self.entry.refresh_from_db()
        self.assertEqual(self.entry.recipe, self.meatballs)                 # ours untouched

    def test_bot_api(self):
        allow_api_keys(self.kim)
        _token, raw = BotToken.issue(self.kim, "Kim's bot", ["meals:read", "meals:write"])
        auth = {"HTTP_AUTHORIZATION": f"Bearer {raw}"}
        c = Client()
        for path in ("meals/plan", "meals/groceries", "meals/ideas", "meals/recipes"):
            body = c.get(API + path, **auth).content.decode()
            for ours in ("Family Meatballs", "milk", "Try ramen", "Double the garlic"):
                self.assertNotIn(ours, body, path)
        self.assertEqual(c.generic("PATCH", API + f"meals/groceries/{self.milk.pk}", json.dumps({"checked": True}),
                                   content_type="application/json", **auth).status_code, 404)
        self.assertEqual(c.post(API + "meals/plan", json.dumps({"date": self.today.isoformat(), "slot": "lunch",
                                                                "recipe_id": self.meatballs.pk}),
                                content_type="application/json", **auth).status_code, 404)
        MealPlanEntry.objects.filter(pk=self.entry.pk).update(from_bot=True)
        self.assertEqual(c.delete(API + f"meals/plan/{self.entry.pk}", **auth).status_code, 404)
        # Kim's writes land in Kim's household.
        c.post(API + "meals/recipes", json.dumps({"title": "Family Meatballs"}), content_type="application/json", **auth)
        self.assertEqual(Recipe.objects.filter(title="Family Meatballs").count(), 2)
        self.assertEqual(Recipe.objects.get(title="Family Meatballs", household=self.theirs).created_by, self.kim)

    def test_bot_api_without_a_household(self):
        loner = with_apps(User.objects.create_user(username="lee", password=PASSWORD), ["meals"])
        allow_api_keys(loner)
        _token, raw = BotToken.issue(loner, "bot", ["meals:read"])
        response = Client().get(API + "meals/recipes", HTTP_AUTHORIZATION=f"Bearer {raw}")
        self.assertEqual(response.status_code, 409)

    def test_planner_overlay_and_home_only_show_our_meals(self):
        from albright_labs import launcher
        from albright_planner import sources
        with_apps(self.kim, ["meals", "planner"])
        self.assertEqual(sources.meals(self.kim, self.today, self.today, show=True), [])
        self.assertEqual(launcher.meals(self.kim), "No dinner planned")
        self.assertEqual(launcher.meals(self.nate), "Family Meatballs")


class SetupTests(Base):
    def test_no_household_gets_set_up_your_kitchen(self):
        loner = with_apps(User.objects.create_user(username="lee", password=PASSWORD, first_name="Lee"), ["meals"])
        c = self.as_user(loner)
        for path in (url("week"), url("recipes"), url("groceries"), url("ideas")):
            page = c.get(path).content.decode()
            self.assertIn("Set up your kitchen", page, path)
            self.assertNotIn("Family Meatballs", page)
        c.post(url("setup"), {"name": "Lee's kitchen"})
        self.assertEqual(households.household_of(loner).name, "Lee's kitchen")
        self.assertNotIn("Set up your kitchen", c.get(url("week")).content.decode())


# ---------------------------------------------------------------------------
# The library, and Save to my recipes
# ---------------------------------------------------------------------------

class LibraryTests(Base):
    def test_library_is_read_only_for_everyone_with_meals(self):
        meatballs = self.recipe(source_url="https://example.com/meatballs")
        kim = self.as_user(self.kim)
        self.assertEqual(kim.get(url("recipe", meatballs.pk)).status_code, 404)
        self.client.post(url("recipe_visibility", meatballs.pk), {"visibility": "library"})
        library = kim.get(url("recipes") + "?tab=library").content.decode()
        self.assertIn("Family Meatballs", library)
        page = kim.get(url("recipe", meatballs.pk)).content.decode()
        self.assertIn("Save to my recipes", page)
        self.assertNotIn(url("recipe_edit", meatballs.pk), page)
        self.assertEqual(kim.get(url("recipe_edit", meatballs.pk)).status_code, 404)
        self.assertEqual(kim.post(url("recipe_visibility", meatballs.pk), {"visibility": "household"}).status_code, 404)
        self.assertNotIn("Family Meatballs", kim.get(url("recipes")).content.decode())     # not in her own recipes

    def test_save_to_my_recipes_copies_with_credit_and_survives_unsharing(self):
        meatballs = self.recipe(source_url="https://example.com/meatballs", visibility=Recipe.LIBRARY,
                                ingredients=[{"name": "beef"}])
        kim = self.as_user(self.kim)
        response = kim.post(url("recipe_save_copy", meatballs.pk))
        copy = Recipe.objects.get(household=self.theirs)
        self.assertRedirects(response, url("recipe", copy.pk), fetch_redirect_response=False)
        self.assertEqual((copy.title, copy.saved_from, copy.saved_from_name, copy.source_url, copy.created_by, copy.visibility),
                         ("Family Meatballs", meatballs, "Albrights", "https://example.com/meatballs", self.kim, "household"))
        page = kim.get(url("recipe", copy.pk)).content.decode()
        self.assertIn("Saved from Albrights", page)
        self.assertIn(url("recipe_edit", copy.pk), page)                   # hers to edit, rate, plan
        self.client.post(url("recipe_visibility", meatballs.pk), {"visibility": "household"})
        self.assertEqual(kim.get(url("recipe", meatballs.pk)).status_code, 404)
        self.assertEqual(kim.get(url("recipe", copy.pk)).status_code, 200)


# ---------------------------------------------------------------------------
# Sharing one recipe with one person
# ---------------------------------------------------------------------------

class ShareTests(Base):
    def share(self, who, recipe=None):
        return self.client.post(url("recipe_share", (recipe or self.meatballs).pk), {"who": who},
                                HTTP_X_REQUESTED_WITH="XMLHttpRequest")

    def setUp(self):
        super().setUp()
        self.meatballs = self.recipe()

    def test_exact_username_or_email_only(self):
        for partial in ("ki", "Kim Smith", "kim@example", "@example.com", ""):
            response = self.share(partial)
            self.assertEqual(response.status_code, 400, partial)
        self.assertFalse(RecipeShare.objects.exists())
        self.assertEqual(self.share("KIM").json()["shares"], [{"user": self.kim.pk, "name": "Kim"}])   # any case
        RecipeShare.objects.all().delete()
        self.assertTrue(self.share("kim@example.com").json()["ok"])
        no_meals = with_apps(User.objects.create_user(username="quill-no-meals", password=PASSWORD), ["notes"])
        self.assertEqual(self.share(no_meals.username).status_code, 400)          # needs Meals
        self.assertEqual(self.share("sam").status_code, 400)                    # already in our household
        page = self.client.get(url("recipe", self.meatballs.pk)).content.decode()
        self.assertNotIn("quill-no-meals", page)                                           # nobody is ever listed

    def test_recipient_reads_saves_and_gets_a_notice(self):
        self.share("kim")
        share = RecipeShare.objects.get()
        self.assertEqual((share.shared_by, share.shared_with), (self.nate, self.kim))
        kim = self.as_user(self.kim)
        week = kim.get(url("week")).content.decode()
        self.assertIn("<strong>Nathan</strong> shared", week)
        self.assertIn("Family Meatballs</a> with you", week)
        tab = kim.get(url("recipes") + "?tab=shared").content.decode()
        self.assertIn("Shared by <span class=\"meals-by\">Nathan</span>", tab)
        page = kim.get(url("recipe", self.meatballs.pk)).content.decode()
        self.assertIn("shared this with you", page)
        self.assertNotIn(url("recipe_edit", self.meatballs.pk), page)
        self.assertEqual(kim.post(url("recipe_plan", self.meatballs.pk), {"date": self.today, "slot": "dinner"}).status_code, 404)
        self.assertEqual(kim.post(url("recipe_share", self.meatballs.pk), {"who": "nate"}).status_code, 404)  # can't reshare
        share.refresh_from_db()
        self.assertIsNotNone(share.seen_at)                                     # opening it clears the notice
        self.assertNotIn("with you", kim.get(url("week")).content.decode())
        kim.post(url("recipe_save_copy", self.meatballs.pk))
        self.assertTrue(Recipe.objects.filter(household=self.theirs, saved_from=self.meatballs).exists())

    def test_removing_hides_it_again_but_copies_stay(self):
        self.share("kim")
        kim = self.as_user(self.kim)
        kim.post(url("recipe_save_copy", self.meatballs.pk))
        response = self.client.post(url("recipe_share", self.meatballs.pk), {"action": "remove", "user": self.kim.pk},
                                    HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(response.json()["shares"], [])
        self.assertEqual(kim.get(url("recipe", self.meatballs.pk)).status_code, 404)
        self.assertNotIn("Family Meatballs", kim.get(url("recipes") + "?tab=shared").content.decode()
                         .split('id="recipe-list"', 1)[1])
        self.assertTrue(Recipe.objects.filter(household=self.theirs).exists())

    def test_share_dialog_never_lists_people(self):
        page = self.client.get(url("recipes")).content.decode()
        dialog = page.split('id="share-dialog"', 1)[1].split("</dialog>", 1)[0]
        self.assertNotIn("<datalist", dialog)
        self.assertNotIn("kim", dialog.lower())
        self.assertIn('autocomplete="off"', dialog)


# ---------------------------------------------------------------------------
# Ratings: on the cards, private to the household, and the filters
# ---------------------------------------------------------------------------

class CardRatingTests(Base):
    def test_card_dropdown_saves_without_opening_the_recipe(self):
        meatballs = self.recipe()
        self.rate(self.wife, meatballs, FAV, "So good")
        page = self.client.get(url("recipes")).content.decode()
        card = page.split(f'data-recipe="{meatballs.pk}"', 1)[1].split("</article>", 1)[0]
        link_end = card.index("</a>")
        self.assertLess(link_end, card.index('<select name="level"'))          # the drop-down isn't inside the link
        self.assertIn('<option value="" selected>Not rated</option>', card)
        self.assertIn("Sam: ❤️ Favorite - &ldquo;So good&rdquo;", card)          # her stamp, with her note on tap
        response = self.client.post(url("recipe_rate", meatballs.pk), {"level": str(STAPLE)}, HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["label"], "Staple")
        self.assertEqual(RecipeRating.objects.get(recipe=meatballs, user=self.nate).level, STAPLE)
        self.assertEqual(RecipeRating.objects.get(recipe=meatballs, user=self.wife).note, "So good")    # untouched

    def test_ratings_never_leave_the_household(self):
        meatballs = self.recipe(visibility=Recipe.LIBRARY)
        self.rate(self.wife, meatballs, WONT, "Too salty")
        kim = self.as_user(self.kim)
        self.rate(self.kim, meatballs, FAV, "Kim loves it")
        for page in (kim.get(url("recipe", meatballs.pk)).content.decode(), kim.get(url("recipes") + "?tab=library").content.decode()):
            self.assertNotIn("Too salty", page)
            self.assertNotIn("Sam", page)
        ours = self.client.get(url("recipe", meatballs.pk)).content.decode()
        self.assertIn("Too salty", ours)
        self.assertNotIn("Kim loves it", ours)
        self.assertNotIn("Kim loves it", self.client.get(url("recipes")).content.decode())
        allow_api_keys(self.kim)
        _token, raw = BotToken.issue(self.kim, "bot", ["meals:read"])
        body = Client().get(API + "meals/recipes?scope=library", HTTP_AUTHORIZATION=f"Bearer {raw}").json()
        self.assertEqual(body["recipes"][0]["ratings"], [{"by": "Kim", "level": FAV, "label": "Favorite"}])


class RatingFilterTests(Base):
    def setUp(self):
        super().setUp()
        self.r = {name: self.recipe(name) for name in ("Tacos", "Chili", "Liver", "Pho", "Salad")}
        self.rate(self.nate, self.r["Tacos"], STAPLE)
        self.rate(self.wife, self.r["Tacos"], FAV)
        self.rate(self.nate, self.r["Chili"], FAV)
        self.rate(self.wife, self.r["Chili"], WONT)
        self.rate(self.wife, self.r["Pho"], FAV)
        self.rate(self.nate, self.r["Liver"], WONT)
        self.rate(self.kim, self.r["Salad"], STAPLE)                         # not ours: never counts

    def titles(self, query):
        page = self.client.get(url("recipes") + "?" + query).content.decode()
        return sorted(re.findall(r'class="meals-recipe-card__title">([^<]+?)(?: <|</strong>)', page))

    def test_filters(self):
        wife = self.wife.pk
        self.assertEqual(self.titles("rated_by=me&rating=6"), ["Tacos"])                      # my Staples
        self.assertEqual(self.titles(f"rated_by={wife}&rating=5"), ["Pho", "Tacos"])          # her Favorites
        self.assertEqual(self.titles("rated_by=anyone&rating=1"), ["Chili", "Liver"])         # anyone: won't eat
        self.assertEqual(self.titles("rated_by=me&rating=none"), ["Pho", "Salad"])            # I haven't rated
        self.assertEqual(self.titles("rated_by=me&rating=5&rating=6"), ["Chili", "Tacos"])    # more than one level
        self.assertEqual(self.titles("rated_by=anyone&rating=none"), ["Salad"])               # nobody here rated it
        self.assertEqual(self.titles("love=1"), ["Tacos"])                                    # we both love
        self.assertEqual(self.titles(f"rated_by={self.kim.pk}&rating=6"), ["Tacos"])          # not a member: me
        page = self.client.get(url("recipes") + f"?rated_by={wife}&rating=5").content.decode()
        self.assertIn(f'<option value="{wife}" selected>Sam</option>', page)                 # kept in the URL / form

    def test_same_filters_in_the_bot_api(self):
        allow_api_keys(self.nate)
        _token, raw = BotToken.issue(self.nate, "bot", ["meals:read"])

        def api(query):
            body = Client().get(API + "meals/recipes?" + query, HTTP_AUTHORIZATION=f"Bearer {raw}").json()
            return sorted(r["title"] for r in body["recipes"])
        self.assertEqual(api("rated_by=me&rating=6"), ["Tacos"])
        self.assertEqual(api(f"rated_by={self.wife.pk}&rating=5"), ["Pho", "Tacos"])
        self.assertEqual(api("rated_by=anyone&rating=1"), ["Chili", "Liver"])
        self.assertEqual(api("rating=none"), ["Pho", "Salad"])
        self.assertEqual(api("love=1"), ["Tacos"])
        self.assertEqual(Client().get(API + "meals/recipes?scope=everything", HTTP_AUTHORIZATION=f"Bearer {raw}").status_code, 400)


# ---------------------------------------------------------------------------
# My account > Household
# ---------------------------------------------------------------------------

class HouseholdPageTests(Base):
    def test_creator_adds_by_exact_name_and_removes(self):
        loner = with_apps(User.objects.create_user(username="lee", password=PASSWORD, email="lee@example.com"), ["meals"])
        self.client.post(reverse("labs:household"), {"action": "add", "who": "le"})
        self.assertIsNone(households.household_of(loner))
        self.client.post(reverse("labs:household"), {"action": "add", "who": "LEE@example.com"})
        self.assertEqual(households.household_of(loner), self.ours)
        self.client.post(reverse("labs:household"), {"action": "add", "who": "kim"})          # already in one
        self.assertEqual(households.household_of(self.kim), self.theirs)
        self.client.post(reverse("labs:household"), {"action": "remove", "user": loner.pk})
        self.assertIsNone(households.household_of(loner))
        page = self.client.get(reverse("labs:household")).content.decode()
        self.assertIn("Albrights", page)
        self.assertIn("Sam", page)

    def test_only_the_creator_manages(self):
        sam = self.as_user(self.wife)
        loner = with_apps(User.objects.create_user(username="lee", password=PASSWORD), ["meals"])
        sam.post(reverse("labs:household"), {"action": "add", "who": "lee"})
        self.assertIsNone(households.household_of(loner))
        sam.post(reverse("labs:household"), {"action": "rename", "name": "Mine now"})
        self.ours.refresh_from_db()
        self.assertEqual(self.ours.name, "Albrights")
        sam.post(reverse("labs:household"), {"action": "leave"})
        self.assertIsNone(households.household_of(self.wife))

    def test_invite_signs_them_straight_in(self):
        self.client.post(reverse("labs:household"), {"action": "invite", "email": "new@example.com", "name": "Robin"})
        invite = Invitation.objects.get(email="new@example.com")
        self.assertEqual((invite.household, invite.created_by), (self.ours, self.nate))
        self.assertEqual(sorted(invite.apps), ["meals", "money"])                # only household apps the inviter has
        raw = "test-token-" + "x" * 20
        from albright_labs.models import hash_token
        Invitation.objects.filter(pk=invite.pk).update(token_hash=hash_token(raw))
        Client().post(reverse("labs:signup", args=[raw]), {"username": "robin", "password1": "a-Strong-pass-123!",
                                                           "password2": "a-Strong-pass-123!"})
        robin = User.objects.get(username="robin")
        self.assertEqual(households.household_of(robin), self.ours)
        self.assertEqual(sorted(UserPreference.objects.get(user=robin).allowed_apps), ["meals", "money"])
