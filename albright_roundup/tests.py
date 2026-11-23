"""Roundups: what a briefing says (only what its owner can see), its words, its schedule and its issues."""
from datetime import date, datetime, time, timedelta, timezone as dt_tz
from unittest import mock
from zoneinfo import ZoneInfo

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase

from albright_labs.testing import share_with, with_apps
from albright_planner.models import PlannerSettings, ScheduleItem
from albright_planner.weather import Weather

from . import build, schedule, service
from .models import MONTH, TODAY, WEEK, Roundup, RoundupIssue

SUNDAY = date(2026, 11, 22)
NY = ZoneInfo("America/New_York")
PASSWORD = "pw-roundup-1"


def wx(day, high=80, low=65, icon="clear", pop=10):
    return Weather(day, high, low, "Clear", icon, pop)


class Base(TestCase):
    def setUp(self):
        cache.clear()
        p = mock.patch("albright_planner.planner.today", return_value=SUNDAY)
        p.start()
        self.addCleanup(p.stop)
        self.wx = {SUNDAY + timedelta(days=n): wx(SUNDAY + timedelta(days=n)) for n in range(1, 8)}
        self.wx[date(2026, 11, 25)] = wx(date(2026, 11, 25), 74, 62, "rain", 60)
        w = mock.patch("albright_planner.weather.attach", side_effect=lambda user, occs, days: dict(self.wx))
        w.start()
        self.addCleanup(w.stop)
        self.me = with_apps(User.objects.create_user(username="nathan", password=PASSWORD))
        self.wife = with_apps(User.objects.create_user(username="lexi", first_name="Lexi", password=PASSWORD))
        self.stranger = with_apps(User.objects.create_user(username="stranger", password=PASSWORD), ["planner"])

    def item(self, title, owner=None, **fields):
        fields.setdefault("date", SUNDAY + timedelta(days=1))
        return ScheduleItem.objects.create(owner=owner or self.me, title=title, **fields)

    def week(self, user=None, sections=None):
        return build.build(user or self.me, WEEK, date(2026, 11, 23), date(2026, 11, 29), sections)


