"""Weigh-in schedules (weigh_in.py): each frequency's dates (weekly Mon & Thu, every 3 days, monthly on the 31st in
a 30-day month, the end date) in the person's zone; the one private, yellow Fitness "Weigh-in" task per schedule;
logging a weight from the task (one entry, checked off, replace not duplicate), from the Body page and the Bot API
(checks off that day's task), Skip today, unchecking (the weight stays), and edit / pause / delete changing only
future, not-completed weigh-ins."""
from datetime import date, time, timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from albright_bot_api.models import BotToken
from albright_labs import sharing
from albright_labs.testing import allow_api_keys, grants_of, with_apps
from albright_planner import planner
from albright_planner.models import ItemCompletion, PlannerSettings, ScheduleItem

from . import body, weigh_in
from .models import WeighInLog, WeighInSchedule, WeightEntry

PASSWORD = "pw-not-real-12345"
API = "/api/bot/v1/"
ZONE = "America/Los_Angeles"
AJAX = {"HTTP_X_REQUESTED_WITH": "XMLHttpRequest"}


def url(name, *args):
    return reverse(f"albright_fitness:{name}", args=args)


def plan(**fields):
    """An unsaved schedule for working out dates."""
    defaults = {"frequency": "weekly", "days_of_week": [], "start_date": date(2026, 11, 16), "time_of_day": time(7)}
    return WeighInSchedule(**{**defaults, **fields})


class DateTests(TestCase):
    def test_weekly_on_mon_and_thu(self):
        s = plan(frequency="weekly", days_of_week=[0, 3])                        # Nov 16 2026 is a Monday
        self.assertEqual(weigh_in.dates(s, date(2026, 11, 16), 4),
                         [date(2026, 11, 16), date(2026, 11, 19), date(2026, 11, 23), date(2026, 11, 26)])
        self.assertEqual(weigh_in.describe(s), "Every Mon & Thu at 7:00 AM")

    def test_every_3_days_from_the_start(self):
        s = plan(frequency="every_n_days", interval_n=3, start_date=date(2026, 11, 16))
        self.assertEqual(weigh_in.dates(s, date(2026, 11, 18), 3), [date(2026, 11, 19), date(2026, 11, 22), date(2026, 11, 25)])
        self.assertEqual(weigh_in.describe(s), "Every 3 days at 7:00 AM")

    def test_monthly_on_the_31st_uses_a_30_day_months_last_day(self):
        s = plan(frequency="monthly", day_of_month=31, start_date=date(2027, 1, 1))
        self.assertEqual(weigh_in.dates(s, date(2027, 1, 1), 4),
                         [date(2027, 1, 31), date(2027, 2, 28), date(2027, 3, 31), date(2027, 4, 30)])
        thirty = plan(frequency="monthly", day_of_month=30, start_date=date(2027, 1, 1))
        self.assertEqual(weigh_in.dates(thirty, date(2027, 1, 1), 3), [date(2027, 1, 30), date(2027, 2, 28), date(2027, 3, 30)])
        self.assertIn("(or the last day)", weigh_in.describe(s))

    def test_daily_respects_the_end_date(self):
        s = plan(frequency="daily", start_date=date(2026, 11, 16), end_date=date(2026, 11, 18), time_of_day=None)
        self.assertEqual(weigh_in.dates(s, date(2026, 11, 1), 10), [date(2026, 11, 16), date(2026, 11, 17), date(2026, 11, 18)])
        self.assertEqual(weigh_in.describe(s), "Every day until Nov 18, 2026")


