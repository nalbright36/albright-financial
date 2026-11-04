"""Albright Planner: occurrence expansion for every repeat type (the 2nd weekday of the month, end dates and
counts), completions per occurrence, "this occurrence / this and following / all" changes, skip and move
exceptions, overdue rollover, quick-add parsing, the Week and Month views, drag-to-move, sharing and 404s, Home's
Today panel, the registry entry, data-app="planner", and the app's stylesheet on every Planner page."""
import json
import re
from datetime import date, time, timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.contrib.staticfiles import finders
from django.core.cache import cache
from django.test import Client, TestCase
from django.urls import reverse

from albright_labs import app_access, apps_registry
from albright_labs.models import UserPreference
from albright_labs.testing import kitchen, launcher_tiles, with_apps

from . import planner, quickadd, recurrence
from .models import ItemCompletion, ItemException, ItemShare, ScheduleItem

PASSWORD = "pw-not-real-12345"
TODAY = date(2026, 11, 4)                       # a Wednesday
OUTLOOK = "albright_trading_app.views.get_market_outlook"


def url(name, *args):
    return reverse(f"albright_planner:{name}", args=args)


def d(month, day, year=2026):
    return date(year, month, day)


class Base(TestCase):
    def setUp(self):
        cache.clear()
        patcher = mock.patch("albright_planner.planner.today", return_value=TODAY)
        patcher.start()
        self.addCleanup(patcher.stop)
        outlook = mock.patch(OUTLOOK, return_value=None)
        outlook.start()
        self.addCleanup(outlook.stop)
        self.me = with_apps(User.objects.create_user(username="nate", password=PASSWORD))
        self.wife = with_apps(User.objects.create_user(username="sam", password=PASSWORD), ["planner"])
        self.stranger = with_apps(User.objects.create_user(username="stranger", password=PASSWORD), ["planner"])
        self.kitchen = kitchen(self.me)
        self.client.force_login(self.me)

    def item(self, title="Item", owner=None, **fields):
        fields.setdefault("date", TODAY)
        return ScheduleItem.objects.create(owner=owner or self.me, title=title, **fields)

    def dates(self, item, start, end):
        return [o.date for o in recurrence.occurrences(item, start, end)]

    def as_user(self, user):
        client = Client()
        client.force_login(user)
        return client


