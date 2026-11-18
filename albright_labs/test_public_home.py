"""The public home page at "/" (public_views): visitors get the landing page (no app chrome, nothing private,
strict headers plus Google Fonts, share tags), signed-in users keep Home; "Request an invite" (one row per email,
a valid email, 5 posts an hour per IP, a honeypot, the admin emailed); the footer pages; Admin > Invite requests
(list, Send invite through the normal invite flow, Decline)."""
from unittest import mock

from django.contrib.auth.models import User
from django.contrib.staticfiles import finders
from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils.html import escape

from albright_control.models import AuditEntry

from .models import Invitation, InviteRequest, RateLimitHit
from .public_views import THANKS
from .testing import with_apps

OUTLOOK = "albright_trading_app.views.get_market_outlook"
CLIENT = "alpaca.trading.client.TradingClient"
INVITE_URL = "/invite/"


class LandingPageTests(TestCase):
    def get(self, url="/"):
        with mock.patch(OUTLOOK) as outlook, mock.patch(CLIENT) as alpaca:
            response = self.client.get(url)
        outlook.assert_not_called()       # no market data
        alpaca.assert_not_called()        # no API calls
        return response

    def test_anonymous_root_renders_the_landing_page(self):
        response = self.get()
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "albright_labs/landing.html")
        self.assertTemplateNotUsed(response, "index.html")              # not the app base
        page = response.content.decode()
        self.assertIn("Everything you run at home, in one place.", page)
        for anchor in ('id="top"', 'id="apps"', 'id="features"', 'id="circles"', 'id="ai"', 'id="invite"'):
            self.assertIn(anchor, page)
        for private in ("app-launcher", "navbar__links", "Logout", "Watchlist", "Grok Bot"):
            self.assertNotIn(private, page)
        self.assertNotIn("logo-mark-white.png\"", page.replace("/static/icons/logo-mark-white.png\"", ""))
        self.assertIn('src="/static/icons/logo-mark-white.png"', page)
        self.assertIn('<link rel="stylesheet" href="/static/css/landing.css">', page)
        self.assertTrue(finders.find("css/landing.css"))

    def test_links_go_to_sign_in_the_invite_form_and_sections(self):
        page = self.get().content.decode()
        login = reverse("user_login")
        self.assertIn(f'href="{login}" id="nav-sign-in">Sign in</a>', page)
        self.assertIn(f'href="{login}" id="cta-sign-in">Sign in</a>', page)
        self.assertEqual(page.count('href="#invite">Request an invite</a>'), 2)   # header + hero
        for section in ("apps", "features", "circles", "ai"):
            self.assertIn(f'class="navlink" href="#{section}"', page)
        for name in ("privacy", "terms", "contact"):
            self.assertIn(f'href="{reverse("labs:" + name)}"', page)
        self.assertIn(f'action="{reverse("labs:invite_request")}"', page)
        self.assertIn('name="csrfmiddlewaretoken"', page)

    def test_title_description_share_tags_and_icons(self):
        page = self.get().content.decode()
        self.assertIn("<title>Albright Labs - everything you run at home, in one place</title>", page)
        self.assertIn('<meta name="description" content="Plan the week', page)
        self.assertIn('<meta property="og:title" content="Albright Labs', page)
        self.assertIn('<meta property="og:description" content="Plan the week', page)
        self.assertRegex(page, r'<meta property="og:image" content="https?://[^"]+/static/icons/og-image.png">')
        self.assertIn('<meta name="twitter:card" content="summary_large_image">', page)
        self.assertIn('href="/static/icons/favicon-32.png"', page)
        self.assertIn('href="/static/icons/apple-touch-icon.png"', page)
        from PIL import Image
        with Image.open(finders.find("icons/og-image.png")) as image:
            self.assertEqual(image.size, (1200, 630))
            self.assertEqual(image.convert("RGB").getpixel((5, 5)), (0, 0, 0))          # black

    def test_strict_headers_allow_google_fonts_and_nothing_inline(self):
        response = self.get()
        csp = dict(part.strip().split(" ", 1) for part in response["Content-Security-Policy"].split(";"))
        self.assertEqual(csp["script-src"], "'self'")
        self.assertEqual(csp["style-src"], "'self' https://fonts.googleapis.com")
        self.assertEqual(csp["font-src"], "'self' https://fonts.gstatic.com")
        self.assertEqual(csp["frame-ancestors"], "'none'")
        page = response.content.decode()
        self.assertNotIn(' style="', page)
        self.assertNotIn("<style", page)
        self.assertNotIn("<script", page)

    def test_reduced_motion_and_scroll_offset_are_in_the_css(self):
        with open(finders.find("css/landing.css"), encoding="utf-8") as fh:
            css = fh.read()
        self.assertIn("@media (prefers-reduced-motion:reduce)", css)
        self.assertIn("scroll-padding-top", css)
        self.assertIn("@media (prefers-reduced-motion:no-preference){html{scroll-behavior:smooth}}", css)
        self.assertIn("fonts.googleapis.com/css2?family=Inter", css)

    def test_old_resume_promo_is_gone(self):
        page = self.get().content.decode()
        for old in ("What I build", "View my interactive resume", "resume.css", 'id="labs-name"'):
            self.assertNotIn(old, page)

    def test_footer_pages(self):
        for name, title in (("privacy", "Privacy"), ("terms", "Terms"), ("contact", "Contact")):
            with self.subTest(page=name):
                response = self.get(reverse("labs:" + name))
                self.assertEqual(response.status_code, 200)
                page = response.content.decode()
                self.assertIn(f'<h1 class="disp ld-doc__h">{title}</h1>', page)
                self.assertIn('class="navlink" href="/#apps"', page)          # back to the home page's sections
                self.assertIn("Content-Security-Policy", response)


