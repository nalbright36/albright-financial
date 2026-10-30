"""Per-app branding: the context processor's app for URLs in each app
(trading by default - login, admin, anything outside the three apps), the
header's brand name and tab title, <body data-app>, and the app launcher
buttons (all three, each with its own dot, the current one highlighted)."""
import re
from pathlib import Path
from unittest import mock

from django.contrib.auth.models import User
from django.test import RequestFactory, TestCase
from django.urls import resolve, reverse

from albright_trading_app.context_processors import current_app
from albright_labs.testing import current_tiles, launcher_panel, launcher_tiles, with_apps

HOME_OUTLOOK = "albright_trading_app.views.get_market_outlook"
STYLE = Path(__file__).resolve().parent / "static" / "style.css"


def processor_for(url):
    request = RequestFactory().get(url)
    request.resolver_match = resolve(url)
    return current_app(request)


class ContextProcessorTests(TestCase):
    def test_app_for_urls_in_each_app(self):
        cases = {
            reverse("trading_home"): "trading",
            reverse("trading_account"): "trading",
            reverse("grok_bot"): "trading",
            reverse("strategies"): "trading",
            reverse("albright_reselling_app:dashboard"): "reselling",
            reverse("albright_reselling_app:watchlist"): "reselling",
            reverse("albright_crypto_app:inbox"): "opportunities",
            reverse("albright_crypto_app:pipeline"): "opportunities",
            reverse("albright_crypto_app:profile"): "opportunities",
            reverse("user_login"): "trading",          # outside the three apps -> trading
            reverse("password_reset"): "trading",
        }
        for url, app in cases.items():
            with self.subTest(url=url):
                self.assertEqual(processor_for(url)["current_app"], app)

    def test_brand_and_flags(self):
        self.assertEqual(processor_for(reverse("trading_home"))["app_brand"], "Albright Trading")
        reseller = processor_for(reverse("albright_reselling_app:dashboard"))
        self.assertEqual((reseller["app_brand"], reseller["in_reselling_app"], reseller["in_opportunities_app"]),
                         ("Albright Reselling", True, False))
        opp = processor_for(reverse("albright_crypto_app:inbox"))
        self.assertEqual((opp["app_brand"], opp["in_reselling_app"], opp["in_opportunities_app"]),
                         ("Albright Opportunities", False, True))

    def test_no_resolver_match_is_trading(self):
        request = RequestFactory().get("/nowhere/")
        request.resolver_match = None
        self.assertEqual(current_app(request)["current_app"], "trading")


@mock.patch(HOME_OUTLOOK, return_value=None)
class HeaderTests(TestCase):
    PAGES = {
        "trading": ("trading_home", "Albright Trading"),
        "reselling": ("albright_reselling_app:dashboard", "Albright Reselling"),
        "opportunities": ("albright_crypto_app:inbox", "Albright Opportunities"),
    }

    def setUp(self):
        self.client.force_login(with_apps(User.objects.create_user(username="brand", password="pw-not-real-12345")))

    def page(self, name):
        return self.client.get(reverse(name)).content.decode()

    def test_brand_name_title_and_data_app_per_app(self, _outlook):
        for app, (url_name, brand) in self.PAGES.items():
            with self.subTest(app=app):
                content = self.page(url_name)
                self.assertIn(f'<body data-app="{app}">', content)
                self.assertIn(f'class="navbar__brand">{brand}</a>', content)
                self.assertRegex(content, rf"<title>([^<]* · )?{brand}</title>")
                self.assertNotIn("Albright Financial", content)
                self.assertNotIn("{#", content)   # no template comment leaking into the page

    def test_page_title_prefixes_keep_the_app_brand(self, _outlook):
        self.assertIn("<title>Grok Bot · Albright Trading</title>", self.page("grok_bot"))
        self.assertIn("<title>Pipeline · Albright Opportunities</title>", self.page("albright_crypto_app:pipeline"))

    def test_login_page_defaults_to_trading(self, _outlook):
        self.client.logout()
        content = self.page("user_login")
        self.assertIn('<body data-app="trading">', content)
        self.assertIn('class="navbar__brand">Albright Trading</a>', content)

    def test_switch_buttons_all_three_current_highlighted(self, _outlook):
        for app, (url_name, _brand) in self.PAGES.items():
            with self.subTest(app=app):
                content = self.page(url_name)
                tiles = launcher_tiles(content)
                self.assertEqual([label for _target, label in tiles],
                                 ["Home", "Trade", "Resell", "Opps", "Meals"])   # short labels; no Admin for a regular user
                self.assertEqual([target for target, _label in tiles], ["labs", "trading", "reselling", "opportunities", "meals"])
                self.assertEqual(current_tiles(content), [app])
                self.assertRegex(launcher_panel(content), rf'data-app-target="{app}" title="[^"]+" aria-current="page"')


class StylesheetTests(TestCase):
    """The accent tokens exist for each app in both themes, and nothing
    that should follow the accent is still a hardcoded green."""

    def setUp(self):
        self.css = STYLE.read_text(encoding="utf-8")

    def test_each_app_and_theme_has_its_accent(self):
        for selector, accent in (('body[data-app="trading"] {', "#34D399"), ('body[data-app="reselling"] {', "#60A5FA"),
                                 ('body[data-app="opportunities"] {', "#A78BFA"),
                                 ('[data-theme="light"] body[data-app="trading"] {', "#047857"),
                                 ('[data-theme="light"] body[data-app="reselling"] {', "#1D4ED8"),
                                 ('[data-theme="light"] body[data-app="opportunities"] {', "#6D28D9")):
            with self.subTest(selector=selector):
                block = self.css.split(f"\n{selector}", 1)[1].split("}", 1)[0]
                self.assertIn(f"--accent: {accent};", block)
        for rgb in ("16, 185, 129", "59, 130, 246", "139, 92, 246"):
            self.assertIn(f"--accent-rgb: {rgb};", self.css)
        for token in ("--accent-soft:", "--accent-glow:", "--accent-contrast: #FFFFFF;"):
            self.assertIn(token, self.css)

    def test_glows_dot_and_focus_follow_the_accent(self):
        self.assertIn("--page-glow-1: rgba(var(--accent-rgb), var(--page-glow-1-alpha));", self.css)
        brand_dot = self.css.split(".navbar__brand::before {\n  background: rgb(", 1)
        self.assertEqual(len(brand_dot), 2)
        self.assertNotRegex(self.css, r"border-color: #10B981|background: #10B981|rgba\(16, 185, 129, 0\.65\)")
        self.assertNotIn("theme-reselling", self.css)
        self.assertNotIn("theme-opportunities", self.css)

    def test_semantic_colors_unchanged(self):
        self.assertIn("--gain: var(--emerald);", self.css)
        self.assertIn("--loss: var(--red);", self.css)
        self.assertIn("--warn: var(--amber);", self.css)
        self.assertIn(".badge.gain { background: var(--gain-bg); color: var(--gain); }", self.css)