class ExpansionTests(Base):
    def test_every_repeat_type(self):
        cases = [
            (recurrence.build("daily", d(11, 2)), d(11, 2), d(11, 2), d(11, 5), [d(11, 2), d(11, 3), d(11, 4), d(11, 5)]),
            (recurrence.build("weekdays", d(11, 2)), d(11, 6), d(11, 6), d(11, 10), [d(11, 6), d(11, 9), d(11, 10)]),
            (recurrence.build("weekly", d(11, 2), days=["MO", "WE", "FR"]), d(11, 2), d(11, 2), d(11, 13),
             [d(11, 2), d(11, 4), d(11, 6), d(11, 9), d(11, 11), d(11, 13)]),
            (recurrence.build("every_n_weeks", d(11, 3), days=["TU"], interval=2), d(11, 3), d(11, 1), d(12, 5),
             [d(11, 3), d(11, 17), d(12, 1)]),
            (recurrence.build("monthly_day", d(11, 1), month_day=1), d(11, 1), d(11, 1), d(1, 31, 2027),
             [d(11, 1), d(12, 1), d(1, 1, 2027)]),
            (recurrence.build("monthly_nth", d(11, 10), nth=2, nth_weekday="TU"), d(11, 10), d(11, 1), d(1, 31, 2027),
             [d(11, 10), d(12, 8), d(1, 12, 2027)]),                                  # the 2nd Tuesday
            (recurrence.build("monthly_nth", d(11, 27), nth=-1, nth_weekday="FR"), d(11, 27), d(11, 1), d(12, 31),
             [d(11, 27), d(12, 25)]),                                                 # the last Friday
            (recurrence.build("yearly", d(3, 5)), d(3, 5), d(1, 1), d(12, 31, 2028), [d(3, 5), d(3, 5, 2027), d(3, 5, 2028)]),
        ]
        for rule, start, view_start, view_end, expected in cases:
            with self.subTest(rule=rule):
                item = self.item(recurrence=rule, date=start)
                self.assertEqual(self.dates(item, view_start, view_end), expected)

    def test_end_date_and_count(self):
        until = self.item(recurrence="FREQ=DAILY", date=d(11, 2), recurrence_until=d(11, 4))
        self.assertEqual(self.dates(until, d(11, 1), d(11, 30)), [d(11, 2), d(11, 3), d(11, 4)])
        counted = self.item(recurrence="FREQ=WEEKLY;BYDAY=MO", date=d(11, 2), recurrence_count=3)
        self.assertEqual(self.dates(counted, d(11, 1), d(12, 31)), [d(11, 2), d(11, 9), d(11, 16)])
        self.assertEqual(self.dates(counted, d(11, 10), d(12, 31)), [d(11, 16)])     # counted from the start, not the view

    def test_one_off_and_range(self):
        one = self.item(date=d(11, 6))
        self.assertEqual(self.dates(one, d(11, 1), d(11, 30)), [d(11, 6)])
        self.assertEqual(self.dates(one, d(11, 7), d(11, 30)), [])

    def test_choices_round_trip_and_summary(self):
        for repeat, kwargs in (("daily", {}), ("weekdays", {}), ("weekly", {"days": ["MO", "WE", "FR"]}),
                               ("every_n_weeks", {"days": ["TU"], "interval": 3}), ("monthly_day", {"month_day": 15}),
                               ("monthly_nth", {"nth": 2, "nth_weekday": "TU"}), ("yearly", {})):
            with self.subTest(repeat=repeat):
                self.assertEqual(recurrence.describe(recurrence.build(repeat, d(11, 2), **kwargs))["repeat"], repeat)
        self.assertEqual(recurrence.summary("FREQ=WEEKLY;BYDAY=MO,WE,FR", d(11, 2), until=d(12, 31), today=TODAY),
                         "Every Mon, Wed, Fri until Dec 31")
        self.assertEqual(recurrence.summary("FREQ=MONTHLY;BYDAY=2TU", d(11, 10), today=TODAY), "Monthly on the 2nd Tuesday")
        self.assertEqual(recurrence.summary("FREQ=DAILY", d(11, 2), count=10, today=TODAY), "Every day, 10 times")
        self.assertEqual(recurrence.summary("", d(11, 2)), "")


