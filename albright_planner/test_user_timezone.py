"""Timed items keep the zone they were made in (ScheduleItem.timezone) and show in the viewer's current zone
(PlannerSettings.timezone, which follows the browser): the day and time move, the occurrence key doesn't, all-day
items never shift, a weekly series keeps its local time across a DST change, today() is the viewer's day, and
planner/timezone/ only saves a real zone while auto is on."""
import json
from datetime import date, datetime, time, timedelta, timezone as dt_timezone
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from albright_labs.testing import with_apps

from . import planner
from .models import ItemCompletion, PlannerSettings, ScheduleItem

PASSWORD = "pw-not-real-12345"
DAY = date(2026, 11, 4)          # a Wednesday, after the US DST change


class ZoneTestCase(TestCase):
    def setUp(self):
        self.user = with_apps(User.objects.create_user(username="traveler", password=PASSWORD), ["planner"])
        self.client.force_login(self.user)

    def set_zone(self, name, auto=True, user=None):
        PlannerSettings.objects.update_or_create(user=user or self.user,
                                                 defaults={"timezone": name, "auto_timezone": auto})

    def item(self, start, zone="America/New_York", day=DAY, **fields):
        return ScheduleItem.objects.create(owner=self.user, title=fields.pop("title", "Call"), date=day,
                                           start_time=start, timezone=zone, **fields)

    def occs(self, start, end=None):
        return planner.occurrences(self.user, start, end or start)


class ViewerZoneTests(ZoneTestCase):
    def test_new_york_evening_shows_two_hours_earlier_in_denver(self):
        item = self.item(time(20, 0), end_time=time(21, 0))
        self.set_zone("America/Denver")
        [occ] = self.occs(DAY)
        self.assertEqual((occ.item, occ.date, occ.start_time, occ.end_time), (item, DAY, time(18, 0), time(19, 0)))
        self.assertEqual((occ.occurrence_date, occ.tz_days), (DAY, 0))
        self.assertFalse(occ.moved)

    def test_late_los_angeles_item_shows_the_next_day_in_new_york(self):
        item = self.item(time(23, 30), zone="America/Los_Angeles")
        self.set_zone("America/New_York")
        self.assertEqual(self.occs(DAY), [])                         # not on its original day any more
        [occ] = self.occs(DAY + timedelta(days=1))
        self.assertEqual((occ.item, occ.date, occ.start_time), (item, DAY + timedelta(days=1), time(2, 30)))
        self.assertEqual((occ.occurrence_date, occ.tz_days), (DAY, 1))
        self.assertFalse(occ.moved)
        # The week view's range picks it up on the right day too.
        week = self.occs(planner.week_start(DAY), planner.week_start(DAY) + timedelta(days=6))
        self.assertEqual([(o.date, o.start_time) for o in week], [(DAY + timedelta(days=1), time(2, 30))])

    def test_all_day_items_never_shift(self):
        item = self.item(None, zone="America/Los_Angeles")
        for zone in ("America/New_York", "Asia/Tokyo", "Pacific/Honolulu"):
            with self.subTest(zone=zone):
                self.set_zone(zone)
                [occ] = self.occs(DAY - timedelta(days=1), DAY + timedelta(days=1))
                self.assertEqual((occ.item, occ.date, occ.start_time, occ.tz_days), (item, DAY, None, 0))

    def test_weekly_series_keeps_its_local_time_across_dst(self):
        # Every Monday 8 PM New York, from before the Nov 1, 2026 change to after it.
        start = date(2026, 10, 19)
        self.item(time(20, 0), day=start, recurrence="FREQ=WEEKLY;BYDAY=MO", kind=ScheduleItem.EVENT)
        self.set_zone("America/Denver")
        occs = self.occs(start, start + timedelta(days=27))
        self.assertEqual([(o.date, o.start_time) for o in occs],
                         [(start + timedelta(weeks=n), time(18, 0)) for n in range(4)])
        self.assertTrue(all(not o.moved for o in occs))

    def test_same_zone_is_untouched(self):
        self.item(time(23, 30))
        [occ] = self.occs(DAY)
        self.assertEqual((occ.date, occ.start_time, occ.tz_days), (DAY, time(23, 30), 0))

    def test_completing_a_shifted_occurrence_keeps_the_original_key(self):
        item = self.item(time(23, 30), zone="America/Los_Angeles")
        self.set_zone("America/New_York")
        [occ] = self.occs(DAY + timedelta(days=1))
        self.client.post(reverse("albright_planner:toggle", args=[item.pk]),
                         {"date": occ.occurrence_date.isoformat()})
        self.assertEqual(list(ItemCompletion.objects.filter(item=item).values_list("occurrence_date", flat=True)),
                         [DAY])
        [occ] = self.occs(DAY + timedelta(days=1))
        self.assertTrue(occ.done)

    def test_day_view_lists_it_at_the_viewers_time(self):
        item = self.item(time(20, 0))
        self.set_zone("America/Denver")
        content = self.client.get(reverse("albright_planner:day") + f"?date={DAY.isoformat()}").content.decode()
        row = content.split(f'data-item="{item.pk}"', 1)[1].split("</li>", 1)[0]
        self.assertIn(f'data-date="{DAY.isoformat()}"', content.split(f'data-item="{item.pk}"', 1)[1][:200])
        self.assertIn('<span class="pl-ind__time pl-time">6:00', row)

    def test_edit_shows_my_time_and_saves_it_back_in_the_items_zone(self):
        item = self.item(time(20, 0), end_time=time(21, 0))
        self.set_zone("America/Denver")
        url = reverse("albright_planner:edit", args=[item.pk])
        form = self.client.get(url + f"?date={DAY.isoformat()}").context["form"]
        self.assertEqual((form.initial["date"], form.initial["start_time"], form.initial["end_time"]),
                         (DAY, time(18, 0), time(19, 0)))
        self.client.post(url, {"title": "Call", "kind": "task", "date": DAY.isoformat(), "start_time": "19:00",
                               "end_time": "20:00", "priority": "none", "repeat": "none", "ends": "never",
                               "occurrence": DAY.isoformat()})
        item.refresh_from_db()
        self.assertEqual((item.date, item.start_time, item.end_time, item.timezone),
                         (DAY, time(21, 0), time(22, 0), "America/New_York"))

    def test_new_items_are_made_in_my_zone(self):
        self.set_zone("America/Denver")
        self.client.post(reverse("albright_planner:quick_add"), {"text": "Dentist tomorrow 3pm"})
        self.assertEqual(ScheduleItem.objects.get(title__icontains="Dentist").timezone, "America/Denver")