class SignedInHomeTests(TestCase):
    def test_logged_in_root_still_shows_home(self):
        user = with_apps(User.objects.create_user(username="nate", password="pw-not-real-12345"))
        self.client.force_login(user)
        response = self.client.get("/")
        self.assertTemplateUsed(response, "albright_labs/hub.html")
        self.assertTemplateNotUsed(response, "albright_labs/landing.html")
        self.assertNotIn("Content-Security-Policy", response)


@override_settings(EMAIL_CONFIGURED=True)
class InviteRequestTests(TestCase):
    def setUp(self):
        self.boss = User.objects.create_superuser(username="boss", password="pw-not-real-12345",
                                                  email="boss@example.com")

    def post(self, ip="203.0.113.5", **data):
        return self.client.post(INVITE_URL, {"email": "", "note": "", "website": "", **data}, REMOTE_ADDR=ip)

    def test_valid_post_creates_one_request_and_shows_thanks(self):
        response = self.post(email="  New.Person@Example.com ", note="Family of four")
        self.assertRedirects(response, "/?invite=thanks#invite", fetch_redirect_response=False)
        row = InviteRequest.objects.get()
        self.assertEqual((row.email, row.note, row.status), ("new.person@example.com", "Family of four", "new"))
        self.assertEqual(len(row.ip_hash), 64)
        self.assertNotIn("203.0.113.5", row.ip_hash)
        page = self.client.get("/?invite=thanks").content.decode()
        self.assertIn(escape(THANKS), page)
        self.assertNotIn('id="invite-form"', page)                    # the thanks replaces the form

    def test_admin_is_emailed_about_a_new_request(self):
        self.post(email="someone@example.com", note="Saw it at work")
        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        self.assertEqual(message.to, ["boss@example.com"])
        self.assertIn("someone@example.com", message.subject)
        self.assertIn("Saw it at work", message.body)
        self.assertIn(reverse("labs_admin:invite_requests"), message.body)

    def test_duplicate_email_shows_the_same_thanks_and_adds_nothing(self):
        self.post(email="dupe@example.com")
        mail.outbox.clear()
        response = self.post(email="DUPE@example.com", note="again")
        self.assertRedirects(response, "/?invite=thanks#invite", fetch_redirect_response=False)
        self.assertEqual(InviteRequest.objects.count(), 1)
        self.assertEqual(InviteRequest.objects.get().note, "")
        self.assertEqual(mail.outbox, [])

    def test_bad_email_is_rejected(self):
        response = self.post(email="not-an-email")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(InviteRequest.objects.count(), 0)
        page = response.content.decode()
        self.assertIn("Enter a valid email address.", page)
        self.assertIn('value="not-an-email"', page)
        self.assertIn('aria-invalid="true"', page)
        self.assertEqual(self.post(email="").status_code, 400)

    def test_honeypot_submissions_are_ignored(self):
        response = self.post(email="bot@example.com", website="http://spam.example")
        self.assertRedirects(response, "/?invite=thanks#invite", fetch_redirect_response=False)
        self.assertEqual(InviteRequest.objects.count(), 0)
        self.assertEqual(mail.outbox, [])

    def test_rate_limited_to_five_an_hour_per_ip(self):
        for n in range(5):
            self.assertEqual(self.post(email=f"p{n}@example.com").status_code, 302)
        response = self.post(email="p6@example.com")
        self.assertEqual(response.status_code, 429)
        self.assertEqual(InviteRequest.objects.count(), 5)
        self.assertEqual(self.post(ip="198.51.100.9", email="other@example.com").status_code, 302)   # another IP
        RateLimitHit.objects.filter(scope="invite").delete()                                          # an hour on
        self.assertEqual(self.post(email="p7@example.com").status_code, 302)

    def test_needs_csrf(self):
        client = self.client_class(enforce_csrf_checks=True)
        response = client.post(INVITE_URL, {"email": "csrf@example.com"})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(InviteRequest.objects.count(), 0)

    def test_get_is_not_allowed(self):
        self.assertEqual(self.client.get(INVITE_URL).status_code, 405)


