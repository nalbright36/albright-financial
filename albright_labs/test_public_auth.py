"""The logged-out pages - sign in, sign out, sign up from an invite link, password reset (request, sent, choose,
saved) - on templates/base_public.html, the home page's base: "Albright Labs", never "Trading", no app chrome,
nothing inline, labeled fields with inline errors, a show/hide password button. Sign-in still works and keeps
?next=; the emails use the same name and dark look."""
import re
from urllib.parse import urlsplit

from django.contrib.auth.models import User
from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse

from .models import Invitation

SITE = "https://labs.example.com"
PASSWORD = "Correct-Horse-9"
APP_CHROME = ("navbar__chip", 'id="navbar-links"', 'id="app-launcher"', 'id="account-menu"', 'id="theme-toggle"',
              "style.css", "launcher.css")


@override_settings(SITE_URL=SITE, EMAIL_CONFIGURED=True,
                   EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class Base(TestCase):
    def setUp(self):
        self.boss = User.objects.create_superuser(username="boss", password=PASSWORD, email="boss@example.com")
        self.pat = User.objects.create_user(username="pat", email="pat@example.com", password=PASSWORD)

    def assertPublic(self, response, heading=None):
        """Rendered on base_public.html with the brand and nothing of the app's."""
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "base_public.html")
        self.assertTemplateNotUsed(response, "index.html")
        page = response.content.decode()
        self.assertIn("Albright Labs", page)
        self.assertNotIn("Trading", page)
        self.assertIn('<link rel="stylesheet" href="/static/css/public.css">', page)
        self.assertIn('src="/static/icons/logo-mark-white.png"', page)
        self.assertIn('class="ld-footer"', page)
        for chrome in APP_CHROME:
            self.assertNotIn(chrome, page)
        self.assertNotIn(' style="', page)                 # works under the strict public CSP
        self.assertNotIn("<style", page)
        self.assertNotRegex(page, r"<script(?![^>]*\ssrc=)")
        self.assertNotRegex(page, r"\son[a-z]+=")
        if heading:
            self.assertIn(f'<h1 class="disp pa-title" id="pa-title">{heading}</h1>', page)
            self.assertRegex(page, rf"<title>[^<]*· Albright Labs</title>")
            self.assertIn(f'<a href="{reverse("labs:root")}" class="pa-brand"', page)
            self.assertIn('<meta name="robots" content="noindex">', page)
        return page

    def reset_link(self):
        self.client.post(reverse("password_reset"), {"email": "pat@example.com"})
        link = re.search(r"(%s/reset/\S+/)" % re.escape(SITE), mail.outbox[-1].body).group(1)
        return urlsplit(link).path


