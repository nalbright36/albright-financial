"""Resume each app where you were - only with pages that belong to it (albright_labs.resume_nav, js/app_nav.js):
the rules each page carries, saving/validating per app, shared pages never saved, invalid saved values ignored and
cleared (the old /money/household/ that sent the Money tile to Circles), and Money's main page never redirecting to
Circles for someone with Money access."""
import json
import pathlib
import re
from unittest import mock

from django.contrib.auth.models import User
from django.contrib.staticfiles import finders
from django.core.cache import cache
from django.test import Client, TestCase
from django.urls import reverse

from albright_labs import apps_registry, circles, resume_nav
from albright_labs.models import Space, UserPreference
from albright_labs.testing import with_apps

PASSWORD = "pw-not-real-12345"


def static(path):
    return pathlib.Path(finders.find(path)).read_text(encoding="utf-8")


class Base(TestCase):
    def setUp(self):
        cache.clear()
        outlook = mock.patch("albright_trading_app.views.get_market_outlook", return_value=None)
        outlook.start()
        self.addCleanup(outlook.stop)
        self.me = with_apps(User.objects.create_user(username="nate", password=PASSWORD))
        self.client.force_login(self.me)

    def rules(self, user=None):
        user = user or self.me
        return resume_nav.rules(user, UserPreference.for_user(user))


class RulesTests(Base):
    def test_each_allowed_app_gets_its_main_page_and_prefixes(self):
        rules = self.rules()
        self.assertEqual(set(rules["apps"]), set(apps_registry.grantable_keys()))     # not Admin, not Home
        self.assertEqual(rules["apps"]["money"], {"main": reverse("albright_money:dashboard"), "prefixes": ["/money/"]})
        self.assertEqual(rules["apps"]["planner"]["prefixes"], ["/planner/"])
        self.assertEqual(rules["apps"]["trade"], {"main": reverse("trading_home"), "prefixes": []})   # at the site root
        self.assertIn("/money/", rules["claimed"])
        self.assertIn("/control/", rules["claimed"])                                  # Admin's, though not allowed
        self.assertIn(reverse("albright_money:household"), rules["retired"])
        for shared in ("/home/", "/notifications/", "/user_login/", "/user_logout/"):
            self.assertIn(shared, rules["shared"])

    def test_only_apps_the_user_can_still_open(self):
        meals_only = with_apps(User.objects.create_user(username="pat", password=PASSWORD), ["meals"])
        self.assertEqual(list(self.rules(meals_only)["apps"]), ["meals"])
        admin = User.objects.create_superuser(username="boss", password=PASSWORD, email="")
        self.assertIn("admin", self.rules(admin)["apps"])

    def test_every_page_carries_the_rules_and_visitors_get_none(self):
        page = self.client.get(reverse("albright_planner:day")).content.decode()
        found = re.search(r'<script id="nav-resume-rules" type="application/json">(.*?)</script>', page, re.S)
        self.assertEqual(json.loads(found.group(1)), self.rules())
        self.assertLess(page.index('id="nav-resume-rules"'), page.index("js/app_nav.js"))
        self.assertNotIn('id="nav-resume-rules"', Client().get(reverse("user_login")).content.decode())


class BelongsTests(Base):
    def test_an_apps_own_pages_are_kept(self):
        rules = self.rules()
        for key, url in (("money", "/money/"), ("money", reverse("albright_money:transactions") + "?account=3"),
                         ("planner", reverse("albright_planner:week") + "#today"), ("notes", "/notes/12/"),
                         ("trade", reverse("trading_home")), ("trade", reverse("market_scanner"))):
            with self.subTest(key=key, url=url):
                self.assertTrue(resume_nav.belongs(url, key, rules))

    def test_every_apps_main_and_where_i_land_pages_belong_to_it_and_no_other(self):
        rules = self.rules(User.objects.create_superuser(username="boss", password=PASSWORD, email=""))
        for app in apps_registry.APPS:
            for url_name in [app.landing_url_name] + [name for _k, _l, name in app.pages]:
                path = reverse(url_name)
                with self.subTest(app=app.key, path=path):
                    self.assertTrue(resume_nav.belongs(path, app.key, rules))
                    others = [k for k in rules["apps"] if k != app.key and resume_nav.belongs(path, k, rules)]
                    self.assertEqual(others, [])

    def test_shared_pages_are_never_any_apps_last_page(self):
        rules = self.rules()
        shared = [reverse(name) for name in ("labs:account", "labs:circles", "labs:people", "labs:activity",
                                              "labs:notification_settings", "labs:hub", "labs:root", "user_login",
                                              "user_logout", "password_reset", "offline", "labs:notifications_panel")]
        for url in shared:
            for key in rules["apps"]:
                with self.subTest(url=url, key=key):
                    self.assertFalse(resume_nav.belongs(url, key, rules))

    def test_invalid_values_are_refused(self):
        rules = self.rules()
        cases = [
            ("money", reverse("albright_money:household")),        # the old page that now redirects to Circles
            ("money", reverse("labs:circles")),                    # a shared page saved under Money
            ("money", "/planner/week/"),                           # another app's page
            ("trade", "/money/"),                                  # Trading never claims another app's page
            ("money", "https://evil.example/money/"), ("money", "//evil.example/money/"), ("money", "/\\evil"),
            ("money", ""), ("money", None), ("money", 42),
            ("home", "/home/"), ("nope", "/money/"),               # Home and unknown keys
        ]
        for key, url in cases:
            with self.subTest(key=key, url=url):
                self.assertFalse(resume_nav.belongs(url, key, rules))

    def test_an_app_the_user_can_no_longer_open_is_refused(self):
        meals_only = with_apps(User.objects.create_user(username="pat", password=PASSWORD), ["meals"])
        rules = self.rules(meals_only)
        self.assertFalse(resume_nav.belongs("/money/", "money", rules))
        self.assertTrue(resume_nav.belongs(reverse("albright_meals:groceries"), "meals", rules))


