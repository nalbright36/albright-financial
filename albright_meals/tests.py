"""Albright Meals: access (a Meals-only account sees just Meals, 404 for
everything else, lands on This week; created from Admin > Users with a
password setup link); grocery quick-add parsing and the aisle guess; adding
the week's ingredients merges duplicates; checking items and clearing them;
the 20-second refresh endpoint; the week page; every Bot API endpoint with
and without its scope, the plan and recipe upserts, validation and the
activity log; "Recipe Bot" labels; the Home tile; the digest line."""
import json
import re
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from albright_bot_api.models import BotRequestLog, BotToken
from albright_labs.models import UserPreference

from . import groceries, plan
from .digest import digest_lines
from .models import GroceryItem, MealIdea, MealPlanEntry, MealsSettings, Recipe

PASSWORD = "pw-not-real-12345"
OUTLOOK = "albright_trading_app.views.get_market_outlook"
API = "/api/bot/v1/"


def switch_buttons(content):
    row = content.split('class="navbar__switch-row"')[1].split("</div>")[0]
    return re.findall(r'class="app-switch-btn[^"]*" data-app-target="(\w+)"[^>]*>([^<]+)</a>', row)


class Base(TestCase):
    def setUp(self):
        self.outlook = mock.patch(OUTLOOK, return_value=None)
        self.outlook.start()
        self.addCleanup(self.outlook.stop)
        self.owner = User.objects.create_superuser(username="nate", password=PASSWORD, email="", first_name="Nate")
        self.client.force_login(self.owner)
        self.today = timezone.localdate()
        self.monday = plan.week_start(self.today)

    def recipe(self, title, ingredients, **kw):
        return Recipe.objects.create(title=title, ingredients=ingredients, created_by=self.owner, **kw)


# ---------------------------------------------------------------------------
# Access: the Meals-only account
# ---------------------------------------------------------------------------

