"""Home as an app picker: a greeting and the allowed + shown apps' tiles in the user's order (Favorites first,
Admin last); Edit mode saves the order, pins and visibility; "Open after sign-in" (the picker, the last app, an
app or one of its pages) after signing in and on "/", with "next" still winning; "/?picker=1" always the picker
and every Home link using it; single-app users skipping the picker; visitors getting the public landing; and the
Today panel off by default."""
from datetime import datetime
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from albright_trading_proj.middleware import LAST_APP_COOKIE

from . import views
from .models import UserPreference
from .testing import favorite_tiles, home_tiles, launcher_links, with_apps
from .tests import OUTLOOK

PASSWORD = "pw-not-real-12345"
PICKER = "/?picker=1"
ALL_APPS = ["trade", "resell", "opps", "meals", "notes", "planner"]


class Base(TestCase):
    def setUp(self):
        outlook = mock.patch(OUTLOOK, return_value=None)
        outlook.start()
        self.addCleanup(outlook.stop)
        self.user = with_apps(User.objects.create_user(username="nate", first_name="Nathan", password=PASSWORD))
        self.client.force_login(self.user)

    def pref(self, **fields):
        pref = UserPreference.for_user(self.user)
        for name, value in fields.items():
            setattr(pref, name, value)
        pref.save()
        return pref

    def page(self, url=PICKER):
        return self.client.get(url).content.decode()

    def sign_in(self, **extra):
        self.client.logout()
        return self.client.post(reverse("user_login"), {"username": "nate", "password": PASSWORD, **extra})


class PickerTests(Base):
    def test_greeting_and_tiles_only(self):
        response = self.client.get("/")
        self.assertTemplateUsed(response, "albright_labs/hub.html")
        page = response.content.decode()
        self.assertIn('id="labs-greeting">Good ', page)
        self.assertIn(", Nathan</h1>", page)
        self.assertEqual(home_tiles(page), ALL_APPS)
        for gone in ('id="planner-today"', 'id="nav-form"', 'id="landing-form"', "data-tile-url"):
            self.assertNotIn(gone, page)

    def test_greeting_by_time_of_day(self):
        tz = timezone.get_current_timezone()
        for hour, part in ((7, "morning"), (13, "afternoon"), (20, "evening"), (2, "evening")):
            moment = timezone.make_aware(datetime(2026, 11, 2, hour), tz)
            self.assertEqual(views.greeting(self.user, moment), f"Good {part}, Nathan")
        self.assertEqual(views.greeting(User(username="sam"), timezone.make_aware(datetime(2026, 11, 2, 9), tz)),
                         "Good morning, sam")

    def test_each_tile_has_icon_name_and_a_line_filled_in_later(self):
        page = self.page()
        tile = page.split('data-key="meals"', 1)[1].split("</li>", 1)[0]
        self.assertIn(f'href="{reverse("albright_meals:week")}"', tile)
        self.assertIn('class="labs-pick__icon"', tile)
        self.assertIn('<span class="labs-pick__name">Meals</span>', tile)
        self.assertIn('<span class="labs-pick__line" data-line-for="meals"></span>', tile)   # empty until hub.js
        self.assertIn(f'data-lines-url="{reverse("labs:launcher_lines")}"', page)
        self.assertIn('data-app-accent="meals"', page)

    def test_only_allowed_and_shown_apps_in_their_order(self):
        self.pref(allowed_apps=["trade", "opps", "meals", "notes"], visible_apps=["meals", "trade", "notes"],
                  app_order=["notes", "meals", "opps", "trade"])
        page = self.page()
        self.assertEqual(home_tiles(page), ["notes", "meals", "trade"])
        self.assertEqual(home_tiles(page, include_hidden=True), ["notes", "meals", "opps", "trade"])  # for Edit mode
        self.assertNotIn('data-key="resell"', page)

    def test_favorites_first(self):
        self.pref(pinned_apps=["planner", "resell"])
        page = self.page()
        self.assertEqual(favorite_tiles(page), ["resell", "planner"])            # in their order
        self.assertEqual(home_tiles(page), ["resell", "planner", "trade", "opps", "meals", "notes"])
        self.assertIn(">Favorites</h2>", page)
        self.assertNotIn('id="picker-favorites" hidden', page)

    def test_no_favorites_row_until_something_is_pinned(self):
        self.assertIn('id="picker-favorites" hidden', self.page())

    def test_admin_last_for_superusers_only(self):
        self.assertNotIn("admin", home_tiles(self.page()))
        self.user.is_superuser = True
        self.user.save()
        self.pref(app_order=["admin", "opps", "trade", "resell", "meals", "notes", "planner"])
        self.assertEqual(home_tiles(self.page())[-1], "admin")

    def test_visitors_get_the_public_landing_never_tiles(self):
        self.client.logout()
        for url in ("/", PICKER):
            response = self.client.get(url)
            self.assertTemplateUsed(response, "albright_labs/landing.html")
            self.assertEqual(home_tiles(response.content.decode(), include_hidden=True), [])
        self.assertEqual(self.client.get(reverse("labs:hub")).status_code, 302)       # sign in first


