"""/briefing/ and the schedule sheet: owner-only, Planner-only, and what they show."""
from datetime import date, time

from django.contrib.auth.models import User
from django.urls import reverse

from albright_labs.testing import with_apps

from . import schedule
from .models import EMAIL, WEEK, Roundup, RoundupIssue
from .tests import PASSWORD, Base

URL = reverse("roundup:briefing")


class BriefingPageTests(Base):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.me)
        self.item("Pickleball", date=date(2026, 11, 24), kind="event", checkable=False, start_time=time(18, 30))

    def test_week_is_made_and_shown(self):
        r = self.client.get(URL + "?scope=week")
        self.assertContains(r, "Pickleball")
        self.assertContains(r, "The short version")
        self.assertContains(r, 'class="rb-strip"')
        issue = RoundupIssue.objects.get()
        self.assertEqual(issue.owner, self.me)
        self.assertIsNotNone(issue.read_at)

    def test_today_and_month_tabs(self):
        self.assertContains(self.client.get(URL + "?scope=today"), 'class="card rb-day"')
        self.assertContains(self.client.get(URL + "?scope=month"), "Pickleball")
        self.assertEqual(RoundupIssue.objects.count(), 2)

    def test_titles_are_escaped(self):
        self.item("<script>alert(1)</script>", date=date(2026, 11, 25))
        r = self.client.get(URL + "?scope=week")
        self.assertNotContains(r, "<script>alert(1)</script>")
        self.assertContains(r, "&lt;script&gt;")

    def test_someone_elses_issue_is_a_404(self):
        theirs = self.client_for(self.wife).get(URL)
        self.assertEqual(theirs.status_code, 200)
        other = RoundupIssue.objects.get(owner=self.wife)
        self.client.force_login(self.me)
        self.assertEqual(self.client.get(URL + f"?issue={other.pk}").status_code, 404)
        self.assertEqual(self.client.get(URL + "?issue=abc").status_code, 404)

    def client_for(self, user):
        self.client.force_login(user)
        return self.client

    def test_needs_sign_in_and_the_planner(self):
        self.client.logout()
        self.assertEqual(self.client.get(URL).status_code, 302)
        plain = with_apps(User.objects.create_user(username="plain", password=PASSWORD), ["meals"])
        self.client.force_login(plain)
        self.assertEqual(self.client.get(URL).status_code, 404)
        self.assertEqual(self.client.post(reverse("roundup:turn_on")).status_code, 404)

    def test_refresh_rebuilds(self):
        self.client.get(URL + "?scope=week")
        self.item("Replace Christmas bulbs", date=date(2026, 11, 28))
        r = self.client.post(reverse("roundup:refresh"), {"scope": "week"})
        self.assertRedirects(r, URL + "?scope=week", fetch_redirect_response=False)
        self.assertIn("Replace Christmas bulbs", RoundupIssue.objects.get().summary)
        self.assertEqual(self.client.post(reverse("roundup:refresh"), {"scope": "year"}).status_code, 404)
        self.assertEqual(self.client.get(reverse("roundup:refresh")).status_code, 405)


class ScheduleSheetTests(Base):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.me)

    def test_save(self):
        rows = {r.scope: r for r in schedule.ensure_presets(self.me)}
        w, t, m = rows["week"], rows["today"], rows["month"]
        r = self.client.post(reverse("roundup:schedule"), {
            f"r{w.pk}_on": "1", f"r{w.pk}_day": "5", f"r{w.pk}_time": "17:30",
            f"r{t.pk}_on": "1", f"r{t.pk}_freq": "daily", f"r{t.pk}_time": "06:00",
            f"r{m.pk}_mday": "40", f"r{m.pk}_time": "nope", "email": "1", "next": "https://evil.example/"})
        self.assertRedirects(r, URL, fetch_redirect_response=False)
        w, t, m = (Roundup.objects.get(pk=x.pk) for x in (w, t, m))
        self.assertEqual((w.enabled, w.weekdays, w.send_time), (True, [5], time(17, 30)))
        self.assertEqual((t.enabled, t.freq, t.send_time), (True, "daily", time(6, 0)))
        self.assertEqual((m.enabled, m.month_day, m.send_time, m.next_run_at), (False, 28, time(8, 0), None))
        self.assertEqual(w.channels, [EMAIL])
        self.assertIsNotNone(w.next_run_at)

    def test_only_my_rows_change(self):
        theirs = schedule.preset(self.wife, WEEK)
        self.client.post(reverse("roundup:schedule"), {f"r{theirs.pk}_on": "1", "push": "1"})
        self.assertFalse(Roundup.objects.get(pk=theirs.pk).enabled)


class TileTests(Base):
    def test_briefing_tile_is_in_the_catalog(self):
        from albright_labs import home_tiles
        self.assertIn("planner.briefing", home_tiles.catalog(self.me))
