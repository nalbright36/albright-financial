"""The branded error pages (albright_trading_proj.errors): each handler's status and template, the
standalone 500 (renders with the database down, shows a reference that matches the log), JSON
under /api/, the CSRF page, Sign in with next on a signed-out 404, and the superuser-only preview.

Runs against a test URLconf (this module): the site's URLs plus a few views that fail on purpose,
and the same handler400/403/404/500 the root urls.py registers."""
import json
import os
import re
from unittest import mock

from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied, SuspiciousOperation
from django.db import OperationalError
from django.http import HttpResponse, HttpResponseForbidden
from django.test import Client, TestCase, override_settings
from django.urls import path, reverse
from django.views.decorators.http import require_POST

from albright_labs import app_access, ratelimit
from albright_trading_proj import errors
from albright_trading_proj.urls import handler400, handler403, handler404, handler500  # noqa: F401
from albright_trading_proj.urls import urlpatterns as site_urlpatterns

SECRET_DETAIL = "db-password-hunter2-in-the-exception"


def boom(request):
    raise ValueError(SECRET_DETAIL)


def boom_db(request):
    raise OperationalError(f"could not connect to server: {SECRET_DETAIL}")


def bad(request):
    raise SuspiciousOperation(SECRET_DETAIL)


def denied(request):
    raise PermissionDenied(SECRET_DETAIL)


@require_POST
def post_only(request):
    return HttpResponse("posted")


def limited(request):
    return ratelimit.too_many(request, "login")


def own_forbidden(request):
    return HttpResponseForbidden("a view's own message")


urlpatterns = [
    path("test-errors/boom/", boom),
    path("test-errors/boom-db/", boom_db),
    path("test-errors/bad/", bad),
    path("test-errors/denied/", denied),
    path("test-errors/post-only/", post_only),
    path("test-errors/limited/", limited),
    path("test-errors/own-forbidden/", own_forbidden),
    path("api/test-errors/boom/", boom),
] + site_urlpatterns

TRACE_MARKERS = ("Traceback", 'File "', "ValueError", "OperationalError", "SuspiciousOperation", SECRET_DETAIL)


