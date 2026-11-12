"""Weather for the Planner (idea #77): daily forecasts from Open-Meteo (no API
key; api.open-meteo.com and geocoding-api.open-meteo.com are on PythonAnywhere's
free-account whitelist).

A person has a home location on PlannerSettings (weather_label / _lat / _lon).
An item can have its own (location_label / _lat / _lon) when it won't happen
there; the forecast uses that instead. Day and Week attach a chip to each item
and a high/low on the Day header and each Week column (the home location).

Forecasts are cached a few hours per rounded lat/lon (one API call covers the
next 16 days). Past days and days past the forecast window show nothing. Every
fetch is wrapped: a down or slow API never fails the page. Geocoding is only
used when someone saves a location, and a failure is a form error, not a 500.
"""
import re
from collections import namedtuple
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP

import requests
from django.core.cache import cache

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
CACHE_SECONDS = 4 * 3600
EMPTY_CACHE_SECONDS = 10 * 60
FORECAST_DAYS = 16
TIMEOUT = 5
GEOCODE_TIMEOUT = 8
USER_AGENT = "AlbrightPlanner/1.0"
# Two decimal places is about 1 km: close enough for a daily forecast, and
# nearby items share a cache key.
COORD_PLACES = 2

Weather = namedtuple("Weather", "date high low label icon")


class LookupDown(Exception):
    """The geocoding API didn't answer; the place itself may be fine."""


# WMO weather interpretation codes -> (short label, icon key for the SVG).
# https://open-meteo.com/en/docs
_CODES = {
    0: ("Clear", "clear"),
    1: ("Mostly clear", "clear"),
    2: ("Partly cloudy", "partly"),
    3: ("Cloudy", "cloudy"),
    45: ("Fog", "fog"),
    48: ("Fog", "fog"),
    51: ("Drizzle", "rain"),
    53: ("Drizzle", "rain"),
    55: ("Drizzle", "rain"),
    56: ("Freezing drizzle", "rain"),
    57: ("Freezing drizzle", "rain"),
    61: ("Rain", "rain"),
    63: ("Rain", "rain"),
    65: ("Heavy rain", "rain"),
    66: ("Freezing rain", "rain"),
    67: ("Freezing rain", "rain"),
    71: ("Snow", "snow"),
    73: ("Snow", "snow"),
    75: ("Heavy snow", "snow"),
    77: ("Snow", "snow"),
    80: ("Showers", "rain"),
    81: ("Showers", "rain"),
    82: ("Heavy showers", "rain"),
    85: ("Snow showers", "snow"),
    86: ("Snow showers", "snow"),
    95: ("Thunderstorm", "storm"),
    96: ("Thunderstorm", "storm"),
    99: ("Thunderstorm", "storm"),
}

COORD_RE = re.compile(r"^\s*(-?\d{1,3}(?:\.\d+)?)\s*,\s*(-?\d{1,3}(?:\.\d+)?)\s*$")


def describe(code):
    """(label, icon) for a WMO weather code; unknown codes look like cloudy."""
    try:
        code = int(code)
    except (TypeError, ValueError):
        return "Cloudy", "cloudy"
    return _CODES.get(code, ("Cloudy", "cloudy"))


def _round(value):
    return round(float(value), COORD_PLACES)


def _cache_key(lat, lon):
    return f"planner.wx:{_round(lat):.2f}:{_round(lon):.2f}"


def _dec(value):
    return Decimal(str(value)).quantize(Decimal("0.00001"), rounding=ROUND_HALF_UP)


def coords(lat, lon):
    """(Decimal lat, Decimal lon), or None if they aren't a real point."""
    try:
        lat, lon = float(lat), float(lon)
    except (TypeError, ValueError):
        return None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    return _dec(lat), _dec(lon)


def _from_coords_text(query):
    match = COORD_RE.match(query or "")
    if not match:
        return None
    pair = coords(match.group(1), match.group(2))
    if not pair:
        return None
    lat, lon = pair
    return {"label": f"{_round(lat):.2f}, {_round(lon):.2f}", "lat": lat, "lon": lon}


def _place_label(hit):
    name = (hit.get("name") or "").strip()
    admin = (hit.get("admin1") or "").strip()
    country = (hit.get("country") or "").strip()
    code = (hit.get("country_code") or "").strip().upper()
    parts = [name] if name else []
    if admin and admin != name:
        parts.append(admin)
    if code not in ("US", "USA") and country and country not in parts:
        parts.append(country)
    return ", ".join(parts) or "Unknown place"


