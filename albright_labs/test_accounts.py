"""Locked-down accounts: no apps by default, invite-only sign-up, email through
Gmail SMTP (mocked here - Django's test runner swaps in the locmem backend),
password setup/reset links on the real domain, per-IP rate limits, and the
audit log."""
import hashlib
import re
import smtplib
from datetime import timedelta
from unittest import mock

from django.apps import apps as django_apps
from django.contrib.auth.models import User
from django.core import mail
from django.test import TestCase, override_settings
from django.urls import NoReverseMatch, reverse
from django.utils import timezone

from albright_control.models import AuditEntry, IntegrationCheck

from . import app_access, apps_registry, emails, ratelimit
from .models import Invitation, RateLimitHit, UserPreference

SITE = "https://natealbright36.pythonanywhere.com"
PASSWORD = "a-Long-unusual-pass-9481"
OUTLOOK = "albright_trading_app.views.get_market_outlook"


def invite_link(content):
    match = re.search(r'value="(%s/join/([^"/]+)/)" id="invite-link-value"' % re.escape(SITE), content)
    return (match.group(1), match.group(2)) if match else (None, None)


def path_of(link):
    return link.replace(SITE, "")


@override_settings(SITE_URL=SITE, EMAIL_CONFIGURED=True,
                   EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class Base(TestCase):
    def setUp(self):
        outlook = mock.patch(OUTLOOK, return_value=None)
        outlook.start()
        self.addCleanup(outlook.stop)
        self.boss = User.objects.create_superuser(username="boss", password=PASSWORD, email="boss@example.com",
                                                  first_name="Nate")
        self.client.force_login(self.boss)

    def invite(self, email="sam@example.com", apps=("meals",), days=7, name="Sam"):
        """Create an invite from Admin > Users; returns (invite, raw token, page content)."""
        response = self.client.post(reverse("albright_control:users"), {
            "action": "invite", "email": email, "name": name, "apps": list(apps), "days": days})
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        _link, raw = invite_link(content)
        self.assertTrue(raw, "the invite link is always shown")
        return Invitation.objects.get(email=email), raw, content

    def sign_up(self, raw, username="sam", password=PASSWORD, **extra):
        return self.client.post(reverse("labs:signup", args=[raw]),
                                {"username": username, "password1": password, "password2": password, **extra})

    def actions(self):
        return list(AuditEntry.objects.order_by("pk").values_list("action", flat=True))


# ---------------------------------------------------------------------------
# No apps by default
# ---------------------------------------------------------------------------

class NoAppsByDefaultTests(Base):
    def test_new_user_has_no_apps_and_sees_access_pending(self):
        newbie = User.objects.create_user(username="newbie", password=PASSWORD)
        self.assertEqual(UserPreference.for_user(newbie).allowed_apps, [])
        self.assertEqual(app_access.allowed_keys(newbie, UserPreference.for_user(newbie)), [])
        self.client.force_login(newbie)
        home = self.client.get("/")
        self.assertContains(home, "Your account is set up. You'll see apps here once access is granted.")
        self.assertContains(home, f'href="{reverse("user_logout")}" class="btn btn-secondary btn-sm" id="pending-logout"')
        self.assertContains(self.client.get(reverse("labs:hub")), 'id="access-pending"')
        for app in apps_registry.APPS:
            with self.subTest(app=app.key):
                self.assertEqual(self.client.get(reverse(app.landing_url_name)).status_code, 404)

    def test_admin_add_user_starts_with_no_apps(self):
        page = self.client.get(reverse("albright_control:users")).content.decode()
        create_form = page.split('id="create-user-form"', 1)[1]
        self.assertNotIn("checked", create_form.split("</fieldset>", 1)[0])           # nothing ticked by default
        self.client.post(reverse("albright_control:users"), {"action": "create", "username": "pat", "password": ""})
        self.assertEqual(UserPreference.objects.get(user__username="pat").allowed_apps, [])

    def test_existing_users_keep_their_access(self):
        """Migration 0003: accounts without a row (every app, under the old default) get a row saying so;
        accounts with a row are left alone."""
        migration = __import__("albright_labs.migrations.0003_invitations", fromlist=["keep_existing_access"])
        old = User.objects.create_user(username="old", password=PASSWORD)
        arranged = User.objects.create_user(username="arranged", password=PASSWORD)
        UserPreference.objects.create(user=arranged, allowed_apps=["resell"])
        migration.keep_existing_access(django_apps, None)
        self.assertEqual(UserPreference.objects.get(user=old).allowed_apps, ["trade", "resell", "opps", "meals"])
        self.assertEqual(UserPreference.objects.get(user=arranged).allowed_apps, ["resell"])
        self.client.force_login(old)
        self.assertEqual(self.client.get(reverse("albright_meals:week")).status_code, 200)

    def test_superuser_always_has_everything(self):
        self.assertFalse(UserPreference.objects.filter(user=self.boss).exists())
        self.assertEqual(app_access.allowed_keys(self.boss, UserPreference.for_user(self.boss)),
                         apps_registry.all_keys())
        self.assertNotContains(self.client.get(reverse("labs:hub")), 'id="access-pending"')
        self.assertEqual(self.client.get(reverse("albright_control:overview")).status_code, 200)


# ---------------------------------------------------------------------------
# No public sign-up
# ---------------------------------------------------------------------------

class NoPublicSignupTests(Base):
    def test_old_registration_pages_are_gone(self):
        self.client.logout()
        for path in ("/registration/", "/registrationsuccess/"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 404)
                self.assertEqual(self.client.post(path, {"username": "x", "password": "y"}).status_code, 404)
        with self.assertRaises(NoReverseMatch):
            reverse("registration")
        login = self.client.get(reverse("user_login")).content.decode()
        self.assertNotIn("Create an Account", login)
        self.assertNotIn(">Register<", login)
        self.assertIn(f'href="{reverse("password_reset")}"', login)
        self.assertIn("Forgot password?", login)

    def test_sign_up_without_a_valid_invite_is_404(self):
        self.client.logout()
        for token in ("nope", "x" * 43, "a" * 200):
            with self.subTest(token=token[:10]):
                self.assertEqual(self.client.get(reverse("labs:signup", args=[token])).status_code, 404)
                self.assertEqual(self.sign_up(token).status_code, 404)
        self.assertFalse(User.objects.filter(username="sam").exists())


# ---------------------------------------------------------------------------
# Invites
# ---------------------------------------------------------------------------

class InviteTests(Base):
    def test_invite_page_always_shows_the_link_with_copy_and_send_buttons(self):
        invite, raw, content = self.invite()
        self.assertIn('data-copy-target="invite-link-value"', content)
        self.assertIn('id="invite-send-email"', content)
        self.assertEqual(len(mail.outbox), 0)                       # not emailed until asked
        self.assertIn(f'data-invite="{invite.pk}" data-status="pending"', content)

    def test_token_is_stored_only_as_a_hash(self):
        invite, raw, _ = self.invite()
        self.assertEqual(invite.token_hash, hashlib.sha256(raw.encode()).hexdigest())
        for value in Invitation.objects.values().get(pk=invite.pk).values():
            self.assertNotIn(raw, str(value))
        self.assertGreaterEqual(len(raw), 40)
        self.assertNotIn(raw, self.client.get(reverse("albright_control:users")).content.decode())   # shown once

    def test_invite_grants_exactly_its_apps(self):
        invite, raw, _ = self.invite(apps=["meals", "resell"])
        self.client.logout()
        page = self.client.get(reverse("labs:signup", args=[raw]))
        self.assertContains(page, 'value="sam@example.com" readonly disabled')
        response = self.sign_up(raw)
        self.assertRedirects(response, reverse("labs:root"), fetch_redirect_response=False)
        sam = User.objects.get(username="sam")
        self.assertEqual((sam.email, sam.first_name, sam.is_staff, sam.is_superuser), ("sam@example.com", "Sam", False, False))
        self.assertTrue(sam.check_password(PASSWORD))
        self.assertEqual(UserPreference.objects.get(user=sam).allowed_apps, ["resell", "meals"])
        self.assertEqual(int(self.client.session["_auth_user_id"]), sam.pk)             # signed in
        self.assertEqual(self.client.get(reverse("albright_meals:week")).status_code, 200)
        self.assertEqual(self.client.get(reverse("trading_home")).status_code, 404)
        invite.refresh_from_db()
        self.assertEqual((invite.status, invite.used_by), ("used", sam))

    def test_invite_with_no_apps_lands_on_access_pending(self):
        _invite, raw, _ = self.invite(apps=[])
        self.client.logout()
        self.sign_up(raw)
        self.assertContains(self.client.get("/"), 'id="access-pending"')

    def test_single_use(self):
        _invite, raw, _ = self.invite()
        self.client.logout()
        self.sign_up(raw)
        self.client.logout()
        self.assertEqual(self.client.get(reverse("labs:signup", args=[raw])).status_code, 404)
        self.assertEqual(self.sign_up(raw, username="sam2").status_code, 404)
        self.assertFalse(User.objects.filter(username="sam2").exists())

    def test_expiry(self):
        invite, raw, _ = self.invite(days=1)
        self.assertAlmostEqual(invite.expires_at, timezone.now() + timedelta(days=1), delta=timedelta(minutes=1))
        Invitation.objects.filter(pk=invite.pk).update(expires_at=timezone.now() - timedelta(seconds=1))
        self.client.logout()
        self.assertEqual(self.client.get(reverse("labs:signup", args=[raw])).status_code, 404)
        self.assertEqual(Invitation.objects.get(pk=invite.pk).status, "expired")

    def test_default_expiry_is_seven_days(self):
        self.assertIn('<option value="7" selected>', self.client.get(reverse("albright_control:users")).content.decode())

    def test_revoke(self):
        invite, raw, _ = self.invite()
        self.client.post(reverse("albright_control:users"), {"action": "invite_revoke", "invite_id": invite.pk})
        self.assertEqual(Invitation.objects.get(pk=invite.pk).status, "revoked")
        self.client.logout()
        self.assertEqual(self.client.get(reverse("labs:signup", args=[raw])).status_code, 404)

    def test_resend_makes_a_new_link_and_emails_it(self):
        invite, old_raw, _ = self.invite()
        Invitation.objects.filter(pk=invite.pk).update(expires_at=timezone.now() - timedelta(hours=1))   # expired
        response = self.client.post(reverse("albright_control:users"), {"action": "invite_resend", "invite_id": invite.pk})
        link, new_raw = invite_link(response.content.decode())
        self.assertNotEqual(new_raw, old_raw)
        self.assertEqual(Invitation.objects.get(pk=invite.pk).status, "pending")
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn(link, mail.outbox[0].body)
        self.client.logout()
        self.assertEqual(self.client.get(reverse("labs:signup", args=[old_raw])).status_code, 404)
        self.assertEqual(self.client.get(path_of(link)).status_code, 200)

    def test_email_is_locked_to_the_invite(self):
        _invite, raw, _ = self.invite()
        self.client.logout()
        self.sign_up(raw, email="someone-else@example.com")
        self.assertEqual(User.objects.get(username="sam").email, "sam@example.com")

    def test_password_validators_apply(self):
        _invite, raw, _ = self.invite()
        self.client.logout()
        response = self.sign_up(raw, password="123")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "This password is too short")
        self.assertFalse(User.objects.filter(username="sam").exists())
        mismatch = self.client.post(reverse("labs:signup", args=[raw]),
                                    {"username": "sam", "password1": PASSWORD, "password2": PASSWORD + "x"})
        self.assertContains(mismatch, "The two passwords don")

    def test_taken_username_and_email(self):
        User.objects.create_user(username="Sam", password=PASSWORD)
        _invite, raw, _ = self.invite()
        self.client.logout()
        self.assertContains(self.sign_up(raw, username="sam"), "That username is taken.")
        self.client.force_login(self.boss)
        User.objects.create_user(username="pat", email="pat@example.com", password=PASSWORD)
        response = self.client.post(reverse("albright_control:users"), {
            "action": "invite", "email": "PAT@example.com", "apps": ["meals"], "days": 7})
        self.assertContains(response, "There&#x27;s already an account with this email.", status_code=400)

    def test_signed_in_visitor_is_told_to_log_out_first(self):
        invite, raw, _ = self.invite()
        response = self.client.get(reverse("labs:signup", args=[raw]))
        self.assertContains(response, 'id="signup-signed-in"')
        self.assertEqual(Invitation.objects.get(pk=invite.pk).status, "pending")

    def test_invites_are_superuser_only(self):
        _invite, raw, _ = self.invite()
        pat = User.objects.create_user(username="pat", password=PASSWORD)
        self.client.force_login(pat)
        self.assertEqual(self.client.post(reverse("albright_control:users"), {
            "action": "invite", "email": "x@example.com", "days": 7}).status_code, 404)
        self.assertFalse(Invitation.objects.filter(email="x@example.com").exists())


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------