@override_settings(DEBUG=False, ROOT_URLCONF="albright_trading_proj.test_errors", ERROR_TELEGRAM_ALERTS=False)
class ErrorPageTests(TestCase):
    def setUp(self):
        self.client = Client(raise_request_exception=False)

    def assertClean(self, response):
        body = response.content.decode()
        for marker in TRACE_MARKERS:
            self.assertNotIn(marker, body)
        from django.conf import settings
        self.assertNotIn(settings.SECRET_KEY, body)

    def assertBranded(self, response, status, template):
        self.assertEqual(response.status_code, status)
        self.assertTemplateUsed(response, template)
        self.assertTemplateNotUsed(response, "index.html")
        body = response.content.decode()
        self.assertIn('id="error-page"', body)
        self.assertIn(f'<p class="err__code">{status}</p>', body)
        self.assertIn("logo-mark-white.png", body)
        self.assertIn("logo-mark-dark.png", body)
        self.assertIn('alt="Albright"', body)
        self.assertIn("Go back", body)
        self.assertIn("prefers-color-scheme", body)
        self.assertClean(response)

    def test_400(self):
        response = self.client.get("/test-errors/bad/")
        self.assertBranded(response, 400, "errors/400.html")
        self.assertContains(response, "That request didn&#x27;t work", status_code=400)

    def test_403(self):
        response = self.client.get("/test-errors/denied/")
        self.assertBranded(response, 403, "errors/403.html")
        self.assertContains(response, "You don&#x27;t have access to this", status_code=403)
        self.assertContains(response, 'id="error-sign-in"', status_code=403)      # signed out

    def test_404_signed_out_offers_sign_in_with_next(self):
        response = self.client.get("/no-such-page/?tab=2")
        self.assertBranded(response, 404, "errors/404.html")
        self.assertContains(response, "We couldn&#x27;t find that page", status_code=404)
        login = reverse("user_login")
        self.assertContains(response, f'href="{login}?next=%2Fno-such-page%2F%3Ftab%3D2" id="error-sign-in"',
                            status_code=404)
        self.assertContains(response, f'href="{reverse("labs:root")}" id="error-home"', status_code=404)
        self.assertNotContains(response, 'id="error-app"', status_code=404)

    def test_404_signed_in_goes_home_to_the_picker_and_offers_the_app(self):
        boss = User.objects.create_superuser("boss", "boss@example.com", "pw")
        self.client.force_login(boss)
        response = self.client.get("/meals/no-such-page/")
        self.assertBranded(response, 404, "errors/404.html")
        self.assertContains(response, f'href="{reverse("labs:hub")}" id="error-home"', status_code=404)
        self.assertContains(response, f'href="{reverse("albright_meals:week")}" id="error-app">Go to Meals</a>',
                            status_code=404)
        self.assertNotContains(response, 'id="error-sign-in"', status_code=404)

    def test_404_for_an_app_without_access_reads_like_any_other_404(self):
        user = User.objects.create_user("abby", password="pw")
        app_access.new_user_preference(user, ["notes"])
        self.client.force_login(user)
        hidden = self.client.get(reverse("albright_meals:week"))
        missing = self.client.get("/no-such-page/")
        self.assertBranded(hidden, 404, "errors/404.html")
        self.assertNotContains(hidden, "Meals", status_code=404)                   # no hint it exists
        self.assertNotContains(hidden, 'id="error-app"', status_code=404)
        strip = lambda r: re.sub(r"<title>.*?</title>", "", r.content.decode(), flags=re.S)  # noqa: E731
        self.assertEqual(strip(hidden), strip(missing))

    def test_405_brands_require_post_and_keeps_allow(self):
        response = self.client.get("/test-errors/post-only/")
        self.assertBranded(response, 405, "errors/405.html")
        self.assertEqual(response["Allow"], "POST")

    def test_429_rate_limit_page(self):
        response = self.client.get("/test-errors/limited/")
        self.assertBranded(response, 429, "errors/429.html")
        minutes = ratelimit.LIMITS["login"][1]
        self.assertContains(response, f"Please wait {minutes} minutes and try again.", status_code=429)
        self.assertEqual(response["Retry-After"], str(minutes * 60))

    def test_429_default_text(self):
        response = errors.too_many_requests(mock.Mock(path="/x/", path_info="/x/", META={}, user=None,
                                                      resolver_match=None, get_full_path=lambda: "/x/"))
        self.assertIn("Too many requests", response.content.decode())
        self.assertIn("Please wait a minute and try again.", response.content.decode())

    def test_a_views_own_4xx_content_is_left_alone(self):
        response = self.client.get("/test-errors/own-forbidden/")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.content, b"a view's own message")

    def test_500_shows_a_reference_that_matches_the_log(self):
        with self.assertLogs("albright.errors", level="ERROR") as logs:
            response = self.client.get("/test-errors/boom/")
        self.assertBranded(response, 500, "errors/500.html")
        self.assertContains(response, "Something went wrong on our end", status_code=500)
        reference = re.search(r'id="error-reference">Reference: <code>([0-9A-F]{8})</code>',
                              response.content.decode()).group(1)
        self.assertIn(reference, logs.output[0])
        self.assertIn(SECRET_DETAIL, "\n".join(logs.output))                     # full error server-side only

    def test_500_renders_with_the_database_unavailable(self):
        def down(*args, **kwargs):
            raise OperationalError("database is down")
        with mock.patch("django.db.backends.base.base.BaseDatabaseWrapper.cursor", side_effect=down), \
                mock.patch("django.db.backends.base.base.BaseDatabaseWrapper.connect", side_effect=down), \
                self.assertLogs("albright.errors", level="ERROR"):
            response = self.client.get("/test-errors/boom-db/")
        self.assertBranded(response, 500, "errors/500.html")
        self.assertRegex(response.content.decode(), r'Reference: <code>[0-9A-F]{8}</code>')

    def test_500_falls_back_if_its_template_cannot_render(self):
        with mock.patch("albright_trading_proj.errors.loader.get_template", side_effect=RuntimeError("no")):
            response = errors.render_500("ABCD1234")
        self.assertEqual(response.status_code, 500)
        self.assertIn("ABCD1234", response.content.decode())

    def test_api_paths_get_json(self):
        missing = self.client.get("/api/nothing-here/")
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(missing["Content-Type"], "application/json")
        self.assertEqual(json.loads(missing.content), {"error": "not found", "status": 404})
        with self.assertLogs("albright.errors", level="ERROR"):
            broken = self.client.get("/api/test-errors/boom/")
        self.assertEqual(broken.status_code, 500)
        data = json.loads(broken.content)
        self.assertEqual(data["error"], "server error")
        self.assertEqual(data["status"], 500)
        self.assertRegex(data["reference"], r"^[0-9A-F]{8}$")
        self.assertClean(broken)

    def test_json_accept_gets_json(self):
        response = self.client.get("/no-such-page/", HTTP_ACCEPT="application/json")
        self.assertEqual(json.loads(response.content), {"error": "not found", "status": 404})
        response = self.client.get("/test-errors/post-only/", HTTP_ACCEPT="application/json")
        self.assertEqual(json.loads(response.content), {"error": "method not allowed", "status": 405})

    def test_csrf_failure_page(self):
        client = Client(enforce_csrf_checks=True)
        with self.assertLogs("albright.errors", level="WARNING") as logs:
            response = client.post(reverse("user_login"), {"username": "x", "password": "y"})
        self.assertIn("CSRF", logs.output[0])                                    # the reason goes to the log only
        self.assertBranded(response, 403, "errors/csrf_failure.html")
        self.assertContains(response, "Your session expired", status_code=403)
        self.assertContains(response, "Refresh the page and try again.", status_code=403)
        self.assertNotContains(response, "CSRF", status_code=403)

    def test_preview_is_superuser_only(self):
        url = reverse("albright_control:error_preview", args=["404"])
        self.assertEqual(self.client.get(url).status_code, 404)                  # signed out
        user = User.objects.create_user("abby", password="pw")
        self.client.force_login(user)
        self.assertEqual(self.client.get(url).status_code, 404)                  # not a superuser
        self.client.force_login(User.objects.create_superuser("boss", "boss@example.com", "pw"))
        for code, template in (("400", "400"), ("403", "403"), ("404", "404"), ("405", "405"),
                               ("429", "429"), ("500", "500"), ("csrf", "csrf_failure")):
            response = self.client.get(reverse("albright_control:error_preview", args=[code]))
            self.assertEqual(response.status_code, 200, code)
            self.assertTemplateUsed(response, f"errors/{template}.html")
        self.assertContains(self.client.get(reverse("albright_control:error_preview", args=["500"])), "PREVIEW")
        visitor = self.client.get(url + "?as=visitor")
        self.assertContains(visitor, 'id="error-sign-in"')
        self.assertEqual(self.client.get(reverse("albright_control:error_preview", args=["418"])).status_code, 404)