@override_settings(BOT_API_REQUIRE_HTTPS=False)
class Base(TestCase):
    def setUp(self):
        cache.clear()
        outlook = mock.patch("albright_trading_app.views.get_market_outlook", return_value=None)
        outlook.start()
        self.addCleanup(outlook.stop)
        self.me = allow_api_keys(with_apps(User.objects.create_user(username="nate", password=PASSWORD, first_name="Nate"),
                                           ["fitness", "planner"]))
        PlannerSettings.objects.filter(pk=PlannerSettings.for_user(self.me).pk).update(timezone=ZONE)
        self.client.force_login(self.me)
        self.today = planner.today(self.me)

    def create(self, **fields):
        data = {"frequency": "daily", "time_of_day": "07:00", "mode": "new", **fields}
        response = self.client.post(url("weigh_in_save"), data)
        self.assertEqual(response.status_code, 302, getattr(response, "content", b"")[:500])
        return WeighInSchedule.objects.get(owner=self.me, active=True)

    def task(self, schedule):
        schedule.refresh_from_db()
        return schedule.planner_item

    def log(self, item, day, weight, **extra):
        return self.client.post(url("weigh_in_log", item.pk), {"date": day.isoformat(), "weight": weight, **extra}, **AJAX)


class TaskTests(Base):
    def test_a_private_yellow_fitness_task_linked_to_the_schedule_in_my_zone(self):
        schedule = self.create(frequency="weekly", days_of_week=[0, 3])
        self.assertEqual(schedule.start_date, self.today)                      # default: today, in my zone
        item = self.task(schedule)
        self.assertEqual((item.title, item.source_app, item.source_id, item.kind, item.timezone),
                         ("Weigh-in", "fitness", f"weighin-{schedule.pk}", "task", ZONE))
        self.assertEqual((item.recurrence, item.start_time), ("FREQ=WEEKLY;BYDAY=MO,TH", time(7)))
        self.assertTrue(item.is_weigh_in and item.one_at_a_time)
        self.assertEqual(item.source_url, url("body") + "#fx-weigh-in-schedule")
        self.assertEqual(grants_of(item), [])
        friend = with_apps(User.objects.create_user(username="kim", password=PASSWORD), ["planner"])
        with self.assertRaises(sharing.ShareError):
            sharing.share(self.me, item, who=friend.username)

    def test_shows_yellow_with_log_weight_on_the_planner_and_today(self):
        schedule = self.create()
        page = self.client.get(reverse("albright_planner:day")).content.decode()
        self.assertIn("is-fitness", page)
        self.assertIn("Log weight", page)
        self.assertIn(f'action="{url("weigh_in_log", self.task(schedule).pk)}"', page)
        self.assertNotIn('data-log-url', page.split("Weigh-in", 1)[0][-1500:])      # not a "Log this workout?" task
        panel = planner.today_panel(self.me)
        self.assertEqual([o.item.title for o in panel["workouts"]], ["Weigh-in"])
        self.assertTrue(panel["weigh_ins_only"])

    def test_the_body_card_says_it_in_plain_words(self):
        self.create(frequency="weekly", days_of_week=[0, 3])
        page = self.client.get(url("body")).content.decode()
        self.assertIn("Every Mon &amp; Thu at 7:00 AM", page)
        self.assertIn('id="fx-wis-pause"', page)
        self.assertIn('id="fx-wis-delete"', page)

    def test_invalid_form_shows_errors_next_to_the_fields(self):
        response = self.client.post(url("weigh_in_save"), {"frequency": "weekly", "mode": "new"})
        self.assertEqual(response.status_code, 400)
        self.assertContains(response, 'id="fx-wis-days-error"', status_code=400)
        self.assertFalse(WeighInSchedule.objects.exists())

    def test_preview_lists_the_next_three_dates(self):
        data = self.client.get(url("weigh_in_preview"), {"frequency": "every_n_days", "interval_n": "3",
                                                         "start_date": "2026-11-16", "time_of_day": "07:00"}).json()
        self.assertEqual(len(data["dates"]), 3)
        self.assertIn("at 7:00 AM", data["dates"][0])

    def test_a_new_schedule_replaces_the_old_one(self):
        first = self.create(frequency="daily")
        second = self.create(frequency="weekly", days_of_week=[2])
        self.assertFalse(WeighInSchedule.objects.filter(pk=first.pk).exists())
        self.assertEqual(WeighInSchedule.objects.filter(owner=self.me).count(), 1)
        self.assertEqual(ScheduleItem.objects.filter(owner=self.me, source_id__startswith="weighin-").count(), 1)
        self.assertEqual(self.task(second).recurrence, "FREQ=WEEKLY;BYDAY=WE")

    def test_planner_edit_goes_to_the_schedule(self):
        item = self.task(self.create())
        response = self.client.get(reverse("albright_planner:edit", args=[item.pk]))
        self.assertRedirects(response, item.source_url, fetch_redirect_response=False)