class ScriptTests(TestCase):
    """js/app_nav.js applies the same check (valid() mirrors resume_nav.belongs()) everywhere it touches the store."""

    def test_saving_reading_and_cleanup_all_validate(self):
        js = static("js/app_nav.js")
        self.assertIn('JSON.parse(document.getElementById("nav-resume-rules").textContent)', js)
        valid = js[js.index("function valid(key, url) {"):js.index("function loadLast() {")]
        for check in ("!RULES || !sameSite(url)", "RULES.apps && RULES.apps[key]", 'path === "/"',
                      "(RULES.retired || []).indexOf(path) >= 0", "startsWithAny(path, RULES.shared)",
                      "return startsWithAny(path, app.prefixes);", "return !startsWithAny(path, RULES.claimed);"):
            self.assertIn(check, valid)
        remember = js[js.index("function remember() {"):js.index("function forget(key) {")]
        self.assertIn("if (!valid(key, last[key])) delete last[key];", remember)            # clean up on load
        self.assertIn('if (KEY && KEY !== "home" && valid(KEY, here())) last[KEY] = here();', remember)
        self.assertIn("if (!RULES) return last;", remember)                                 # no rules: no change
        point = js[js.index("function point(link, key) {"):js.index('document.querySelectorAll(".app-launcher__tile')]
        self.assertIn('key !== KEY && key !== "home" && sameSite(url) && valid(key, url)', point)
        self.assertIn("} else if (RULES && !valid(key, url)) {\n                forget(key);", point)

    def test_a_tile_that_lands_in_another_app_drops_the_entry_and_opens_the_main_page(self):
        js = static("js/app_nav.js")
        check = js[js.index("function checkResumed() {"):js.index('document.addEventListener("click", function (event) {\n        var link = event.target.closest && event.target.closest("a[data-resume-key]")')]
        for part in ("resumed.key === KEY) return false;", "referrerPath() !== resumed.from",
                     "RESUMED_FRESH_MS", "forget(resumed.key);", "window.location.replace(main)"):
            self.assertIn(part, check)
        self.assertIn('link.setAttribute("data-resume-key", key);', js)
        self.assertIn("if (checkResumed()) return;", js)


class MoneyMainPageTests(Base):
    """Money's dashboard always shows Money for someone with Money access - it never redirects (to Circles or
    anywhere): no Personal space yet, a bad ?space=, or a remembered space they can no longer see."""

    def assert_money(self, response):
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "albright_money/dashboard.html")

    def test_no_personal_space_yet(self):
        Space.objects.filter(user=self.me).delete()
        self.assert_money(self.client.get(reverse("albright_money:dashboard")))
        self.assertTrue(Space.objects.filter(user=self.me).exists())                # made on the spot

    def test_bad_or_foreign_space_ids_fall_back_to_personal(self):
        other = with_apps(User.objects.create_user(username="kim", password=PASSWORD), ["money"])
        theirs = circles.personal_space(other)
        for value in ("abc", "999999", str(theirs.pk)):
            with self.subTest(space=value):
                response = self.client.get(reverse("albright_money:dashboard"), {"space": value})
                self.assert_money(response)
                self.assertEqual(response.wsgi_request.space, circles.personal_space(self.me))

    def test_a_remembered_space_that_is_gone(self):
        UserPreference.objects.filter(user=self.me).update(current_spaces={"money": 999999})
        self.assert_money(self.client.get(reverse("albright_money:dashboard")))

    def test_money_only_user(self):
        money_only = with_apps(User.objects.create_user(username="pat", password=PASSWORD), ["money"])
        client = Client()
        client.force_login(money_only)
        self.assert_money(client.get(reverse("albright_money:dashboard")))

    def test_the_old_household_page_is_retired_from_resume(self):
        # It still redirects to Circles for an old bookmark - which is why it must never be a saved last page.
        response = self.client.get(reverse("albright_money:household"))
        self.assertRedirects(response, reverse("labs:circles"), fetch_redirect_response=False)
        self.assertFalse(resume_nav.belongs(reverse("albright_money:household"), "money", self.rules()))
