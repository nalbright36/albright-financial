"""Roundups going out: due ones are made once, the owner (only) is pinged, late/quiet/empty rules, Home catch-up."""
from datetime import date, datetime, time, timedelta, timezone as dt_tz
from io import StringIO
from unittest import mock

from django.core import mail
from django.core.management import call_command
from django.test import override_settings
from django.urls import reverse

from albright_labs.models import PushSubscription

from . import deliver, schedule
from .models import EMAIL, PUSH, WEEK, Roundup, RoundupIssue
from .tests import PASSWORD, Base

SIX_PM = datetime(2026, 11, 22, 23, 0, tzinfo=dt_tz.utc)   # Sun Nov 22, 6:00 PM in New York


@mock.patch("albright_labs.push.vapid_enabled", return_value=True)
@mock.patch("albright_labs.push.send_to")
class DeliverTests(Base):
    def setUp(self):
        super().setUp()
        self.r = schedule.preset(self.me, WEEK)
        self.r.enabled = True
        self.r.next_run_at = SIX_PM
        self.r.save()
        self.item("Pickleball", date=date(2026, 11, 24), kind="event", checkable=False, start_time=time(18, 30))
        self.mine = PushSubscription.objects.create(user=self.me, endpoint="https://push.example/me", p256dh="k", auth="a")
        PushSubscription.objects.create(user=self.wife, endpoint="https://push.example/lexi", p256dh="k", auth="a")

    def test_due_is_made_once_and_only_i_am_pinged(self, send_to, _v):
        self.assertEqual(deliver.run_due(SIX_PM + timedelta(minutes=3)), (1, 1))
        issue = RoundupIssue.objects.get(roundup=self.r)
        self.assertEqual((issue.period_start, issue.period_end), (date(2026, 11, 23), date(2026, 11, 29)))
        send_to.assert_called_once()
        sub, kw = send_to.call_args.args[0], send_to.call_args.kwargs
        self.assertEqual(sub, self.mine)
        self.assertEqual(kw["url"], reverse("roundup:briefing") + f"?issue={issue.pk}")
        self.assertEqual(kw["tag"], f"roundup-{self.r.pk}")
        self.assertIn("Pickleball", kw["body"])
        self.assertEqual(issue.delivered, {"push": 1})
        self.r.refresh_from_db()
        self.assertEqual(self.r.next_run_at, datetime(2026, 11, 29, 23, 0, tzinfo=dt_tz.utc))
        self.assertIsNotNone(self.r.last_sent_at)
        self.assertEqual(deliver.run_due(SIX_PM + timedelta(minutes=8)), (0, 0))   # nothing due now
        send_to.assert_called_once()

    def test_a_second_run_cant_claim_it(self, send_to, _v):
        stale = Roundup.objects.get(pk=self.r.pk)
        self.assertTrue(deliver._claim(self.r, SIX_PM))
        self.assertFalse(deliver._claim(stale, SIX_PM))

    def test_late_is_made_without_a_ping(self, send_to, _v):
        self.assertEqual(deliver.run_due(SIX_PM + timedelta(hours=5)), (1, 0))
        send_to.assert_not_called()
        self.assertEqual(deliver.run_due(SIX_PM + timedelta(hours=5, minutes=5)), (0, 0))

    def test_quiet_hours_no_push_but_email(self, send_to, _v):
        self.me.email = "nathan@example.com"
        self.me.save()
        self.r.channels = [PUSH, EMAIL]
        self.r.save()
        with mock.patch("albright_labs.activity.in_quiet_hours", return_value=True), \
                override_settings(EMAIL_CONFIGURED=True, EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend"):
            deliver.run_due(SIX_PM)
        send_to.assert_not_called()
        self.assertEqual(len(mail.outbox), 1)

    @override_settings(EMAIL_CONFIGURED=True, EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
    def test_email_is_mine_only(self, send_to, _v):
        self.me.email = "nathan@example.com"
        self.me.save()
        self.r.channels = [EMAIL]
        self.r.save()
        self.item("Lexi's book club", owner=self.wife, date=date(2026, 11, 25))
        deliver.run_due(SIX_PM)
        send_to.assert_not_called()
        [m] = mail.outbox
        self.assertEqual(m.to, ["nathan@example.com"])
        self.assertEqual(m.subject, "Your week ahead: Mon Nov 23 – Sun Nov 29")
        self.assertIn("Pickleball", m.body)
        self.assertNotIn("book club", m.body + m.alternatives[0][0])

    def test_skip_if_empty(self, send_to, _v):
        self.r.skip_if_empty = True
        self.r.save()
        from albright_planner.models import ScheduleItem
        ScheduleItem.objects.filter(owner=self.me).delete()
        deliver.run_due(SIX_PM)
        send_to.assert_not_called()
        self.assertTrue(RoundupIssue.objects.filter(roundup=self.r).exists())

    def test_no_planner_no_ping(self, send_to, _v):
        from albright_labs.testing import with_apps
        with_apps(self.me, ["meals"])
        deliver.run_due(SIX_PM)
        send_to.assert_not_called()
        self.assertFalse(RoundupIssue.objects.exists())

    def test_off_is_never_made(self, send_to, _v):
        Roundup.objects.filter(pk=self.r.pk).update(enabled=False)
        self.assertEqual(deliver.run_due(SIX_PM), (0, 0))

    def test_home_catches_up_without_a_ping(self, send_to, _v):
        self.client.login(username="nathan", password=PASSWORD)
        with mock.patch("django.utils.timezone.now", return_value=SIX_PM + timedelta(minutes=5)):
            self.client.get(reverse("labs:hub"))
        self.assertFalse(RoundupIssue.objects.filter(period_start=date(2026, 11, 23)).exists())   # inside the grace
        with mock.patch("django.utils.timezone.now", return_value=SIX_PM + timedelta(minutes=20)):
            r = self.client.get(reverse("labs:hub"))
        self.assertTrue(RoundupIssue.objects.filter(period_start=date(2026, 11, 23)).exists())
        self.assertContains(r, "Your week ahead")
        send_to.assert_not_called()

    def test_command(self, send_to, _v):
        out = StringIO()
        with mock.patch("django.utils.timezone.now", return_value=SIX_PM):
            call_command("send_roundups", stdout=out)
        self.assertIn("Made 1 briefing(s); pinged 1.", out.getvalue())