class EmailTests(Base):
    def test_send_by_email(self):
        invite, raw, _ = self.invite(apps=["meals"])
        response = self.client.post(reverse("albright_control:users"),
                                    {"action": "invite_email", "invite_id": invite.pk, "token": raw})
        self.assertContains(response, "Emailed.")
        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        self.assertEqual(message.to, ["sam@example.com"])
        self.assertEqual(message.subject, "Nate invited you to Albright Labs")
        link = f"{SITE}/join/{raw}/"
        self.assertIn(link, message.body)
        self.assertIn("Meals", message.body)
        self.assertIn("expires", message.body)
        html, kind = message.alternatives[0]
        self.assertEqual(kind, "text/html")
        self.assertIn(f'href="{link}"', html)
        self.assertIn("Albright Labs", html)
        self.assertIsNotNone(Invitation.objects.get(pk=invite.pk).last_sent_at)

    def test_send_by_email_needs_the_current_token(self):
        invite, _raw, _ = self.invite()
        self.client.post(reverse("albright_control:users"), {"action": "invite_email", "invite_id": invite.pk,
                                                            "token": "guess"})
        self.assertEqual(len(mail.outbox), 0)

    def test_fails_gracefully_when_not_configured(self):
        invite, raw, _ = self.invite()
        with override_settings(EMAIL_CONFIGURED=False):
            response = self.client.post(reverse("albright_control:users"),
                                        {"action": "invite_email", "invite_id": invite.pk, "token": raw})
            self.assertEqual(emails.send("test", "x@example.com", {}), (False, emails.NOT_CONFIGURED))
        self.assertContains(response, "Not emailed: Email isn&#x27;t set up")
        self.assertEqual(invite_link(response.content.decode())[1], raw)           # the link is still there
        self.assertEqual(len(mail.outbox), 0)

    def test_fails_gracefully_when_gmail_refuses(self):
        with mock.patch("django.core.mail.EmailMultiAlternatives.send",
                        side_effect=smtplib.SMTPAuthenticationError(535, b"bad")):
            ok, message = emails.send("test", "x@example.com", {})
        self.assertFalse(ok)
        self.assertIn("app password", message)
        with mock.patch("django.core.mail.EmailMultiAlternatives.send", side_effect=OSError("network")):
            self.assertFalse(emails.send("test", "x@example.com", {})[0])

    def test_password_setup_link_uses_email_and_always_shows_the_link(self):
        pat = User.objects.create_user(username="pat", email="pat@example.com")
        response = self.client.post(reverse("albright_control:users"), {"action": "setup_link", "user_id": pat.pk})
        self.assertContains(response, "Emailed to pat@example.com")
        link = re.search(r'readonly value="([^"]+)" id="setup-link-value"', response.content.decode()).group(1)
        self.assertTrue(link.startswith(f"{SITE}/reset/"))
        self.assertIn(link, mail.outbox[0].body)
        self.assertEqual(mail.outbox[0].subject, "Set your password for Albright Labs")
        self.assertIn("Nate set up an Albright Labs account", mail.outbox[0].body)

    def test_links_use_the_real_domain_not_the_request_host(self):
        _invite, raw, content = self.invite()
        self.assertNotIn("testserver/join", content)
        self.assertIn(f"{SITE}/join/{raw}/", content)

    def test_integrations_email_row_and_test_button(self):
        page = self.client.get(reverse("albright_control:integrations")).content.decode()
        row = page.split('data-integration="email"', 1)[1].split("</tr>", 1)[0]
        self.assertIn("Configured", row)
        self.assertIn("Send test email to me", row)
        self.assertNotIn("app-password", row)
        self.client.post(reverse("albright_control:test_integration", args=["email"]))
        self.assertEqual(mail.outbox[0].to, ["boss@example.com"])
        self.assertTrue(IntegrationCheck.objects.get(integration="email").ok)
        with override_settings(EMAIL_CONFIGURED=False):
            row = self.client.get(reverse("albright_control:integrations")).content.decode().split(
                'data-integration="email"', 1)[1].split("</tr>", 1)[0]
            self.assertIn("Missing", row)

    def test_test_email_needs_an_address(self):
        self.boss.email = ""
        self.boss.save()
        self.client.post(reverse("albright_control:test_integration", args=["email"]))
        self.assertEqual(len(mail.outbox), 0)
        self.assertFalse(IntegrationCheck.objects.get(integration="email").ok)