class CompletionTests(Base):
    def test_check_off_records_only_that_occurrence(self):
        item = self.item(recurrence="FREQ=DAILY", date=d(11, 1))
        response = self.client.post(url("toggle", item.pk), {"date": "2026-11-04"}, HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(response.json(), {"ok": True, "done": True})
        self.assertEqual(list(ItemCompletion.objects.values_list("occurrence_date", flat=True)), [d(11, 4)])
        done = {o.date: o.done for o in recurrence.occurrences(item, d(11, 3), d(11, 5))}
        self.assertEqual(done, {d(11, 3): False, d(11, 4): True, d(11, 5): False})
        self.client.post(url("toggle", item.pk), {"date": "2026-11-04"}, HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertFalse(ItemCompletion.objects.exists())                             # tapping again undoes it

    def test_not_scheduled_that_day(self):
        item = self.item(recurrence="FREQ=WEEKLY;BYDAY=MO", date=d(11, 2))
        response = self.client.post(url("toggle", item.pk), {"date": "2026-11-04"}, HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(response.status_code, 400)
        self.assertFalse(ItemCompletion.objects.exists())

    def test_completed_items_are_struck_through(self):
        item = self.item(title="Dishes")
        planner.toggle(item, TODAY, self.me)
        page = self.client.get(url("day")).content.decode()
        self.assertRegex(page, r'<li class="pl-item is-done"[^>]*data-item="%d"' % item.pk)
        self.assertIn('aria-checked="true" aria-label="Done: Dishes"', page)
        self.assertNotIn("Dishes", self.client.get(url("day") + "?done=0").content.decode().split('id="pl-day"')[1])


class ChangeTests(Base):
    def setUp(self):
        super().setUp()
        self.series = self.item(title="Standup", recurrence="FREQ=DAILY", date=d(11, 2), start_time=time(9))

    def titles(self, item_or_items, start, end):
        out = {}
        for item in item_or_items:
            for o in recurrence.occurrences(item, start, end):
                out[o.date] = o.title
        return out

    def test_this_occurrence_only(self):
        planner.change(self.series, d(11, 4), planner.THIS, {"title": "Standup (demo)", "start_time": time(10)})
        exc = ItemException.objects.get()
        self.assertEqual((exc.occurrence_date, exc.action, exc.new_title, exc.new_start_time),
                         (d(11, 4), "move", "Standup (demo)", time(10)))
        occ = {o.date: o for o in recurrence.occurrences(self.series, d(11, 3), d(11, 5))}
        self.assertEqual((occ[d(11, 3)].title, occ[d(11, 4)].title, occ[d(11, 5)].title), ("Standup", "Standup (demo)", "Standup"))
        self.assertEqual(occ[d(11, 4)].start_time, time(10))
        self.assertEqual(ScheduleItem.objects.count(), 1)

    def test_this_and_following(self):
        planner.toggle(self.series, d(11, 3), self.me)
        planner.toggle(self.series, d(11, 6), self.me)
        new = planner.change(self.series, d(11, 5), planner.FOLLOWING, {"title": "Standup v2"})
        self.series.refresh_from_db()
        self.assertEqual(self.series.recurrence_until, d(11, 4))
        self.assertEqual((new.date, new.title, new.recurrence), (d(11, 5), "Standup v2", "FREQ=DAILY"))
        self.assertEqual(self.titles([self.series, new], d(11, 3), d(11, 6)),
                         {d(11, 3): "Standup", d(11, 4): "Standup", d(11, 5): "Standup v2", d(11, 6): "Standup v2"})
        self.assertEqual(list(self.series.completions.values_list("occurrence_date", flat=True)), [d(11, 3)])
        self.assertEqual(list(new.completions.values_list("occurrence_date", flat=True)), [d(11, 6)])

    def test_this_and_following_on_a_counted_series(self):
        counted = self.item(title="Course", recurrence="FREQ=DAILY", date=d(11, 2), recurrence_count=5)
        new = planner.change(counted, d(11, 4), planner.FOLLOWING, {"title": "Course (room B)"})
        counted.refresh_from_db()
        self.assertEqual(self.dates(counted, d(11, 1), d(11, 30)), [d(11, 2), d(11, 3)])
        self.assertEqual(self.dates(new, d(11, 1), d(11, 30)), [d(11, 4), d(11, 5), d(11, 6)])

    def test_all_occurrences(self):
        planner.change(self.series, d(11, 4), planner.ALL, {"title": "Daily standup"})
        self.assertEqual(set(self.titles([self.series], d(11, 2), d(11, 8)).values()), {"Daily standup"})
        weekly = self.item(title="Gym", recurrence="FREQ=WEEKLY;BYDAY=MO", date=d(11, 2))
        planner.change(weekly, d(11, 9), planner.ALL, {"date": d(11, 10)})               # move the series a day later
        weekly.refresh_from_db()
        self.assertEqual((weekly.date, weekly.recurrence), (d(11, 3), "FREQ=WEEKLY;BYDAY=TU"))
        self.assertEqual(self.dates(weekly, d(11, 1), d(11, 15)), [d(11, 3), d(11, 10)])

    def test_skip_and_move_exceptions(self):
        planner.remove(self.series, d(11, 4), planner.THIS)
        self.assertEqual(ItemException.objects.get().action, "skip")
        self.assertNotIn(d(11, 4), self.dates(self.series, d(11, 2), d(11, 6)))
        planner.change(self.series, d(11, 5), planner.THIS, {"date": d(11, 12)})          # moved a week later
        self.assertNotIn(d(11, 5), self.dates(self.series, d(11, 2), d(11, 6)))
        moved = [o for o in recurrence.occurrences(self.series, d(11, 12), d(11, 12)) if o.moved]
        self.assertEqual([(o.date, o.occurrence_date) for o in moved], [(d(11, 12), d(11, 5))])   # moved in from outside the range

    def test_edit_form_with_scope(self):
        response = self.client.post(url("edit", self.series.pk) + "?date=2026-11-04", {
            "occurrence": "2026-11-04", "title": "Standup moved", "kind": "task", "date": "2026-11-04",
            "start_time": "09:30", "repeat": "daily", "ends": "never", "priority": "none", "scope": "this",
            "next": url("day")})
        self.assertRedirects(response, url("day"), fetch_redirect_response=False)
        exc = ItemException.objects.get()
        self.assertEqual((exc.new_title, exc.new_start_time), ("Standup moved", time(9, 30)))
        self.series.refresh_from_db()
        self.assertEqual(self.series.title, "Standup")

    def test_remove_following_and_all(self):
        planner.remove(self.series, d(11, 5), planner.FOLLOWING)
        self.series.refresh_from_db()
        self.assertEqual(self.series.recurrence_until, d(11, 4))
        planner.remove(self.series, d(11, 3), planner.ALL)
        self.assertFalse(ScheduleItem.objects.exists())


class OverdueTests(Base):
    def test_one_off_tasks_roll_into_today_until_done_or_moved(self):
        late = self.item(title="Return library books", date=d(11, 1))
        self.item(title="Old event", date=d(11, 1), kind="event", checkable=False)
        self.item(title="Repeating", date=d(10, 1), recurrence="FREQ=DAILY", recurrence_count=100)   # no end: test_one_at_a_time
        self.assertEqual([o.item for o in planner.overdue(self.me)], [late])
        page = self.client.get(url("day")).content.decode()
        self.assertIn('id="pl-overdue-h"', page)
        self.assertIn("Overdue &middot; Nov 1", page)
        self.assertNotIn('id="pl-overdue-h"', self.client.get(url("day") + "?date=2026-11-05").content.decode())
        planner.toggle(late, d(11, 1), self.me)
        self.assertEqual(planner.overdue(self.me), [])
        planner.toggle(late, d(11, 1), self.me)
        planner.change(late, d(11, 1), planner.ALL, {"date": TODAY})                        # moved to today
        self.assertEqual(planner.overdue(self.me), [])


class QuickAddTests(Base):
    def test_examples(self):
        rent = quickadd.parse("Pay rent every month on the 1st", TODAY)
        self.assertEqual((rent["title"], rent["recurrence"], rent["date"]), ("Pay rent", "FREQ=MONTHLY;BYMONTHDAY=1", d(12, 1)))
        gym = quickadd.parse("Gym Mon Wed Fri 6am", TODAY)
        self.assertEqual((gym["title"], gym["recurrence"], gym["start_time"], gym["date"]),
                         ("Gym", "FREQ=WEEKLY;BYDAY=MO,WE,FR", time(6), TODAY))
        dentist = quickadd.parse("Call dentist tomorrow 3pm", TODAY)
        self.assertEqual((dentist["title"], dentist["date"], dentist["start_time"], dentist["recurrence"]),
                         ("Call dentist", d(11, 5), time(15), ""))

    def test_more_phrases_and_leftovers(self):
        cases = {
            "Book club on the 2nd Tuesday of every month 7pm": ("Book club", "FREQ=MONTHLY;BYDAY=2TU", d(11, 10), time(19)),
            "Trash every Thursday until Dec 31": ("Trash", "FREQ=WEEKLY;BYDAY=TH", d(11, 5), None),
            "Meeting Dec 3 3-4pm": ("Meeting", "", d(12, 3), time(15)),
            "Buy 3 apples": ("Buy 3 apples", "", TODAY, None),
            "Lunch friday at 12": ("Lunch", "", d(11, 6), time(12)),
        }
        for text, (title, rule, day, start) in cases.items():
            with self.subTest(text=text):
                r = quickadd.parse(text, TODAY)
                self.assertEqual((r["title"], r["recurrence"], r["date"], r["start_time"]), (title, rule, day, start))
        self.assertEqual(quickadd.parse("Trash every Thursday until Dec 31", TODAY)["recurrence_until"], d(12, 31))
        self.assertEqual(quickadd.parse("Vitamins daily 10 times", TODAY)["recurrence_count"], 10)

    def test_quick_add_creates_the_item(self):
        response = self.client.post(url("quick_add"), {"text": "Gym Mon Wed Fri 6am", "next": url("week")})
        self.assertRedirects(response, url("week"), fetch_redirect_response=False)
        item = ScheduleItem.objects.get()
        self.assertEqual((item.title, item.owner, item.created_by, item.start_time), ("Gym", self.me, "nate", time(6)))


class ViewTests(Base):
    def test_week_shows_each_item_in_its_column(self):
        self.item(title="Gym", recurrence="FREQ=WEEKLY;BYDAY=MO,WE,FR", date=d(11, 2), start_time=time(6),
                  recurrence_count=100)
        self.item(title="Dentist", date=d(11, 5), start_time=time(15), kind="event", checkable=False)
        self.item(title="Next week thing", date=d(11, 12))
        page = self.client.get(url("week") + "?date=2026-11-04").content.decode()
        cols = re.findall(r'data-drop-date="(\d{4}-\d{2}-\d{2})"', page)
        self.assertEqual(cols, [(d(11, 2) + timedelta(days=i)).isoformat() for i in range(7)])
        by_col = {c: page.split(f'data-drop-date="{c}"', 1)[1].split("</section>", 1)[0] for c in cols}
        for col, titles in (("2026-11-02", ["Gym"]), ("2026-11-04", ["Gym"]), ("2026-11-05", ["Dentist"]),
                            ("2026-11-06", ["Gym"]), ("2026-11-03", [])):
            self.assertEqual(re.findall(r'class="pl-item__title">([^<]+)<', by_col[col]), titles)
        self.assertNotIn("Next week thing", page)
        self.assertIn('draggable="true"', page)
        self.assertIn('id="pl-scope"', page)

    def test_month_grid_with_more_link(self):
        for i in range(5):
            self.item(title=f"Thing {i}", date=d(11, 20))
        self.item(title="Pay rent", recurrence="FREQ=MONTHLY;BYMONTHDAY=1", date=d(11, 1), recurrence_count=12)
        page = self.client.get(url("month") + "?date=2026-11-15").content.decode()
        cell = page.split('data-date="2026-11-20"', 1)[1].split('role="gridcell"', 1)[0]
        self.assertEqual(len(re.findall(r'class="pl-chip[ "]', cell)), 3)
        self.assertIn("+2 more", cell)
        self.assertIn(f'href="{url("day")}?date=2026-11-20"', cell)
        self.assertIn("Pay rent", page.split('data-date="2026-11-01"', 1)[1].split('role="gridcell"', 1)[0])
        self.assertIn("Pay rent", page.split('data-date="2026-12-01"', 1)[1].split('role="gridcell"', 1)[0])   # next month's first week
        self.assertEqual(len(re.findall(r'role="gridcell"', page)), 42)              # Mon Oct 26 - Sun Dec 6: 6 weeks

    def test_day_timeline_and_last_view_remembered(self):
        self.item(title="Breakfast", start_time=time(7, 30), kind="event", checkable=False)
        self.item(title="Groceries")
        page = self.client.get(url("day")).content.decode()
        self.assertIn("Groceries", page.split('id="pl-allday"', 1)[1].split("</ul>", 1)[0])
        self.assertIn("Breakfast", page.split('data-hour="7"', 1)[1].split("</li>\n", 1)[0])
        self.client.get(url("month"))
        self.assertRedirects(self.client.get(url("home")), url("month"), fetch_redirect_response=False)

    def test_filters(self):
        self.item(title="A task")
        self.item(title="An event", kind="event", checkable=False)
        theirs = self.item(title="Shared one", owner=self.wife)
        ItemShare.objects.create(item=theirs, user=self.me)
        body = lambda q: self.client.get(url("day") + q).content.decode().split('id="pl-day"')[1]
        self.assertNotIn("An event", body("?kind=task"))
        self.assertNotIn("A task", body("?kind=event"))
        shared = body("?shared=1")
        self.assertIn("Shared one", shared)
        self.assertNotIn("A task", shared)

    def test_new_item_form_builds_the_rule(self):
        response = self.client.post(url("new"), {
            "title": "Book club", "kind": "event", "date": "2026-11-10", "start_time": "19:00", "end_time": "21:00",
            "repeat": "monthly_nth", "nth": "2", "nth_weekday": "TU", "ends": "after", "count": "6", "priority": "none",
            "next": url("month")})
        self.assertRedirects(response, url("month"), fetch_redirect_response=False)
        item = ScheduleItem.objects.get()
        self.assertEqual((item.recurrence, item.recurrence_count, item.checkable), ("FREQ=MONTHLY;BYDAY=2TU", 6, False))
        page = self.client.get(url("edit", item.pk)).content.decode()
        self.assertIn("Monthly on the 2nd Tuesday, 6 times", page)
        self.assertIn('name="scope"', page)


class MoveTests(Base):
    def post(self, item, body, client=None):
        return (client or self.client).post(url("move", item.pk), data=json.dumps(body), content_type="application/json",
                                            HTTP_X_REQUESTED_WITH="XMLHttpRequest")

    def test_drag_moves_a_one_off(self):
        item = self.item(date=d(11, 4))
        self.assertEqual(self.post(item, {"date": "2026-11-04", "to": "2026-11-06"}).json(), {"ok": True})
        item.refresh_from_db()
        self.assertEqual(item.date, d(11, 6))

    def test_drag_a_repeating_item(self):
        item = self.item(recurrence="FREQ=DAILY", date=d(11, 2))
        self.post(item, {"date": "2026-11-04", "to": "2026-11-07", "scope": "this"})
        exc = ItemException.objects.get()
        self.assertEqual((exc.occurrence_date, exc.new_date, exc.action), (d(11, 4), d(11, 7), "move"))
        self.assertEqual(self.post(item, {"date": "2026-11-04", "to": "bad"}).status_code, 400)
        self.assertEqual(self.post(item, {"date": "2026-11-04", "to": "2026-11-07", "scope": "nope"}).status_code, 400)

    def test_others_cant_move(self):
        item = self.item()
        self.assertEqual(self.post(item, {"date": TODAY.isoformat(), "to": "2026-11-06"}, self.as_user(self.stranger)).status_code, 404)
        ItemShare.objects.create(item=item, user=self.wife, permission="view")
        self.assertEqual(self.post(item, {"date": TODAY.isoformat(), "to": "2026-11-06"}, self.as_user(self.wife)).status_code, 404)


class SharingTests(Base):
    def test_private_items_404_for_others(self):
        item = self.item(title="Private")
        stranger = self.as_user(self.stranger)
        self.assertEqual(stranger.get(url("edit", item.pk)).status_code, 404)
        for name, data in (("toggle", {"date": TODAY.isoformat()}), ("remove", {"date": TODAY.isoformat()}),
                           ("share_add", {"user": self.stranger.pk})):
            with self.subTest(name=name):
                self.assertEqual(stranger.post(url(name, item.pk), data).status_code, 404)
        self.assertNotIn("Private", stranger.get(url("day")).content.decode())
        self.assertTrue(ScheduleItem.objects.filter(pk=item.pk).exists())

    def test_view_share_is_read_only(self):
        item = self.item(title="Family dinner")
        ItemShare.objects.create(item=item, user=self.wife, permission="view")
        wife = self.as_user(self.wife)
        self.assertIn("Family dinner", wife.get(url("day")).content.decode())
        page = wife.get(url("edit", item.pk)).content.decode()
        self.assertIn("(view only)", page)
        self.assertIn("<fieldset class=\"pl-fieldset\" disabled>", page)
        self.assertEqual(wife.post(url("toggle", item.pk), {"date": TODAY.isoformat()}).status_code, 404)
        self.assertEqual(wife.post(url("edit", item.pk), {"title": "x"}).status_code, 404)

    def test_edit_share_can_check_off_and_edit_but_not_delete_or_share(self):
        item = self.item(title="Groceries")
        ItemShare.objects.create(item=item, user=self.wife, permission="edit")
        wife = self.as_user(self.wife)
        self.assertEqual(wife.post(url("toggle", item.pk), {"date": TODAY.isoformat()},
                                   HTTP_X_REQUESTED_WITH="XMLHttpRequest").json()["done"], True)
        self.assertEqual(ItemCompletion.objects.get().completed_by, self.wife)
        wife.post(url("edit", item.pk), {"title": "Groceries + milk", "kind": "task", "date": TODAY.isoformat(),
                                         "repeat": "none", "ends": "never", "priority": "none"})
        item.refresh_from_db()
        self.assertEqual(item.title, "Groceries + milk")
        self.assertEqual(wife.post(url("remove", item.pk), {"scope": "all"}).status_code, 404)
        self.assertEqual(wife.post(url("share_add", item.pk), {"user": self.stranger.pk}).status_code, 404)

    def test_owner_shares_with_planner_users_only(self):
        item = self.item()
        no_planner = with_apps(User.objects.create_user(username="notes_only", password=PASSWORD), ["notes"])
        page = self.client.get(url("edit", item.pk)).content.decode()
        self.assertIn(f'name="share_{self.wife.pk}"', page)                # "Share with" in the form
        self.assertNotIn("notes_only", page)
        self.client.post(url("share_add", item.pk), {"user": no_planner.pk})
        self.assertFalse(ItemShare.objects.exists())
        self.client.post(url("share_add", item.pk), {"user": self.wife.pk, "permission": "edit"})
        self.assertEqual(ItemShare.objects.get().permission, "edit")


class HomeTests(Base):
    def show_panel(self, on=True):
        from albright_labs.models import UserPreference
        UserPreference.objects.filter(user=self.me).update(show_today_panel=on)

    def test_today_panel_is_off_by_default(self):
        self.item(title="Water plants")
        page = self.client.get(reverse("labs:hub")).content.decode()
        self.assertNotIn('id="planner-today"', page)
        self.assertNotIn("planner.css", page)

    def test_today_panel(self):
        self.show_panel()
        self.item(title="Water plants")
        self.item(title="Standup", kind="event", checkable=False, start_time=time(9))
        self.item(title="Late task", date=d(11, 1))
        page = self.client.get(reverse("labs:hub")).content.decode()
        panel = page.split('id="planner-today"', 1)[1].split("</section>", 1)[0]
        self.assertIn("Water plants", panel.split(">Tasks<", 1)[1].split(">Events<", 1)[0])
        self.assertIn("Standup", panel.split(">Events<", 1)[1])
        self.assertIn("Late task", panel.split(">Tasks<", 1)[1].split(">Events<", 1)[0])   # overdue: in the list
        self.assertIn(">2 open today &middot; 1 overdue</p>", panel)
        self.assertIn('class="pl-check-form"', panel)
        self.assertRegex(page, r'<link rel="stylesheet" href="/static/albright_planner/planner\.css\?v=\d+">')

    def test_no_panel_without_planner_access(self):
        self.show_panel()
        with_apps(self.me, ["notes", "meals"])
        self.assertNotIn('id="planner-today"', self.client.get(reverse("labs:hub")).content.decode())

    def test_launcher_and_home_tile_numbers(self):
        from albright_labs import launcher, tiles
        self.item(title="One")
        self.item(title="Late", date=d(11, 1))
        self.assertEqual(launcher.planner(self.me), "2 tasks today")
        self.assertEqual(tiles.planner(self.me)["value"], "2")


class AppEntryTests(Base):
    def test_registry_entry(self):
        app = apps_registry.BY_KEY["planner"]
        self.assertEqual((app.label, app.short_label, app.brand, app.accent, app.color.lower(), app.stylesheet, app.script),
                         ("Planner", "Plan", "Albright Planner", "planner", "#84cc16", "albright_planner/planner.css",
                          "albright_planner/planner.js"))
        self.assertTrue(finders.find(app.stylesheet))
        self.assertTrue(finders.find(app.script))

    def test_every_planner_page_has_data_app_and_the_stylesheet(self):
        item = self.item()
        for path in (url("day"), url("week"), url("month"), url("new"), url("edit", item.pk)):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                page = response.content.decode()
                self.assertIn('<body data-app="planner">', page)
                self.assertEqual(len(re.findall(r'<link rel="stylesheet" href="/static/albright_planner/planner\.css\?v=\d+">', page)), 1)
                self.assertEqual(len(re.findall(r'<script src="/static/albright_planner/planner\.js\?v=\d+"></script>', page)), 1)
                for ref in re.findall(r'(?:href|src)="/static/([^"?]+)', page):
                    self.assertTrue(finders.find(ref), ref)
        self.assertIn(("planner", "Plan"), launcher_tiles(self.client.get(url("day")).content.decode()))

    def test_new_users_have_no_access(self):
        newbie = User.objects.create_user(username="new", password=PASSWORD)
        self.assertFalse(app_access.can_use(newbie, UserPreference.for_user(newbie), "planner"))
        client = Client()
        client.force_login(newbie)
        self.assertEqual(client.get(url("day")).status_code, 404)

    def test_other_apps_get_their_own_stylesheet_from_the_registry(self):
        from albright_notes.models import Note
        note = Note.objects.create(owner=self.me, title="N")
        page = self.client.get(reverse("albright_notes:note", args=[note.pk])).content.decode()
        self.assertEqual(len(re.findall(r'albright_notes/notes\.css', page)), 1)
        self.assertNotIn("albright_planner/planner.css", page)