class LogTests(Base):
    def test_logging_from_the_task_makes_one_entry_and_checks_it_off(self):
        body.log_weight(self.me, self.today - timedelta(days=3), Decimal("182.4"), "lb")
        item = self.task(self.create())
        data = self.log(item, self.today, "182.0").json()
        self.assertEqual(data["result"], "182.0 lb (−0.4 since last)")
        self.assertEqual(WeightEntry.objects.filter(owner=self.me, date=self.today).count(), 1)
        self.assertTrue(ItemCompletion.objects.filter(item=item, occurrence_date=self.today).exists())
        self.assertEqual(WeighInLog.objects.get(item=item).weight_entry.date, self.today)
        page = self.client.get(reverse("albright_planner:day")).content.decode()
        self.assertIn("182.0 lb (−0.4 since last)", page)

    def test_a_weight_already_logged_that_day_is_replaced_after_asking(self):
        body.log_weight(self.me, self.today, Decimal("182.4"), "lb")        # (checks off the task - reopen it)
        item = self.task(self.create())
        ItemCompletion.objects.filter(item=item).delete()
        response = self.log(item, self.today, "181.6")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["confirm"], "Replace today's 182.4 lb?")
        self.log(item, self.today, "181.6", replace="1")
        entries = WeightEntry.objects.filter(owner=self.me, date=self.today)
        self.assertEqual(entries.count(), 1)
        self.assertEqual(body.shown(entries.get().weight_kg, "lb"), Decimal("181.6"))

    def test_same_limits_as_the_body_page(self):
        item = self.task(self.create())
        self.assertEqual(self.log(item, self.today, "12").status_code, 400)
        self.assertEqual(self.log(item, self.today, "abc").status_code, 400)
        self.assertFalse(WeightEntry.objects.exists())
        self.log(item, self.today, "180.26")
        self.assertEqual(body.shown(WeightEntry.objects.get().weight_kg, "lb"), Decimal("180.3"))   # one decimal

    def test_logging_on_the_body_page_checks_off_that_days_task(self):
        item = self.task(self.create())
        self.client.post(url("log_weight"), {"weight": "180", "unit": "lb", "date": self.today.isoformat()})
        self.assertTrue(ItemCompletion.objects.filter(item=item, occurrence_date=self.today).exists())
        self.assertIn("180.0 lb", self.client.get(reverse("albright_planner:day")).content.decode())

    def test_the_bot_api_logs_and_checks_it_off(self):
        item = self.task(self.create())
        _token, raw = BotToken.issue(self.me, "bot", ["fitness:write", "schedule:read"])
        api = Client()
        data = api.post(API + "fitness/weight", {"weight": 179.5, "unit": "lb", "date": self.today.isoformat()},
                        content_type="application/json", HTTP_AUTHORIZATION=f"Bearer {raw}").json()
        self.assertTrue(data["planner_completed"])
        self.assertTrue(ItemCompletion.objects.filter(item=item, occurrence_date=self.today).exists())
        listed = api.get(API + "schedule", {"start": self.today.isoformat(), "end": self.today.isoformat()},
                         HTTP_AUTHORIZATION=f"Bearer {raw}").json()["occurrences"]
        row = next(o for o in listed if o["item_id"] == item.pk)
        self.assertEqual((row["type"], row["done"], row["weigh_in"]), ("weigh_in", True,
                                                                       {"weight": "179.5", "unit": "lb", "skipped": False}))

    def test_skip_marks_it_done_with_no_weight(self):
        item = self.task(self.create())
        data = self.client.post(url("weigh_in_log", item.pk), {"date": self.today.isoformat(), "skip": "1"}, **AJAX).json()
        self.assertEqual((data["result"], data["skipped"]), ("Skipped", True))
        self.assertTrue(ItemCompletion.objects.filter(item=item, occurrence_date=self.today).exists())
        self.assertFalse(WeightEntry.objects.exists())
        self.assertIn('wi-result is-skipped', self.client.get(reverse("albright_planner:day")).content.decode())

    def test_unchecking_reopens_it_and_keeps_the_weight(self):
        item = self.task(self.create())
        self.log(item, self.today, "180")
        response = self.client.post(reverse("albright_planner:toggle", args=[item.pk]), {"date": self.today.isoformat()}, **AJAX)
        self.assertEqual((response.json()["done"], response.json()["note"]), (False, "Weight kept - edit it on the Body page."))
        self.assertTrue(WeightEntry.objects.filter(owner=self.me, date=self.today).exists())
        self.assertFalse(WeighInLog.objects.filter(item=item).exists())
        self.assertFalse(ItemCompletion.objects.filter(item=item).exists())

    def test_someone_elses_weigh_in_is_a_404(self):
        item = self.task(self.create())
        other = with_apps(User.objects.create_user(username="kim", password=PASSWORD), ["fitness", "planner"])
        c = Client()
        c.force_login(other)
        self.assertEqual(c.post(url("weigh_in_log", item.pk), {"date": self.today.isoformat(), "weight": "150"}).status_code, 404)


