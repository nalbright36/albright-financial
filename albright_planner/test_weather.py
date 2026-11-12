"""Weather on Day and Week (idea #77): a home location on Planner settings, an
optional place on an item when it isn't at home, a high/low line on the Day
header and Week columns (not on every item — an away item keeps a pin and its
high). Today also shows the current temperature (and Feels like) and an hourly
strip; Week columns have a rain-chance bar. show_weather (default on) hides
every forecast and skips the fetches. Open-Meteo is called through weather.py;
every test mocks the HTTP. Forecasts cover about the past week through the
next 16 days. The page stays up if the API is down. weather_auto follows the
device (planner/location/) and falls back to the typed home."""
import json
from datetime import time, timedelta
from decimal import Decimal
from unittest import mock

import requests
from django.core.cache import cache
from . import weather
from .models import PlannerSettings, ScheduleItem
from .tests import TODAY, Base, url

LONE_TREE = {"name": "Lone Tree", "latitude": 39.5244, "longitude": -104.8822,
             "admin1": "Colorado", "country": "United States", "country_code": "US"}
DENVER = {"name": "Denver", "latitude": 39.7392, "longitude": -104.9903,
          "admin1": "Colorado", "country": "United States", "country_code": "US"}
NOMINATIM_LONE_TREE = {"name": "Lone Tree", "address": {
    "city": "Lone Tree", "state": "Colorado", "country": "United States", "country_code": "us"}}
NOMINATIM_DENVER = {"name": "Denver", "address": {
    "city": "Denver", "state": "Colorado", "country": "United States", "country_code": "us"}}
# Past week + today + 15 future days. Highs: past 68°, today (Lone Tree) 72°,
# later 70°. Denver (away / GPS) is 65° today so the two places differ in HTML.
# Current (10 AM): Lone Tree 58° (feels 54°), Denver 50°. Rain chance: past 10%,
# today 20%, later 40% at home; Denver 60% today.
PAST, FUTURE = 7, 16
HOME_DAILY = {
    "time": [(TODAY + timedelta(days=i)).isoformat() for i in range(-PAST, FUTURE)],
    "weather_code": [2] * PAST + [0] + [2] * (FUTURE - 1),
    "temperature_2m_max": [68] * PAST + [72.4] + [70] * (FUTURE - 1),
    "temperature_2m_min": [40] * PAST + [45.2] + [44] * (FUTURE - 1),
    "precipitation_probability_max": [10] * PAST + [20] + [40] * (FUTURE - 1),
}
AWAY_DAILY = {
    "time": [(TODAY + timedelta(days=i)).isoformat() for i in range(-PAST, FUTURE)],
    "weather_code": [63] * PAST + [61] + [63] * (FUTURE - 1),
    "temperature_2m_max": [60] * PAST + [65.0] + [60] * (FUTURE - 1),
    "temperature_2m_min": [40] * PAST + [42.0] + [40] * (FUTURE - 1),
    "precipitation_probability_max": [50] * PAST + [60] + [50] * (FUTURE - 1),
}
HOME_CURRENT = {
    "time": f"{TODAY.isoformat()}T10:00",
    "temperature_2m": 58.4,
    "apparent_temperature": 54.1,
    "weather_code": 0,
}
AWAY_CURRENT = {
    "time": f"{TODAY.isoformat()}T10:00",
    "temperature_2m": 50.0,
    "apparent_temperature": 47.0,
    "weather_code": 61,
}


def _diurnal(high, low, hour):
    """A simple day curve (low near 6 AM, high near 3 PM) so the hourly strip has real numbers."""
    high, low = float(high), float(low)
    span = high - low
    if hour <= 6:
        return low + span * 0.08 * hour / 6
    if hour <= 15:
        return low + span * (hour - 6) / 9
    return high - span * 0.65 * (hour - 15) / 9


def _hourly(daily, today_hour=10, today_temp=None):
    times, temps, pops, codes = [], [], [], []
    days = daily["time"]
    highs = daily["temperature_2m_max"]
    lows = daily["temperature_2m_min"]
    dcodes = daily["weather_code"]
    dpops = daily.get("precipitation_probability_max") or [0] * len(days)
    for i, raw in enumerate(days):
        high, low = highs[i], lows[i]
        pop = dpops[i] if i < len(dpops) else 0
        code = dcodes[i] if i < len(dcodes) else 0
        for h in range(24):
            times.append(f"{raw}T{h:02d}:00")
            t = _diurnal(high, low, h)
            if raw == TODAY.isoformat() and h == today_hour and today_temp is not None:
                t = today_temp
            temps.append(round(t, 1))
            # Hourly rain chance leans into the afternoon, capped by the day's max.
            mid = 1 - abs(h - 15) / 15
            pops.append(int(round(float(pop) * (0.35 + 0.65 * mid))))
            codes.append(code)
    return {"time": times, "temperature_2m": temps, "precipitation_probability": pops, "weather_code": codes}