class MealsOnlyAccountTests(Base):
    def create_wife(self, **extra):
        response = self.client.post(reverse("albright_control:users"), {
            "action": "create", "username": "sam", "first_name": "Sam", "email": "sam@example.com",
            "password": "", "apps": ["meals"], **extra})
        self.assertRedirects(response, reverse("albright_control:users"), fetch_redirect_response=False)
        return User.objects.get(username="sam")

    def test_created_from_admin_users_with_meals_only(self):
        sam = self.create_wife()
        self.assertFalse(sam.is_staff or sam.is_superuser)
        self.assertFalse(sam.has_usable_password())                 # she chooses it from the setup link
        pref = UserPreference.objects.get(user=sam)
        self.assertEqual(pref.allowed_apps, ["meals"])
        self.assertEqual((pref.landing, pref.landing_page), ("page", "meals:week"))

    def test_she_sees_only_meals_and_everything_else_is_404(self):
        sam = self.create_wife()
        self.client.force_login(sam)
        week = self.client.get(reverse("albright_meals:week"))
        self.assertEqual(week.status_code, 200)
        self.assertEqual(switch_buttons(week.content.decode()), [("labs", "Home"), ("meals", "Meals")])
        self.assertContains(week, '<body data-app="meals">')
        self.assertContains(week, 'class="navbar__brand">Albright Meals</a>')
        for url in (reverse("trading_home"), reverse("grok_bot"), reverse("albright_reselling_app:dashboard"),
                    reverse("albright_reselling_app:watchlist_summary"), reverse("albright_crypto_app:inbox"),
                    reverse("albright_control:overview"), reverse("albright_control:users"),
                    reverse("albright_resume_admin:overview"), reverse("labs:tile", args=["trade"])):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 404)
        for name in ("groceries", "ideas", "recipes"):
            self.assertEqual(self.client.get(reverse(f"albright_meals:{name}")).status_code, 200)
        hub = self.client.get(reverse("labs:hub")).content.decode()
        self.assertEqual(re.findall(r'data-tile="(\w+)"', hub), ["meals"])

    def test_she_lands_on_this_week(self):
        sam = self.create_wife()
        sam.set_password(PASSWORD)
        sam.save()
        self.client.logout()
        self.client.post(reverse("user_login"), {"username": "sam", "password": PASSWORD})
        self.assertRedirects(self.client.get("/"), reverse("albright_meals:week"), fetch_redirect_response=False)

    def test_setup_link_shown_when_email_is_off(self):
        sam = self.create_wife()
        with override_settings(EMAIL_CONFIGURED=False):
            page = self.client.post(reverse("albright_control:users"), {"action": "setup_link", "user_id": sam.pk})
        self.assertContains(page, "this site can&#x27;t send email")
        link = re.search(r'id="setup-link-value"[^>]*|value="(http[^"]+)"', page.content.decode())
        url = re.search(r'value="(http://testserver/reset/[^"]+)" id="setup-link-value"|'
                        r'readonly value="(http://testserver/reset/[^"]+)"', page.content.decode())
        self.assertTrue(link and url)
        self.assertEqual(len(mail.outbox), 0)
        self.set_password_from(url.group(1) or url.group(2), sam)

    def set_password_from(self, link, user):
        self.client.logout()
        path = link.replace("http://testserver", "")
        form_page = self.client.get(path, follow=True)                 # Django swaps the token for a session
        self.assertContains(form_page, 'id="set-password-form"')
        done = self.client.post(form_page.redirect_chain[-1][0] if form_page.redirect_chain else path,
                                {"new_password1": "a-Long-unusual-pass-9481", "new_password2": "a-Long-unusual-pass-9481"})
        self.assertRedirects(done, reverse("password_reset_complete"), fetch_redirect_response=False)
        user.refresh_from_db()
        self.assertTrue(user.check_password("a-Long-unusual-pass-9481"))
        self.assertEqual(self.client.get(path, follow=True).context["validlink"], False)   # used up

    @override_settings(EMAIL_CONFIGURED=True, EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
    def test_setup_link_emailed_when_email_is_configured(self):
        sam = self.create_wife()
        response = self.client.post(reverse("albright_control:users"), {"action": "setup_link", "user_id": sam.pk})
        self.assertRedirects(response, reverse("albright_control:users"), fetch_redirect_response=False)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["sam@example.com"])
        link = re.search(r"(http://testserver/reset/\S+)", mail.outbox[0].body).group(1)
        self.set_password_from(link, sam)

    def test_setup_link_is_superuser_only(self):
        sam = self.create_wife()
        self.client.force_login(sam)
        self.assertEqual(self.client.post(reverse("albright_control:users"),
                                          {"action": "setup_link", "user_id": sam.pk}).status_code, 404)

    def test_public_reset_page_without_email(self):
        self.client.logout()
        with override_settings(EMAIL_CONFIGURED=False):
            self.assertContains(self.client.get(reverse("password_reset")), 'id="reset-email-off"')
        self.assertContains(self.client.get(reverse("user_login")), f'href="{reverse("password_reset")}"')

    def test_other_users_without_meals_get_404(self):
        other = User.objects.create_user(username="pat", password=PASSWORD)
        UserPreference.objects.create(user=other, allowed_apps=["trade"])
        self.client.force_login(other)
        for url in (reverse("albright_meals:week"), reverse("albright_meals:grocery_list"),
                    reverse("labs:tile", args=["meals"])):
            self.assertEqual(self.client.get(url).status_code, 404)


# ---------------------------------------------------------------------------
# Groceries
# ---------------------------------------------------------------------------

class QuickAddParsingTests(TestCase):
    def test_lines(self):
        cases = {
            "2 lb chicken thighs": ("chicken thighs", Decimal("2"), "lb", "", "meat"),
            "1 1/2 cups milk": ("milk", Decimal("1.5"), "cup", "", "dairy"),
            "½ tsp cumin": ("cumin", Decimal("0.5"), "tsp", "", "pantry"),
            "3 onions": ("onions", Decimal("3"), "", "", "produce"),
            "milk (whole)": ("milk", None, "", "whole", "dairy"),
            "2 cans black beans": ("black beans", Decimal("2"), "can", "", "pantry"),
            "1 onion, diced": ("onion", Decimal("1"), "", "diced", "produce"),
            "12 oz frozen peas": ("frozen peas", Decimal("12"), "oz", "", "frozen"),
            "paper towels": ("paper towels", None, "", "", "household"),
            "6 bagels": ("bagels", Decimal("6"), "", "", "bakery"),
            "2 lbs. ground beef": ("ground beef", Decimal("2"), "lb", "", "meat"),
            "mystery thing": ("mystery thing", None, "", "", "other"),
        }
        for line, (name, qty, unit, note, aisle) in cases.items():
            with self.subTest(line=line):
                parsed = groceries.parse_line(line)
                self.assertEqual((parsed["name"], parsed["quantity"], parsed["unit"], parsed["note"], parsed["aisle"]),
                                 (name, qty, unit, note, aisle))

    def test_aisle_guess_prefers_the_longer_phrase(self):
        self.assertEqual(groceries.guess_aisle("black pepper"), "pantry")
        self.assertEqual(groceries.guess_aisle("red bell pepper"), "produce")
        self.assertEqual(groceries.guess_aisle("peanut butter"), "pantry")
        self.assertEqual(groceries.guess_aisle("chicken broth"), "pantry")
        self.assertEqual(groceries.guess_aisle("vanilla ice cream"), "frozen")

    def test_same_thing_matching(self):
        self.assertEqual(groceries.name_key("Onions"), groceries.name_key("onion"))
        self.assertEqual(groceries.name_key("tomatoes"), groceries.name_key("Tomato"))
        self.assertEqual(groceries.name_key("berries"), groceries.name_key("berry"))
        self.assertNotEqual(groceries.name_key("onion"), groceries.name_key("green onion"))