def geocode(query):
    """A place name (or "lat, lon") -> {label, lat, lon} with Decimal coordinates.
    None if nothing matches. Raises LookupDown if the API is unreachable."""
    query = (query or "").strip()
    if not query:
        return None
    direct = _from_coords_text(query)
    if direct:
        return direct
    if len(query) < 2:
        return None
    try:
        response = requests.get(
            GEOCODE_URL,
            params={"name": query, "count": 1, "language": "en"},
            headers={"Accept": "application/json", "User-Agent": USER_AGENT},
            timeout=GEOCODE_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise LookupDown() from exc
    if response.status_code >= 500 or response.status_code == 429:
        raise LookupDown()
    if response.status_code >= 400:
        return None
    try:
        data = response.json()
    except ValueError as exc:
        raise LookupDown() from exc
    results = data.get("results") or []
    if not results:
        return None
    hit = results[0]
    pair = coords(hit.get("latitude"), hit.get("longitude"))
    if not pair:
        return None
    return {"label": _place_label(hit), "lat": pair[0], "lon": pair[1]}


def same_place(label, lat, lon, query):
    """True if `query` is the already-saved label (no need to geocode again)."""
    return bool(query and label and query.strip() == label and lat is not None and lon is not None)


def forecasts(lat, lon):
    """{date: Weather} for the next FORECAST_DAYS at this point. Empty on any failure."""
    try:
        pair = coords(lat, lon)
        if not pair:
            return {}
        key = _cache_key(*pair)
        raw = cache.get(key)
        if raw is not None:
            return {} if raw.get("_empty") else _parse(raw)
        raw = _fetch(pair[0], pair[1])
        if raw is None:
            cache.set(key, {"_empty": True}, EMPTY_CACHE_SECONDS)
            return {}
        cache.set(key, raw, CACHE_SECONDS)
        return _parse(raw)
    except Exception:
        return {}


def _fetch(lat, lon):
    try:
        response = requests.get(
            FORECAST_URL,
            params={
                "latitude": float(lat),
                "longitude": float(lon),
                "daily": "weather_code,temperature_2m_max,temperature_2m_min",
                "temperature_unit": "fahrenheit",
                "timezone": "auto",
                "forecast_days": FORECAST_DAYS,
            },
            headers={"Accept": "application/json", "User-Agent": USER_AGENT},
            timeout=TIMEOUT,
        )
    except requests.RequestException:
        return None
    if response.status_code >= 400:
        return None
    try:
        data = response.json()
    except ValueError:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("daily"), dict):
        return None
    return data["daily"]


def _parse(daily):
    times = daily.get("time") or []
    codes = daily.get("weather_code") or daily.get("weathercode") or []
    highs = daily.get("temperature_2m_max") or []
    lows = daily.get("temperature_2m_min") or []
    out = {}
    for i, raw_day in enumerate(times):
        try:
            day = date.fromisoformat(raw_day)
        except (TypeError, ValueError):
            continue
        try:
            high = int(round(float(highs[i])))
            low = int(round(float(lows[i])))
        except (IndexError, TypeError, ValueError):
            continue
        try:
            code = codes[i]
        except IndexError:
            code = None
        label, icon = describe(code)
        out[day] = Weather(day, high, low, label, icon)
    return out


def home(prefs):
    """(label, lat, lon) for the user's saved home, or None."""
    if prefs is None or prefs.weather_lat is None or prefs.weather_lon is None:
        return None
    pair = coords(prefs.weather_lat, prefs.weather_lon)
    if not pair:
        return None
    return (prefs.weather_label or "Home", pair[0], pair[1])


def item_place(item):
    """(label, lat, lon) if the item has its own location, else None."""
    if item is None or getattr(item, "location_lat", None) is None or getattr(item, "location_lon", None) is None:
        return None
    pair = coords(item.location_lat, item.location_lon)
    if not pair:
        return None
    return (item.location_label or "Away", pair[0], pair[1])


def home_label(user):
    from .models import PlannerSettings
    found = home(PlannerSettings.for_user(user))
    return found[0] if found else ""


def _window(user):
    from . import planner
    start = planner.today(user)
    return start, start + timedelta(days=FORECAST_DAYS - 1)


def _in_window(day, start, end):
    return day is not None and start <= day <= end


def attach(user, occs, days):
    """Set o.weather (and o.weather_place, when the item is not at home) on
    each occurrence. Returns {date: Weather} for the user's home, for Day and
    Week headers. Never raises."""
    from .models import PlannerSettings
    home_wx = {}
    try:
        home_place = home(PlannerSettings.for_user(user))
        start, end = _window(user)
        wanted = {}

        def need(place):
            if place:
                wanted[_cache_key(place[1], place[2])] = (place[1], place[2])

        need(home_place)
        items = []
        for o in occs or []:
            if getattr(o, "is_meal", False) or not getattr(o, "item", None):
                continue
            items.append(o)
            if _in_window(getattr(o, "date", None), start, end):
                need(item_place(o.item) or home_place)
        for day in days or []:
            if _in_window(day, start, end):
                need(home_place)

        fetched = {key: forecasts(lat, lon) for key, (lat, lon) in wanted.items()}

        def lookup(place, day):
            if not place or not _in_window(day, start, end):
                return None
            return fetched.get(_cache_key(place[1], place[2]), {}).get(day)

        for o in items:
            own = item_place(o.item)
            place = own or home_place
            o.weather = lookup(place, o.date)
            o.weather_place = ""
            if o.weather and own:
                if not home_place or _cache_key(own[1], own[2]) != _cache_key(home_place[1], home_place[2]):
                    o.weather_place = own[0]

        if home_place:
            daily = fetched.get(_cache_key(home_place[1], home_place[2]), {})
            for day in days or []:
                wx = daily.get(day) if _in_window(day, start, end) else None
                if wx:
                    home_wx[day] = wx
    except Exception:
        pass
    return home_wx