HOME_HOURLY = _hourly(HOME_DAILY, 10, 58.4)
AWAY_HOURLY = _hourly(AWAY_DAILY, 10, 50.0)


def _forecast(daily, current, hourly):
    return {"daily": daily, "current": current, "hourly": hourly}


class FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


def fake_get(url, params=None, **kwargs):
    """Route Open-Meteo and Nominatim calls by host (and, for forecasts, by rounded latitude)."""
    params = params or {}
    if "geocoding-api.open-meteo.com" in url:
        name = (params.get("name") or "").lower()
        if "lone tree" in name:
            return FakeResponse({"results": [LONE_TREE]})
        if "denver" in name:
            return FakeResponse({"results": [DENVER]})
        return FakeResponse({"results": []})
    if "nominatim.openstreetmap.org" in url:
        lat = round(float(params.get("lat") or 0), 2)
        if lat == 39.74:
            return FakeResponse(NOMINATIM_DENVER)
        if 39.50 <= lat <= 39.55:
            return FakeResponse(NOMINATIM_LONE_TREE)
        return FakeResponse({"error": "Unable to geocode"})
    if "api.open-meteo.com" in url:
        lat = round(float(params.get("latitude") or 0), 2)
        away = lat == 39.74
        daily = AWAY_DAILY if away else HOME_DAILY
        current = AWAY_CURRENT if away else HOME_CURRENT
        hourly = AWAY_HOURLY if away else HOME_HOURLY
        # A current-only refresh still returns the current block (and may omit daily).
        if params.get("daily") is None and params.get("current"):
            return FakeResponse({"current": current})
        return FakeResponse(_forecast(daily, current, hourly))
    raise AssertionError(f"unexpected URL: {url}")


class WeatherTests(Base):
    def setUp(self):
        super().setUp()
        self.get = mock.patch("albright_planner.weather.requests.get", side_effect=fake_get)
        self.get.start()
        self.addCleanup(self.get.stop)

    def set_home(self, user=None, label="Lone Tree, Colorado", lat=39.5244, lon=-104.8822):
        prefs = PlannerSettings.for_user(user or self.me)
        prefs.weather_label = label
        prefs.weather_lat = Decimal(str(lat))
        prefs.weather_lon = Decimal(str(lon))
        prefs.save()
        return prefs

    def set_place(self, item, label="Denver, Colorado", lat=39.7392, lon=-104.9903):
        item.location_label = label
        item.location_lat = Decimal(str(lat))
        item.location_lon = Decimal(str(lon))
        item.save(update_fields=["location_label", "location_lat", "location_lon"])
        return item

    def set_gps(self, label="Lone Tree, Colorado", lat=39.52, lon=-104.88, auto=True):
        prefs = PlannerSettings.for_user(self.me)
        prefs.weather_auto = auto
        prefs.weather_auto_label = label
        prefs.weather_auto_lat = Decimal(str(lat))
        prefs.weather_auto_lon = Decimal(str(lon))
        prefs.save(update_fields=["weather_auto", "weather_auto_label", "weather_auto_lat", "weather_auto_lon"])
        return prefs


class GeocodeTests(WeatherTests):
    def test_city_and_state_becomes_a_label_and_coordinates(self):
        found = weather.geocode("Lone Tree, CO")
        self.assertEqual(found["label"], "Lone Tree, Colorado")
        self.assertEqual(found["lat"], Decimal("39.52440"))
        self.assertEqual(found["lon"], Decimal("-104.88220"))

    def test_unknown_place_is_none(self):
        self.assertIsNone(weather.geocode("zzzzz-not-a-place"))

    def test_api_down_raises_lookupdown(self):
        with mock.patch("albright_planner.weather.requests.get", side_effect=requests.ConnectionError):
            with self.assertRaises(weather.LookupDown):
                weather.geocode("Lone Tree, CO")

    def test_lat_lon_paste_does_not_hit_the_api(self):
        live = weather.requests.get
        live.reset_mock()
        found = weather.geocode("39.54, -104.88")
        self.assertEqual(found["label"], "39.54, -104.88")
        self.assertEqual(float(found["lat"]), 39.54)
        self.assertEqual(float(found["lon"]), -104.88)
        live.assert_not_called()

    def test_us_label_drops_the_country_foreign_keeps_it(self):
        self.assertEqual(weather._place_label(LONE_TREE), "Lone Tree, Colorado")
        self.assertEqual(weather._place_label({
            "name": "Berlin", "admin1": "Berlin", "country": "Germany", "country_code": "DE",
        }), "Berlin, Germany")

    def test_reverse_geocode_returns_a_city_label(self):
        found = weather.reverse_geocode(39.5244, -104.8822)
        self.assertEqual(found["label"], "Lone Tree, Colorado")
        self.assertEqual(float(found["lat"]), 39.52)
        self.assertEqual(float(found["lon"]), -104.88)

    def test_reverse_geocode_falls_back_to_coordinates_when_down(self):
        cache.clear()
        with mock.patch("albright_planner.weather.requests.get", side_effect=requests.Timeout):
            found = weather.reverse_geocode(39.52, -104.88)
        self.assertEqual(found["label"], "39.52, -104.88")
        self.assertEqual(float(found["lat"]), 39.52)


