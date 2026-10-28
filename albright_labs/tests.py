"""Albright Labs: the public landing at "/" (no private or market data, the
resume button only while the resume page is on, the strict headers); Home
for signed-in users (tiles that fail on their own); the "where I land"
setting after sign-in and on "/" ("next" still wins); the header's Home
button and brand; the last_app cookie; and the resume page's stylesheet
(served, allowed by its CSP, and flagged by `check` when not collected)."""
import os
import tempfile
from types import SimpleNamespace
from unittest import mock

from django.contrib.auth.models import User
from django.contrib.staticfiles import finders
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse

from albright_resume.models import Project, ResumeSettings
from albright_trading_proj.middleware import LAST_APP_COOKIE

from . import tiles
from .checks import static_files_collected
from .models import LandingSettings, UserPreference

OUTLOOK = "albright_trading_app.views.get_market_outlook"
CLIENT = "alpaca.trading.client.TradingClient"


def resume_site(enabled=True, name="Pat Example"):
    site = ResumeSettings.objects.get_or_create(pk=1)[0]
    site.page_enabled, site.full_name = enabled, name
    site.save()
    return site


class PublicLandingTests(TestCase):
    def setUp(self):
        resume_site(enabled=True)
        LandingSettings.objects.update_or_create(pk=1, defaults={"headline": "I build data tools that pay for themselves."})
        Project.objects.all().delete()
        Project.objects.create(title="Shown project", tagline="A published one-liner.", published=True, order=1)
        Project.objects.create(title="Secret draft", tagline="Not published.", published=False, order=2)
        Project.objects.create(title="Placeholder tagline", tagline="[PLACEHOLDER] pitch",
                               description="The real description.", published=True, order=3)

    def get(self):
        with mock.patch(OUTLOOK) as outlook, mock.patch(CLIENT) as alpaca:
            response = self.client.get("/")
        outlook.assert_not_called()       # no market data
        alpaca.assert_not_called()        # no API calls
        return response

    def test_landing_for_visitors(self):
        response = self.get()
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "albright_labs/landing.html")
        page = response.content.decode()
        self.assertIn("Albright Labs", page)
        self.assertIn(f'href="{reverse("user_login")}" id="sign-in">Sign in</a>', page)
        self.assertIn('<h1 class="rs-name" id="labs-name">Pat Example</h1>', page)
        self.assertIn("I build data tools that pay for themselves.", page)
        self.assertIn(f'href="{reverse("albright_resume:page")}" id="resume-button">View my interactive resume</a>', page)
        self.assertIn("Shown project", page)
        self.assertIn("A published one-liner.", page)
        self.assertIn("The real description.", page)     # placeholder tagline skipped, description used
        self.assertNotIn("[PLACEHOLDER", page)
        self.assertNotIn("Secret draft", page)            # unpublished never shown
        self.assertIn("&copy;", page)

    def test_no_private_data_or_app_links(self):
        User.objects.create_user(username="someone-private", password="pw-not-real-12345")
        page = self.get().content.decode()
        for private in ("someone-private", "Logout", "Grok Bot", "Watchlist", "Inbox", "Trading Account",
                        "equity", "app-switch-btn", "navbar__links"):
            self.assertNotIn(private, page)

    def test_strict_security_headers(self):
        response = self.get()
        csp = response["Content-Security-Policy"]
        self.assertIn("script-src 'self'", csp)
        self.assertIn("style-src 'self'", csp)
        self.assertIn("frame-ancestors 'none'", csp)
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")
        self.assertNotIn("<script>", response.content.decode())   # nothing inline for the CSP to block

    def test_resume_button_hides_when_the_resume_page_is_off(self):
        resume_site(enabled=False)
        page = self.get().content.decode()
        self.assertNotIn('id="resume-button"', page)
        self.assertNotIn(f'href="{reverse("albright_resume:page")}"', page)
        self.assertIn("Shown project", page)

    def test_name_falls_back_to_labs_without_settings(self):
        ResumeSettings.objects.filter(pk=1).update(full_name="")
        LandingSettings.objects.all().delete()
        self.assertIn('id="labs-name">Albright Labs</h1>', self.get().content.decode())


class HomeTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="nate", password="pw-not-real-12345")
        self.client.force_login(self.user)

    def test_signed_in_root_shows_home_tiles(self):
        response = self.client.get("/")
        self.assertTemplateUsed(response, "albright_labs/hub.html")
        page = response.content.decode()
        for key in ("trade", "resell", "opps"):
            self.assertIn(f'data-tile="{key}"', page)
            self.assertIn(f'data-tile-url="{reverse("labs:tile", args=[key])}"', page)
        self.assertNotIn('data-tile="admin"', page)
        self.assertIn('<body data-app="labs">', page)
        self.assertIn('class="navbar__brand">Albright Labs</a>', page)
        self.assertEqual(self.client.get(reverse("labs:hub")).status_code, 200)

    def test_admin_tile_for_superusers_only(self):
        self.assertEqual(self.client.get(reverse("labs:tile", args=["admin"])).status_code, 404)
        self.client.force_login(User.objects.create_superuser(username="boss", password="pw-not-real-12345", email=""))
        self.assertIn('data-tile="admin"', self.client.get(reverse("labs:hub")).content.decode())
        data = self.client.get(reverse("labs:tile", args=["admin"])).json()
        self.assertTrue(data["ok"])

    def test_each_tile_number(self):
        self.assertEqual(self.client.get(reverse("labs:tile", args=["trade"])).json(),
                         {"ok": False, "note": "Connect your Alpaca account"})
        resell = self.client.get(reverse("labs:tile", args=["resell"])).json()
        self.assertEqual((resell["ok"], resell["value"]), (True, "0"))
        self.assertIn("to list", resell["note"])
        opps = self.client.get(reverse("labs:tile", args=["opps"])).json()
        self.assertEqual((opps["ok"], opps["value"]), (True, "0"))

    def test_trade_tile_shows_todays_pnl(self):
        from albright_trading_app.models import AlpacaCredentials
        creds = AlpacaCredentials(user=self.user, is_paper=True)
        creds.set_credentials("PKNOTREAL1", "not-a-real-secret")
        creds.save()
        client = mock.MagicMock()
        client.get_account.return_value = SimpleNamespace(equity="105250.50", last_equity="104000.00")
        with mock.patch(CLIENT, return_value=client):
            data = self.client.get(reverse("labs:tile", args=["trade"])).json()
        self.assertEqual((data["value"], data["tone"]), ("+$1,250.50", "gain"))
        self.assertIn("paper", data["note"])
        client.get_account.side_effect = RuntimeError("alpaca down")
        with mock.patch(CLIENT, return_value=client):
            self.assertEqual(self.client.get(reverse("labs:tile", args=["trade"])).json(),
                             {"ok": False, "note": "Couldn't reach Alpaca"})

    def test_a_failing_tile_fails_alone(self):
        broken = mock.patch.object(tiles, "resell", side_effect=ValueError("boom"))
        with broken, self.assertLogs("albright_labs.views", "ERROR"):
            response = self.client.get(reverse("labs:tile", args=["resell"]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"ok": False, "note": "Not available right now"})
        self.assertTrue(self.client.get(reverse("labs:tile", args=["opps"])).json()["ok"])   # the others still work
        with broken:
            self.assertEqual(self.client.get(reverse("labs:hub")).status_code, 200)   # Home itself never calls them
        self.assertEqual(self.client.get(reverse("labs:tile", args=["nope"])).status_code, 404)

    def test_tiles_need_sign_in(self):
        self.client.logout()
        self.assertEqual(self.client.get(reverse("labs:tile", args=["opps"])).status_code, 302)
        self.assertEqual(self.client.get(reverse("labs:hub")).status_code, 302)


class LandingPreferenceTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="nate", password="pw-not-real-12345")

    def prefer(self, landing, page=""):
        UserPreference.objects.update_or_create(user=self.user, defaults={"landing": landing, "landing_page": page})

    def sign_in(self, **extra):
        return self.client.post(reverse("user_login"), {"username": "nate", "password": "pw-not-real-12345", **extra})

    def test_default_is_home(self):
        self.assertRedirects(self.sign_in(), reverse("labs:root"), fetch_redirect_response=False)
        self.assertTemplateUsed(self.client.get("/"), "albright_labs/hub.html")

    def test_specific_pages(self):
        for key, target in (("resell:watchlist", reverse("albright_reselling_app:watchlist")),
                            ("opps:inbox", reverse("albright_crypto_app:inbox")),
                            ("trade:grok_bot", reverse("grok_bot")),
                            ("resell:dashboard", reverse("albright_reselling_app:dashboard"))):
            with self.subTest(page=key):
                self.client.logout()
                self.prefer("page", key)
                self.sign_in()
                self.assertRedirects(self.client.get("/"), target, fetch_redirect_response=False)

    def test_last_used_app(self):
        self.prefer("last_app")
        self.client.force_login(self.user)
        for cookie, target in (("reseller", reverse("albright_reselling_app:dashboard")),
                               ("opportunities", reverse("albright_crypto_app:inbox")),
                               ("crypto", reverse("albright_crypto_app:inbox")),     # the old cookie value
                               ("trading", reverse("trading_home"))):
            with self.subTest(cookie=cookie):
                self.client.cookies[LAST_APP_COOKIE] = cookie
                self.assertRedirects(self.client.get("/"), target, fetch_redirect_response=False)
        del self.client.cookies[LAST_APP_COOKIE]
        self.assertTemplateUsed(self.client.get("/"), "albright_labs/hub.html")      # nothing remembered: Home
        self.client.cookies[LAST_APP_COOKIE] = "admin"
        self.assertTemplateUsed(self.client.get("/"), "albright_labs/hub.html")      # not a superuser: Home

    def test_admin_pages_only_for_superusers(self):
        self.prefer("page", "admin:health")
        self.client.force_login(self.user)
        self.assertTemplateUsed(self.client.get("/"), "albright_labs/hub.html")
        self.user.is_superuser = True
        self.user.save()
        self.assertRedirects(self.client.get("/"), reverse("albright_control:health"), fetch_redirect_response=False)

    def test_next_still_wins(self):
        self.prefer("page", "opps:inbox")
        response = self.sign_in(next=reverse("albright_reselling_app:watchlist"))
        self.assertRedirects(response, reverse("albright_reselling_app:watchlist"), fetch_redirect_response=False)
        self.client.logout()
        response = self.sign_in(next="https://evil.example.com/")          # off-site next ignored
        self.assertRedirects(response, reverse("labs:root"), fetch_redirect_response=False)

    def test_saving_the_setting(self):
        self.client.force_login(self.user)
        self.client.post(reverse("labs:save_landing"), {"landing": "page", "landing_page": "resell:watchlist"})
        pref = UserPreference.objects.get(user=self.user)
        self.assertEqual((pref.landing, pref.landing_page), ("page", "resell:watchlist"))
        self.client.post(reverse("labs:save_landing"), {"landing": "page", "landing_page": "admin:health"})
        self.assertEqual(UserPreference.objects.get(user=self.user).landing_page, "resell:watchlist")  # refused
        self.client.post(reverse("labs:save_landing"), {"landing": "last_app", "landing_page": "opps:inbox"})
        pref.refresh_from_db()
        self.assertEqual((pref.landing, pref.landing_page), ("last_app", ""))
        response = self.client.get(reverse("labs:hub"))
        self.assertEqual(response.context["landing_form"].initial["landing"], "last_app")
        self.assertNotIn("System health", response.content.decode().split('id="landing-form"')[1])  # Admin: superusers only


class HeaderTests(TestCase):
    def setUp(self):
        self.client.force_login(User.objects.create_user(username="nate", password="pw-not-real-12345"))

    def switch_row(self, url):
        with mock.patch(OUTLOOK, return_value=None):
            content = self.client.get(url).content.decode()
        return content.split('class="navbar__switch-row"')[1].split("</div>")[0]

    def test_home_button_comes_first_with_the_labs_dot(self):
        for url in (reverse("labs:hub"), reverse("trading_home"), reverse("albright_reselling_app:dashboard"),
                    reverse("albright_crypto_app:inbox")):
            with self.subTest(url=url):
                row = self.switch_row(url)
                self.assertLess(row.index('data-app-target="labs"'), row.index('data-app-target="trading"'))
                self.assertIn(f'<a href="{reverse("labs:hub")}" class="app-switch-btn', row)
        self.assertIn('class="app-switch-btn is-current" data-app-target="labs"', self.switch_row(reverse("labs:hub")))

    def test_app_brands_unchanged(self):
        with mock.patch(OUTLOOK, return_value=None):
            for url, brand in ((reverse("trading_home"), "Albright Trading"),
                               (reverse("albright_reselling_app:dashboard"), "Albright Reselling"),
                               (reverse("albright_crypto_app:inbox"), "Albright Opportunities")):
                self.assertIn(f'class="navbar__brand">{brand}</a>', self.client.get(url).content.decode())

    def test_hub_and_resume_visits_never_change_the_last_app_cookie(self):
        self.client.get(reverse("albright_crypto_app:inbox"))
        self.client.get(reverse("labs:hub"))
        self.assertEqual(self.client.cookies[LAST_APP_COOKIE].value, "opportunities")