# ---------------------------------------------------------------------------
# Forgot password
# ---------------------------------------------------------------------------

class ForgotPasswordTests(Base):
    def setUp(self):
        super().setUp()
        self.pat = User.objects.create_user(username="pat", email="pat@example.com", password=PASSWORD)
        self.client.logout()

    def test_same_answer_whether_or_not_the_account_exists(self):
        known = self.client.post(reverse("password_reset"), {"email": "pat@example.com"})
        unknown = self.client.post(reverse("password_reset"), {"email": "nobody@example.com"})
        self.assertRedirects(known, reverse("password_reset_done"), fetch_redirect_response=False)
        self.assertRedirects(unknown, reverse("password_reset_done"), fetch_redirect_response=False)
        self.assertEqual(len(mail.outbox), 1)                    # only the real account gets an email
        self.assertEqual(self.client.get(reverse("password_reset_done")).content,
                         self.client.get(reverse("password_reset_done")).content)

    def test_reset_email_uses_the_real_domain_and_works(self):
        self.client.post(reverse("password_reset"), {"email": "pat@example.com"})
        message = mail.outbox[0]
        self.assertEqual(message.subject, "Reset your Albright Labs password")
        link = re.search(r"(%s/reset/\S+/)" % re.escape(SITE), message.body).group(1)
        self.assertIn(f'href="{link}"', message.alternatives[0][0])
        form = self.client.get(path_of(link), follow=True)
        done = self.client.post(form.redirect_chain[-1][0], {"new_password1": PASSWORD + "2",
                                                              "new_password2": PASSWORD + "2"})
        self.assertRedirects(done, reverse("password_reset_complete"), fetch_redirect_response=False)
        self.pat.refresh_from_db()
        self.assertTrue(self.pat.check_password(PASSWORD + "2"))

    def test_send_failure_still_gives_the_same_answer(self):
        with mock.patch("django.core.mail.EmailMultiAlternatives.send", side_effect=OSError("down")):
            response = self.client.post(reverse("password_reset"), {"email": "pat@example.com"})
        self.assertRedirects(response, reverse("password_reset_done"), fetch_redirect_response=False)
        self.assertIn("failed", AuditEntry.objects.get(action="password_reset_sent").details)