class ForecastTests(WeatherTests):
    def test_high_low_and_label_for_today(self):
        wx = weather.forecasts(39.5244, -104.8822)[TODAY]
        self.assertEqual((wx.high, wx.low, wx.label, wx.icon), (72, 45, "Clear", "clear"))
        self.assertEqual((wx.temp, wx.feels, wx.pop, wx.current_hour), (58, 54, 20, 10))
        self.assertTrue(wx.hourly)
        self.assertEqual(wx.hourly[10].temp, 58)   # 10 AM matches the current reading

    def test_yesterday_is_in_the_forecast(self):
        wx = weather.forecasts(39.5244, -104.8822)[TODAY - timedelta(days=1)]
        self.assertEqual((wx.high, wx.low, wx.label), (68, 40, "Partly cloudy"))

    def test_second_call_uses_the_cache(self):
        live = weather.requests.get
        live.reset_mock()
        weather.forecasts(39.5244, -104.8822)
        weather.forecasts(39.5244, -104.8822)
        self.assertEqual(live.call_count, 1)

    def test_api_down_returns_empty_not_an_error(self):
        cache.clear()
        with mock.patch("albright_planner.weather.requests.get", side_effect=requests.Timeout):
            self.assertEqual(weather.forecasts(39.5244, -104.8822), {})

    def test_unknown_weather_code_is_cloudy(self):
        self.assertEqual(weather.describe(1234), ("Cloudy", "cloudy"))
        self.assertEqual(weather.describe(0), ("Clear", "clear"))
        self.assertEqual(weather.describe(95), ("Thunderstorm", "storm"))

    def test_a_day_with_a_missing_low_still_shows(self):
        cache.clear()
        daily = {
            "time": [TODAY.isoformat()],
            "weather_code": [0],
            "temperature_2m_max": [72],
            "temperature_2m_min": [None],
        }
        with mock.patch("albright_planner.weather.requests.get",
                        return_value=FakeResponse({"daily": daily})):
            wx = weather.forecasts(39.5244, -104.8822)[TODAY]
        self.assertEqual((wx.high, wx.low), (72, 72))
        self.assertIsNone(wx.temp)
        self.assertIsNone(wx.pop)
        self.assertEqual(wx.hourly, ())

    def test_missing_current_and_pop_still_shows_high_low(self):
        """A slimmer Open-Meteo body (no current, no rain, no hourly) still parses."""
        cache.clear()
        daily = {
            "time": [TODAY.isoformat()],
            "weather_code": [0],
            "temperature_2m_max": [72],
            "temperature_2m_min": [45],
        }
        with mock.patch("albright_planner.weather.requests.get",
                        return_value=FakeResponse({"daily": daily})):
            wx = weather.forecasts(39.5244, -104.8822)[TODAY]
        self.assertEqual((wx.high, wx.low, wx.temp, wx.feels, wx.pop, wx.hourly),
                         (72, 45, None, None, None, ()))

    def test_expired_current_is_refetched_without_refetching_the_forecast(self):
        live = weather.requests.get
        live.reset_mock()
        weather.forecasts(39.5244, -104.8822)
        self.assertEqual(live.call_count, 1)
        cache.delete(weather._current_key(39.5244, -104.8822))
        wx = weather.forecasts(39.5244, -104.8822)[TODAY]
        self.assertEqual(live.call_count, 2)
        params = live.call_args_list[1].kwargs.get("params") or {}
        self.assertIn("current", params)
        self.assertIsNone(params.get("daily"))
        self.assertEqual(wx.temp, 58)


