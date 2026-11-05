"""One at a time: a repeating task with no end shows only its current occurrence (the earliest not done or
skipped) in every view and count, a late one stays on Today as overdue without copies, and checking it off,
skipping, moving or unchecking moves it on (on schedule, or from the completion date). Items with an end date
or count, and events, still show every occurrence. The Bot API follows the same rule (?expand=all lists them
all), and a check-off on a shared task moves it on for everyone."""
import re
from datetime import date, time
from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import Client, TestCase
from django.urls import reverse

from albright_bot_api.models import BotToken
from albright_labs.testing import allow_api_keys, share_with, with_apps

from . import planner
from .models import ItemCompletion, ItemException, ScheduleItem

PASSWORD = "pw-not-real-12345"
MONDAYS = "FREQ=WEEKLY;BYDAY=MO"


def d(month, day, year=2026):
    return date(year, month, day)


def url(name, *args):
    return reverse(f"albright_planner:{name}", args=args)


class Base(TestCase):
    def setUp(self):
        cache.clear()
        clock = mock.patch("albright_planner.planner.today", return_value=d(11, 4))     # a Wednesday
        self.clock = clock.start()
        self.addCleanup(clock.stop)
        outlook = mock.patch("albright_trading_app.views.get_market_outlook", return_value=None)
        outlook.start()
        self.addCleanup(outlook.stop)
        self.me = with_apps(User.objects.create_user(username="nate", password=PASSWORD))
        self.wife = with_apps(User.objects.create_user(username="sam", first_name="Sam", password=PASSWORD),
                              ["planner"])
        self.client.force_login(self.me)

    def on(self, day):
        self.clock.return_value = day

    def task(self, title="Trash", rule=MONDAYS, start=d(11, 2), **fields):
        return ScheduleItem.objects.create(owner=self.me, title=title, date=start, recurrence=rule, **fields)

    def shown(self, item, start, end, user=None, **kw):
        """(date, done) of the item's occurrences between start and end."""
        return [(o.date, o.done) for o in planner.occurrences(user or self.me, start, end, **kw) if o.item.pk == item.pk]

    def current(self, item):
        item.refresh_from_db()
        return planner.current_occurrence(item)

    def done(self, item, day, user=None):
        item.refresh_from_db()
        return planner.toggle(item, day, user or self.me, done=True)

    def count_on_page(self, page, item):
        return len(re.findall(rf'data-item="{item.pk}"', page))


class WhichItemsTests(Base):
    def test_only_open_ended_repeating_tasks(self):
        self.assertTrue(self.task().one_at_a_time)
        self.assertFalse(self.task(recurrence_until=d(12, 31)).one_at_a_time)
        self.assertFalse(self.task(recurrence_count=5).one_at_a_time)
        self.assertFalse(self.task(kind="event", checkable=True).one_at_a_time)
        self.assertFalse(self.task(rule="").one_at_a_time)

    def test_end_date_count_and_events_show_every_occurrence(self):
        until = self.task("Until", recurrence_until=d(11, 30))
        counted = self.task("Counted", recurrence_count=3)
        event = self.task("Class", kind="event", checkable=False)
        mondays = [d(11, 2), d(11, 9), d(11, 16), d(11, 23), d(11, 30)]
        self.assertEqual([x for x, _ in self.shown(until, d(11, 1), d(11, 30))], mondays)
        self.assertEqual([x for x, _ in self.shown(counted, d(11, 1), d(11, 30))], mondays[:3])
        self.assertEqual([x for x, _ in self.shown(event, d(11, 1), d(11, 30))], mondays)
        self.assertEqual(planner.overdue(self.me), [])          # a missed one of these doesn't roll over


