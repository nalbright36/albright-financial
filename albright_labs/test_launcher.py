"""The header's app launcher (templates/index.html, albright_labs.launcher): it
replaces the old row of app buttons and "More" menu in every app; shows Home
first, then the apps the user can open and shows, in their order, Admin last
and only for superusers; highlights the current app; is hidden for someone
with one app; its live lines come from one cached JSON endpoint where one app
failing just leaves that tile's line empty. Plus the Meals orange accent."""
from unittest import mock

from django.conf import settings
from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse

from . import apps_registry, launcher, tiles
from .models import UserPreference
from .testing import current_tiles, launcher_links, launcher_tiles

PASSWORD = "pw-not-real-12345"
OUTLOOK = "albright_trading_app.views.get_market_outlook"
CSS = settings.BASE_DIR / "albright_trading_app" / "static" / "style.css"


class Base(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        outlook = mock.patch(OUTLOOK, return_value=None)
        outlook.start()
        self.addCleanup(outlook.stop)
        self.user = User.objects.create_user(username="pat", password=PASSWORD)
        self.client.force_login(self.user)

    def set_pref(self, **fields):
        pref, _ = UserPreference.objects.get_or_create(user=self.user)
        for name, value in fields.items():
            setattr(pref, name, value)
        pref.save()

    def page(self, url):
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def labels(self, url=None):
        return [label for _target, label in launcher_tiles(self.page(url or reverse("labs:hub")))]


class LauncherTilesTests(Base):
    def test_allowed_and_visible_apps_in_the_users_order_home_first(self):
        self.set_pref(allowed_apps=["trade", "resell", "opps", "meals"], visible_apps=["meals", "trade", "opps"],
                      app_order=["meals", "opps", "trade", "resell"])
        self.assertEqual(self.labels(), ["Home", "Meals", "Opps", "Trade"])        # Resell hidden, not allowed: none
        links = launcher_links(self.page(reverse("labs:hub")))
        self.assertEqual(links, {"Home": reverse("labs:hub"), "Meals": reverse("albright_meals:week"),
                                 "Opps": reverse("albright_crypto_app:inbox"), "Trade": reverse("trading_home")})

    def test_not_allowed_apps_never_show_even_if_marked_visible(self):
        self.set_pref(allowed_apps=["trade", "meals"], visible_apps=["trade", "meals", "opps", "admin"],
                      app_order=["opps", "admin", "meals", "trade"])
        self.assertEqual(self.labels(), ["Home", "Meals", "Trade"])

    def test_admin_last_and_only_for_superusers(self):
        self.assertNotIn("Admin", self.labels())
        self.user.is_superuser = True
        self.user.save()
        self.set_pref(app_order=["admin", "meals", "trade", "resell", "opps"],
                      visible_apps=["admin", "meals", "trade", "resell", "opps"])
        self.assertEqual(self.labels(), ["Home", "Meals", "Trade", "Resell", "Opps", "Admin"])

    def test_current_app_is_highlighted(self):
        self.user.is_superuser = True
        self.user.save()
        for url, target in ((reverse("labs:hub"), "labs"), (reverse("trading_home"), "trading"),
                            (reverse("albright_reselling_app:dashboard"), "reselling"),
                            (reverse("albright_crypto_app:inbox"), "opportunities"),
                            (reverse("albright_meals:week"), "meals"),
                            (reverse("albright_control:overview"), "admin")):
            with self.subTest(url=url):
                content = self.page(url)
                self.assertEqual(current_tiles(content), [target])
                self.assertEqual(content.count('aria-current="page"', content.index('id="app-launcher-panel"'),
                                               content.index('class="app-launcher__customize"')), 1)

    def test_hidden_for_someone_with_one_app(self):
        self.set_pref(allowed_apps=["meals"], visible_apps=["meals"], app_order=["meals"])
        week = self.page(reverse("albright_meals:week"))
        self.assertNotIn('id="app-launcher"', week)
        self.assertNotIn("Switch app", week)
        self.assertIn('id="theme-toggle"', week)                       # the rest of the header is still there
        self.assertIn('class="navbar__brand">Albright Meals</a>', week)
        self.assertIn('id="nav-account-link"', week)
        # Two apps, one of them hidden: still something to switch to.
        self.set_pref(allowed_apps=["meals", "trade"], visible_apps=["meals"], app_order=["meals", "trade"])
        self.assertEqual(self.labels(reverse("albright_meals:week")), ["Home", "Meals"])

    def test_visitors_get_no_launcher(self):
        self.client.logout()
        login = self.client.get(reverse("user_login")).content.decode()
        self.assertNotIn('id="app-launcher"', login)
        self.assertIn('id="theme-toggle"', login)

    def test_button_and_panel_markup(self):
        content = self.page(reverse("trading_home"))
        nav = content[content.index('<nav class="navbar">'):content.index("</nav>")]
        tools = nav[nav.index('class="navbar__tools"'):]
        # Between the theme toggle and the account menu, at the right end of the header.
        self.assertLess(tools.index('id="theme-toggle"'), tools.index('id="app-launcher-button"'))
        self.assertLess(tools.index('id="app-launcher-button"'), tools.index('navbar__account-trigger'))
        self.assertIn('id="app-launcher-button" aria-label="Switch app" aria-haspopup="true" aria-expanded="false" '
                      'aria-controls="app-launcher-panel"', nav)
        self.assertIn('id="app-launcher-panel" role="menu" aria-label="Apps"', nav)
        self.assertIn(f'data-lines-url="{reverse("labs:launcher_lines")}"', nav)
        self.assertIn(f'<a href="{reverse("labs:account")}#nav-form" class="app-launcher__customize" role="menuitem">'
                      'Customize</a>', nav)
        self.assertIn('<span class="app-launcher__line" data-line-for="home">Overview</span>', nav)
        self.assertIn('<span class="app-launcher__line" data-line-for="trade"></span>', nav)   # loads after opening
        self.assertIn('class="app-launcher__icon" aria-hidden="true"><svg', nav)

    def test_old_app_buttons_and_more_menu_are_gone(self):
        self.user.is_superuser = True
        self.user.save()
        for url in (reverse("labs:hub"), reverse("trading_home"), reverse("albright_meals:week"),
                    reverse("albright_control:overview")):
            with self.subTest(url=url):
                content = self.page(url)
                for old in ("app-switch-btn", "app-switch-more", "navbar__switch-row", ">More <"):
                    self.assertNotIn(old, content)
        css = CSS.read_text(encoding="utf-8")
        for old in (".app-switch-btn", ".app-switch-more", ".navbar__switch-row"):
            self.assertNotIn(old, css)


class LauncherLinesTests(Base):
    def fake_lines(self, **overrides):
        values = {"trade": lambda user: "+$12.50 today", "resell": lambda user: "3 to list",
                  "opps": lambda user: "4 new in Inbox", "meals": lambda user: "Tacos",
                  "admin": lambda user: "All good"}
        values.update(overrides)
        return mock.patch.dict(launcher.LINES, values)

    def get(self):
        response = self.client.get(reverse("labs:launcher_lines"))
        self.assertEqual(response.status_code, 200)
        return response.json()["lines"]

    def test_returns_each_apps_line(self):
        self.set_pref(allowed_apps=["trade", "resell", "opps", "meals"])
        with self.fake_lines():
            self.assertEqual(self.get(), {"home": "Overview", "trade": "+$12.50 today", "resell": "3 to list",
                                          "opps": "4 new in Inbox", "meals": "Tacos"})

    def test_one_app_failing_leaves_only_its_line_empty(self):
        def broken(user):
            raise RuntimeError("Alpaca is down")

        def unavailable(user):
            raise tiles.TileUnavailable("Connect your Alpaca account")

        with self.fake_lines(trade=broken), self.assertLogs("albright_labs.launcher", "ERROR"):
            lines = self.get()
        self.assertIsNone(lines["trade"])
        self.assertEqual((lines["meals"], lines["opps"]), ("Tacos", "4 new in Inbox"))
        cache.clear()
        with self.fake_lines(resell=unavailable):
            lines = self.get()
        self.assertIsNone(lines["resell"])
        self.assertEqual(lines["trade"], "+$12.50 today")

    def test_cached_for_a_minute_per_user(self):
        self.assertEqual(launcher.CACHE_SECONDS, 60)
        calls = []
        with self.fake_lines(meals=lambda user: calls.append(user.pk) or "Soup"):
            self.get()
            self.get()
            self.assertEqual(calls, [self.user.pk])
            other = User.objects.create_user(username="sam", password=PASSWORD)
            self.client.force_login(other)
            self.get()
            self.assertEqual(calls, [self.user.pk, other.pk])
        response = self.client.get(reverse("labs:launcher_lines"))
        self.assertIn("no-cache", response["Cache-Control"])                     # never cached by the browser

    def test_only_the_apps_in_the_users_launcher(self):
        self.set_pref(allowed_apps=["trade", "meals"], visible_apps=["meals"], app_order=["meals", "trade"])
        with self.fake_lines():
            self.assertEqual(self.get(), {"home": "Overview", "meals": "Tacos"})
        self.user.is_superuser = True
        self.user.save()
        cache.clear()
        self.set_pref(visible_apps=["meals", "admin"])
        with self.fake_lines():
            self.assertEqual(self.get()["admin"], "All good")

    def test_sign_in_required(self):
        self.client.logout()
        response = self.client.get(reverse("labs:launcher_lines"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("next=" + reverse("labs:launcher_lines"), response["Location"])

    def test_real_line_functions(self):
        from albright_meals.models import MealPlanEntry
        from django.utils import timezone
        self.assertEqual(launcher.meals(self.user), "No dinner planned")
        MealPlanEntry.objects.create(date=timezone.localdate(), slot="dinner",
                                     title="Slow cooker chicken tortilla soup with lime")
        self.assertEqual(launcher.meals(self.user), "Slow cooker chicken tortill…")
        with mock.patch("albright_control.jobs.health_rows", return_value=[{"status": "ok"}]):
            self.assertEqual(launcher.admin(self.user), "All good")
        with mock.patch("albright_control.jobs.health_rows",
                        return_value=[{"status": "failing"}, {"status": "late"}, {"status": "ok"}]):
            self.assertEqual(launcher.admin(self.user), "2 problems")
        with mock.patch("albright_reselling_app.selling.board_columns",
                        return_value={"to_list": [1, 2], "listed": [{"step_due": True}, {}]}):
            self.assertEqual(launcher.resell(self.user), "2 to list · 1 markdown due")
        with mock.patch("albright_reselling_app.selling.board_columns", return_value={"to_list": [], "listed": []}):
            self.assertEqual(launcher.resell(self.user), "Nothing to list")
        with mock.patch.object(tiles, "trade", return_value={"value": "-$3.10"}):
            self.assertEqual(launcher.trade(self.user), "-$3.10 today")
        self.assertRegex(launcher.opps(self.user), r"^\d+ new in Inbox$")


class MealsAccentTests(TestCase):
    def setUp(self):
        self.css = CSS.read_text(encoding="utf-8")

    def block(self, selector):
        return self.css.split(selector + " {", 1)[1].split("}", 1)[0]

    def test_meals_is_orange(self):
        self.assertEqual(apps_registry.BY_KEY["meals"].color, "#F97316")
        dark = self.block('\nbody[data-app="meals"]')
        self.assertIn("--accent-rgb: 249, 115, 22;", dark)
        self.assertIn("--accent: #FB923C;", dark)
        self.assertIn("--accent-solid: #C2410C;", dark)
        self.assertIn("--accent: #C2410C;", self.block('[data-theme="light"] body[data-app="meals"]'))
        self.assertIn("--app-meals: #FB923C;", self.css)       # dark
        self.assertIn("--app-meals: #F97316;", self.css)       # light
        for pink in ("#EC4899", "#F472B6", "#BE185D", "#9D174D", "236, 72, 153"):
            self.assertNotIn(pink, self.css)
        self.assertIn('.app-launcher__tile[data-app-target="meals"] { --app-dot: var(--app-meals); }', self.css)
        self.assertIn('.labs-tile[data-app-accent="meals"] { --labs-tile-color: var(--app-meals); }', self.css)

    def test_warnings_stay_clearly_amber(self):
        """Amber leans yellow (hue ~40-50°) so a warning badge never reads as Meals orange (~25°)."""
        self.assertIn("--amber: #FACC15;", self.css)     # dark
        self.assertIn("--amber: #865E00;", self.css)     # light
        self.assertNotIn("--amber: #A64B08;", self.css)