@override_settings(DEBUG=False)
class ResumeStylesheetTests(TestCase):
    def test_resume_page_links_its_stylesheet_and_the_csp_allows_it(self):
        resume_site(enabled=True)
        response = self.client.get(reverse("albright_resume:page"))
        page = response.content.decode()
        self.assertIn('<link rel="stylesheet" href="/static/resume/resume.css">', page)
        self.assertIn('<script src="/static/labs/theme.js"></script>', page)
        csp = dict(part.strip().split(" ", 1) for part in response["Content-Security-Policy"].split(";"))
        self.assertEqual(csp["style-src"], "'self'")          # same-origin /static/ is 'self'
        self.assertEqual(csp["script-src"], "'self'")
        self.assertIn("'self'", csp["font-src"])
        self.assertTrue(finders.find("resume/resume.css"))     # collectstatic picks it up
        self.assertTrue(finders.find("labs/theme.js"))

    def test_check_warns_about_uncollected_static_files(self):
        with tempfile.TemporaryDirectory() as root:
            with override_settings(STATIC_ROOT=root):
                warnings = static_files_collected()
                self.assertEqual(len(warnings), 1)
                self.assertIn("static file(s) aren't in STATIC_ROOT yet", warnings[0].msg)
                self.assertIn("collectstatic", warnings[0].hint)
                call_command("collectstatic", interactive=False, verbosity=0)
                self.assertEqual(static_files_collected(), [])
                os.remove(os.path.join(root, "resume", "resume.css"))
                self.assertIn("resume/resume.css", static_files_collected()[0].msg)


class SecurityHeaderScopeTests(TestCase):
    """The strict CSP is only on the public pages ("/" for visitors, /resume/),
    and those pages have nothing inline for it to block; the signed-in apps
    send no CSP (as before), so their own assets - inline theme script, Google
    Fonts, chart library - keep working."""

    PUBLIC_INLINE = r"<script(?![^>]*\bsrc=)[^>]*>|<style\b|\sstyle=\"|\son[a-z]+=\""

    def setUp(self):
        resume_site(enabled=True)

    def test_public_pages_get_the_strict_csp_and_have_nothing_inline(self):
        import re
        for url in ("/", reverse("albright_resume:page")):
            with self.subTest(url=url):
                with mock.patch(OUTLOOK):
                    response = self.client.get(url)
                csp = response["Content-Security-Policy"]
                for directive in ("default-src 'self'", "script-src 'self'", "style-src 'self'",
                                  "font-src 'self'", "img-src 'self' data:"):
                    self.assertIn(directive, csp)
                self.assertIsNone(re.search(self.PUBLIC_INLINE, response.content.decode()), url)

    def test_signed_in_apps_send_no_csp(self):
        self.client.force_login(User.objects.create_superuser(username="boss", password="pw-not-real-12345", email=""))
        with mock.patch(OUTLOOK, return_value=None):
            for url in ("/", reverse("labs:hub"), reverse("trading_home"), reverse("albright_reselling_app:dashboard"),
                        reverse("albright_crypto_app:inbox"), reverse("albright_control:overview"),
                        reverse("albright_resume_admin:overview"), reverse("user_login")):
                with self.subTest(url=url):
                    response = self.client.get(url)
                    self.assertEqual(response.status_code, 200)
                    self.assertNotIn("Content-Security-Policy", response)

    def test_login_page_for_visitors_has_no_csp(self):
        self.assertNotIn("Content-Security-Policy", self.client.get(reverse("user_login")))

    def test_every_page_layout_points_at_a_favicon_that_exists(self):
        self.assertTrue(finders.find("images/favicon.svg"))
        with mock.patch(OUTLOOK):
            for url in ("/", reverse("albright_resume:page"), reverse("user_login")):
                self.assertIn('href="/static/images/favicon.svg"', self.client.get(url).content.decode())


class LocalStaticServingTests(TestCase):
    """SERVE_STATIC_LOCALLY=1 (local .env) serves /static/ with DEBUG off;
    without it nothing is added (production serves static files itself)."""

    def urls_with(self, **flags):
        import importlib
        from django.urls import clear_url_caches
        import albright_trading_proj.urls as project_urls
        with override_settings(**flags):
            module = importlib.reload(project_urls)
            patterns = list(module.urlpatterns)
        importlib.reload(project_urls)
        clear_url_caches()
        return patterns

    def serves_static(self, patterns):
        return any(getattr(p, "pattern", None) and p.pattern.match("static/style.css") for p in patterns
                   if "static" in str(getattr(p, "pattern", "")))

    def test_flag_adds_static_serving_only_when_debug_is_off(self):
        self.assertTrue(self.serves_static(self.urls_with(DEBUG=False, SERVE_STATIC_LOCALLY=True)))
        self.assertFalse(self.serves_static(self.urls_with(DEBUG=False, SERVE_STATIC_LOCALLY=False)))
        self.assertFalse(self.serves_static(self.urls_with(DEBUG=True, SERVE_STATIC_LOCALLY=True)))

    def test_served_css_has_the_css_type(self):
        from django.contrib.staticfiles.views import serve
        from django.test import RequestFactory
        response = serve(RequestFactory().get("/static/style.css"), "style.css", insecure=True)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response["Content-Type"].startswith("text/css"))