class SettingsLocationTests(WeatherTests):
    def test_settings_page_has_the_home_location_field(self):
        page = self.client.get(url("settings")).content.decode()
        self.assertIn('name="weather_location"', page)
        self.assertIn("Home location", page)
        self.assertIn('name="weather_auto"', page)
        self.assertIn("Use my current location", page)
        self.assertIn('name="weather_auto" value="1" checked', page)
        self.assertIn('name="show_weather"', page)
        self.assertIn("Show weather", page)
        self.assertIn('name="show_weather" value="1" checked', page)

    def test_saving_a_place_geocodes_and_stores_it(self):
        response = self.client.post(url("settings"), {
            "weather_location": "Lone Tree, CO",
            "weather_auto": "1",
            "auto_timezone": "1",
            "complete_when_checklist_done": "1",
        })
        self.assertEqual(response.status_code, 302)
        prefs = PlannerSettings.objects.get(user=self.me)
        self.assertEqual(prefs.weather_label, "Lone Tree, Colorado")
        self.assertEqual(prefs.weather_lat, Decimal("39.52440"))
        self.assertEqual(prefs.weather_lon, Decimal("-104.88220"))
        self.assertTrue(prefs.weather_auto)
        saved = self.client.get(url("settings")).content.decode()
        self.assertIn('value="Lone Tree, Colorado"', saved)

    def test_saving_the_same_label_does_not_geocode_again(self):
        self.set_home()
        live = weather.requests.get
        live.reset_mock()
        self.client.post(url("settings"), {
            "weather_location": "Lone Tree, Colorado",
            "weather_auto": "1",
            "auto_timezone": "1",
            "complete_when_checklist_done": "1",
        })
        self.assertFalse(any("geocoding-api" in (c.args[0] if c.args else "") for c in live.call_args_list))

    def test_unknown_place_leaves_the_saved_one_and_shows_an_error(self):
        self.set_home()
        response = self.client.post(url("settings"), {
            "weather_location": "zzzzz-not-a-place",
            "weather_auto": "1",
            "auto_timezone": "1",
        }, follow=True)
        self.assertContains(response, "find that place")
        prefs = PlannerSettings.objects.get(user=self.me)
        self.assertEqual(prefs.weather_label, "Lone Tree, Colorado")

    def test_clearing_the_field_hides_weather(self):
        self.set_home()
        self.client.post(url("settings"), {"weather_location": "", "auto_timezone": "1", "weather_auto": "1"})
        prefs = PlannerSettings.objects.get(user=self.me)
        self.assertEqual(prefs.weather_label, "")
        self.assertIsNone(prefs.weather_lat)
        self.assertIsNone(prefs.weather_lon)

    def test_lookup_down_keeps_the_saved_place(self):
        self.set_home()
        with mock.patch("albright_planner.weather.requests.get", side_effect=requests.ConnectionError):
            response = self.client.post(url("settings"), {
                "weather_location": "Denver, CO",
                "weather_auto": "1",
                "auto_timezone": "1",
            }, follow=True)
        self.assertContains(response, "look up that place")
        prefs = PlannerSettings.objects.get(user=self.me)
        self.assertEqual(prefs.weather_label, "Lone Tree, Colorado")

    def test_turning_auto_off_keeps_the_typed_home(self):
        self.set_home()
        self.client.post(url("settings"), {
            "weather_location": "Lone Tree, Colorado",
            "auto_timezone": "1",
        })
        prefs = PlannerSettings.objects.get(user=self.me)
        self.assertFalse(prefs.weather_auto)
        self.assertEqual(prefs.weather_label, "Lone Tree, Colorado")


class ItemLocationTests(WeatherTests):
    def test_new_item_with_a_place_geocodes_and_stores_it(self):
        response = self.client.post(url("new"), {
            "title": "Hike", "kind": "event", "date": TODAY.isoformat(),
            "priority": "none", "repeat": "none", "ends": "never",
            "location": "Denver, CO",
        })
        self.assertEqual(response.status_code, 302)
        item = ScheduleItem.objects.get(title="Hike")
        self.assertEqual(item.location_label, "Denver, Colorado")
        self.assertEqual(item.location_lat, Decimal("39.73920"))

    def test_empty_location_stays_empty(self):
        self.client.post(url("new"), {
            "title": "At home", "kind": "task", "date": TODAY.isoformat(),
            "priority": "none", "repeat": "none", "ends": "never", "checkable": "on",
            "location": "",
        })
        item = ScheduleItem.objects.get(title="At home")
        self.assertEqual(item.location_label, "")
        self.assertIsNone(item.location_lat)

    def test_unknown_place_is_a_form_error_and_keeps_the_text(self):
        response = self.client.post(url("new"), {
            "title": "Hike", "kind": "event", "date": TODAY.isoformat(),
            "priority": "none", "repeat": "none", "ends": "never",
            "location": "zzzzz-not-a-place",
        })
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "find that place")
        self.assertContains(response, "zzzzz-not-a-place")
        self.assertFalse(ScheduleItem.objects.filter(title="Hike").exists())

    def test_edit_clears_an_item_location(self):
        item = self.item("Trip", kind=ScheduleItem.EVENT)
        self.set_place(item)
        self.client.post(url("edit", item.pk), {
            "title": "Trip", "kind": "event", "date": TODAY.isoformat(),
            "priority": "none", "repeat": "none", "ends": "never", "occurrence": TODAY.isoformat(),
            "location": "",
        })
        item.refresh_from_db()
        self.assertEqual(item.location_label, "")
        self.assertIsNone(item.location_lat)


