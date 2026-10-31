"""The Opportunities app's navigation (display name only - package, app label
and URL namespace stay albright_crypto_app): the header launcher's "Opps" tile
in every app (linking straight to the Inbox, never "/"), the app's own
nav (Inbox, Pipeline, Earnings, Settings - no type or status tabs), the
/opportunities/ prefix with the old /crypto/ and /albright_crypto_app/ URLs
and the old Crypto / Income pages redirecting, the remembered-app cookie,
page titles, and the app's verbose name."""
import re
from unittest import mock

from django.apps import apps
from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from albright_trading_proj.middleware import LAST_APP_COOKIE
from albright_labs.testing import launcher_links, with_apps

HOME_OUTLOOK = "albright_trading_app.views.get_market_outlook"


@mock.patch(HOME_OUTLOOK, return_value=None)
class OpportunitiesNavTests(TestCase):
    def setUp(self):
        self.client.force_login(with_apps(User.objects.create_user(username="tester", password="pw-not-real-12345")))

    def switch_buttons(self, url):
        return launcher_links(self.client.get(url).content.decode())

    def nav_links(self, url):
        content = self.client.get(url).content.decode()
        links = content.split('class="navbar__links"')[1].split("</div>")[0]
        return re.findall(r'<a href="([^"]*)"[^>]*>([^<]+)</a>', links)

    def test_opportunities_button_in_every_app_links_directly_to_the_inbox(self, _outlook):
        home = reverse("albright_crypto_app:inbox")
        self.assertEqual(home, "/opportunities/")
        for page in (reverse("trading_home"), reverse("albright_reselling_app:dashboard")):
            with self.subTest(page=page):
                buttons = self.switch_buttons(page)
                self.assertEqual(buttons.get("Opps"), home)
                self.assertNotIn("Crypto", buttons)

    def test_opportunities_app_shows_all_three_with_itself_highlighted(self, _outlook):
        content = self.client.get(reverse("albright_crypto_app:inbox")).content.decode()
        buttons = self.switch_buttons(reverse("albright_crypto_app:inbox"))
        self.assertEqual(buttons, {"Home": reverse("labs:hub"), "Trade": reverse("trading_home"),
                                   "Resell": reverse("albright_reselling_app:dashboard"),
                                   "Opps": reverse("albright_crypto_app:inbox"), "Meals": reverse("albright_meals:week"),
                                   "Notes": reverse("albright_notes:list")})
        self.assertIn('class="app-launcher__tile is-current" role="menuitem" tabindex="-1" data-app-key="opps" '
                      'data-app-target="opportunities" title="Albright Opportunities" aria-current="page"', content)

    def test_nav_is_inbox_pipeline_earnings_settings(self, _outlook):
        for name in ("inbox", "pipeline", "earnings", "settings"):
            with self.subTest(page=name):
                links = [link for link in self.nav_links(reverse(f"albright_crypto_app:{name}"))
                         if link[1] not in ("My account", "Logout")]
                self.assertEqual(links, [
                    (reverse("albright_crypto_app:inbox"), "Inbox"),
                    (reverse("albright_crypto_app:pipeline"), "Pipeline"),
                    (reverse("albright_crypto_app:earnings"), "Earnings"),
                    (reverse("albright_crypto_app:settings"), "Settings"),
                ])

    def test_no_type_or_status_tabs_and_add_is_a_button_on_inbox_and_pipeline(self, _outlook):
        for name in ("inbox", "pipeline"):
            content = self.client.get(reverse(f"albright_crypto_app:{name}")).content.decode()
            self.assertNotIn('id="type-tabs"', content)
            self.assertNotIn('id="crypto-tabs"', content)
            self.assertIn(f'<a href="{reverse("albright_crypto_app:add")}" class="btn btn-primary btn-sm">Add opportunity</a>',
                          content)

    def test_no_switch_button_points_at_root(self, _outlook):
        for page in (reverse("trading_home"), reverse("albright_reselling_app:dashboard"),
                     reverse("albright_crypto_app:inbox")):
            self.assertNotIn("/", self.switch_buttons(page).values())

    def last_used_app(self):
        from albright_labs.models import UserPreference
        UserPreference.objects.update_or_create(user=User.objects.get(username="tester"), defaults={"landing": "last_app"})

    def test_visiting_sets_cookie_and_root_returns_there(self, _outlook):
        self.last_used_app()   # "/" follows the cookie only with the "Last used app" landing setting
        response = self.client.get(reverse("albright_crypto_app:pipeline"))
        self.assertEqual(response.cookies[LAST_APP_COOKIE].value, "opportunities")
        self.assertRedirects(self.client.get("/"), reverse("albright_crypto_app:inbox"))

    def test_old_crypto_cookie_is_treated_the_same(self, _outlook):
        self.last_used_app()
        self.client.cookies[LAST_APP_COOKIE] = "crypto"
        self.assertRedirects(self.client.get("/"), reverse("albright_crypto_app:inbox"))

    def test_brand_titles_and_theme(self, _outlook):
        content = self.client.get(reverse("albright_crypto_app:pipeline")).content.decode()
        self.assertIn(f'<a href="{reverse("albright_crypto_app:inbox")}" class="navbar__brand">', content)
        self.assertIn('<body data-app="opportunities">', content)
        self.assertIn("<title>Pipeline · Albright Opportunities</title>", content)
        for name, title in (("inbox", "Inbox"), ("hidden", "Hidden"), ("earnings", "Earnings"),
                            ("settings", "Settings"), ("filters", "Filters"), ("profile", "Profile")):
            self.assertIn(f"<title>{title} · Albright Opportunities</title>",
                          self.client.get(reverse(f"albright_crypto_app:{name}")).content.decode())

    def test_settings_has_profile_filters_sources_and_email_tabs(self, _outlook):
        content = self.client.get(reverse("albright_crypto_app:filters")).content.decode()
        tabs = content.split('id="settings-tabs"')[1].split("</nav>")[0]
        self.assertEqual(re.findall(r'href="([^"]+)"', tabs), [
            reverse("albright_crypto_app:profile"), reverse("albright_crypto_app:filters"),
            reverse("albright_crypto_app:settings"), reverse("albright_crypto_app:gmail")])

    def test_verbose_name(self, _outlook):
        self.assertEqual(apps.get_app_config("albright_crypto_app").verbose_name, "Opportunities")

    def test_trading_and_reseller_buttons_still_work(self, _outlook):
        trading = self.client.get(reverse("trading_home"))
        self.assertEqual(trading.cookies[LAST_APP_COOKIE].value, "trading")
        reseller = self.client.get(reverse("albright_reselling_app:dashboard"))
        self.assertEqual(reseller.cookies[LAST_APP_COOKIE].value, "reseller")


