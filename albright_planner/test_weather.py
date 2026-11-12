"""Weather on Day and Week (idea #77): a home location on Planner settings, an
optional place on an item when it isn't at home, a forecast chip on each item
and a high/low line on the Day header and Week columns. Open-Meteo is called
through weather.py; every test mocks the HTTP. The page stays up if the API is
down, and past or far-off days show nothing. Forecasts are cached so a second
load of the same place doesn't hit the network."""
from datetime import date, time, timedelta
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
# Highs chosen so Lone Tree (72) and Denver (65) are obviously different in the HTML.
HOME_DAILY = {
    "time": [(TODAY + timedelta(days=i)).isoformat() for i in range(16)],
    "weather_code": [0] + [2] * 15,
    "temperature_2m_max": [72.4] + [70] * 15,
    "temperature_2m_min": [45.2] + [44] * 15,
}
AWAY_DAILY = {
    "time": [(TODAY + timedelta(days=i)).isoformat() for i in range(16)],
    "weather_code": [61] + [63] * 15,
    "temperature_2m_max": [65.0] + [60] * 15,
    "temperature_2m_min": [42.0] + [40] * 15,
}


class FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


def fake_get(url, params=None, **kwargs):
    """Route Open-Meteo calls by host (and, for forecasts, by rounded latitude)."""
    params = params or {}
    if "geocoding-api.open-meteo.com" in url:
        name = (params.get("name") or "").lower()
        if "lone tree" in name:
            return FakeResponse({"results": [LONE_TREE]})
        if "denver" in name:
            return FakeResponse({"results": [DENVER]})
        return FakeResponse({"results": []})
    if "api.open-meteo.com" in url:
        lat = round(float(params.get("latitude") or 0), 2)
        daily = AWAY_DAILY if lat == 39.74 else HOME_DAILY
        return FakeResponse({"daily": daily})
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


class ForecastTests(WeatherTests):
    def test_high_low_and_label_for_today(self):
        wx = weather.forecasts(39.5244, -104.8822)[TODAY]
        self.assertEqual((wx.high, wx.low, wx.label, wx.icon), (72, 45, "Clear", "clear"))

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


class SettingsLocationTests(WeatherTests):
    def test_settings_page_has_the_home_location_field(self):
        page = self.client.get(url("settings")).content.decode()
        self.assertIn('name="weather_location"', page)
        self.assertIn("Home location", page)

    def test_saving_a_place_geocodes_and_stores_it(self):
        response = self.client.post(url("settings"), {
            "weather_location": "Lone Tree, CO",
            "auto_timezone": "1",
            "complete_when_checklist_done": "1",
        })
        self.assertEqual(response.status_code, 302)
        prefs = PlannerSettings.objects.get(user=self.me)
        self.assertEqual(prefs.weather_label, "Lone Tree, Colorado")
        self.assertEqual(prefs.weather_lat, Decimal("39.52440"))
        self.assertEqual(prefs.weather_lon, Decimal("-104.88220"))
        saved = self.client.get(url("settings")).content.decode()
        self.assertIn('value="Lone Tree, Colorado"', saved)

    def test_saving_the_same_label_does_not_geocode_again(self):
        self.set_home()
        live = weather.requests.get
        live.reset_mock()
        self.client.post(url("settings"), {
            "weather_location": "Lone Tree, Colorado",
            "auto_timezone": "1",
            "complete_when_checklist_done": "1",
        })
        self.assertFalse(any("geocoding-api" in (c.args[0] if c.args else "") for c in live.call_args_list))

    def test_unknown_place_leaves_the_saved_one_and_shows_an_error(self):
        self.set_home()
        response = self.client.post(url("settings"), {
            "weather_location": "zzzzz-not-a-place",
            "auto_timezone": "1",
        }, follow=True)
        self.assertContains(response, "find that place")
        prefs = PlannerSettings.objects.get(user=self.me)
        self.assertEqual(prefs.weather_label, "Lone Tree, Colorado")

    def test_clearing_the_field_hides_weather(self):
        self.set_home()
        self.client.post(url("settings"), {"weather_location": "", "auto_timezone": "1"})
        prefs = PlannerSettings.objects.get(user=self.me)
        self.assertEqual(prefs.weather_label, "")
        self.assertIsNone(prefs.weather_lat)
        self.assertIsNone(prefs.weather_lon)

    def test_lookup_down_keeps_the_saved_place(self):
        self.set_home()
        with mock.patch("albright_planner.weather.requests.get", side_effect=requests.ConnectionError):
            response = self.client.post(url("settings"), {
                "weather_location": "Denver, CO",
                "auto_timezone": "1",
            }, follow=True)
        self.assertContains(response, "look up that place")
        prefs = PlannerSettings.objects.get(user=self.me)
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