def _wx_chips(html):
    """Count weather chips, not the pl-wx__icon / __temps / __pin children."""
    import re
    return len(re.findall(r'class="pl-wx[ "]', html))


def _after_day_list(page):
    """The Day view's item list (everything after the header weather)."""
    return page.partition('id="pl-day"')[2]


class DayWeekWeatherTests(WeatherTests):
    def test_day_view_shows_weather_in_the_header_not_on_home_items(self):
        self.set_home()
        self.item("Morning run", kind=ScheduleItem.TASK, start_time=time(7, 0))
        self.item("Evening walk", kind=ScheduleItem.TASK, start_time=time(18, 0))
        page = self.client.get(url("day")).content.decode()
        self.assertIn('id="pl-day-wx"', page)
        self.assertIn("72°", page)
        self.assertIn("45°", page)
        self.assertIn("Clear", page)
        self.assertIn("Lone Tree, Colorado", page)
        self.assertIn("Morning run", page)
        self.assertIn("Evening walk", page)
        self.assertIn('class="pl-wx', page)
        # The same forecast is only at the top, not repeated on every task.
        self.assertEqual(_wx_chips(_after_day_list(page)), 0)
        self.assertNotIn("pl-wx__pin", page)

    def test_second_load_does_not_hit_the_api_again(self):
        self.set_home()
        self.item("Run")
        live = weather.requests.get
        live.reset_mock()
        self.client.get(url("day"))
        self.client.get(url("day"))
        forecast_calls = [c for c in live.call_args_list if c.args and "api.open-meteo.com" in c.args[0]]
        self.assertEqual(len(forecast_calls), 1)

    def test_item_at_another_place_keeps_a_pin_and_the_high(self):
        self.set_home()
        trip = self.item("Game in Denver", kind=ScheduleItem.EVENT, start_time=time(19, 0))
        self.set_place(trip)
        page = self.client.get(url("day")).content.decode()
        # Header is still home (clear, 72°); the away item is a pin and Denver's 65°.
        self.assertIn("72°", page)
        self.assertIn("65°", page)
        self.assertIn("pl-wx__pin", page)
        self.assertIn("is-minimal", _after_day_list(page))
        self.assertIn("Denver, Colorado", page)
        self.assertIn("Game in Denver", page)
        # The away chip is the only one in the item list.
        self.assertEqual(_wx_chips(_after_day_list(page)), 1)

    def test_no_home_and_no_item_place_shows_nothing_and_does_not_fetch(self):
        self.item("Just a task")
        live = weather.requests.get
        live.reset_mock()
        page = self.client.get(url("day")).content.decode()
        self.assertNotIn("pl-day-wx", page)
        self.assertNotIn('class="pl-wx', page)
        live.assert_not_called()

    def test_item_with_its_own_place_shows_weather_even_without_a_home(self):
        trip = self.item("Game in Denver", kind=ScheduleItem.EVENT)
        self.set_place(trip)
        page = self.client.get(url("day")).content.decode()
        self.assertNotIn("pl-day-wx", page)          # no home: no day header
        self.assertIn("65°", page)
        self.assertIn("pl-wx__pin", page)
        self.assertIn("Rain", page)                   # view tooltip / visually-hidden

    def test_api_down_still_renders_the_day(self):
        self.set_home()
        self.item("Run")
        cache.clear()
        with mock.patch("albright_planner.weather.requests.get", side_effect=requests.Timeout):
            response = self.client.get(url("day"))
        self.assertEqual(response.status_code, 200)
        page = response.content.decode()
        self.assertIn("Run", page)
        self.assertNotIn("pl-day-wx", page)
        self.assertNotIn("72°", page)

    def test_yesterday_shows_weather(self):
        self.set_home()
        yesterday = TODAY - timedelta(days=1)
        page = self.client.get(url("day") + f"?date={yesterday.isoformat()}").content.decode()
        self.assertIn("pl-day-wx", page)
        self.assertIn("68°", page)
        self.assertIn("10%", page)                 # rain chance, past days still have it
        self.assertNotIn("pl-wx-hero__temp", page) # no current temp on a past day
        self.assertNotIn("pl-wx-hourly", page)     # no hourly strip on a past day
        self.assertNotIn("Feels", page)

    def test_a_day_more_than_a_week_ago_shows_no_weather(self):
        self.set_home()
        old = TODAY - timedelta(days=8)
        self.item("Last week", date=old)
        page = self.client.get(url("day") + f"?date={old.isoformat()}").content.decode()
        self.assertNotIn("pl-day-wx", page)
        self.assertNotIn('class="pl-wx', page)

    def test_far_ahead_day_shows_no_weather(self):
        self.set_home()
        far = TODAY + timedelta(days=20)
        self.item("Far trip", date=far, kind=ScheduleItem.EVENT)
        page = self.client.get(url("day") + f"?date={far.isoformat()}").content.decode()
        self.assertNotIn("pl-day-wx", page)
        self.assertNotIn('class="pl-wx', page)

    def test_navigating_each_day_around_today_shows_header_weather(self):
        """Regression: slice 1 only forecast from today on, so stepping back a
        day (or looking at Mon/Tue of the current week) blanked the weather
        while later days still had it."""
        self.set_home()
        for delta in range(-7, 16):
            day = TODAY + timedelta(days=delta)
            page = self.client.get(url("day") + f"?date={day.isoformat()}").content.decode()
            self.assertIn("pl-day-wx", page, f"missing weather on {day}")
        too_old = self.client.get(url("day") + f"?date={(TODAY - timedelta(days=8)).isoformat()}").content.decode()
        self.assertNotIn("pl-day-wx", too_old)
        too_far = self.client.get(url("day") + f"?date={(TODAY + timedelta(days=16)).isoformat()}").content.decode()
        self.assertNotIn("pl-day-wx", too_far)

    def test_week_view_has_weather_on_every_column_not_on_home_items(self):
        self.set_home()
        self.item("Run", start_time=time(7, 0))
        page = self.client.get(url("week")).content.decode()
        self.assertIn("72°", page)
        self.assertIn("68°", page)          # Monday and Tuesday, before today
        self.assertIn("Run", page)
        # Seven column-head chips; the home item itself has none.
        self.assertEqual(_wx_chips(page), 7)
        self.assertNotIn("pl-wx__pin", page)

    def test_view_panel_shows_weather_and_an_away_place(self):
        self.set_home()
        trip = self.item("Game in Denver", kind=ScheduleItem.EVENT, start_time=time(19, 0))
        self.set_place(trip)
        page = self.client.get(url("item", trip.pk) + f"?date={TODAY.isoformat()}").content.decode()
        self.assertIn('id="pl-view-weather"', page)
        self.assertIn("65°", page)
        self.assertIn("Rain", page)
        self.assertIn("Denver, Colorado", page)