# ---------------------------------------------------------------------------
# Rate limits
# ---------------------------------------------------------------------------

class RateLimitTests(Base):
    def setUp(self):
        super().setUp()
        self.client.logout()

    def test_failed_sign_ins_are_limited_per_ip(self):
        attempts = ratelimit.LIMITS["login"][0]
        for _ in range(attempts):
            response = self.client.post(reverse("user_login"), {"username": "boss", "password": "wrong"})
            self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="login-error"')
        blocked = self.client.post(reverse("user_login"), {"username": "boss", "password": PASSWORD})
        self.assertEqual(blocked.status_code, 429)                 # even the right password waits
        self.assertIn("Retry-After", blocked)
        self.assertNotIn("_auth_user_id", self.client.session)
        other_ip = self.client.post(reverse("user_login"), {"username": "boss", "password": PASSWORD},
                                    HTTP_X_REAL_IP="203.0.113.9")
        self.assertEqual(other_ip.status_code, 302)

    def test_a_good_sign_in_clears_the_count(self):
        for _ in range(3):
            self.client.post(reverse("user_login"), {"username": "boss", "password": "wrong"})
        self.client.post(reverse("user_login"), {"username": "boss", "password": PASSWORD})
        self.assertFalse(RateLimitHit.objects.filter(scope="login").exists())

    def test_sign_up_page_is_limited(self):
        for n in range(ratelimit.LIMITS["signup"][0]):
            self.assertEqual(self.client.get(reverse("labs:signup", args=[f"guess{n}"])).status_code, 404)
        self.assertEqual(self.client.get(reverse("labs:signup", args=["another"])).status_code, 429)

    def test_forgot_password_is_limited(self):
        for _ in range(ratelimit.LIMITS["reset"][0]):
            self.client.post(reverse("password_reset"), {"email": "nobody@example.com"})
        response = self.client.post(reverse("password_reset"), {"email": "boss@example.com"})
        self.assertEqual(response.status_code, 429)
        self.assertEqual(len(mail.outbox), 0)

    def test_old_hits_expire(self):
        for _ in range(ratelimit.LIMITS["reset"][0]):
            self.client.post(reverse("password_reset"), {"email": "nobody@example.com"})
        RateLimitHit.objects.update(created_at=timezone.now() - timedelta(hours=2))
        self.assertRedirects(self.client.post(reverse("password_reset"), {"email": "nobody@example.com"}),
                             reverse("password_reset_done"), fetch_redirect_response=False)


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------

