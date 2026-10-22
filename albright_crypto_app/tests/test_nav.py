"""The rename to "Opportunities" (display only - package, app label and
URL namespace stay albright_crypto_app): the header's "Opportunities"
app-switch button in every app (linking straight to the app, never "/"),
the /opportunities/ prefix with the old /crypto/ and /albright_crypto_app/
URLs redirecting there, the remembered-app cookie recording
"opportunities" (an old "crypto" cookie treated the same), page titles,
and the app's verbose name."""
import re
from unittest import mock

from django.apps import apps
from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from albright_trading_proj.middleware import LAST_APP_COOKIE

HOME_OUTLOOK = "albright_trading_app.views.get_market_outlook"


@mock.patch(HOME_OUTLOOK, return_value=None)
class OpportunitiesNavTests(TestCase):
    def setUp(self):
        self.client.force_login(User.objects.create_user(username="tester", password="pw-not-real-12345"))

    def switch_buttons(self, url):
        content = self.client.get(url).content.decode()
        row = content.split('class="navbar__switch-row"')[1].split("</div>")[0]
        return dict((label, href) for href, label in
                    re.findall(r'<a href="([^"]*)" class="app-switch-btn[^"]*"[^>]*>([^<]+)</a>', row))

    def test_opportunities_button_in_every_app_links_directly_to_the_app(self, _outlook):
        home = reverse("albright_crypto_app:dashboard")
        self.assertEqual(home, "/opportunities/")
        for page in (reverse("trading_home"), reverse("albright_reselling_app:dashboard")):
            with self.subTest(page=page):
                buttons = self.switch_buttons(page)
                self.assertEqual(buttons.get("Opps"), home)
                self.assertNotIn("Crypto", buttons)

    def test_opportunities_app_shows_all_three_with_itself_highlighted(self, _outlook):
        content = self.client.get(reverse("albright_crypto_app:dashboard")).content.decode()
        buttons = self.switch_buttons(reverse("albright_crypto_app:dashboard"))
        self.assertEqual(buttons, {"Trade": reverse("trading_home"),
                                   "Resell": reverse("albright_reselling_app:dashboard"),
                                   "Opps": reverse("albright_crypto_app:dashboard")})
        self.assertIn('class="app-switch-btn is-current" data-app-target="opportunities" title="Albright Opportunities" '
                      'aria-current="page"', content)

    def test_no_switch_button_points_at_root(self, _outlook):
        for page in (reverse("trading_home"), reverse("albright_reselling_app:dashboard"),
                     reverse("albright_crypto_app:dashboard")):
            self.assertNotIn("/", self.switch_buttons(page).values())

    def test_visiting_sets_cookie_and_root_returns_there(self, _outlook):
        response = self.client.get(reverse("albright_crypto_app:income"))
        self.assertEqual(response.cookies[LAST_APP_COOKIE].value, "opportunities")
        self.assertRedirects(self.client.get("/"), reverse("albright_crypto_app:dashboard"))

    def test_old_crypto_cookie_is_treated_the_same(self, _outlook):
        self.client.cookies[LAST_APP_COOKIE] = "crypto"
        self.assertRedirects(self.client.get("/"), reverse("albright_crypto_app:dashboard"))

    def test_nav_brand_titles_and_theme(self, _outlook):
        content = self.client.get(reverse("albright_crypto_app:crypto")).content.decode()
        self.assertIn(f'<a href="{reverse("albright_crypto_app:dashboard")}" class="navbar__brand">', content)
        for name in ("dashboard", "crypto", "income", "add", "earnings", "settings"):
            self.assertIn(f'href="{reverse(f"albright_crypto_app:{name}")}"', content)
        self.assertIn('<body data-app="opportunities">', content)
        self.assertIn("<title>Crypto · Albright Opportunities</title>", content)
        self.assertIn("<title>Albright Opportunities</title>",
                      self.client.get(reverse("albright_crypto_app:dashboard")).content.decode())
        self.assertIn("<title>Income · Albright Opportunities</title>",
                      self.client.get(reverse("albright_crypto_app:income")).content.decode())

    def test_verbose_name(self, _outlook):
        self.assertEqual(apps.get_app_config("albright_crypto_app").verbose_name, "Opportunities")

    def test_trading_and_reseller_buttons_still_work(self, _outlook):
        trading = self.client.get(reverse("trading_home"))
        self.assertEqual(trading.cookies[LAST_APP_COOKIE].value, "trading")
        reseller = self.client.get(reverse("albright_reselling_app:dashboard"))
        self.assertEqual(reseller.cookies[LAST_APP_COOKIE].value, "reseller")


class LegacyRedirectTests(TestCase):
    def test_old_prefixes_redirect_keeping_path_and_query(self):
        for old in ("/crypto/", "/albright_crypto_app/"):
            with self.subTest(old=old):
                response = self.client.get(old)
                self.assertEqual(response.status_code, 301)
                self.assertEqual(response["Location"], reverse("albright_crypto_app:dashboard"))

                response = self.client.get(old + "opportunity/12/", {"tab": "doing"})
                self.assertEqual(response.status_code, 301)
                self.assertEqual(response["Location"],
                                 reverse("albright_crypto_app:opportunity", args=[12]) + "?tab=doing")

                response = self.client.get(old + "earnings/?export=csv")
                self.assertEqual(response["Location"], reverse("albright_crypto_app:earnings") + "?export=csv")