class OneShowsTests(Base):
    def test_one_occurrence_in_every_view_and_count(self):
        item = self.task(start=d(11, 9))                      # next Monday
        self.assertEqual(self.shown(item, d(11, 1), d(12, 31)), [(d(11, 9), False)])
        week = self.client.get(url("week") + "?date=2026-11-09").content.decode()
        self.assertEqual(self.count_on_page(week, item), 1)
        later = self.client.get(url("week") + "?date=2026-11-16").content.decode()
        self.assertEqual(self.count_on_page(later, item), 0)
        month = self.client.get(url("month") + "?date=2026-11-01").content.decode()
        self.assertEqual(month.count('<span class="pl-chip__title">Trash</span>'), 1)            # Month's chips
        self.on(d(11, 9))
        day = self.client.get(url("day")).content.decode()
        self.assertEqual(self.count_on_page(day, item), 1)
        todays, overdue = planner.open_tasks_today(self.me)
        self.assertEqual((len(todays), len(overdue)), (1, 0))
        self.assertEqual(len(planner.today_panel(self.me)["tasks"]), 1)

    def test_past_one_is_overdue_on_today_without_copies(self):
        item = self.task(start=d(10, 5))                      # five Mondays ago, never done
        self.on(d(11, 9))                                      # a Monday: no new copy today
        self.assertEqual(self.current(item), d(10, 5))
        self.assertEqual(self.shown(item, d(11, 9), d(11, 9)), [])
        overdue = planner.overdue(self.me)
        self.assertEqual([(o.item.pk, o.date) for o in overdue], [(item.pk, d(10, 5))])
        self.assertEqual(self.shown(item, d(10, 1), d(11, 30)), [(d(10, 5), False)])
        page = self.client.get(url("day")).content.decode()
        self.assertEqual(self.count_on_page(page, item), 1)
        self.assertIn("Overdue &middot; due Mon, Oct 5", page)
        todays, overdue = planner.open_tasks_today(self.me)
        self.assertEqual((len(todays), len(overdue)), (0, 1))
        self.assertEqual(planner.today_panel(self.me)["overdue"], 1)
        self.assertEqual([(o.item.pk, o.date) for o in planner.today_panel(self.me)["tasks"]], [(item.pk, d(10, 5))])

    def test_detail_says_one_at_a_time(self):
        item = self.task(start=d(11, 9))
        page = self.client.get(url("edit", item.pk)).content.decode()
        self.assertIn("Repeats every Mon · next appears after this one is done", page)
        self.assertIn("due Mon Nov 9, 2026", page)
        self.assertIn("Skip this one", page)


class OnScheduleTests(Base):
    def test_on_time_shows_the_next_scheduled_one(self):
        item = self.task()
        self.on(d(11, 2))
        self.assertTrue(self.done(item, d(11, 2)))
        self.assertEqual(self.current(item), d(11, 9))
        self.assertEqual(self.shown(item, d(11, 1), d(11, 30)), [(d(11, 2), True), (d(11, 9), False)])

    def test_late_skips_missed_ones(self):
        item = self.task(start=d(10, 5))
        self.on(d(11, 4))                                      # Wednesday, a month late
        self.done(item, d(10, 5))
        self.assertEqual(self.current(item), d(11, 9))
        self.assertEqual(self.shown(item, d(10, 1), d(11, 30)), [(d(10, 5), True), (d(11, 9), False)])
        self.assertEqual(planner.overdue(self.me), [])

    def test_done_late_on_wednesday_shows_next_monday(self):
        item = self.task()
        self.done(item, d(11, 2))                              # today is Wed Nov 4
        self.assertEqual(self.current(item), d(11, 9))

    def test_done_early_moves_past_its_own_date(self):
        item = self.task(start=d(11, 9))
        self.done(item, d(11, 9))                              # done on Wed Nov 4, before it was due
        self.assertEqual(self.current(item), d(11, 16))


class FromCompletionTests(Base):
    def test_daily(self):
        item = self.task(rule="FREQ=DAILY", start=d(11, 1), repeat_from_completion=True)
        self.assertEqual(self.current(item), d(11, 1))
        self.done(item, d(11, 1))                              # on Nov 4
        item.refresh_from_db()
        self.assertEqual((item.next_due, item.repeat_anchor), (d(11, 5), d(11, 4)))
        self.assertEqual(self.shown(item, d(11, 1), d(11, 30)), [(d(11, 1), True), (d(11, 5), False)])

    def test_every_n_days_chain_continues_from_each_completion(self):
        item = self.task(rule="FREQ=DAILY;INTERVAL=3", start=d(11, 1), repeat_from_completion=True)
        self.on(d(11, 10))
        self.done(item, d(11, 1))
        self.assertEqual(self.current(item), d(11, 13))
        self.on(d(11, 15))
        self.done(item, d(11, 13))
        self.assertEqual(self.current(item), d(11, 18))
        self.assertEqual(ItemCompletion.objects.filter(item=item).count(), 2)

    def test_weekly(self):
        item = self.task(repeat_from_completion=True)
        self.done(item, d(11, 2))                              # on Wed Nov 4
        self.assertEqual(self.current(item), d(11, 11))

    def test_monthly(self):
        item = self.task(rule="FREQ=MONTHLY;BYMONTHDAY=1", start=d(11, 1), repeat_from_completion=True)
        self.done(item, d(11, 1))
        self.assertEqual(self.current(item), d(12, 4))
        self.on(d(1, 31, 2027))
        self.done(item, d(12, 4))
        self.assertEqual(self.current(item), d(2, 28, 2027))

    def test_form_saves_the_setting(self):
        response = self.client.post(url("new"), {
            "title": "Water plants", "kind": "task", "date": "2026-11-04", "priority": "none", "repeat": "every_n_days",
            "day_interval": "3", "ends": "never", "repeat_from_completion": "on"})
        self.assertEqual(response.status_code, 302)
        item = ScheduleItem.objects.get(title="Water plants")
        self.assertEqual((item.recurrence, item.repeat_from_completion), ("FREQ=DAILY;INTERVAL=3", True))
        page = self.client.get(url("edit", item.pk)).content.decode()
        self.assertIn("Repeats every 3 days after it&#x27;s done", page)

    def test_not_kept_for_an_item_that_ends(self):
        self.client.post(url("new"), {"title": "Course", "kind": "task", "date": "2026-11-04", "priority": "none",
                                      "repeat": "daily", "ends": "after", "count": "5", "repeat_from_completion": "on"})
        self.assertFalse(ScheduleItem.objects.get(title="Course").repeat_from_completion)