class BuildTests(Base):
    def setUp(self):
        super().setUp()
        self.item("Pickleball", date=date(2026, 11, 24), kind="event", checkable=False, start_time=time(18, 30))
        self.item("Replace Christmas bulbs", date=date(2026, 11, 28))
        self.item("Walk Brutus", date=date(2026, 11, 23), recurrence="FREQ=DAILY", start_time=time(7, 0))
        self.item("Renew car registration", date=date(2026, 11, 20))

    def test_days_items_and_counts(self):
        p = self.week()
        self.assertEqual([d["dow"] for d in p["days"]], ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"])
        tue = p["days"][1]
        self.assertEqual([(r["title"], r["time"], r["kind"]) for r in tue["items"]], [("Pickleball", "6:30 PM", "event")])
        self.assertEqual(p["counts"], {"events": 1, "tasks": 1, "workouts": 0, "overdue": 1})
        self.assertEqual(p["range"], "Mon Nov 23 – Sun Nov 29")
        self.assertEqual(p["title"], "Your week ahead")

    def test_daily_repeat_is_one_line(self):
        p = self.week()
        self.assertEqual([r["title"] for r in p["repeats"]], ["Walk Brutus"])
        self.assertNotIn("Walk Brutus", [r["title"] for d in p["days"] for r in d["items"]])

    def test_daily_repeat_stays_in_a_one_day_roundup(self):
        p = build.build(self.me, TODAY, date(2026, 11, 23), date(2026, 11, 23))
        self.assertEqual(p["repeats"], [])
        self.assertIn("Walk Brutus", [r["title"] for r in p["days"][0]["items"]])

    def test_overdue_and_weather(self):
        p = self.week()
        self.assertEqual(p["overdue"][0]["title"], "Renew car registration")
        self.assertEqual(p["overdue"][0]["due"], "Fri Nov 20")
        self.assertEqual(p["days"][2]["weather"]["icon"], "rain")
        self.assertEqual(p["rain"], ["Wed"])
        self.assertEqual(p["temps"], [62, 80])

    def test_sections_off(self):
        p = self.week(sections=["days"])
        self.assertEqual(p["overdue"], [])
        self.assertEqual(p["repeats"], [])
        self.assertIsNone(p["days"][0]["weather"])
        self.assertIsNone(p["money"])
        self.assertIsNone(p["shared"])

    def test_summary_and_push(self):
        p = self.week()
        text = build.summary(p)
        self.assertIn("Coming up: Pickleball (Tue 6:30 PM).", text)
        self.assertIn("To do: Replace Christmas bulbs (Sat).", text)
        self.assertIn("Every day: Walk Brutus.", text)
        self.assertIn("Rain likely Wed.", text)
        self.assertIn("1 overdue: Renew car registration.", text)
        self.assertEqual(build.push_text(p), "Pickleball Tue 6:30 PM · 1 overdue · Rain Wed")

    def test_empty_week(self):
        p = self.week(user=self.stranger)
        self.assertTrue(p["empty"])
        self.assertEqual(build.summary(p), "Nothing on the calendar. Rain likely Wed.")


class PrivacyTests(Base):
    def test_only_what_the_owner_can_see(self):
        shared = self.item("Christmas tree pickup", owner=self.wife, kind="event", checkable=False,
                           date=date(2026, 11, 27), start_time=time(10, 0))
        share_with(shared, self.me, "view")
        self.item("Lexi's private dentist", owner=self.wife, date=date(2026, 11, 27))
        self.item("Stranger's thing", owner=self.stranger, date=date(2026, 11, 27))
        self.item("Stranger overdue", owner=self.stranger, date=date(2026, 11, 20))
        p = self.week()
        titles = [r["title"] for d in p["days"] for r in d["items"]]
        self.assertEqual(titles, ["Christmas tree pickup"])
        self.assertEqual(p["days"][4]["items"][0]["by"], "Lexi")
        self.assertEqual(p["overdue"], [])

    def test_apps_they_cant_open_are_left_out(self):
        self.item("Leg day", source_app="fitness", source_id="session-1", date=date(2026, 11, 27))
        with mock.patch("albright_roundup.build._money", return_value={"left": "10", "text": "x"}) as money:
            p = self.week(user=self.stranger)
        money.assert_not_called()
        self.assertIsNone(p["money"])
        self.assertIsNone(p["groceries"])
        self.assertEqual(self.week()["counts"]["workouts"], 1)
        self.assertEqual(self.week(sections=["days"])["counts"]["workouts"], 0)


class ScheduleTests(Base):
    def at(self, y, m, d, h, mi=0):
        return datetime(y, m, d, h, mi, tzinfo=NY)

    def test_periods(self):
        self.assertEqual(schedule.period(WEEK, self.at(2026, 11, 22, 18)), (date(2026, 11, 23), date(2026, 11, 29)))
        self.assertEqual(schedule.period(TODAY, self.at(2026, 11, 23, 7)), (date(2026, 11, 23), date(2026, 11, 23)))
        self.assertEqual(schedule.period(TODAY, self.at(2026, 11, 23, 21)), (date(2026, 11, 24), date(2026, 11, 24)))
        self.assertEqual(schedule.period(MONTH, self.at(2026, 12, 1, 8)), (date(2026, 12, 1), date(2026, 12, 31)))
        self.assertEqual(schedule.period(MONTH, self.at(2027, 2, 1, 8)), (date(2027, 2, 1), date(2027, 2, 28)))

    def roundup(self, **f):
        defaults = dict(owner=self.me, name="R", scope=WEEK, freq="weekly", weekdays=[6], send_time=time(18, 0))
        defaults.update(f)
        return Roundup.objects.create(**defaults)

    def test_next_run_weekly_weekdays_monthly(self):
        r = self.roundup()
        self.assertEqual(schedule.next_run(r, self.at(2026, 11, 22, 18, 1)), self.at(2026, 11, 29, 18))
        self.assertEqual(schedule.next_run(r, self.at(2026, 11, 22, 17, 59)), self.at(2026, 11, 22, 18))
        w = self.roundup(freq="weekdays", send_time=time(7, 0))
        self.assertEqual(schedule.next_run(w, self.at(2026, 11, 27, 8)), self.at(2026, 11, 30, 7))
        m = self.roundup(freq="monthly", month_day=1, send_time=time(8, 0))
        self.assertEqual(schedule.next_run(m, self.at(2026, 11, 22, 8)), self.at(2026, 12, 1, 8))

    def test_next_run_follows_their_zone_and_dst(self):
        r = self.roundup()
        after = datetime(2027, 3, 13, 12, tzinfo=dt_tz.utc)
        self.assertEqual(schedule.next_run(r, after), datetime(2027, 3, 14, 22, tzinfo=dt_tz.utc))   # 6 PM EDT
        PlannerSettings.objects.update_or_create(user=self.me, defaults={"timezone": "America/Los_Angeles"})
        cache.clear()
        r = Roundup.objects.get(pk=r.pk)
        self.assertEqual(schedule.next_run(r, after), datetime(2027, 3, 15, 1, tzinfo=dt_tz.utc))    # 6 PM PDT

    def test_presets_and_words(self):
        rows = schedule.ensure_presets(self.me)
        self.assertEqual([(r.name, r.scope, r.enabled) for r in rows],
                         [("Week ahead", WEEK, False), ("Today", TODAY, False), ("Month ahead", MONTH, False)])
        self.assertEqual(len(schedule.ensure_presets(self.me)), 3)
        self.assertEqual([schedule.when_text(r) for r in rows], ["Sun 6 PM", "Weekdays 7 AM", "1st, 8 AM"])
        self.assertEqual(schedule.clock(time(18, 30)), "6:30 PM")


class IssueTests(Base):
    def test_issue_is_made_once_and_rebuilt_on_request(self):
        r = schedule.preset(self.me, WEEK)
        when = datetime(2026, 11, 22, 23, 5, tzinfo=dt_tz.utc)               # Sun 6:05 PM in New York
        first = service.issue_for(r, now=when)
        self.assertEqual((first.period_start, first.period_end), (date(2026, 11, 23), date(2026, 11, 29)))
        self.assertEqual(first.summary_source, "rules")
        self.item("Pickleball", date=date(2026, 11, 24), kind="event", checkable=False)
        self.assertEqual(service.issue_for(r, now=when).pk, first.pk)
        again = service.issue_for(r, now=when, rebuild=True)
        self.assertEqual(again.pk, first.pk)
        self.assertIn("Pickleball", again.summary)
        self.assertEqual(RoundupIssue.objects.count(), 1)

    def test_current_is_owner_only(self):
        mine = service.issue_for(schedule.preset(self.me, WEEK))
        self.assertEqual(service.current(self.me), mine)
        self.assertIsNone(service.current(self.wife))
        self.assertEqual(service.history(self.wife), [])

    def test_needs_the_planner(self):
        plain = with_apps(User.objects.create_user(username="plain", password=PASSWORD), ["meals"])
        self.assertTrue(service.can_use(self.me))
        self.assertFalse(service.can_use(plain))