class ChangeTests(Base):
    def test_editing_changes_only_future_not_completed_weigh_ins(self):
        schedule = self.create(frequency="daily", time_of_day="07:00")
        old = self.task(schedule)
        self.log(old, self.today, "180")
        self.client.post(url("weigh_in_save"), {"frequency": "daily", "time_of_day": "08:30", "mode": "edit",
                                                "start_date": schedule.start_date.isoformat()})
        new = self.task(schedule)
        old.refresh_from_db()
        self.assertNotEqual(old.pk, new.pk)
        self.assertEqual((old.start_time, old.recurrence_until), (time(7), self.today))          # completed one untouched
        self.assertTrue(ItemCompletion.objects.filter(item=old, occurrence_date=self.today).exists())
        self.assertEqual((new.start_time, new.date), (time(8, 30), self.today + timedelta(days=1)))
        self.assertEqual(new.source_id, f"weighin-{schedule.pk}")

    def test_editing_with_nothing_completed_just_replaces_the_task(self):
        schedule = self.create(frequency="daily")
        old = self.task(schedule)
        self.client.post(url("weigh_in_save"), {"frequency": "weekly", "days_of_week": ["4"], "time_of_day": "07:00",
                                                "mode": "edit", "start_date": schedule.start_date.isoformat()})
        self.assertFalse(ScheduleItem.objects.filter(pk=old.pk).exists())
        self.assertEqual(self.task(schedule).recurrence, "FREQ=WEEKLY;BYDAY=FR")

    def test_deleting_keeps_completed_weigh_ins_and_weights(self):
        schedule = self.create(frequency="daily")
        item = self.task(schedule)
        self.log(item, self.today, "180")
        self.client.post(url("weigh_in_delete", schedule.pk))
        self.assertFalse(WeighInSchedule.objects.exists())
        item.refresh_from_db()
        self.assertEqual(item.recurrence_until, self.today)                                       # no future ones
        self.assertTrue(ItemCompletion.objects.filter(item=item, occurrence_date=self.today).exists())
        self.assertTrue(WeightEntry.objects.filter(owner=self.me).exists())

    def test_pausing_removes_future_ones_and_resuming_brings_them_back(self):
        schedule = self.create(frequency="daily")
        item = self.task(schedule)
        self.client.post(url("weigh_in_pause", schedule.pk))
        schedule.refresh_from_db()
        self.assertFalse(schedule.active)
        self.assertIsNone(schedule.planner_item)
        self.assertFalse(ScheduleItem.objects.filter(pk=item.pk).exists())                       # nothing completed
        self.assertIn("Paused", self.client.get(url("body")).content.decode())
        self.client.post(url("weigh_in_pause", schedule.pk))
        self.assertTrue(self.task(schedule).is_weigh_in)