class PagesTests(Base):
    def test_sign_in(self):
        page = self.assertPublic(self.client.get(reverse("user_login")), "Sign in")
        self.assertTemplateUsed(self.client.get(reverse("user_login")), "base_public_form.html")
        self.assertIn('<label class="pa-label" for="username">Username</label>', page)
        self.assertIn('<label class="pa-label" for="password">Password</label>', page)
        self.assertIn(f'href="{reverse("password_reset")}" id="forgot-password">Forgot password?</a>', page)
        self.assertIn(f'href="{reverse("labs:root")}#invite" id="request-invite">Request an invite</a>', page)
        self.assertIn(f'href="{reverse("labs:root")}" id="back-home">Back to home</a>', page)
        self.assertIn('<button type="submit" class="btn btn-pri pa-submit">Sign in</button>', page)
        self.assertIn('<script src="/static/js/public.js" defer></script>', page)

    def test_show_hide_password_is_a_real_button(self):
        page = self.client.get(reverse("user_login")).content.decode()
        self.assertIn('<button type="button" class="pa-pw__toggle" data-password-toggle aria-controls="password" '
                      'aria-label="Show password" hidden>Show</button>', page)

    def test_sign_out_lands_on_sign_in_saying_so(self):
        self.client.force_login(self.pat)
        response = self.client.get(reverse("user_logout"))
        self.assertRedirects(response, reverse("user_login"), fetch_redirect_response=False)
        page = self.assertPublic(self.client.get(reverse("user_login")), "Sign in")
        self.assertIn('<p class="pa-notice" role="status">You&#x27;re signed out.</p>', page)

    def test_sign_up(self):
        invite, raw = Invitation.create(email="new@example.com", apps=["notes"], created_by=self.boss)
        response = self.client.get(reverse("labs:signup", args=[raw]))
        page = self.assertPublic(response, "Create your account")
        self.assertIn('value="new@example.com" readonly disabled', page)
        self.assertEqual(page.count("data-password-toggle"), 2)           # password and again
        self.assertIn('class="ld-input pa-input"', page)

    def test_sign_up_errors_are_inline_and_above(self):
        invite, raw = Invitation.create(email="new@example.com", apps=["notes"], created_by=self.boss)
        response = self.client.post(reverse("labs:signup", args=[raw]),
                                    {"username": "pat", "password1": PASSWORD, "password2": PASSWORD})
        page = self.assertPublic(response, "Create your account")
        self.assertIn('id="form-error"', page)
        self.assertIn('<p class="pa-field-error" id="id_username-error">That username is taken.</p>', page)
        self.assertRegex(page, r'<input[^>]*name="username"[^>]*aria-invalid="true"')
        self.assertRegex(page, r'<input[^>]*name="username"[^>]*aria-describedby="id_username-help id_username-error"')

    def test_reset_request_sent_choose_and_saved(self):
        self.assertPublic(self.client.get(reverse("password_reset")), "Reset your password")
        sent = self.client.post(reverse("password_reset"), {"email": "pat@example.com"}, follow=True)
        self.assertContains(sent, 'id="reset-sent"')
        self.assertPublic(sent, "Check your email")
        form = self.client.get(self.reset_link(), follow=True)
        page = self.assertPublic(form, "Choose a new password")
        self.assertIn('id="set-password-form"', page)
        self.assertEqual(page.count("data-password-toggle"), 2)
        done = self.client.post(form.redirect_chain[-1][0], {"new_password1": PASSWORD + "2",
                                                              "new_password2": PASSWORD + "2"}, follow=True)
        self.assertPublic(done, "Password saved")

    def test_reset_errors_and_a_used_link(self):
        bad = self.client.post(reverse("password_reset"), {"email": "not-an-email"})
        page = self.assertPublic(bad, "Reset your password")
        self.assertIn('class="pa-field-error" id="id_email-error"', page)
        form = self.client.get(self.reset_link(), follow=True)
        mismatch = self.client.post(form.redirect_chain[-1][0], {"new_password1": PASSWORD + "2",
                                                                  "new_password2": PASSWORD + "3"})
        page = self.assertPublic(mismatch, "Choose a new password")
        self.assertIn('id="form-error"', page)
        self.assertIn('id="id_new_password2-error"', page)
        used = self.client.get(reverse("password_reset_confirm", args=["MQ", "set-password"]))
        page = self.assertPublic(used, "Choose a new password")
        self.assertIn('id="reset-link-invalid"', page)

    @override_settings(EMAIL_CONFIGURED=False)
    def test_reset_with_email_off(self):
        page = self.assertPublic(self.client.get(reverse("password_reset")), "Reset your password")
        self.assertIn('id="reset-email-off"', page)


class SignInTests(Base):
    def test_signs_in_and_goes_home(self):
        response = self.client.post(reverse("user_login"), {"username": "pat", "password": PASSWORD})
        self.assertRedirects(response, reverse("labs:root"), fetch_redirect_response=False)
        self.assertEqual(int(self.client.session["_auth_user_id"]), self.pat.pk)

    def test_respects_next(self):
        target = reverse("labs:account")
        page = self.client.get(reverse("user_login") + f"?next={target}").content.decode()
        self.assertIn(f'<input type="hidden" name="next" value="{target}">', page)
        response = self.client.post(reverse("user_login"), {"username": "pat", "password": PASSWORD, "next": target})
        self.assertRedirects(response, target, fetch_redirect_response=False)

    def test_ignores_an_off_site_next(self):
        response = self.client.post(reverse("user_login"), {"username": "pat", "password": PASSWORD,
                                                            "next": "https://evil.example.com/"})
        self.assertRedirects(response, reverse("labs:root"), fetch_redirect_response=False)

    def test_wrong_password_shows_the_error_box_and_keeps_the_username(self):
        response = self.client.post(reverse("user_login"), {"username": "pat", "password": "wrong"})
        page = self.assertPublic(response, "Sign in")
        self.assertIn("<p class=\"pa-alert\" role=\"alert\" id=\"login-error\">That username and password didn't "
                      "match. Please try again.</p>", page)
        self.assertIn('name="username" value="pat"', page)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_csrf_is_required(self):
        from django.test import Client
        client = Client(enforce_csrf_checks=True)
        self.assertContains(client.get(reverse("user_login")), 'name="csrfmiddlewaretoken"')
        self.assertEqual(client.post(reverse("user_login"), {"username": "pat", "password": PASSWORD}).status_code,
                         403)


class EmailTests(Base):
    def test_reset_and_invite_emails_are_albright_labs_and_dark(self):
        self.reset_link()
        Invitation.create(email="new@example.com", apps=["notes"], created_by=self.boss)
        from . import invites
        invite, raw = Invitation.create(email="other@example.com", apps=["notes"], created_by=self.boss)
        invites.send(invite, raw)
        for message in mail.outbox:
            with self.subTest(subject=message.subject):
                html = message.alternatives[0][0]
                self.assertIn("Albright Labs", message.subject + message.body)
                self.assertNotIn("Trading", message.subject + message.body + html)
                self.assertIn("background:#07090C", html)                  # the public pages' dark page
                self.assertIn("background:#10141A", html)                  # and card
                self.assertIn("background:#F4F5F7", html)                  # a white button
                self.assertNotIn("#2563EB", html)
