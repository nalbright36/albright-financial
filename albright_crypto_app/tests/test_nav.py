"""The header's "Crypto" app-switch button in every app (linking straight to
the crypto dashboard, never "/"), the remembered-app cookie recording
"crypto", and "/" sending a crypto user back to the crypto dashboard."""
import re
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from albright_trading_proj.middleware import LAST_APP_COOKIE

HOME_OUTLOOK = "albright_trading_app.views.get_market_outlook"


@mock.patch(HOME_OUTLOOK, return_value=None)
class CryptoNavTests(TestCase):
    def setUp(self):
        self.client.force_login(User.objects.create_user(username="tester", password="pw-not-real-12345"))

    def switch_buttons(self, url):
        content = self.client.get(url).content.decode()
        row = content.split('class="navbar__switch-row"')[1].split("</div>")[0]
        return dict((label, href) for href, label in
                    re.findall(r'<a href="([^"]*)" class="app-switch-btn">([^<]+)</a>', row))

    def test_crypto_button_in_every_app_links_directly_to_the_crypto_dashboard(self, _outlook):
        crypto = reverse("albright_crypto_app:dashboard")
        for page in (reverse("trading_home"), reverse("albright_reselling_app:dashboard")):
            with self.subTest(page=page):
                self.assertEqual(self.switch_buttons(page).get("Crypto"), crypto)

    def test_crypto_app_shows_the_other_two(self, _outlook):
        buttons = self.switch_buttons(reverse("albright_crypto_app:dashboard"))
        self.assertEqual(buttons, {"Trading": reverse("trading_home"),
                                   "Reselling": reverse("albright_reselling_app:dashboard")})

    def test_no_switch_button_points_at_root(self, _outlook):
        for page in (reverse("trading_home"), reverse("albright_reselling_app:dashboard"),
                     reverse("albright_crypto_app:dashboard")):
            self.assertNotIn("/", self.switch_buttons(page).values())

    def test_visiting_crypto_sets_cookie_and_root_returns_there(self, _outlook):
        response = self.client.get(reverse("albright_crypto_app:dashboard"))
        self.assertEqual(response.cookies[LAST_APP_COOKIE].value, "crypto")
        self.assertRedirects(self.client.get("/"), reverse("albright_crypto_app:dashboard"))

    def test_crypto_nav_and_brand_stay_in_the_crypto_app(self, _outlook):
        content = self.client.get(reverse("albright_crypto_app:dashboard")).content.decode()
        self.assertIn(f'<a href="{reverse("albright_crypto_app:dashboard")}" class="navbar__brand">', content)
        for name in ("add", "earnings", "settings"):
            self.assertIn(f'href="{reverse(f"albright_crypto_app:{name}")}"', content)
        self.assertIn('class="theme-crypto"', content)

    def test_trading_and_reseller_buttons_still_work(self, _outlook):
        trading = self.client.get(reverse("trading_home"))
        self.assertEqual(trading.cookies[LAST_APP_COOKIE].value, "trading")
        reseller = self.client.get(reverse("albright_reselling_app:dashboard"))
        self.assertEqual(reseller.cookies[LAST_APP_COOKIE].value, "reseller")