class OtherActionsTests(Base):
    def skip(self, item, day):
        return self.client.post(url("remove", item.pk), {"date": day.isoformat(), "scope": "this"})

    def test_skip_moves_to_the_next(self):
        item = self.task()                                      # current: Mon Nov 2 (overdue on Wed Nov 4)
        self.skip(item, d(11, 2))
        self.assertEqual(self.current(item), d(11, 9))
        self.assertTrue(ItemException.objects.filter(item=item, occurrence_date=d(11, 2), action="skip").exists())
        self.assertEqual(self.shown(item, d(11, 1), d(11, 30)), [(d(11, 9), False)])
        self.assertEqual(planner.overdue(self.me), [])

    def test_several_skips_in_a_row(self):
        item = self.task()
        for expected in (d(11, 9), d(11, 16), d(11, 23)):
            self.skip(item, self.current(item))
            self.assertEqual(self.current(item), expected)
        self.assertEqual(self.shown(item, d(11, 1), d(11, 30)), [(d(11, 23), False)])

    def test_skip_from_completion_uses_the_skip_date(self):
        item = self.task(rule="FREQ=DAILY;INTERVAL=3", start=d(11, 1), repeat_from_completion=True)
        self.skip(item, d(11, 1))                               # on Nov 4
        self.assertEqual(self.current(item), d(11, 7))

    def test_move_affects_only_this_one(self):
        item = self.task(start=d(11, 9))
        response = self.client.post(url("move", item.pk), data='{"date": "2026-11-09", "to": "2026-11-11", "scope": "this"}',
                                    content_type="application/json")
        self.assertEqual(response.json(), {"ok": True})
        self.assertEqual(self.shown(item, d(11, 1), d(11, 30)), [(d(11, 11), False)])
        self.assertEqual(self.current(item), d(11, 9))           # still that occurrence, shown on Wednesday
        self.on(d(11, 11))
        self.done(item, d(11, 9))
        self.assertEqual(self.current(item), d(11, 16))
        self.assertEqual(self.shown(item, d(11, 1), d(11, 30)), [(d(11, 11), True), (d(11, 16), False)])

    def test_undo_restores_the_previous_one(self):
        item = self.task()
        self.client.post(url("toggle", item.pk), {"date": "2026-11-02"})
        self.assertEqual(self.current(item), d(11, 9))
        self.client.post(url("toggle", item.pk), {"date": "2026-11-02"})
        self.assertEqual(self.current(item), d(11, 2))
        self.assertEqual(self.shown(item, d(11, 1), d(11, 30)), [(d(11, 2), False)])
        self.assertFalse(ItemCompletion.objects.filter(item=item).exists())

    def test_undo_from_completion(self):
        item = self.task(rule="FREQ=DAILY;INTERVAL=3", start=d(11, 1), repeat_from_completion=True)
        self.done(item, d(11, 1))
        self.assertEqual(self.current(item), d(11, 7))
        item.refresh_from_db()
        planner.toggle(item, d(11, 1), self.me, done=False)
        item.refresh_from_db()
        self.assertEqual((self.current(item), item.repeat_anchor), (d(11, 1), None))
        self.assertEqual(self.shown(item, d(11, 1), d(11, 30)), [(d(11, 1), False)])

    def test_a_date_that_isnt_an_occurrence_is_refused(self):
        item = self.task(rule="FREQ=DAILY;INTERVAL=3", start=d(11, 1), repeat_from_completion=True)
        self.done(item, d(11, 1))                                # next: Nov 7
        response = self.client.post(url("toggle", item.pk), {"date": "2026-11-08"},
                                    HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.client.post(url("toggle", item.pk), {"date": "2026-11-07"},
                                          HTTP_X_REQUESTED_WITH="XMLHttpRequest").json(), {"ok": True, "done": True})