class OldPageRedirectTests(TestCase):
    def setUp(self):
        self.client.force_login(with_apps(User.objects.create_user(username="tester", password="pw-not-real-12345")))

    def test_old_crypto_and_income_pages_go_to_the_inbox(self):
        inbox, hidden = reverse("albright_crypto_app:inbox"), reverse("albright_crypto_app:hidden")
        cases = [
            ("/opportunities/crypto/", {}, inbox + "?type=crypto"),
            ("/opportunities/income/", {}, inbox),
            ("/opportunities/crypto/", {"tab": "ranked"}, inbox + "?type=crypto"),
            ("/opportunities/crypto/", {"tab": "blocked"}, hidden + "?type=crypto"),
            ("/opportunities/income/", {"tab": "excluded"}, hidden),
            ("/opportunities/income/", {"tab": "doing"}, reverse("albright_crypto_app:pipeline")),
            ("/opportunities/crypto/", {"tab": "submitted"}, reverse("albright_crypto_app:pipeline")),
        ]
        for url, params, target in cases:
            with self.subTest(url=url, params=params):
                self.assertRedirects(self.client.get(url, params), target, fetch_redirect_response=False)


class LegacyRedirectTests(TestCase):
    def test_old_prefixes_redirect_keeping_path_and_query(self):
        for old in ("/crypto/", "/albright_crypto_app/"):
            with self.subTest(old=old):
                response = self.client.get(old)
                self.assertEqual(response.status_code, 301)
                self.assertEqual(response["Location"], reverse("albright_crypto_app:inbox"))

                response = self.client.get(old + "opportunity/12/", {"tab": "doing"})
                self.assertEqual(response.status_code, 301)
                self.assertEqual(response["Location"],
                                 reverse("albright_crypto_app:opportunity", args=[12]) + "?tab=doing")

                response = self.client.get(old + "earnings/?export=csv")
                self.assertEqual(response["Location"], reverse("albright_crypto_app:earnings") + "?export=csv")

    def test_old_prefix_old_type_page_ends_in_the_inbox(self):
        self.client.force_login(with_apps(User.objects.create_user(username="tester2", password="pw-not-real-12345")))
        response = self.client.get("/crypto/income/", follow=True)
        self.assertEqual(response.redirect_chain[-1][0], reverse("albright_crypto_app:inbox"))
