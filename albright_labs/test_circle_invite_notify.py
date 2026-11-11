"""In-site circle invites for people who already have an account: no email, a notification
with Accept / Decline, the Circles page as a backup, and the existing email / join-link flow
left as it is for addresses with no account (albright_labs.circles and activity)."""
from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.contrib.contenttypes.models import ContentType
from django.core import mail
from django.core.cache import cache
from django.db.models.fields.json import DataContains
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from . import activity, circles
from .models import ActivityEvent, ActivityRead, CircleInvite, CircleMembership, NotificationSettings
from .testing import with_apps

PASSWORD = "pw-not-real-12345"


def _sqlite_json_contains(self, compiler, connection):
    """Enough of Postgres @> for JSON lists (membership.apps, circle.enabled_apps) on sqlite tests."""
    lhs, lhs_params = self.process_lhs(compiler, connection)
    rhs, rhs_params = self.process_rhs(compiler, connection)
    sql = ("EXISTS (SELECT 1 FROM json_each(%s) AS _l "
           "WHERE _l.value IN (SELECT _r.value FROM json_each(%s) AS _r))" % (lhs, rhs))
    return sql, tuple(lhs_params) + tuple(rhs_params)


@override_settings(BOT_API_REQUIRE_HTTPS=False)
class Base(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._contains_sqlite = getattr(DataContains, "as_sqlite", None)
        DataContains.as_sqlite = _sqlite_json_contains

    @classmethod
    def tearDownClass(cls):
        if cls._contains_sqlite is None:
            delattr(DataContains, "as_sqlite")
        else:
            DataContains.as_sqlite = cls._contains_sqlite
        super().tearDownClass()

    def setUp(self):
        cache.clear()
        outlook = mock.patch("albright_trading_app.views.get_market_outlook", return_value=None)
        outlook.start()
        self.addCleanup(outlook.stop)
        self.nate = with_apps(User.objects.create_user(username="nate", password=PASSWORD, first_name="Nathan",
                                                       email="nate@example.com"), ["meals", "money", "planner"])
        self.sam = with_apps(User.objects.create_user(username="sam", password=PASSWORD, first_name="Sam",
                                                      email="sam@example.com"), ["meals", "money"])
        self.kim = with_apps(User.objects.create_user(username="kim", password=PASSWORD, first_name="Kim"),
                             ["meals", "money"])
        self.client.force_login(self.nate)

    def as_user(self, user):
        c = Client()
        c.force_login(user)
        return c

    def circle(self, name="Albright Family", apps=("meals", "money")):
        return circles.create(self.nate, name, apps)

    def invite_event(self, user, invite=None):
        qs = ActivityEvent.objects.filter(app="circles", verb="invited", recipient=user)
        if invite is not None:
            qs = qs.filter(content_type=ContentType.objects.get_for_model(CircleInvite), object_id=invite.pk)
        return qs.get()


class ExistingUserInviteTests(Base):
    def test_existing_user_gets_a_notification_and_no_email(self):
        circle = self.circle()
        with mock.patch("albright_labs.invites.send") as send:
            self.client.post(reverse("labs:circle", args=[circle.pk]),
                             {"action": "invite", "who": "sam@example.com"})
            send.assert_not_called()
        invite = CircleInvite.objects.get(circle=circle, user=self.sam)
        self.assertIsNone(invite.signups.first())
        event = self.invite_event(self.sam, invite)
        self.assertEqual(event.actor, self.nate)
        self.assertEqual(event.circle, circle)
        self.assertEqual(event.audience, ActivityEvent.CIRCLE)
        self.assertEqual(event.summary, "invited you to join Albright Family")
        self.assertEqual(event.url, reverse("labs:circle_invite", args=[invite.pk]))
        self.assertEqual([activity.text(e) for e in activity.notifications(self.sam)],
                         ["Nathan invited you to join Albright Family"])
        self.assertEqual(activity.notifications(self.nate).filter(verb="invited").count(), 0)
        self.assertEqual(activity.notifications(self.kim).filter(verb="invited").count(), 0)
        self.assertEqual(activity.unread_count(self.sam), 1)

    def test_unknown_email_is_still_emailed_and_has_no_notification(self):
        circle = self.circle()
        with mock.patch("albright_labs.invites.send", return_value=(True, "Sent.")) as send:
            self.client.post(reverse("labs:circle", args=[circle.pk]),
                             {"action": "invite", "who": "new.person@example.com"})
            send.assert_called_once()
        invite = CircleInvite.objects.get(circle=circle, email="new.person@example.com")
        self.assertIsNone(invite.user)
        self.assertFalse(ActivityEvent.objects.filter(verb="invited").exists())
        self.assertIsNotNone(invite.signups.get())

    def test_signup_from_email_creates_the_notification(self):
        circle = self.circle()
        _ci, _raw, site, site_raw = circles.invite(self.nate, circle, "new.person@example.com")
        self.assertFalse(ActivityEvent.objects.filter(verb="invited").exists())
        Client().post(reverse("labs:signup", args=[site_raw]), {
            "username": "robin", "password1": "a-Strong-pass-123!", "password2": "a-Strong-pass-123!"})
        robin = User.objects.get(username="robin")
        event = self.invite_event(robin)
        self.assertEqual(event.summary, "invited you to join Albright Family")
        self.assertEqual(circles.pending_invites(robin)[0].pk, event.object_id)

    def test_bell_and_circles_page_have_accept_and_decline(self):
        circle = self.circle()
        invite, _raw, _s, _r = circles.invite(self.nate, circle, "sam")
        sam = self.as_user(self.sam)
        panel = sam.get(reverse("labs:notifications_panel")).content.decode()
        self.assertIn("Nathan invited you to join Albright Family", panel)
        self.assertIn(f'action="{reverse("labs:circle_invite", args=[invite.pk])}"', panel)
        self.assertIn('name="action" value="accept"', panel)
        self.assertIn('name="action" value="decline"', panel)
        page = sam.get(reverse("labs:circles")).content.decode()
        self.assertIn('id="circle-invites"', page)
        self.assertIn("Albright Family", page)
        self.assertIn('value="accept"', page)
        self.assertIn('value="decline"', page)
        self.assertIn("Review", page)

    def test_accept_from_the_notification_joins_and_notifies_the_owner(self):
        circle = self.circle()
        invite, _raw, _s, _r = circles.invite(self.nate, circle, "sam")
        event = self.invite_event(self.sam, invite)
        sam = self.as_user(self.sam)
        response = sam.post(reverse("labs:circle_invite", args=[invite.pk]), {"action": "accept"})
        self.assertRedirects(response, reverse("labs:circle", args=[circle.pk]), fetch_redirect_response=False)
        m = CircleMembership.objects.get(circle=circle, user=self.sam)
        self.assertEqual((m.status, m.apps), (CircleMembership.ACTIVE, []))
        self.assertEqual(CircleInvite.objects.get(pk=invite.pk).status, "accepted")
        self.assertFalse(circles.pending_invites(self.sam))
        self.assertIn("Sam accepted the invite and joined Albright Family",
                      [activity.text(e) for e in activity.notifications(self.nate)])
        self.assertTrue(ActivityRead.objects.filter(user=self.sam, event=event).exists())
        panel = sam.get(reverse("labs:notifications_panel")).content.decode()
        self.assertNotIn('value="accept"', panel)
        again = sam.post(reverse("labs:circle_invite", args=[invite.pk]), {"action": "accept"})
        self.assertRedirects(again, reverse("labs:circle", args=[circle.pk]), fetch_redirect_response=False)

    def test_decline_from_the_circles_page_and_only_the_invitee_can_act(self):
        circle = self.circle()
        invite, _raw, _s, _r = circles.invite(self.nate, circle, "sam")
        self.assertEqual(self.as_user(self.kim).post(reverse("labs:circle_invite", args=[invite.pk]),
                                                     {"action": "accept"}).status_code, 404)
        response = self.as_user(self.sam).post(reverse("labs:circle_invite", args=[invite.pk]),
                                               {"action": "decline"})
        self.assertRedirects(response, reverse("labs:circles"), fetch_redirect_response=False)
        self.assertEqual(CircleInvite.objects.get(pk=invite.pk).status, "declined")
        self.assertFalse(CircleMembership.objects.filter(circle=circle, user=self.sam).exists())
        self.assertFalse(circles.pending_invites(self.sam))
        again = self.as_user(self.sam).post(reverse("labs:circle_invite", args=[invite.pk]),
                                            {"action": "decline"})
        self.assertRedirects(again, reverse("labs:circles"), fetch_redirect_response=False)

    def test_expired_or_revoked_post_explains_and_does_not_join(self):
        circle = self.circle()
        invite, raw, _s, _r = circles.invite(self.nate, circle, "sam")
        CircleInvite.objects.filter(pk=invite.pk).update(expires_at=timezone.now() - timedelta(minutes=1))
        response = self.as_user(self.sam).post(reverse("labs:circle_invite", args=[invite.pk]), {"action": "accept"})
        self.assertRedirects(response, reverse("labs:circles"), fetch_redirect_response=False)
        self.assertFalse(CircleMembership.objects.filter(circle=circle, user=self.sam, status="active").exists())
        # The old email join link still only works for the invited person, and only while pending.
        self.assertEqual(self.as_user(self.sam).get(reverse("labs:circle_join", args=[raw])).status_code, 404)
        again, raw2, _s, _r = circles.invite(self.nate, circle, "sam")
        circles.revoke(self.nate, again)
        response = self.as_user(self.sam).post(reverse("labs:circle_invite", args=[again.pk]), {"action": "accept"})
        self.assertRedirects(response, reverse("labs:circles"), fetch_redirect_response=False)
        self.assertEqual(self.as_user(self.sam).get(reverse("labs:circle_join", args=[raw2])).status_code, 404)

    def test_join_link_still_works_and_opening_a_used_invite_goes_to_the_circle(self):
        circle = self.circle()
        invite, raw, _s, _r = circles.invite(self.nate, circle, "sam")
        sam = self.as_user(self.sam)
        response = sam.get(reverse("labs:circle_join", args=[raw]))
        self.assertRedirects(response, reverse("labs:circle_invite", args=[invite.pk]), fetch_redirect_response=False)
        circles.accept(self.sam, invite, ["meals"])
        opened = sam.get(reverse("labs:circle_invite", args=[invite.pk]))
        self.assertRedirects(opened, reverse("labs:circle", args=[circle.pk]), fetch_redirect_response=False)
        event = self.invite_event(self.sam, invite)
        self.assertEqual(sam.get(reverse("labs:notification_open", args=[event.pk]))["Location"],
                         reverse("labs:circle", args=[circle.pk]))

    def test_invite_notification_stays_when_circles_is_off(self):
        circle = self.circle()
        circles.invite(self.nate, circle, "sam")
        NotificationSettings.objects.update_or_create(user=self.sam, defaults={"levels": {"app:circles": "off"}})
        texts = [activity.text(e) for e in activity.notifications(self.sam)]
        self.assertEqual(texts, ["Nathan invited you to join Albright Family"])

    def test_resend_to_an_existing_user_refreshes_the_notification(self):
        circle = self.circle()
        invite, _raw, _s, _r = circles.invite(self.nate, circle, "sam")
        event = self.invite_event(self.sam, invite)
        ActivityEvent.objects.filter(pk=event.pk).update(updated_at=timezone.now() - timedelta(days=1))
        activity.mark_read(self.sam, event)
        self.assertEqual(activity.unread_count(self.sam), 0)
        self.client.post(reverse("labs:circle", args=[circle.pk]), {"action": "resend", "invite": invite.pk})
        event.refresh_from_db()
        self.assertEqual(ActivityEvent.objects.filter(verb="invited", recipient=self.sam).count(), 1)
        self.assertEqual(activity.unread_count(self.sam), 1)


@override_settings(EMAIL_CONFIGURED=True, EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class InviteEmailTests(Base):
    def test_no_mail_goes_to_an_existing_account(self):
        circle = self.circle()
        self.client.post(reverse("labs:circle", args=[circle.pk]), {"action": "invite", "who": "sam"})
        self.assertEqual(len(mail.outbox), 0)
        self.assertTrue(ActivityEvent.objects.filter(verb="invited", recipient=self.sam).exists())