class SharedTests(Base):
    def test_a_check_off_moves_it_on_for_everyone(self):
        item = self.task(start=d(11, 9))
        share_with(item, self.wife, "edit")
        wife = Client()
        wife.force_login(self.wife)
        self.assertEqual(self.shown(item, d(11, 1), d(11, 30), user=self.wife), [(d(11, 9), False)])
        wife.post(url("toggle", item.pk), {"date": "2026-11-09"})
        for user in (self.me, self.wife):
            self.assertEqual(self.shown(item, d(11, 1), d(11, 30), user=user), [(d(11, 9), True), (d(11, 16), False)])
        done = [o for o in planner.occurrences(self.me, d(11, 9), d(11, 9)) if o.item.pk == item.pk][0]
        self.assertEqual(done.done_by, self.wife)


class BotApiTests(Base):
    PATH = "/api/bot/v1/schedule"

    def setUp(self):
        super().setUp()
        allow_api_keys(self.me)
        _token, self.raw = BotToken.issue(self.me, "Grok bot", ["schedule:read", "schedule:write"])

    def get(self, query):
        return self.client.get(f"{self.PATH}?{query}", HTTP_AUTHORIZATION=f"Bearer {self.raw}", secure=True)

    def rows(self, item, query="start=2026-11-01&end=2026-11-30"):
        return [(r["occurrence_date"], r["done"]) for r in self.get(query).json()["occurrences"] if r["item_id"] == item.pk]

    def test_default_lists_only_the_current_one(self):
        item = self.task()
        self.assertEqual(self.rows(item), [("2026-11-02", False)])
        row = [r for r in self.get("start=2026-11-01&end=2026-11-30").json()["occurrences"] if r["item_id"] == item.pk][0]
        self.assertTrue(row["one_at_a_time"])

    def test_expand_all_lists_every_scheduled_one(self):
        item = self.task()
        self.assertEqual([r for r, _ in self.rows(item, "start=2026-11-01&end=2026-11-30&expand=all")],
                         ["2026-11-02", "2026-11-09", "2026-11-16", "2026-11-23", "2026-11-30"])
        self.assertEqual(self.get("expand=some").status_code, 400)

    def test_completing_moves_it_on(self):
        item = self.task()
        response = self.client.post(f"{self.PATH}/{item.pk}/complete", data='{"occurrence_date": "2026-11-02"}',
                                    content_type="application/json", HTTP_AUTHORIZATION=f"Bearer {self.raw}", secure=True)
        self.assertEqual(response.json()["done"], True)
        self.assertEqual(self.rows(item), [("2026-11-02", True), ("2026-11-09", False)])
        self.client.post(f"{self.PATH}/{item.pk}/uncomplete", data='{"occurrence_date": "2026-11-02"}',
                         content_type="application/json", HTTP_AUTHORIZATION=f"Bearer {self.raw}", secure=True)
        self.assertEqual(self.rows(item), [("2026-11-02", False)])

    def test_create_from_completion(self):
        response = self.client.post(self.PATH, data='{"title": "Water plants", "date": "2026-11-04", '
                                    '"repeat": "every_n_days", "interval": 3, "repeat_from_completion": true}',
                                    content_type="application/json", HTTP_AUTHORIZATION=f"Bearer {self.raw}", secure=True)
        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertEqual((body["rrule"], body["one_at_a_time"], body["repeat_from_completion"]),
                         ("FREQ=DAILY;INTERVAL=3", True, True))
        bad = self.client.post(self.PATH, data='{"title": "x", "date": "2026-11-04", "repeat": "daily", "count": 3, '
                               '"repeat_from_completion": true}', content_type="application/json",
                               HTTP_AUTHORIZATION=f"Bearer {self.raw}", secure=True)
        self.assertEqual(bad.status_code, 400)

    def test_docs_describe_it(self):
        docs = self.client.get("/api/bot/v1/docs", HTTP_AUTHORIZATION=f"Bearer {self.raw}", secure=True).content.decode()
        self.assertIn("expand=all", docs)
        self.assertIn("ONE AT A TIME", docs)