class AuditTests(Base):
    def test_invite_lifecycle_is_audited(self):
        invite, raw, _ = self.invite()
        self.client.post(reverse("albright_control:users"), {"action": "invite_email", "invite_id": invite.pk,
                                                            "token": raw})
        resent = self.client.post(reverse("albright_control:users"), {"action": "invite_resend",
                                                                       "invite_id": invite.pk})
        _link, raw = invite_link(resent.content.decode())
        self.client.logout()
        self.sign_up(raw)
        self.client.force_login(self.boss)
        other, _raw, _ = self.invite(email="kim@example.com")
        self.client.post(reverse("albright_control:users"), {"action": "invite_revoke", "invite_id": other.pk})
        self.assertEqual(self.actions(), ["invite_created", "invite_sent", "invite_resent", "invite_used",
                                          "invite_created", "invite_revoked"])
        used = AuditEntry.objects.get(action="invite_used")
        self.assertEqual(used.user.username, "sam")
        self.assertIn("meals", used.details)
        for entry in AuditEntry.objects.all():
            self.assertNotIn(raw, entry.details)                   # tokens never logged

    def test_password_links_and_access_changes_are_audited(self):
        pat = User.objects.create_user(username="pat", email="pat@example.com", password=PASSWORD)
        self.client.post(reverse("albright_control:users"), {"action": "setup_link", "user_id": pat.pk})
        self.client.post(reverse("albright_control:users"), {"action": "access", "user_id": pat.pk,
                                                            "apps": ["meals", "opps"]})
        self.client.logout()
        self.client.post(reverse("password_reset"), {"email": "pat@example.com"})
        self.assertEqual(self.actions(), ["password_setup_link", "user_access_changed", "password_reset_sent"])
        self.assertIn("none -> opps, meals", AuditEntry.objects.get(action="user_access_changed").details)
