"""Tests for the "last app" cookie: LastAppMiddleware recording which app
a logged-in user last visited, and "/" (home) redirecting a reseller-
cookie user straight to the reseller dashboard. get_market_outlook() is
mocked everywhere "/" is hit directly - it makes a real external call and
what it returns is unrelated to what's being tested here."""
from unittest import mock

from django.contrib.auth.models import User
from django.http import JsonResponse
from django.test import RequestFactory, TestCase
from django.urls import resolve, reverse

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
        response = self.client.get("/")

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

    def test_redirects_to_reseller_dashboard_when_cookie_says_reseller(self):
        self.client.cookies[LAST_APP_COOKIE] = "reseller"

        response = self.client.get("/")

        self.assertRedirects(response, reverse("albright_reselling_app:dashboard"))

    @mock.patch(HOME_OUTLOOK, return_value=None)
    def test_defaults_to_trading_landing_page_without_cookie(self, mock_outlook):
        response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "home.html")

    @mock.patch(HOME_OUTLOOK, return_value=None)
    def test_defaults_to_trading_landing_page_when_cookie_says_trading(self, mock_outlook):
        self.client.cookies[LAST_APP_COOKIE] = "trading"

        response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "home.html")

    @mock.patch(HOME_OUTLOOK, return_value=None)
    def test_anonymous_user_gets_trading_landing_page_regardless_of_cookie(self, mock_outlook):
        self.client.logout()
        self.client.cookies[LAST_APP_COOKIE] = "reseller"

        response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "home.html")


class LoginRedirectTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="tester3", password="pw-not-real-12345")

    @mock.patch(HOME_OUTLOOK, return_value=None)
    def test_login_with_no_next_goes_to_remembered_app(self, mock_outlook):
        self.client.cookies[LAST_APP_COOKIE] = "reseller"

        response = self.client.post(
            reverse("user_login"), {"username": "tester3", "password": "pw-not-real-12345"},
        )

        # "/" itself redirects again (to the reseller dashboard) once
        # logged in with that cookie - fetch_redirect_response=False since
        # assertRedirects' own follow-and-check-200 can't handle a chained
        # redirect.
        self.assertRedirects(response, "/", fetch_redirect_response=False)
        follow = self.client.get("/")
        self.assertRedirects(follow, reverse("albright_reselling_app:dashboard"))

    def test_login_with_next_honors_it(self):
        response = self.client.post(
            f"{reverse('user_login')}?next={reverse('albright_reselling_app:ledger')}",
            {
                "username": "tester3", "password": "pw-not-real-12345",
                "next": reverse("albright_reselling_app:ledger"),
            },
        )

        self.assertRedirects(response, reverse("albright_reselling_app:ledger"))

    def test_login_ignores_off_site_next(self):
        response = self.client.post(
            reverse("user_login"),
            {"username": "tester3", "password": "pw-not-real-12345", "next": "https://evil.example.com/"},
        )

        self.assertRedirects(response, "/", fetch_redirect_response=False)