class TodayTests(ZoneTestCase):
    def test_today_is_the_viewers_day(self):
        # 04:30 UTC in July: 10:30 PM the day before in Denver, 12:30 AM in New York.
        frozen = datetime(2026, 7, 10, 4, 30, tzinfo=dt_timezone.utc)
        other = with_apps(User.objects.create_user(username="easterner", password=PASSWORD), ["planner"])
        self.set_zone("America/Denver")
        self.set_zone("America/New_York", user=other)
        with mock.patch("django.utils.timezone.now", return_value=frozen):
            self.assertEqual(planner.today(self.user), date(2026, 7, 9))
            self.assertEqual(planner.today(other), date(2026, 7, 10))

    def test_bad_saved_zone_falls_back(self):
        self.set_zone("Not/AZone")
        self.assertEqual(planner.user_zone(self.user).key, "America/New_York")


class AutoDetectTests(ZoneTestCase):
    def post(self, zone):
        return self.client.post(reverse("albright_planner:timezone"), data=json.dumps({"timezone": zone}),
                                content_type="application/json", HTTP_X_REQUESTED_WITH="XMLHttpRequest")

    def test_saves_the_browsers_zone_while_auto(self):
        response = self.post("America/Denver")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"ok": True, "timezone": "America/Denver", "auto": True, "changed": True})
        self.assertEqual(PlannerSettings.objects.get(user=self.user).timezone, "America/Denver")
        self.assertFalse(self.post("America/Denver").json()["changed"])

    def test_unknown_zone_refused(self):
        self.assertEqual(self.post("Not/AZone").status_code, 400)
        self.assertEqual(self.post("").status_code, 400)
        self.assertFalse(PlannerSettings.objects.filter(user=self.user, timezone="Not/AZone").exists())

    def test_auto_off_keeps_the_setting(self):
        self.set_zone("America/Chicago", auto=False)
        response = self.post("America/Denver")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["changed"])
        self.assertEqual(PlannerSettings.objects.get(user=self.user).timezone, "America/Chicago")

    def test_planner_pages_carry_the_zone(self):
        self.set_zone("America/Denver")
        content = self.client.get(reverse("albright_planner:day")).content.decode()
        self.assertIn('data-planner-tz="America/Denver" data-planner-tz-auto="1"', content)
        self.assertIn(f'data-planner-tz-url="{reverse("albright_planner:timezone")}"', content)
        self.assertIn("Intl.DateTimeFormat().resolvedOptions().timeZone", content)

    def test_settings_page_sets_a_manual_zone(self):
        settings = self.client.get(reverse("albright_planner:settings"))
        self.assertContains(settings, "Time zone: <strong>America/New_York</strong>")
        self.client.post(reverse("albright_planner:settings"), {"timezone": "America/Phoenix"})   # auto box off
        prefs = PlannerSettings.objects.get(user=self.user)
        self.assertEqual((prefs.timezone, prefs.auto_timezone), ("America/Phoenix", False))
        self.client.post(reverse("albright_planner:settings"), {"timezone": "Not/AZone"})
        self.assertEqual(PlannerSettings.objects.get(user=self.user).timezone, "America/Phoenix")


class BotApiUnchangedTests(ZoneTestCase):
    def test_bot_api_listing_keeps_stored_times(self):
        from albright_bot_api.models import BotToken
        from albright_labs.testing import allow_api_keys
        allow_api_keys(self.user)
        self.item(time(20, 0))
        self.set_zone("America/Denver")
        _token, raw = BotToken.issue(self.user, "Reader", ["schedule:read"])
        response = self.client.get(reverse("bot_api_v1:schedule") + f"?start={DAY.isoformat()}&end={DAY.isoformat()}",
                                   HTTP_AUTHORIZATION=f"Bearer {raw}", secure=True)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertIn("20:00", response.content.decode())
        self.assertNotIn("18:00", response.content.decode())