class EditModeTests(Base):
    def test_edit_mode_renders_the_controls(self):
        page = self.page()
        self.assertIn(f'href="{PICKER}&amp;edit=1"', page)
        self.assertNotIn("labs-picker is-editing", page)
        page = self.page(PICKER + "&edit=1")
        self.assertIn("labs-picker is-editing", page)
        self.assertIn('name="visible" value="trade" checked', page)
        self.assertIn('name="move" value="up:trade"', page)
        self.assertIn('id="open-after"', page)
        self.assertIn('id="show-today"', page)

    def test_done_saves_order_and_visibility(self):
        response = self.client.post(reverse("labs:save_picker"), {
            "order": ["planner", "meals", "trade", "resell", "opps", "notes"],
            "visible": ["planner", "meals", "trade"], "open_after": "home"})
        self.assertRedirects(response, PICKER, fetch_redirect_response=False)
        pref = UserPreference.objects.get(user=self.user)
        self.assertEqual(pref.app_order[:6], ["planner", "meals", "trade", "resell", "opps", "notes"])
        self.assertEqual(home_tiles(self.page()), ["planner", "meals", "trade"])
        self.assertEqual([label for label in launcher_links(self.page())], ["Home", "Plan", "Meals", "Trade"])

    def test_arrows_work_without_js(self):
        response = self.client.post(reverse("labs:save_picker"), {"order": ALL_APPS, "visible": ALL_APPS,
                                                                  "open_after": "home", "move": "up:opps"})
        self.assertRedirects(response, PICKER + "&edit=1", fetch_redirect_response=False)
        self.assertEqual(UserPreference.objects.get(user=self.user).app_order[:3], ["trade", "opps", "resell"])

    def test_at_least_one_app_stays_shown(self):
        response = self.client.post(reverse("labs:save_picker"), {"order": ALL_APPS, "open_after": "home"}, follow=True)
        self.assertContains(response, "Keep at least one app in your navigation.")
        self.assertEqual(home_tiles(self.page()), ALL_APPS)

    def test_pins_save_at_once(self):
        response = self.client.post(reverse("labs:toggle_pin"), {"key": "meals"}, HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(response.json(), {"ok": True, "key": "meals", "pinned": True})
        self.assertEqual(favorite_tiles(self.page()), ["meals"])
        response = self.client.post(reverse("labs:toggle_pin"), {"key": "meals"})        # without JS
        self.assertRedirects(response, PICKER, fetch_redirect_response=False)
        self.assertEqual(UserPreference.objects.get(user=self.user).pinned_apps, [])
        self.client.post(reverse("labs:toggle_pin"), {"key": "notes", "editing": "1"})
        self.assertEqual(favorite_tiles(self.page()), ["notes"])
        self.assertEqual(self.client.post(reverse("labs:toggle_pin"), {"key": "admin"}).status_code, 404)   # not theirs

    def test_edit_mode_sets_open_after_and_the_today_panel(self):
        self.client.post(reverse("labs:save_picker"), {"order": ALL_APPS, "visible": ALL_APPS,
                                                       "open_after": "app:meals", "today_panel": "1", "show_today": "1"})
        pref = UserPreference.objects.get(user=self.user)
        self.assertEqual((pref.landing, pref.landing_page, pref.show_today_panel), ("app", "meals", True))
        # Hiding the default app: the picker opens after sign-in instead.
        response = self.client.post(reverse("labs:save_picker"), {"order": ALL_APPS, "visible": ["trade", "resell"],
                                                                  "open_after": "app:meals", "today_panel": "1"},
                                    follow=True)
        self.assertContains(response, "that app is hidden now")
        pref.refresh_from_db()
        self.assertEqual((pref.landing, pref.landing_page, pref.show_today_panel), ("home", "", False))


class OpenAfterSignInTests(Base):
    def test_default_is_the_picker(self):
        self.assertRedirects(self.sign_in(), reverse("labs:root"), fetch_redirect_response=False)
        self.assertTemplateUsed(self.client.get("/"), "albright_labs/hub.html")

    def test_default_app_after_sign_in_and_on_root(self):
        self.pref(landing="app", landing_page="meals")
        response = self.client.get(self.sign_in()["Location"])
        self.assertRedirects(response, reverse("albright_meals:week"), fetch_redirect_response=False)
        self.assertRedirects(self.client.get("/"), reverse("albright_meals:week"), fetch_redirect_response=False)

    def test_a_page_within_the_app(self):
        self.pref(landing="page", landing_page="resell:watchlist")
        self.assertRedirects(self.client.get("/"), reverse("albright_reselling_app:watchlist"),
                             fetch_redirect_response=False)

    def test_last_used_app_is_still_an_option(self):
        self.pref(landing="last_app")
        self.client.cookies[LAST_APP_COOKIE] = "planner"
        self.assertRedirects(self.client.get("/"), reverse("albright_planner:home"), fetch_redirect_response=False)

    def test_next_still_wins(self):
        self.pref(landing="app", landing_page="meals")
        response = self.sign_in(next=reverse("albright_notes:list"))
        self.assertRedirects(response, reverse("albright_notes:list"), fetch_redirect_response=False)

    def test_picker_param_always_shows_the_picker(self):
        for fields in ({"landing": "app", "landing_page": "meals"}, {"landing": "page", "landing_page": "opps:inbox"},
                       {"landing": "last_app"}):
            with self.subTest(**fields):
                self.pref(**fields)
                self.client.cookies[LAST_APP_COOKIE] = "trading"
                response = self.client.get(PICKER)
                self.assertEqual(response.status_code, 200)
                self.assertTemplateUsed(response, "albright_labs/hub.html")
                self.assertEqual(self.client.get(reverse("labs:hub")).status_code, 200)

    def test_my_account_has_the_same_choice(self):
        page = self.page(reverse("labs:account"))
        self.assertIn('<option value="home" selected>App picker (Home)</option>', page)
        self.assertIn('<option value="last_app">Last used app</option>', page)
        self.assertIn('<option value="app:meals">Meals (its main page)</option>', page)
        self.assertIn('<option value="page:meals:recipes">Meals › Recipes</option>', page)
        self.client.post(reverse("labs:save_landing"), {"open_after": "page:planner:week", "today_panel": "1",
                                                        "show_today": "1"})
        pref = UserPreference.objects.get(user=self.user)
        self.assertEqual((pref.landing, pref.landing_page, pref.show_today_panel), ("page", "planner:week", True))
        self.assertIn('<option value="page:planner:week" selected>', self.page(reverse("labs:account")))

    def test_only_shown_apps_are_offered(self):
        self.pref(visible_apps=["trade", "notes"])
        page = self.page(reverse("labs:account"))
        self.assertNotIn('value="app:meals"', page)
        self.client.post(reverse("labs:save_landing"), {"open_after": "app:meals"})
        self.assertEqual(UserPreference.objects.get(user=self.user).landing, "home")      # refused


class HomeLinksTests(Base):
    def test_home_links_open_the_picker(self):
        self.pref(landing="app", landing_page="meals")
        for url in (reverse("albright_meals:week"), reverse("trading_home"), reverse("labs:account")):
            with self.subTest(url=url):
                self.assertEqual(launcher_links(self.page(url))["Home"], PICKER)
        picker = self.page()
        self.assertIn(f'<a href="{PICKER}" class="navbar__brand">', picker)
        self.assertIn(f'<a href="{PICKER}">Home</a>', picker)


class SingleAppTests(Base):
    def test_single_app_users_skip_the_picker(self):
        wife = with_apps(User.objects.create_user(username="sam", password=PASSWORD), ["meals"])
        self.client.force_login(wife)
        for url in ("/", PICKER, reverse("labs:hub")):
            with self.subTest(url=url):
                self.assertRedirects(self.client.get(url), reverse("albright_meals:week"), fetch_redirect_response=False)
        UserPreference.objects.filter(user=wife).update(landing="page", landing_page="meals:groceries")
        self.assertRedirects(self.client.get("/"), reverse("albright_meals:groceries"), fetch_redirect_response=False)
        self.assertEqual(self.client.get(reverse("labs:account")).status_code, 200)        # settings still open

    def test_no_apps_still_shows_access_pending(self):
        self.pref(allowed_apps=[])
        self.assertContains(self.client.get(PICKER), 'id="access-pending"')


class TodayPanelTests(Base):
    def test_hidden_by_default_and_shown_when_enabled(self):
        self.assertFalse(UserPreference.for_user(self.user).show_today_panel)
        self.assertNotIn('id="planner-today"', self.page())
        self.pref(show_today_panel=True)
        self.assertIn('id="planner-today"', self.page())
