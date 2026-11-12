"""Weather app: the forecast page, place chips, NWS alerts, AQI, the hourly
chart and 16-day outlook. Every HTTP call is mocked; a down API never
breaks the page. show_weather only hides forecasts on the Planner."""
from datetime import datetime, timedelta
from decimal import Decimal
from unittest import mock
from zoneinfo import ZoneInfo

import requests
from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from albright_labs.testing import with_apps
from albright_planner.models import PlannerSettings
from albright_planner.weather import LookupDown

from . import services
from .models import WeatherPlace

PASSWORD = "pw-not-real-12345"
TZ = ZoneInfo("America/Denver")
# Fixed "now" so hourly windows and sun times are stable.
NOW = datetime(2026, 11, 12, 7, 40, tzinfo=TZ)
LONE_TREE = (Decimal("39.52440"), Decimal("-104.88220"))


def _iso(dt):
    return dt.replace(tzinfo=None).strftime("%Y-%m-%dT%H:%M")


def _forecast_payload(now=NOW):
    """A complete Open-Meteo body around `now`: current, 48 hourly, 16 daily."""
    start_day = now.date()
    daily_times, highs, lows, codes, pops, sunrise, sunset, uv = [], [], [], [], [], [], [], []
    for i in range(16):
        day = start_day + timedelta(days=i)
        daily_times.append(day.isoformat())
        # Today 54 / 38, then a gentle climb so range bars have a spread.
        highs.append(54 + i)
        lows.append(38 + (i // 2))
        codes.append(80 if i == 0 else (71 if i == 1 else 2))
        pops.append(70 if i == 0 else (60 if i == 1 else 10 + i))
        sunrise.append(f"{day.isoformat()}T06:42")
        sunset.append(f"{day.isoformat()}T16:48")
        uv.append(2 if i == 0 else 3)
    hours, temps, hpops, winds, dews, hums, press, uv_h, hcodes = [], [], [], [], [], [], [], [], []
    hour0 = now.replace(minute=0, second=0, microsecond=0)
    for i in range(48):
        stamp = hour0 + timedelta(hours=i)
        hours.append(_iso(stamp))
        temps.append(39 + (i % 12) - 2)
        hpops.append(20 + (i % 8) * 8)
        winds.append(10 + (i % 5))
        dews.append(28)
        hums.append(61)
        press.append(1015.5)
        uv_h.append(0 if stamp.hour < 8 or stamp.hour > 16 else 2)
        hcodes.append(80 if i < 8 else 2)
    current = {
        "time": _iso(hour0),
        "temperature_2m": 39,
        "apparent_temperature": 34,
        "weather_code": 80,
        "relative_humidity_2m": 61,
        "wind_speed_10m": 12,
        "wind_direction_10m": 320,
        "surface_pressure": 1015.5,
    }
    return {
        "current": current,
        "hourly": {
            "time": hours,
            "temperature_2m": temps,
            "precipitation_probability": hpops,
            "weather_code": hcodes,
            "wind_speed_10m": winds,
            "dew_point_2m": dews,
            "relative_humidity_2m": hums,
            "surface_pressure": press,
            "uv_index": uv_h,
        },
        "daily": {
            "time": daily_times,
            "weather_code": codes,
            "temperature_2m_max": highs,
            "temperature_2m_min": lows,
            "precipitation_probability_max": pops,
            "sunrise": sunrise,
            "sunset": sunset,
            "uv_index_max": uv,
        },
    }


def _aqi_payload():
    return {"current": {"time": _iso(NOW.replace(minute=0)), "us_aqi": 42, "pm2_5": 8.1, "pm10": 12.0}}


def _alerts_payload():
    return {
        "features": [{
            "properties": {
                "event": "Winter Weather Advisory",
                "headline": "Winter Weather Advisory for northeast Colorado until 2 PM Friday",
                "severity": "Moderate",
                "onset": "2026-11-13T00:00:00-07:00",
                "ends": "2026-11-13T14:00:00-07:00",
            }
        }]
    }


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


def http_router(forecast=None, aqi=None, alerts=None, geocode=None, error=None):
    """A requests.get stand-in that routes by URL host/path."""
    forecast = _forecast_payload() if forecast is None else forecast
    aqi = _aqi_payload() if aqi is None else aqi
    alerts = _alerts_payload() if alerts is None else alerts
    geocode = {"results": [{"name": "Denver", "latitude": 39.7392, "longitude": -104.9903,
                             "admin1": "Colorado", "country": "United States", "country_code": "US"}]} if geocode is None else geocode

    def get(url, params=None, headers=None, timeout=None, **kwargs):
        if error is not None:
            raise error
        url = url or ""
        if "air-quality-api.open-meteo.com" in url:
            return FakeResponse(aqi)
        if "api.weather.gov" in url:
            return FakeResponse(alerts)
        if "geocoding-api.open-meteo.com" in url:
            return FakeResponse(geocode)
        if "api.open-meteo.com" in url:
            return FakeResponse(forecast)
        raise AssertionError(f"unexpected HTTP: {url}")

    return get


class Base(TestCase):
    def setUp(self):
        cache.clear()
        self.user = with_apps(User.objects.create_user(username="nate", password=PASSWORD),
                              ["planner", "weather"])
        settings = PlannerSettings.for_user(self.user)
        settings.timezone = "America/Denver"
        settings.weather_label = "Lone Tree, Colorado"
        settings.weather_lat, settings.weather_lon = LONE_TREE
        settings.weather_auto = True
        settings.weather_auto_label = "Current location"
        settings.weather_auto_lat, settings.weather_auto_lon = Decimal("39.73920"), Decimal("-104.99030")
        settings.show_weather = True
        settings.save()
        self.client.force_login(self.user)
        self.clock = mock.patch("albright_planner.weather.local_now", return_value=NOW)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        # Patch the requests module through this app's import only. Do not also
        # patch albright_planner.weather.requests.get: both names are the same
        # module attribute, and the second patch would replace this mock.
        self.http = mock.patch("albright_weather.services.requests.get", side_effect=http_router())
        self.http_obj = self.http.start()
        self.addCleanup(self.http.stop)
        self.geocode = mock.patch(
            "albright_planner.weather.geocode",
            return_value={"label": "Denver, Colorado", "lat": Decimal("39.73920"), "lon": Decimal("-104.99030")},
        )
        self.geocode.start()
        self.addCleanup(self.geocode.stop)

    def page(self, query=""):
        return self.client.get(reverse("albright_weather:home") + query)

    def html(self, query=""):
        response = self.page(query)
        self.assertEqual(response.status_code, 200)
        return response.content.decode()


class PageTests(Base):
    def test_hero_shows_current_conditions(self):
        html = self.html()
        self.assertIn('data-app="weather"', html)
        self.assertIn("albright_weather/weather.css", html)
        self.assertIn("39°", html)
        self.assertIn("Feels like", html)
        self.assertIn("34°", html)
        self.assertIn("Showers", html)
        self.assertIn("H", html)
        self.assertIn("54°", html)
        self.assertIn("L", html)
        self.assertIn("38°", html)
        self.assertIn("12", html)
        self.assertIn("NW", html)
        self.assertIn("61%", html)
        self.assertIn("humidity", html)
        # Viewer zone (America/Denver), not Django TIME_ZONE.
        self.assertIn("7:40 AM", html)
        self.assertIn("MST", html)
        self.assertNotIn("EST", html)

    def test_detail_cards_include_uv_dew_pressure_sun_and_aqi(self):
        html = self.html()
        self.assertIn("UV index", html)
        self.assertIn("Dew point", html)
        self.assertIn("28°", html)
        self.assertIn("Pressure", html)
        self.assertIn("inHg", html)
        self.assertIn("Sun", html)
        self.assertIn("6:42 AM", html)
        self.assertIn("Sunset 4:48 PM", html)
        self.assertIn("Air quality", html)
        self.assertIn("42", html)
        self.assertIn("Good", html)

    def test_alerts_show_nws_event_and_disappear_when_the_api_fails(self):
        html = self.html()
        self.assertIn("Winter Weather Advisory", html)
        cache.clear()
        self.http_obj.side_effect = http_router(alerts=requests.Timeout())
        # A timeout inside _fetch is caught; passing Timeout as payload would break json().
        def failing_get(url, **kwargs):
            if "api.weather.gov" in (url or ""):
                raise requests.Timeout("down")
            return http_router()(url, **kwargs)
        self.http_obj.side_effect = failing_get
        html = self.html()
        self.assertNotIn("Winter Weather Advisory", html)
        self.assertEqual(self.page().status_code, 200)

    def test_failed_nws_status_is_silent(self):
        cache.clear()
        self.http_obj.side_effect = http_router(alerts={"_ignored": True})
        # 500 from NWS: router returns a body; use a FakeResponse via custom get
        def get(url, **kwargs):
            if "api.weather.gov" in (url or ""):
                return FakeResponse({"title": "error"}, status_code=500)
            return http_router()(url, **kwargs)
        self.http_obj.side_effect = get
        html = self.html()
        self.assertNotIn("Winter Weather Advisory", html)
        self.assertIn("39°", html)

    def test_hourly_chart_is_server_svg(self):
        html = self.html()
        self.assertIn("<svg", html)
        self.assertIn('class="wx-chart__temp"', html)
        self.assertIn('class="wx-chart__rain"', html)
        self.assertIn("mph", html)
        self.assertNotIn("chart.js", html.lower())
        self.assertNotIn("Chart.js", html)

    def test_outlook_has_sixteen_days_and_range_bars(self):
        html = self.html()
        self.assertIn("16-day outlook", html)
        self.assertEqual(html.count('class="wx-day__range"'), 16)
        self.assertIn("Today", html)
        self.assertIn("wx-day__bar", html)

    def test_place_chips_include_home_and_current(self):
        html = self.html()
        self.assertIn("Lone Tree, Colorado", html)
        self.assertIn("Current location", html)
        # Default selection is Current when a GPS fix exists.
        self.assertIn("?place=current", html)
        self.assertRegex(html, r'wx-chip is-active[^>]*>Current location')

    def test_select_home_with_query_string(self):
        html = self.html("?place=home")
        self.assertRegex(html, r'wx-chip is-active[^>]*>Lone Tree, Colorado')

    def test_add_and_remove_a_saved_place(self):
        response = self.client.post(reverse("albright_weather:place_add"), {"q": "Denver"})
        self.assertEqual(response.status_code, 302)
        place = WeatherPlace.objects.get(user=self.user)
        self.assertEqual(place.label, "Denver, Colorado")
        self.assertIn(f"place=p{place.pk}", response["Location"])
        html = self.html(f"?place=p{place.pk}")
        self.assertIn("Denver, Colorado", html)
        self.assertRegex(html, r'wx-chip is-active[^>]*>Denver, Colorado')
        response = self.client.post(reverse("albright_weather:place_remove", args=[place.pk]))
        self.assertEqual(response.status_code, 302)
        self.assertFalse(WeatherPlace.objects.filter(pk=place.pk).exists())

    def test_cannot_remove_someone_elses_place(self):
        other = with_apps(User.objects.create_user(username="sam", password=PASSWORD), ["weather"])
        place = WeatherPlace.objects.create(user=other, label="Boulder", lat=40.01, lon=-105.27, sort=1)
        response = self.client.post(reverse("albright_weather:place_remove", args=[place.pk]))
        self.assertEqual(response.status_code, 404)
        self.assertTrue(WeatherPlace.objects.filter(pk=place.pk).exists())

    def test_geocode_down_leaves_the_page_up(self):
        with mock.patch("albright_planner.weather.geocode", side_effect=LookupDown()):
            response = self.client.post(reverse("albright_weather:place_add"), {"q": "Denver"}, follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("Couldn", response.content.decode())
        self.assertFalse(WeatherPlace.objects.filter(user=self.user).exists())

    def test_show_weather_off_does_not_hide_the_weather_app(self):
        PlannerSettings.for_user(self.user).show_weather = False
        PlannerSettings.objects.filter(user=self.user).update(show_weather=False)
        html = self.html()
        self.assertIn("39°", html)
        self.assertIn("Current conditions", html)

    def test_down_forecast_does_not_break_the_page(self):
        cache.clear()
        def get(url, **kwargs):
            if "api.open-meteo.com" in (url or "") and "air-quality" not in (url or ""):
                raise requests.Timeout("down")
            if "air-quality" in (url or "") or "weather.gov" in (url or ""):
                raise requests.ConnectionError("down")
            return http_router()(url, **kwargs)
        self.http_obj.side_effect = get
        response = self.page()
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn("isn’t available right now", html)
        self.assertNotIn("Traceback", html)

    def test_radar_and_tropics_links(self):
        html = self.html()
        self.assertIn("https://www.rainviewer.com/", html)
        self.assertIn("https://zoom.earth/", html)
        self.assertIn("https://www.nhc.noaa.gov/", html)
        self.assertIn("https://cera.coastalrisk.live/", html)
        self.assertIn("https://zoom.earth/storms/", html)
        self.assertIn("Live radar", html)
        self.assertIn('id="tropics"', html)

    def test_fahrenheit_only(self):
        html = self.html()
        self.assertNotIn("°C", html)
        self.assertIn("°", html)

    def test_anonymous_users_are_sent_to_sign_in(self):
        self.client.logout()
        response = self.page()
        self.assertEqual(response.status_code, 302)
        self.assertIn("login", response["Location"])

    def test_no_access_is_404(self):
        stranger = User.objects.create_user(username="kim", password=PASSWORD)
        self.client.force_login(stranger)
        self.assertEqual(self.page().status_code, 404)

    def test_nws_request_sends_a_user_agent(self):
        cache.clear()
        self.html()
        nws_calls = [c for c in self.http_obj.call_args_list if "api.weather.gov" in (c.args[0] if c.args else "")]
        self.assertTrue(nws_calls)
        headers = nws_calls[0].kwargs.get("headers") or {}
        self.assertIn("User-Agent", headers)
        self.assertTrue(headers["User-Agent"])

    def test_empty_state_when_no_places(self):
        PlannerSettings.objects.filter(user=self.user).update(
            weather_lat=None, weather_lon=None, weather_auto=False,
            weather_auto_lat=None, weather_auto_lon=None,
        )
        html = self.html()
        self.assertIn("Set a Home location", html)
        self.assertIn("Planner settings", html)


class ServiceTests(TestCase):
    def setUp(self):
        cache.clear()

    def test_hourly_chart_is_svg_without_a_library(self):
        now = NOW.replace(tzinfo=None)
        points = [
            services.HourPoint(now + timedelta(hours=i), (now.hour + i) % 24,
                               f"{(now.hour + i) % 24}", 30 + i, 10 * i, 8 + i)
            for i in range(12)
        ]
        svg = services.hourly_chart(points)
        self.assertTrue(str(svg).startswith("<svg"))
        self.assertIn("wx-chart__temp", svg)
        self.assertIn("wx-chart__rain", svg)

    def test_alerts_empty_on_unusable_payload(self):
        self.assertEqual(services.parse_alerts(None), [])
        self.assertEqual(services.parse_alerts({"features": "nope"}), [])
        self.assertEqual(services.parse_alerts({"features": [{}]}), [])

    def test_wind_compass(self):
        self.assertEqual(services.wind_dir(0), "N")
        self.assertEqual(services.wind_dir(320), "NW")
        self.assertEqual(services.wind_dir(None), "")

    def test_forecast_is_cached_and_timeout_is_used(self):
        get = mock.Mock(return_value=FakeResponse(_forecast_payload()))
        with mock.patch("albright_weather.services.requests.get", get):
            first = services.forecast_payload(39.52, -104.88, "America/Denver")
            second = services.forecast_payload(39.52, -104.88, "America/Denver")
        self.assertEqual(first, second)
        self.assertEqual(get.call_count, 1)
        self.assertEqual(get.call_args.kwargs["timeout"], services.TIMEOUT)