class AutoLocationTests(WeatherTests):
    def post_loc(self, lat=39.52, lon=-104.88):
        return self.client.post(
            url("weather_location"),
            data=json.dumps({"lat": lat, "lon": lon}),
            content_type="application/json",
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

    def test_endpoint_reverse_labels_and_saves(self):
        prefs = PlannerSettings.for_user(self.me)
        self.assertTrue(prefs.weather_auto)
        response = self.post_loc(39.52, -104.88)
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["ok"], True)
        self.assertEqual(data["changed"], True)
        self.assertEqual(data["auto"], True)
        self.assertEqual(data["label"], "Lone Tree, Colorado")
        prefs.refresh_from_db()
        self.assertEqual(prefs.weather_auto_label, "Lone Tree, Colorado")
        self.assertEqual(float(prefs.weather_auto_lat), 39.52)
        self.assertEqual(float(prefs.weather_auto_lon), -104.88)

    def test_a_nearby_point_is_not_saved_again(self):
        self.post_loc(39.52, -104.88)
        live = weather.requests.get
        live.reset_mock()
        again = self.post_loc(39.53, -104.89)        # about 1 km, under the 0.05° threshold
        self.assertFalse(again.json()["changed"])
        self.assertEqual(again.json()["label"], "Lone Tree, Colorado")
        self.assertFalse(any("nominatim" in (c.args[0] if c.args else "") for c in live.call_args_list))

    def test_a_farther_point_is_saved(self):
        self.post_loc(39.52, -104.88)
        response = self.post_loc(39.74, -104.99)
        self.assertTrue(response.json()["changed"])
        self.assertEqual(response.json()["label"], "Denver, Colorado")
        prefs = PlannerSettings.objects.get(user=self.me)
        self.assertEqual(prefs.weather_auto_label, "Denver, Colorado")
        self.assertEqual(float(prefs.weather_auto_lat), 39.74)

    def test_auto_off_does_not_save_and_uses_the_typed_home(self):
        self.set_home()
        prefs = PlannerSettings.for_user(self.me)
        prefs.weather_auto = False
        prefs.weather_auto_label = "Denver, Colorado"
        prefs.weather_auto_lat = Decimal("39.74000")
        prefs.weather_auto_lon = Decimal("-104.99000")
        prefs.save()
        response = self.post_loc(40.00, -105.00)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["changed"])
        self.assertFalse(response.json()["auto"])
        prefs.refresh_from_db()
        self.assertEqual(float(prefs.weather_auto_lat), 39.74)     # left alone
        page = self.client.get(url("day")).content.decode()
        self.assertIn("72°", page)
        self.assertIn("Lone Tree, Colorado", page)
        self.assertNotIn("65°", page)

    def test_auto_on_uses_the_gps_fix_not_the_typed_home(self):
        self.set_home()
        self.set_gps(label="Denver, Colorado", lat=39.74, lon=-104.99)
        page = self.client.get(url("day")).content.decode()
        self.assertIn("65°", page)
        self.assertIn("Denver, Colorado", page)
        self.assertIn("Rain", page)
        self.assertNotIn("72°", page)

    def test_auto_on_with_no_gps_yet_falls_back_to_the_typed_home(self):
        self.set_home()
        prefs = PlannerSettings.for_user(self.me)
        self.assertTrue(prefs.weather_auto)
        self.assertIsNone(prefs.weather_auto_lat)
        page = self.client.get(url("day")).content.decode()
        self.assertIn("72°", page)
        self.assertIn("Lone Tree, Colorado", page)

    def test_invalid_coordinates_are_refused(self):
        self.assertEqual(self.post_loc(lat=999, lon=0).status_code, 400)
        self.assertEqual(self.client.post(
            url("weather_location"), data=json.dumps({}),
            content_type="application/json",
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        ).status_code, 400)

    def test_nominatim_down_still_saves_coordinates(self):
        cache.clear()
        with mock.patch("albright_planner.weather.requests.get", side_effect=requests.Timeout):
            response = self.post_loc(39.52, -104.88)
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["ok"])
        self.assertTrue(data["changed"])
        self.assertEqual(data["label"], "39.52, -104.88")
        prefs = PlannerSettings.objects.get(user=self.me)
        self.assertEqual(prefs.weather_auto_label, "39.52, -104.88")

    def test_planner_pages_carry_the_auto_location_hooks(self):
        page = self.client.get(url("day")).content.decode()
        self.assertIn('data-planner-wx-auto="1"', page)
        self.assertIn(f'data-planner-wx-url="{url("weather_location")}"', page)
        self.assertIn("navigator.geolocation", page)
        self.assertIn("pl-wx-geo-denied", page)
        self.assertIn("enableHighAccuracy: false", page)
        prefs = PlannerSettings.for_user(self.me)
        prefs.weather_auto = False
        prefs.save(update_fields=["weather_auto"])
        off = self.client.get(url("day")).content.decode()
        self.assertIn('data-planner-wx-auto="0"', off)