@override_settings(DEBUG=False, ROOT_URLCONF="albright_trading_proj.test_errors", ERROR_TELEGRAM_ALERTS=True)
class ServerErrorAlertTests(TestCase):
    def setUp(self):
        self.client = Client(raise_request_exception=False)
        errors._last_alert.update(at=0.0, suppressed=0)
        self.addCleanup(errors._last_alert.update, at=0.0, suppressed=0)

    def test_alert_carries_the_reference_and_is_throttled(self):
        env = {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "1"}
        with mock.patch.object(errors, "_in_tests", return_value=False), mock.patch.dict(os.environ, env), \
                mock.patch("requests.post") as post, self.assertLogs("albright.errors", level="ERROR"):
            first = self.client.get("/test-errors/boom/")
            self.client.get("/test-errors/boom/")
        self.assertEqual(post.call_count, 1)
        text = post.call_args.kwargs["json"]["text"]
        reference = re.search(r"<code>([0-9A-F]{8})</code>", first.content.decode()).group(1)
        self.assertIn(reference, text)
        self.assertNotIn(SECRET_DETAIL, text)

    def test_no_alert_during_tests(self):
        with mock.patch("requests.post") as post, self.assertLogs("albright.errors", level="ERROR"):
            self.client.get("/test-errors/boom/")
        post.assert_not_called()
