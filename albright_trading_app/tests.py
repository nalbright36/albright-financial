"""Tests for the "last app" cookie: LastAppMiddleware recording which app
a logged-in user last visited, which "/" follows only for users whose
landing setting is "Last used app" (albright_labs - "/" is the Labs hub by
default, and the public landing page for visitors). get_market_outlook() is
mocked wherever the trading landing page is rendered - it makes a real
external call and what it returns is unrelated to what's being tested here."""
import re
from unittest import mock

from django.contrib.auth.models import User
from django.http import JsonResponse
from django.test import RequestFactory, TestCase
from django.urls import resolve, reverse

from albright_labs.models import UserPreference
from albright_trading_proj.middleware import LAST_APP_COOKIE, LastAppMiddleware

HOME_OUTLOOK = "albright_trading_app.views.get_market_outlook"


class LastAppCookieTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="tester", password="pw-not-real-12345")
        self.client.login(username="tester", password="pw-not-real-12345")

    def test_visiting_a_reseller_page_sets_cookie_to_reseller(self):
        response = self.client.get(reverse("albright_reselling_app:dashboard"))

        self.assertEqual(response.cookies[LAST_APP_COOKIE].value, "reseller")

    @mock.patch(HOME_OUTLOOK, return_value=None)
    def test_visiting_a_trading_page_sets_cookie_to_trading(self, mock_outlook):
        response = self.client.get(reverse("trading_home"))

        self.assertEqual(response.cookies[LAST_APP_COOKIE].value, "trading")

    def test_other_trading_pages_also_set_cookie_to_trading(self):
        response = self.client.get(reverse("strategies"))

        self.assertEqual(response.cookies[LAST_APP_COOKIE].value, "trading")

    def test_cookie_attributes(self):
        response = self.client.get(reverse("albright_reselling_app:dashboard"))

        cookie = response.cookies[LAST_APP_COOKIE]
        self.assertEqual(cookie["samesite"], "Lax")
        self.assertTrue(cookie["httponly"])
        self.assertEqual(cookie["max-age"], 60 * 60 * 24 * 365)

    def test_static_file_request_does_not_set_cookie(self):
        response = self.client.get("/static/style.css")

        self.assertNotIn(LAST_APP_COOKIE, response.cookies)

    def test_post_does_not_set_cookie(self):
        entry_count_url = reverse("albright_reselling_app:ledger")
        response = self.client.post(entry_count_url, {"add_entry": "1"})

        self.assertNotIn(LAST_APP_COOKIE, response.cookies)

    def test_anonymous_request_does_not_set_cookie(self):
        self.client.logout()

        response = self.client.get(reverse("albright_reselling_app:dashboard"))

        self.assertNotIn(LAST_APP_COOKIE, response.cookies)

    def test_login_page_does_not_set_cookie(self):
        response = self.client.get(reverse("user_login"))

        self.assertNotIn(LAST_APP_COOKIE, response.cookies)

    def test_logout_does_not_set_cookie(self):
        response = self.client.get(reverse("user_logout"))

        self.assertNotIn(LAST_APP_COOKIE, response.cookies)

    def test_admin_does_not_set_cookie(self):
        response = self.client.get("/admin/login/")

        self.assertNotIn(LAST_APP_COOKIE, response.cookies)

    def test_json_response_does_not_set_cookie(self):
        """Exercised directly against the middleware rather than through
        stock_bars_api (a real view that hits Alpaca) - a 200 JSON
        response is what's actually being tested here, which that view
        can't reliably produce without live API credentials."""
        factory = RequestFactory()
        request = factory.get(reverse("albright_reselling_app:dashboard"))
        request.user = self.user
        request.resolver_match = resolve(request.path)

        middleware = LastAppMiddleware(get_response=lambda r: None)
        response = JsonResponse({"ok": True})

        middleware._maybe_record(request, response)

        self.assertNotIn(LAST_APP_COOKIE, response.cookies)

    def test_non_200_response_does_not_set_cookie(self):
        response = self.client.get("/albright_reselling_app/no-such-page/")

        self.assertEqual(response.status_code, 404)
        self.assertNotIn(LAST_APP_COOKIE, response.cookies)


class RootRedirectTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="tester2", password="pw-not-real-12345")
        self.client.login(username="tester2", password="pw-not-real-12345")

    def test_default_landing_is_home_whatever_the_cookie_says(self):
        self.client.cookies[LAST_APP_COOKIE] = "reseller"

        response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "albright_labs/hub.html")

    def test_last_used_app_setting_follows_the_cookie(self):
        UserPreference.objects.create(user=self.user, landing="last_app")
        self.client.cookies[LAST_APP_COOKIE] = "reseller"

        self.assertRedirects(self.client.get("/"), reverse("albright_reselling_app:dashboard"))

    @mock.patch(HOME_OUTLOOK, return_value=None)
    def test_last_used_app_trading_goes_to_the_trading_landing(self, mock_outlook):
        UserPreference.objects.create(user=self.user, landing="last_app")
        self.client.cookies[LAST_APP_COOKIE] = "trading"

        self.assertRedirects(self.client.get("/"), reverse("trading_home"))

    @mock.patch(HOME_OUTLOOK, return_value=None)
    def test_anonymous_user_gets_the_public_landing_regardless_of_cookie(self, mock_outlook):
        self.client.logout()
        self.client.cookies[LAST_APP_COOKIE] = "reseller"

        response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "albright_labs/landing.html")
        mock_outlook.assert_not_called()


class LoginRedirectTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="tester3", password="pw-not-real-12345")

    def test_login_with_no_next_goes_to_the_landing_setting(self):
        UserPreference.objects.create(user=self.user, landing="last_app")
        self.client.cookies[LAST_APP_COOKIE] = "reseller"

        response = self.client.post(
            reverse("user_login"), {"username": "tester3", "password": "pw-not-real-12345"},
        )

        # "/" itself applies the setting (here: the reseller app's Dashboard)
        # - fetch_redirect_response=False since assertRedirects' own
        # follow-and-check-200 can't handle a chained redirect.
        self.assertRedirects(response, "/", fetch_redirect_response=False)
        follow = self.client.get("/")
        self.assertRedirects(follow, reverse("albright_reselling_app:dashboard"))

    def test_login_with_no_setting_lands_on_home(self):
        response = self.client.post(reverse("user_login"), {"username": "tester3", "password": "pw-not-real-12345"})

        self.assertRedirects(response, "/", fetch_redirect_response=False)
        self.assertTemplateUsed(self.client.get("/"), "albright_labs/hub.html")

    def test_login_with_next_honors_it(self):
        response = self.client.post(
            f"{reverse('user_login')}?next={reverse('albright_reselling_app:ledger')}",
            {
                "username": "tester3", "password": "pw-not-real-12345",
                "next": reverse("albright_reselling_app:ledger"),
            },
        )

        # /ledger/ is a legacy URL that itself redirects on to the Reselling
        # tab - check the login honored "next" without following that hop.
        self.assertRedirects(response, reverse("albright_reselling_app:ledger"), fetch_redirect_response=False)

    def test_login_ignores_off_site_next(self):
        response = self.client.post(
            reverse("user_login"),
            {"username": "tester3", "password": "pw-not-real-12345", "next": "https://evil.example.com/"},
        )

        self.assertRedirects(response, "/", fetch_redirect_response=False)