class DayWeekWeatherTests(WeatherTests):
    def test_day_view_shows_the_header_and_an_item_chip(self):
        self.set_home()
        self.item("Morning run", kind=ScheduleItem.TASK, start_time=time(7, 0))
        page = self.client.get(url("day")).content.decode()
        self.assertIn('id="pl-day-wx"', page)
        self.assertIn("72°", page)
        self.assertIn("45°", page)
        self.assertIn("Clear", page)
        self.assertIn("Lone Tree, Colorado", page)
        self.assertIn("Morning run", page)
        self.assertIn('class="pl-wx', page)
        # The item chip (and the header) both mention 72°.
        self.assertGreaterEqual(page.count("72°"), 2)

    def test_second_load_does_not_hit_the_api_again(self):
        self.set_home()
        self.item("Run")
        live = weather.requests.get
        live.reset_mock()
        self.client.get(url("day"))
        self.client.get(url("day"))
        forecast_calls = [c for c in live.call_args_list if c.args and "api.open-meteo.com" in c.args[0]]
        self.assertEqual(len(forecast_calls), 1)

    def test_item_at_another_place_uses_that_forecast(self):
        self.set_home()
        trip = self.item("Game in Denver", kind=ScheduleItem.EVENT, start_time=time(19, 0))
        self.set_place(trip)
        page = self.client.get(url("day")).content.decode()
        # Header is still home (clear, 72°); the item is Denver rain at 65°.
        self.assertIn("72°", page)
        self.assertIn("65°", page)
        self.assertIn("Rain", page)
        self.assertIn("Denver, Colorado", page)
        self.assertIn("pl-wx__pin", page)
        self.assertIn("Game in Denver", page)

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
        self.assertIn("Rain", page)

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

    def test_past_day_shows_no_weather(self):
        self.set_home()
        self.item("Yesterday", date=TODAY - timedelta(days=1))
        page = self.client.get(url("day") + f"?date={(TODAY - timedelta(days=1)).isoformat()}").content.decode()
        self.assertNotIn("pl-day-wx", page)
        self.assertNotIn('class="pl-wx', page)

    def test_far_ahead_day_shows_no_weather(self):
        self.set_home()
        far = TODAY + timedelta(days=20)
        self.item("Far trip", date=far, kind=ScheduleItem.EVENT)
        page = self.client.get(url("day") + f"?date={far.isoformat()}").content.decode()
        self.assertNotIn("pl-day-wx", page)
        self.assertNotIn('class="pl-wx', page)

    def test_week_view_has_a_chip_on_the_column_and_the_item(self):
        self.set_home()
        self.item("Run", start_time=time(7, 0))
        page = self.client.get(url("week")).content.decode()
        self.assertIn("72°", page)
        self.assertIn("Run", page)
        self.assertIn('class="pl-wx', page)
        # Each of the 16-day window days that fall in this week (today is Wed,
        # week is Mon–Sun, so today plus Thu–Sun = 5 days of forecast) plus the item.
        self.assertGreaterEqual(page.count("72°"), 2)

    def test_view_panel_shows_weather_and_an_away_place(self):
        self.set_home()
        trip = self.item("Game in Denver", kind=ScheduleItem.EVENT, start_time=time(19, 0))
        self.set_place(trip)
        page = self.client.get(url("item", trip.pk) + f"?date={TODAY.isoformat()}").content.decode()
        self.assertIn('id="pl-view-weather"', page)
        self.assertIn("65°", page)
        self.assertIn("Rain", page)
        self.assertIn("Denver, Colorado", page)