class CurrentAndHourlyTests(WeatherTests):
    def test_today_has_the_big_current_temp_rain_chance_and_hourly_strip(self):
        self.set_home()
        self.item("Standup", kind=ScheduleItem.EVENT, start_time=time(9, 0))
        self.item("Lunch with Sam", kind=ScheduleItem.EVENT, start_time=time(12, 30))
        page = self.client.get(url("day")).content.decode()
        self.assertIn("pl-day-wx--hero", page)
        self.assertIn("pl-wx-hero__temp", page)
        self.assertIn("58°", page)
        self.assertIn("Feels 54°", page)
        self.assertIn("72°", page)
        self.assertIn("45°", page)
        self.assertIn("20%", page)
        self.assertIn("pl-wx__pop", page)
        self.assertIn("pl-wx-hourly", page)
        self.assertIn("pl-wx-hourly__nowtag", page)
        self.assertIn("Now", page)
        # Event ticks use the first word of the title.
        self.assertIn('pl-wx-hourly__event">Standup', page)
        self.assertIn('pl-wx-hourly__event">Lunch', page)
        self.assertIn("is-now", page)
        # The hourly window is 6 AM–10 PM (17 hours), and 10 AM is Now.
        self.assertIn(">6a<", page)
        self.assertIn(">10p<", page)
        self.assertNotIn(">5a<", page)
        self.assertNotIn(">11p<", page)

    def test_a_future_day_has_hourly_and_rain_but_no_current_temp(self):
        self.set_home()
        soon = TODAY + timedelta(days=1)
        self.item("Flight", kind=ScheduleItem.EVENT, date=soon, start_time=time(7, 0))
        page = self.client.get(url("day") + f"?date={soon.isoformat()}").content.decode()
        self.assertIn("pl-day-wx--hero", page)
        self.assertIn("70°", page)
        self.assertIn("40%", page)
        self.assertIn("pl-wx-hourly", page)
        self.assertIn('pl-wx-hourly__event">Flight', page)
        self.assertNotIn("pl-wx-hero__temp", page)
        self.assertNotIn("pl-wx-hourly__nowtag", page)
        self.assertNotIn("Feels", page)
        self.assertNotIn("pl-wx__now", page)

    def test_week_columns_have_a_rain_bar_and_today_shows_current_temp(self):
        self.set_home()
        self.item("Run", start_time=time(7, 0))
        page = self.client.get(url("week")).content.decode()
        self.assertIn("72°", page)
        self.assertIn("68°", page)
        self.assertIn("58°", page)             # today's current, small
        self.assertIn("pl-wx__now", page)
        self.assertIn("pl-wx-bar-row", page)
        self.assertIn("20%", page)
        self.assertIn("10%", page)             # Monday/Tuesday
        self.assertIn("40%", page)             # later in the week
        self.assertIn('style="width:20%"', page)
        self.assertEqual(_wx_chips(page), 7)
        self.assertNotIn("pl-wx-hourly", page)  # the strip is Day-only
        self.assertNotIn("pl-wx__pin", page)