class AppSwitchTests(TestCase):
    """The header launcher's app tiles (and the brand link) go straight to
    each app's own landing page by URL name - never "/", which follows the
    remembered app and so used to bounce "Trading" back to the reseller."""

    def setUp(self):
        self.user = User.objects.create_user(username="switcher", password="pw-not-real-12345")
        self.client.login(username="switcher", password="pw-not-real-12345")

    def nav(self, response):
        content = response.content.decode()
        return content[content.index('<nav class="navbar">'):content.index("</nav>")]

    def href(self, nav_html, css_class, target=None):
        if target:
            return re.search(rf'<a href="([^"]*)" class="{css_class}[^"]*"[^>]*data-app-target="{target}"', nav_html).group(1)
        return re.search(rf'<a href="([^"]*)" class="{css_class}"', nav_html).group(1)

    @mock.patch(HOME_OUTLOOK, return_value=None)
    def test_trading_button_goes_to_trading_landing_and_sets_cookie(self, mock_outlook):
        dashboard = self.client.get(reverse("albright_reselling_app:dashboard"))
        self.assertEqual(dashboard.cookies[LAST_APP_COOKIE].value, "reseller")
        trading_href = self.href(self.nav(dashboard), "app-launcher__tile", "trading")

        self.assertEqual(trading_href, reverse("trading_home"))
        self.assertNotEqual(trading_href, "/")

        landed = self.client.get(trading_href)  # last_app is still "reseller" here - no bounce back

        self.assertEqual(landed.status_code, 200)
        self.assertTemplateUsed(landed, "home.html")
        self.assertEqual(landed.cookies[LAST_APP_COOKIE].value, "trading")

    @mock.patch(HOME_OUTLOOK, return_value=None)
    def test_reselling_button_goes_to_reseller_dashboard_and_sets_cookie(self, mock_outlook):
        trading = self.client.get(reverse("trading_home"))
        reseller_href = self.href(self.nav(trading), "app-launcher__tile", "reselling")

        self.assertEqual(reseller_href, reverse("albright_reselling_app:dashboard"))

        landed = self.client.get(reseller_href)

        self.assertEqual(landed.status_code, 200)
        self.assertTemplateUsed(landed, "dashboard.html")
        self.assertEqual(landed.cookies[LAST_APP_COOKIE].value, "reseller")

    @mock.patch(HOME_OUTLOOK, return_value=None)
    def test_brand_link_stays_within_the_current_app(self, mock_outlook):
        reseller_nav = self.nav(self.client.get(reverse("albright_reselling_app:dashboard")))
        trading_nav = self.nav(self.client.get(reverse("trading_home")))

        self.assertEqual(self.href(reseller_nav, "navbar__brand"), reverse("albright_reselling_app:dashboard"))
        self.assertEqual(self.href(trading_nav, "navbar__brand"), reverse("trading_home"))

    @mock.patch(HOME_OUTLOOK, return_value=None)
    def test_no_nav_link_points_at_bare_root(self, mock_outlook):
        for url in (reverse("albright_reselling_app:dashboard"), reverse("trading_home")):
            with self.subTest(page=url):
                self.assertNotIn('href="/"', self.nav(self.client.get(url)))
        trading_nav = self.nav(self.client.get(reverse("trading_home")))
        self.assertIn(f'<a href="{reverse("trading_home")}">Home</a>', trading_nav)

    @mock.patch(HOME_OUTLOOK, return_value=None)
    def test_root_follows_the_remembered_app_with_last_used_app(self, mock_outlook):
        UserPreference.objects.create(user=self.user, landing="last_app")
        self.client.get(reverse("albright_reselling_app:dashboard"))  # remembers "reseller"
        self.assertRedirects(self.client.get("/"), reverse("albright_reselling_app:dashboard"))

        self.client.get(reverse("trading_home"))  # remembers "trading"
        self.assertRedirects(self.client.get("/"), reverse("trading_home"))

    @mock.patch(HOME_OUTLOOK, return_value=None)
    def test_trading_landing_never_redirects_even_for_a_reseller_user(self, mock_outlook):
        self.client.cookies[LAST_APP_COOKIE] = "reseller"

        response = self.client.get(reverse("trading_home"))

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "home.html")
