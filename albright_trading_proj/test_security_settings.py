"""Production hardening flags follow DEBUG (see settings.py, near ALLOWED_HOSTS)."""
from django.conf import settings
from django.test import RequestFactory, SimpleTestCase


class SecuritySettingsTests(SimpleTestCase):
    def test_cookies_secure_whenever_debug_is_off(self):
        self.assertEqual(settings.SESSION_COOKIE_SECURE, not settings.DEBUG)
        self.assertEqual(settings.CSRF_COOKIE_SECURE, not settings.DEBUG)

    def test_allowed_hosts_set_once_with_all_live_hosts(self):
        for host in ("www.sportslabalgo.com", "natealbright36.pythonanywhere.com", "127.0.0.1"):
            self.assertIn(host, settings.ALLOWED_HOSTS)

    def test_forwarded_https_counts_as_secure(self):
        request = RequestFactory().get("/", HTTP_X_FORWARDED_PROTO="https")
        self.assertTrue(request.is_secure())
        self.assertFalse(RequestFactory().get("/").is_secure())