@override_settings(EMAIL_CONFIGURED=True)
class AdminInviteRequestTests(TestCase):
    def setUp(self):
        self.boss = User.objects.create_superuser(username="boss", password="pw-not-real-12345", email="")
        self.first = InviteRequest.objects.create(email="first@example.com", note="Hi there")
        self.second = InviteRequest.objects.create(email="second@example.com")
        self.client.force_login(self.boss)

    def test_admin_page_lists_requests(self):
        response = self.client.get(reverse("labs_admin:invite_requests"))
        self.assertEqual(response.status_code, 200)
        page = response.content.decode()
        self.assertIn("first@example.com", page)
        self.assertIn("Hi there", page)
        self.assertIn("second@example.com", page)
        self.assertIn(reverse("labs_admin:invite_request_send", args=[self.first.pk]), page)
        self.assertIn(reverse("labs_admin:invite_request_decline", args=[self.first.pk]), page)
        self.assertIn('href="/control/labs/"', page)                     # in the Admin nav

    def test_superusers_only(self):
        self.client.force_login(with_apps(User.objects.create_user(username="pat", password="pw-not-real-12345")))
        self.assertNotEqual(self.client.get(reverse("labs_admin:invite_requests")).status_code, 200)
        self.client.logout()
        self.assertNotEqual(self.client.get(reverse("labs_admin:invite_requests")).status_code, 200)

    def test_send_invite_uses_the_invite_flow(self):
        response = self.client.post(reverse("labs_admin:invite_request_send", args=[self.first.pk]),
                                    {"apps": ["planner", "meals", "admin"]})
        self.assertEqual(response.status_code, 200)
        invite = Invitation.objects.get()
        self.assertEqual((invite.email, invite.created_by), ("first@example.com", self.boss))
        self.assertEqual(sorted(invite.apps), ["meals", "planner"])                 # admin isn't grantable
        self.first.refresh_from_db()
        self.assertEqual((self.first.status, self.first.invitation, self.first.handled_by), ("invited", invite, self.boss))
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["first@example.com"])
        self.assertIn("/join/", mail.outbox[0].body)
        self.assertIn("/join/", response.content.decode())                       # the link, to copy
        self.assertTrue(AuditEntry.objects.filter(action="invite_request_sent").exists())
        again = self.client.post(reverse("labs_admin:invite_request_send", args=[self.first.pk]), {"apps": ["meals"]})
        self.assertEqual(again.status_code, 302)
        self.assertEqual(Invitation.objects.count(), 1)                          # not twice

    def test_decline(self):
        response = self.client.post(reverse("labs_admin:invite_request_decline", args=[self.second.pk]))
        self.assertRedirects(response, reverse("labs_admin:invite_requests"))
        self.second.refresh_from_db()
        self.assertEqual(self.second.status, "declined")
        self.assertEqual(Invitation.objects.count(), 0)
        self.assertEqual(mail.outbox, [])
        page = self.client.get(reverse("labs_admin:invite_requests") + "?status=declined").content.decode()
        self.assertIn("second@example.com", page)
        self.assertNotIn("first@example.com", page)