class GroceryPageTests(Base):
    def test_quick_add_merges_with_an_unchecked_line(self):
        url = reverse("albright_meals:groceries")
        self.client.post(url, {"line": "2 lb chicken thighs"})
        self.client.post(url, {"line": "1 lb chicken thigh"})
        item = GroceryItem.objects.get()
        self.assertEqual((item.name, item.quantity, item.unit, item.aisle, item.added_by),
                         ("chicken thighs", Decimal("3"), "lb", "meat", self.owner))
        self.client.post(url, {"line": "8 oz chicken thighs"})                         # other unit: its own line
        self.assertEqual(GroceryItem.objects.count(), 2)
        response = self.client.post(url, {"line": "milk"}, HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        data = response.json()
        self.assertTrue(data["ok"])
        self.assertIn("milk", data["html"])
        self.assertEqual(data["unchecked"], 3)

    def test_checked_lines_never_take_merges(self):
        item = GroceryItem.objects.create(name="eggs", quantity=12, aisle="dairy", checked=True,
                                          checked_at=timezone.now())
        self.client.post(reverse("albright_meals:groceries"), {"line": "6 eggs"})
        self.assertEqual(GroceryItem.objects.count(), 2)
        item.refresh_from_db()
        self.assertEqual(item.quantity, 12)

    def test_checking_strikes_through_and_moves_to_the_bottom_of_its_aisle(self):
        apples = GroceryItem.objects.create(name="apples", aisle="produce")
        GroceryItem.objects.create(name="bananas", aisle="produce")
        GroceryItem.objects.create(name="milk", aisle="dairy")
        response = self.client.post(reverse("albright_meals:grocery_toggle", args=[apples.pk]),
                                    HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        apples.refresh_from_db()
        self.assertTrue(apples.checked)
        self.assertEqual(apples.checked_by, self.owner)
        html = response.json()["html"]
        produce = html.split('data-aisle="produce"')[1].split("</section>")[0]
        self.assertLess(produce.index("bananas"), produce.index("apples"))
        self.assertIn(f'class="meals-item is-checked" data-item="{apples.pk}"', produce)
        self.assertIn('aria-pressed="true"', produce)
        self.assertLess(html.index('data-aisle="produce"'), html.index('data-aisle="dairy"'))   # aisle order
        self.client.post(reverse("albright_meals:grocery_toggle", args=[apples.pk]))   # without JS: redirect
        apples.refresh_from_db()
        self.assertFalse(apples.checked)

    def test_clear_checked(self):
        GroceryItem.objects.create(name="apples", checked=True, checked_at=timezone.now())
        GroceryItem.objects.create(name="milk")
        self.client.post(reverse("albright_meals:grocery_clear"))
        self.assertEqual(list(GroceryItem.objects.values_list("name", flat=True)), ["milk"])

    def test_refresh_endpoint(self):
        url = reverse("albright_meals:grocery_list")
        first = self.client.get(url)
        self.assertIn("no-cache", first["Cache-Control"])
        data = first.json()
        self.assertTrue(data["changed"])
        version = data["version"]
        self.assertEqual(self.client.get(url, {"version": version}).json(), {"changed": False, "version": version})
        GroceryItem.objects.create(name="milk")
        changed = self.client.get(url, {"version": version}).json()
        self.assertTrue(changed["changed"])
        self.assertIn("milk", changed["html"])
        self.assertEqual(changed["unchecked"], 1)
        page = self.client.get(reverse("albright_meals:groceries")).content.decode()
        self.assertIn(f'data-list-url="{url}"', page)
        self.assertIn("REFRESH_MS = 20000", open(
            __import__("pathlib").Path(__file__).parent / "static" / "meals" / "groceries.js", encoding="utf-8").read())
        # The quick-add box is outside the part that's replaced, so typing survives a refresh.
        self.assertLess(page.index('id="grocery-line"'), page.index('id="grocery-list"'))

    def test_add_ingredients_from_the_week_merges_duplicates(self):
        stew = self.recipe("Stew", [{"name": "onion", "quantity": "1", "unit": "", "note": "diced"},
                                    {"name": "chicken thighs", "quantity": "1", "unit": "lb", "note": ""},
                                    {"name": "garlic", "quantity": "2", "unit": "cloves", "note": ""}])
        curry = self.recipe("Curry", [{"name": "Onions", "quantity": "2", "unit": "", "note": ""},
                                      {"name": "chicken thighs", "quantity": "1/2", "unit": "lbs", "note": ""},
                                      {"name": "garlic", "quantity": "1", "unit": "tbsp", "note": ""},
                                      "1 can coconut milk"])
        skipped = self.recipe("Skipped", [{"name": "onion", "quantity": "5"}])
        MealPlanEntry.objects.create(date=self.monday, slot="dinner", recipe=stew)
        MealPlanEntry.objects.create(date=self.monday + timedelta(days=1), slot="dinner", recipe=curry)
        MealPlanEntry.objects.create(date=self.monday + timedelta(days=2), slot="dinner", recipe=skipped,
                                     status="skipped")
        MealPlanEntry.objects.create(date=self.monday + timedelta(days=8), slot="dinner", recipe=skipped)  # next week
        GroceryItem.objects.create(name="onion", quantity=1, aisle="produce")              # already on the list
        self.client.post(reverse("albright_meals:grocery_from_week"), {"start": self.monday.isoformat()})
        rows = {(i.name.lower(), i.unit): i for i in GroceryItem.objects.all()}
        self.assertEqual(rows[("onion", "")].quantity, Decimal("4"))                      # 1 + 2 + the list's 1
        self.assertEqual(rows[("chicken thighs", "lb")].quantity, Decimal("1.5"))
        self.assertEqual(rows[("garlic", "clove")].quantity, Decimal("2"))                # units differ: two lines
        self.assertEqual(rows[("garlic", "tbsp")].quantity, Decimal("1"))
        self.assertEqual(rows[("coconut milk", "can")].quantity, Decimal("1"))
        self.assertEqual(len(rows), 5)

    def test_recipe_add_to_grocery_list(self):
        soup = self.recipe("Soup", [{"name": "carrots", "quantity": "3"}, {"name": "carrot", "quantity": "1"}])
        self.client.post(reverse("albright_meals:recipe_groceries", args=[soup.pk]))
        item = GroceryItem.objects.get()
        self.assertEqual((item.quantity, item.aisle, item.from_recipe), (Decimal("4"), "produce", soup))


# ---------------------------------------------------------------------------
# This week, ideas, recipes
# ---------------------------------------------------------------------------

class WeekTests(Base):
    def test_seven_days_and_week_arrows(self):
        MealPlanEntry.objects.create(date=self.today, slot="dinner", title="Tacos")
        page = self.client.get(reverse("albright_meals:week")).content.decode()
        self.assertEqual(len(re.findall(r'class="card meals-day', page)), 7)
        self.assertIn("Tacos", page)
        self.assertIn(f'?start={(self.monday - timedelta(days=7)).isoformat()}', page)
        self.assertIn(f'?start={(self.monday + timedelta(days=7)).isoformat()}', page)
        later = self.client.get(reverse("albright_meals:week"), {"start": (self.monday + timedelta(days=9)).isoformat()})
        self.assertEqual(later.context["start"], self.monday + timedelta(days=7))      # snaps to Monday
        self.assertNotIn("Tacos", later.content.decode())

    def test_add_move_swap_status_edit(self):
        pasta = self.recipe("Pasta", [])
        self.client.post(reverse("albright_meals:plan_add"), {"date": self.monday.isoformat(), "slot": "dinner",
                                                              "recipe": pasta.pk})
        self.client.post(reverse("albright_meals:plan_add"), {"date": (self.monday + timedelta(days=1)).isoformat(),
                                                              "slot": "dinner", "title": "Leftovers"})
        a = MealPlanEntry.objects.get(recipe=pasta)
        b = MealPlanEntry.objects.get(title="Leftovers")
        self.client.post(reverse("albright_meals:plan_move", args=[a.pk]),
                         {"date": b.date.isoformat(), "slot": "dinner"})
        a.refresh_from_db()
        b.refresh_from_db()
        self.assertEqual((a.date, b.date), (self.monday + timedelta(days=1), self.monday))   # swapped
        self.client.post(reverse("albright_meals:plan_status", args=[a.pk]), {"status": "cooked"})
        a.refresh_from_db()
        self.assertEqual(a.status, "cooked")
        self.client.post(reverse("albright_meals:plan_entry", args=[b.pk]), {
            "date": b.date.isoformat(), "slot": "lunch", "recipe": "", "title": "Sandwiches", "notes": "",
            "status": "planned"})
        b.refresh_from_db()
        self.assertEqual((b.slot, b.title), ("lunch", "Sandwiches"))
        week = self.client.get(reverse("albright_meals:week"), {"start": self.monday.isoformat()}).content.decode()
        self.assertIn(f'href="{reverse("albright_meals:recipe", args=[pasta.pk])}">Pasta</a>', week)   # tap -> recipe

    def test_digest_setting_is_the_owners(self):
        self.assertContains(self.client.get(reverse("albright_meals:week")), 'id="meals-settings"')
        self.client.post(reverse("albright_meals:settings"), {})
        self.assertFalse(MealsSettings.load().digest_tonight)
        sam = User.objects.create_user(username="sam", password=PASSWORD)
        UserPreference.objects.create(user=sam, allowed_apps=["meals"])
        self.client.force_login(sam)
        self.assertNotContains(self.client.get(reverse("albright_meals:week")), 'id="meals-settings"')
        self.client.post(reverse("albright_meals:settings"), {"digest_tonight": "on"})
        self.assertFalse(MealsSettings.load().digest_tonight)                       # unchanged


class IdeaAndRecipeTests(Base):
    def test_idea_into_the_plan_or_declined(self):
        self.client.post(reverse("albright_meals:ideas"), {"title": "Gnocchi", "notes": "sheet pan",
                                                           "link": "https://example.com/gnocchi"})
        idea = MealIdea.objects.get()
        self.assertEqual(idea.added_by, self.owner)
        self.client.post(reverse("albright_meals:idea_plan", args=[idea.pk]),
                         {"date": self.today.isoformat(), "slot": "dinner"})
        idea.refresh_from_db()
        self.assertEqual(idea.status, "planned")
        self.assertEqual(idea.planned_entry.title, "Gnocchi")
        other = MealIdea.objects.create(title="Liver")
        self.client.post(reverse("albright_meals:idea_decline", args=[other.pk]))
        other.refresh_from_db()
        self.assertEqual(other.status, "declined")
        self.assertNotContains(self.client.get(reverse("albright_meals:ideas")), "Liver")
        self.assertContains(self.client.get(reverse("albright_meals:ideas"), {"show": "done"}), "Liver")

    def test_recipe_search_tags_detail_and_add_to_plan(self):
        self.recipe("Chicken curry", [{"name": "chicken", "quantity": "1", "unit": "lb"}], tags=["chicken", "quick"],
                    steps=["Brown the chicken.", "Simmer."])
        self.recipe("Pancakes", [], tags=["breakfast"])
        page = self.client.get(reverse("albright_meals:recipes"), {"q": "curry"})
        self.assertEqual([r.title for r in page.context["recipes"]], ["Chicken curry"])
        self.assertEqual([r.title for r in self.client.get(reverse("albright_meals:recipes"),
                                                           {"tag": "breakfast"}).context["recipes"]], ["Pancakes"])
        curry = Recipe.objects.get(title="Chicken curry")
        detail = self.client.get(reverse("albright_meals:recipe", args=[curry.pk]))
        self.assertContains(detail, "<strong>1 lb</strong> chicken")
        self.assertContains(detail, "<li>Simmer.</li>")
        self.client.post(reverse("albright_meals:recipe_plan", args=[curry.pk]),
                         {"date": self.today.isoformat(), "slot": "dinner"})
        self.assertEqual(MealPlanEntry.objects.get(date=self.today, slot="dinner").recipe, curry)

    def test_recipe_form(self):
        self.client.post(reverse("albright_meals:recipe_add"), {
            "title": "Chili", "ingredients_text": "2 lb ground beef\n1 onion, diced", "steps_text": "Brown.\nSimmer.",
            "tags_text": "Beef, Easy", "description": "", "notes": "", "source_url": "", "image_url": ""})
        chili = Recipe.objects.get(title="Chili")
        self.assertEqual(chili.ingredients, [{"name": "ground beef", "quantity": "2", "unit": "lb", "note": ""},
                                             {"name": "onion", "quantity": "1", "unit": "", "note": "diced"}])
        self.assertEqual((chili.steps, chili.tags, chili.created_by), (["Brown.", "Simmer."], ["beef", "easy"],
                                                                       self.owner))


# ---------------------------------------------------------------------------
# Bot API
# ---------------------------------------------------------------------------

@override_settings(BOT_API_REQUIRE_HTTPS=False)
class BotApiTests(Base):
    def setUp(self):
        super().setUp()
        self.token, self.raw = BotToken.issue(self.owner, "Recipe Bot", ["meals:read", "meals:write"])
        self.read_only, self.read_raw = BotToken.issue(self.owner, "Reader", ["meals:read"])
        self.none, self.none_raw = BotToken.issue(self.owner, "Other bot", ["ideas:read"])

    def call(self, method, path, body=None, raw=None):
        kwargs = {"HTTP_AUTHORIZATION": f"Bearer {raw or self.raw}"}
        if body is not None:
            kwargs.update(data=json.dumps(body), content_type="application/json")
        return getattr(self.client, method.lower())(API + path, **kwargs)

    def test_every_endpoint_needs_its_scope(self):
        entry = MealPlanEntry.objects.create(date=self.today, slot="dinner", title="Bot", from_bot=True)
        item = GroceryItem.objects.create(name="milk")
        reads = ["meals/plan", "meals/groceries", "meals/ideas", "meals/recipes"]
        writes = [("POST", "meals/plan", {"date": self.today.isoformat(), "slot": "lunch", "title": "Soup"}),
                  ("POST", "meals/groceries", {"name": "eggs"}),
                  ("PATCH", f"meals/groceries/{item.pk}", {"checked": True}),
                  ("POST", "meals/ideas", {"title": "Ramen"}),
                  ("POST", "meals/recipes", {"title": "Ramen"}),
                  ("DELETE", f"meals/plan/{entry.pk}", None)]
        for path in reads:
            with self.subTest(read=path):
                self.assertEqual(self.call("GET", path, raw=self.none_raw).status_code, 403)
                self.assertEqual(self.call("GET", path, raw=self.read_raw).status_code, 200)
        for method, path, body in writes:
            with self.subTest(write=path, method=method):
                self.assertEqual(self.call(method, path, body, raw=self.read_raw).status_code, 403)
                self.assertIn(self.call(method, path, body).status_code, (200, 201))
        self.assertEqual(self.client.get(API + "meals/plan").status_code, 401)           # no token at all
        self.assertEqual(self.call("PUT", "meals/plan", {}).status_code, 405)

    def test_every_call_is_logged(self):
        before = BotRequestLog.objects.count()
        self.call("GET", "meals/groceries")
        self.call("POST", "meals/groceries", {"name": "x" * 200})                     # rejected: also logged
        logs = list(BotRequestLog.objects.order_by("-pk")[:2])
        self.assertEqual(BotRequestLog.objects.count(), before + 2)
        self.assertEqual([(l.status, l.endpoint) for l in reversed(logs)],
                         [(200, API + "meals/groceries"), (400, API + "meals/groceries")])
        self.assertIn("listed 0 grocery items", logs[1].summary)

    def test_plan_upsert_by_date_and_slot(self):
        pasta = Recipe.objects.create(title="Pasta")
        first = self.call("POST", "meals/plan", {"date": self.today.isoformat(), "slot": "dinner", "title": "Tacos"})
        self.assertEqual(first.status_code, 201)
        self.assertTrue(first.json()["created"])
        second = self.call("POST", "meals/plan", {"date": self.today.isoformat(), "slot": "dinner",
                                                  "recipe_id": pasta.pk})
        self.assertEqual((second.status_code, second.json()["created"], second.json()["id"]),
                         (200, False, first.json()["id"]))
        entry = MealPlanEntry.objects.get()
        self.assertEqual((entry.recipe, entry.title, entry.from_bot, entry.bot_token), (pasta, "", True, self.token))
        listed = self.call("GET", f"meals/plan?start={self.today.isoformat()}&end={self.today.isoformat()}").json()
        self.assertEqual(listed["entries"][0]["title"], "Pasta")
        self.assertEqual(listed["entries"][0]["added_by"], "Recipe Bot")
        for body in ({"date": "tomorrow", "slot": "dinner", "title": "x"},
                     {"date": self.today.isoformat(), "slot": "brunch", "title": "x"},
                     {"date": self.today.isoformat(), "slot": "dinner"},
                     {"date": self.today.isoformat(), "slot": "dinner", "title": "x" * 201}):
            with self.subTest(body=body):
                self.assertEqual(self.call("POST", "meals/plan", body).status_code, 400)
        self.assertEqual(self.call("POST", "meals/plan", {"date": self.today.isoformat(), "slot": "dinner",
                                                          "recipe_id": 99999}).status_code, 404)
        self.assertEqual(self.call("GET", "meals/plan?start=2026-01-01&end=2026-06-01").status_code, 400)

    def test_delete_only_the_bots_own_entries(self):
        mine = MealPlanEntry.objects.create(date=self.today, slot="lunch", title="Sam's", created_by=self.owner)
        bots = MealPlanEntry.objects.create(date=self.today, slot="dinner", title="Bot's", from_bot=True)
        self.assertEqual(self.call("DELETE", f"meals/plan/{mine.pk}").status_code, 403)
        self.assertEqual(self.call("DELETE", f"meals/plan/{bots.pk}").json(), {"deleted": bots.pk})
        self.assertEqual(list(MealPlanEntry.objects.values_list("title", flat=True)), ["Sam's"])
        self.assertEqual(self.call("DELETE", "meals/plan/99999").status_code, 404)

    def test_groceries_add_merge_and_patch(self):
        GroceryItem.objects.create(name="onion", quantity=1, aisle="produce")
        GroceryItem.objects.create(name="eggs", quantity=6, aisle="dairy", checked=True, checked_at=timezone.now())
        response = self.call("POST", "meals/groceries", {"items": [
            {"name": "Onions", "quantity": 2}, {"name": "eggs", "quantity": "12"},
            {"name": "chicken thighs", "quantity": "1 1/2", "unit": "lbs"}, {"name": "dish soap"}]})
        self.assertEqual(response.status_code, 201)
        results = response.json()["items"]
        self.assertEqual([r["merged"] for r in results], [True, False, False, False])
        self.assertEqual(results[0]["quantity"], "3")
        self.assertEqual((results[2]["unit"], results[2]["quantity"], results[2]["aisle"]), ("lb", "1.5", "meat"))
        self.assertEqual(results[3]["aisle"], "household")
        self.assertEqual(GroceryItem.objects.filter(name="eggs").count(), 2)           # checked one untouched
        soap = GroceryItem.objects.get(name="dish soap")
        self.assertTrue(soap.from_bot)
        patched = self.call("PATCH", f"meals/groceries/{soap.pk}", {"checked": True, "note": "the green one"})
        self.assertEqual((patched.json()["checked"], patched.json()["note"]), (True, "the green one"))
        soap.refresh_from_db()
        self.assertTrue(soap.checked_by_bot)
        for body in ({"items": []}, {"name": ""}, {"name": "x", "quantity": -1}, {"name": "x", "aisle": "garden"},
                     {"items": [{"name": "x"}] * 101}, {"name": "x", "note": "n" * 201}):
            with self.subTest(body=str(body)[:40]):
                self.assertEqual(self.call("POST", "meals/groceries", body).status_code, 400)
        self.assertEqual(self.call("PATCH", f"meals/groceries/{soap.pk}", {"checked": "yes"}).status_code, 400)
        self.assertEqual(self.call("PATCH", f"meals/groceries/{soap.pk}", {}).status_code, 400)

    def test_recipe_upsert_by_title(self):
        created = self.call("POST", "meals/recipes", {
            "title": "Chicken Curry", "ingredients": [{"name": "chicken", "quantity": 1, "unit": "lb"}, "1 onion"],
            "steps": ["Brown.", "Simmer."], "tags": ["Quick", "quick", "chicken"], "servings": 4,
            "source_url": "https://example.com/curry"})
        self.assertEqual(created.status_code, 201)
        recipe_id = created.json()["id"]
        recipe = Recipe.objects.get(pk=recipe_id)
        self.assertEqual(recipe.ingredients[0], {"name": "chicken", "quantity": "1", "unit": "lb", "note": ""})
        self.assertEqual((recipe.tags, recipe.from_bot, recipe.author_label), (["quick", "chicken"], True, "Recipe Bot"))
        updated = self.call("POST", "meals/recipes", {"title": "chicken curry", "notes": "Add more ginger."})
        self.assertEqual((updated.status_code, updated.json()), (200, {"id": recipe_id, "created": False}))
        recipe.refresh_from_db()
        self.assertEqual((recipe.notes, recipe.servings, len(recipe.steps)), ("Add more ginger.", 4, 2))  # kept
        self.assertEqual(Recipe.objects.count(), 1)
        listed = self.call("GET", "meals/recipes?q=curry").json()["recipes"]
        self.assertEqual([r["id"] for r in listed], [recipe_id])
        for body in ({"title": ""}, {"title": "x" * 201}, {"title": "x", "source_url": "javascript:alert(1)"},
                     {"title": "x", "servings": 0}, {"title": "x", "steps": "one"},
                     {"title": "x", "ingredients": [{"quantity": "1"}]}, {"title": "x", "prep_minutes": "ten"}):
            with self.subTest(body=str(body)[:40]):
                self.assertEqual(self.call("POST", "meals/recipes", body).status_code, 400)

    def test_ideas(self):
        created = self.call("POST", "meals/ideas", {"title": "Ramen night", "link": "https://example.com/ramen"})
        self.assertEqual(created.status_code, 201)
        self.assertEqual(self.call("GET", "meals/ideas").json()["ideas"][0]["added_by"], "Recipe Bot")
        self.assertEqual(self.call("POST", "meals/ideas", {"title": "x", "link": "ftp://nope"}).status_code, 400)

    def test_bot_rows_are_labeled_recipe_bot_on_the_site(self):
        self.call("POST", "meals/recipes", {"title": "Bot stew", "ingredients": ["2 carrots"]})
        recipe_id = Recipe.objects.get().pk
        self.call("POST", "meals/plan", {"date": self.today.isoformat(), "slot": "dinner", "recipe_id": recipe_id})
        self.call("POST", "meals/groceries", {"name": "dish soap"})
        self.call("POST", "meals/ideas", {"title": "Bot idea"})
        for url in (reverse("albright_meals:week"), reverse("albright_meals:groceries"), reverse("albright_meals:ideas"),
                    reverse("albright_meals:recipes"), reverse("albright_meals:recipe", args=[recipe_id])):
            with self.subTest(url=url):
                self.assertContains(self.client.get(url), '<span class="meals-by">Recipe Bot</span>')
        self.client.post(reverse("albright_meals:groceries"), {"line": "milk"})
        self.assertEqual(GroceryItem.objects.get(name="milk").author_label, "Nate")

    def test_scopes_in_the_token_forms_and_docs(self):
        page = self.client.get(reverse("albright_control:bot_api")).content.decode()
        self.assertIn('value="meals:read"', page)
        self.assertIn('value="meals:write"', page)
        self.assertIn("Read the Meals plan, grocery list, meal ideas and recipes.", page)
        docs = self.call("GET", "docs").content.decode()
        for path in ("meals/plan", "DELETE meals/plan/<id>", "PATCH meals/groceries/<id>", "POST meals/recipes",
                     "meals:read", "meals:write"):
            self.assertIn(path, docs)


# ---------------------------------------------------------------------------
# Home tile and the digest line
# ---------------------------------------------------------------------------

class TileAndDigestTests(Base):
    def test_home_tile(self):
        self.assertIn('data-tile="meals"', self.client.get(reverse("labs:hub")).content.decode())
        empty = self.client.get(reverse("labs:tile", args=["meals"])).json()
        self.assertEqual((empty["ok"], empty["value"]), (True, "Nothing planned"))
        MealPlanEntry.objects.create(date=self.today, slot="dinner", title="Tacos")
        GroceryItem.objects.create(name="tortillas")
        GroceryItem.objects.create(name="limes")
        GroceryItem.objects.create(name="old", checked=True, checked_at=timezone.now())
        data = self.client.get(reverse("labs:tile", args=["meals"])).json()
        self.assertEqual((data["value"], data["note"], data["size"]), ("Tacos", "tonight · 2 on the grocery list", "text"))

    def test_digest_line(self):
        self.assertEqual(digest_lines(), ["", "Tonight: nothing planned"])
        MealPlanEntry.objects.create(date=self.today, slot="dinner", title="Fish & chips")
        self.assertEqual(digest_lines(), ["", "Tonight: Fish &amp; chips"])          # HTML-escaped for Telegram
        from albright_reselling_app.scanner.alerts import build_digest_message
        self.assertIn("Tonight: Fish &amp; chips", build_digest_message())
        settings_row = MealsSettings.load()
        settings_row.digest_tonight = False
        settings_row.save()
        self.assertEqual(digest_lines(), [])
        self.assertNotIn("Tonight:", build_digest_message())
