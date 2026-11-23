"""A daily (one-at-a-time) task never loses today's occurrence: checking off or skipping an overdue one today - or
after midnight from a page opened the day before - leaves today's showing. Only today's own (or an early one)
moves past today."""
from datetime import date, time

from django.urls import reverse

from . import planner
from .models import PlannerSettings
from .test_one_at_a_time import Base, d

DAILY = "FREQ=DAILY"


class DailyNoSkipTests(Base):
    # Base: today is Wed Nov 4.
    def test_overdue_checked_today_leaves_today(self):
        item = self.task(rule=DAILY, start=d(11, 3))                 # yesterday's still open
        self.done(item, d(11, 3))
        self.assertEqual(self.current(item), d(11, 4))
        self.assertEqual(self.shown(item, d(11, 4), d(11, 4)), [(d(11, 4), False)])
        self.assertEqual(len(planner.today_panel(self.me)["tasks"]), 1)

    def test_checked_after_midnight_from_yesterdays_page(self):
        item = self.task(rule=DAILY, start=d(11, 1))
        self.on(d(11, 3))
        self.done(item, d(11, 1))                                    # caught up: Nov 3 is current
        self.assertEqual(self.current(item), d(11, 3))
        self.on(d(11, 4))                                            # 12:10 AM, tapping Nov 3 on last night's page
        self.client.post(reverse("albright_planner:toggle", args=[item.pk]), {"date": "2026-11-03", "checked": "1"})
        self.assertEqual(self.current(item), d(11, 4))
        self.assertEqual(self.shown(item, d(11, 3), d(11, 5)), [(d(11, 3), True), (d(11, 4), False)])

    def test_today_checked_moves_to_tomorrow(self):
        item = self.task(rule=DAILY, start=d(11, 4))
        self.done(item, d(11, 4))
        self.assertEqual(self.current(item), d(11, 5))

    def test_a_week_late_lands_on_today_not_tomorrow(self):
        item = self.task(rule=DAILY, start=d(10, 28))
        self.done(item, d(10, 28))
        self.assertEqual(self.current(item), d(11, 4))

    def test_skipping_an_overdue_one_leaves_today(self):
        item = self.task(rule=DAILY, start=d(11, 3))
        planner.remove(item, d(11, 3), planner.THIS)
        self.assertEqual(self.current(item), d(11, 4))
        planner.remove(item, d(11, 4), planner.THIS)                 # skipping today's own: on to tomorrow
        self.assertEqual(self.current(item), d(11, 5))

    def test_weekdays_friday_done_on_monday_keeps_monday(self):
        self.on(d(11, 9))                                            # Monday
        item = self.task(rule="FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR", start=d(11, 6))
        self.done(item, d(11, 6))
        self.assertEqual(self.current(item), d(11, 9))

    def test_weekly_late_still_goes_to_next_week(self):
        item = self.task()                                           # Mondays from Nov 2; done Wed Nov 4
        self.done(item, d(11, 2))
        self.assertEqual(self.current(item), d(11, 9))

    def test_late_evening_item_from_another_zone(self):
        # 11:30 PM Los Angeles shows at 2:30 AM the next day in New York: checking off the one on today's list
        # (Nov 3's, shown Wed Nov 4) must leave Nov 4's for Thursday - not jump to Friday.
        item = self.task(rule=DAILY, start=d(11, 3), start_time=time(23, 30), timezone="America/Los_Angeles")
        PlannerSettings.objects.update_or_create(user=self.me, defaults={"timezone": "America/New_York"})
        self.assertEqual(self.shown(item, d(11, 4), d(11, 4)), [(d(11, 4), False)])
        self.done(item, d(11, 3))
        self.assertEqual(self.current(item), d(11, 4))
        self.assertEqual(self.shown(item, d(11, 5), d(11, 5)), [(d(11, 5), False)])

    def test_from_completion_unchanged(self):
        item = self.task(rule=DAILY, start=d(11, 3), repeat_from_completion=True)
        self.done(item, d(11, 3))
        self.assertEqual(self.current(item), d(11, 5))               # "every day after it's done": done Nov 4