class ShowWeatherTests(WeatherTests):
    def test_turning_it_off_hides_weather_everywhere_and_does_not_fetch(self):
        self.set_home()
        self.item("Morning run", start_time=time(7, 0))
        trip = self.item("Game in Denver", kind=ScheduleItem.EVENT, start_time=time(19, 0))
        self.set_place(trip)
        prefs = PlannerSettings.for_user(self.me)
        prefs.show_weather = False
        prefs.save(update_fields=["show_weather"])
        live = weather.requests.get
        live.reset_mock()
        page = self.client.get(url("day")).content.decode()
        self.assertIn("Morning run", page)
        self.assertIn("Game in Denver", page)
        self.assertNotIn("pl-day-wx", page)
        self.assertNotIn("pl-wx-hourly", page)
        self.assertNotIn('class="pl-wx', page)
        self.assertNotIn("72°", page)
        self.assertNotIn("65°", page)
        self.assertNotIn("58°", page)
        self.assertNotIn("pl-wx__pin", page)
        forecast_calls = [c for c in live.call_args_list if c.args and "api.open-meteo.com" in c.args[0]]
        self.assertEqual(forecast_calls, [])
        week = self.client.get(url("week")).content.decode()
        self.assertNotIn("pl-wx-bar-row", week)
        self.assertNotIn("72°", week)
        panel = self.client.get(url("item", trip.pk) + f"?date={TODAY.isoformat()}").content.decode()
        self.assertNotIn("pl-view-weather", panel)
        self.assertNotIn("65°", panel)

    def test_saving_the_settings_form_turns_it_off_and_back_on(self):
        self.set_home()
        self.client.post(url("settings"), {
            "weather_location": "Lone Tree, Colorado",
            "weather_auto": "1",
            "auto_timezone": "1",
            "complete_when_checklist_done": "1",
            "show_weather": "0",
        })
        prefs = PlannerSettings.objects.get(user=self.me)
        self.assertFalse(prefs.show_weather)
        # The geolocation hook is also off so the page doesn't ask for a location.
        page = self.client.get(url("day")).content.decode()
        self.assertIn('data-planner-wx-auto="0"', page)
        self.assertNotIn("72°", page)
        saved = self.client.get(url("settings")).content.decode()
        self.assertNotIn('name="show_weather" value="1" checked', saved)
        # The form sends hidden 0 and, when checked, checkbox 1; last value wins.
        self.client.post(url("settings"), {
            "weather_location": "Lone Tree, Colorado",
            "weather_auto": "1",
            "auto_timezone": "1",
            "complete_when_checklist_done": "1",
            "show_weather": ["0", "1"],
        })
        prefs.refresh_from_db()
        self.assertTrue(prefs.show_weather)
        on = self.client.get(url("day")).content.decode()
        self.assertIn("72°", on)
        self.assertIn("58°", on)

    def test_a_stale_form_without_the_field_leaves_it_on(self):
        """Older tests and a stale settings POST omit show_weather; don't flip it off."""
        self.set_home()
        self.client.post(url("settings"), {
            "weather_location": "Lone Tree, Colorado",
            "weather_auto": "1",
            "auto_timezone": "1",
        })
        prefs = PlannerSettings.objects.get(user=self.me)
        self.assertTrue(prefs.show_weather)
        page = self.client.get(url("day")).content.decode()
        self.assertIn("72°", page)
