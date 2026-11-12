"""Weather for the Planner (idea #77): daily forecasts from Open-Meteo (no API
key; api.open-meteo.com and geocoding-api.open-meteo.com are on PythonAnywhere's
free-account whitelist). Reverse-geocoding a GPS fix uses Nominatim
(nominatim.openstreetmap.org, also on the whitelist), at city level only.

A person has a home location on PlannerSettings (weather_label / _lat / _lon).
When weather_auto is on, the browser posts a rough location to planner/location/
and that fix (weather_auto_*) is used instead; the typed home is the fallback
if they say no or turn the box off. An item can have its own place when it
won't happen at home.

Day and Week show the home forecast at the top (the Day header and each Week
column). Items at home don't repeat it; an item at another place keeps a small
pin and that place's high. The item view panel still shows the full forecast.

Forecasts cover about the past week through the next 16 days, in the viewer's
time zone, cached a few hours per rounded lat/lon. Past that window, nothing
is shown. Every fetch is wrapped: a down or slow API never fails the page.
"""
import re
from collections import namedtuple
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests
from django.core.cache import cache

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
REVERSE_URL = "https://nominatim.openstreetmap.org/reverse"
CACHE_SECONDS = 4 * 3600
EMPTY_CACHE_SECONDS = 10 * 60
REVERSE_CACHE_SECONDS = 30 * 24 * 3600
FORECAST_DAYS = 16
# A week of recent days so Prev from today, and Mon/Tue of the current week,
# still have weather. (Slice 1 only started at today, which made forecasts
# appear to flicker on and off when stepping day to day.)
PAST_DAYS = 7
TIMEOUT = 5
GEOCODE_TIMEOUT = 8
USER_AGENT = "AlbrightPlanner/1.0 (https://natealbright36.pythonanywhere.com)"
# Two decimal places is about 1 km: close enough for a daily forecast, and
# nearby items share a cache key.
COORD_PLACES = 2
# Save a new GPS fix only when it moved about this far (roughly 5 km).
MOVE_DEGREES = 0.05

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


def _cache_key(lat, lon, tz="auto"):
    return f"planner.wx:{_round(lat):.2f}:{_round(lon):.2f}:{tz}"


def _place_key(lat, lon):
    """A timezone-free key, for 'is this the same place?'."""
    return f"{_round(lat):.2f}:{_round(lon):.2f}"


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


def rounded(lat, lon):
    """The same point at COORD_PLACES, or None."""
    pair = coords(lat, lon)
    if not pair:
        return None
    return _dec(_round(pair[0])), _dec(_round(pair[1]))


def _from_coords_text(query):
    match = COORD_RE.match(query or "")
    if not match:
        return None
    pair = coords(match.group(1), match.group(2))
    if not pair:
        return None
    lat, lon = pair
    return {"label": f"{_round(lat):.2f}, {_round(lon):.2f}", "lat": lat, "lon": lon}


def _format_place(name, admin, country, code):
    parts = [name] if name else []
    if admin and admin != name:
        parts.append(admin)
    if (code or "").upper() not in ("US", "USA") and country and country not in parts:
        parts.append(country)
    return ", ".join(parts)


def _place_label(hit):
    name = (hit.get("name") or "").strip()
    admin = (hit.get("admin1") or "").strip()
    country = (hit.get("country") or "").strip()
    code = (hit.get("country_code") or "").strip().upper()
    return _format_place(name, admin, country, code) or "Unknown place"


def _reverse_label(data):
    """A city-level label from a Nominatim reverse payload, or ''."""
    addr = data.get("address") if isinstance(data, dict) else None
    if not isinstance(addr, dict):
        addr = {}
    name = (addr.get("city") or addr.get("town") or addr.get("village") or
            addr.get("municipality") or addr.get("hamlet") or addr.get("county") or
            (data.get("name") if isinstance(data, dict) else "") or "").strip()
    admin = (addr.get("state") or addr.get("region") or "").strip()
    country = (addr.get("country") or "").strip()
    code = (addr.get("country_code") or "").strip().upper()
    return _format_place(name, admin, country, code)


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


def _coord_label(lat, lon):
    return f"{_round(lat):.2f}, {_round(lon):.2f}"


def reverse_geocode(lat, lon):
    """A GPS point -> {label, lat, lon} at city level (Nominatim, zoom=10).
    Always returns a result for a valid point: the coordinate text if the
    lookup is down or empty. Never raises. The point is stored at 2 decimals."""
    pair = rounded(lat, lon)
    if not pair:
        return None
    lat, lon = pair
    fallback = {"label": _coord_label(lat, lon), "lat": lat, "lon": lon}
    key = f"planner.wxrev:{_round(lat):.2f}:{_round(lon):.2f}"
    cached = cache.get(key)
    if cached is not None:
        return cached if cached.get("label") else fallback
    data = _reverse_fetch(lat, lon)
    if data is None:
        cache.set(key, {"_empty": True}, EMPTY_CACHE_SECONDS)
        return fallback
    label = _reverse_label(data) or fallback["label"]
    found = {"label": label, "lat": lat, "lon": lon}
    cache.set(key, found, REVERSE_CACHE_SECONDS)
    return found


def _reverse_fetch(lat, lon):
    try:
        response = requests.get(
            REVERSE_URL,
            params={
                "lat": float(lat),
                "lon": float(lon),
                "format": "json",
                "zoom": 10,
                "addressdetails": 1,
                "accept-language": "en",
            },
            headers={"Accept": "application/json", "User-Agent": USER_AGENT},
            timeout=GEOCODE_TIMEOUT,
        )
    except requests.RequestException:
        return None
    if response.status_code >= 400:
        return None
    try:
        data = response.json()
    except ValueError:
        return None
    if not isinstance(data, dict) or data.get("error"):
        return None
    return data


def same_place(label, lat, lon, query):
    """True if `query` is the already-saved label (no need to geocode again)."""
    return bool(query and label and query.strip() == label and lat is not None and lon is not None)


def moved(lat1, lon1, lat2, lon2, threshold=MOVE_DEGREES):
    """True if the two points are farther than about `threshold` degrees, or one is missing."""
    a, b = coords(lat1, lon1), coords(lat2, lon2)
    if not a:
        return b is not None
    if not b:
        return True
    return abs(float(a[0]) - float(b[0])) > threshold or abs(float(a[1]) - float(b[1])) > threshold


def _zone_name(user):
    """The viewer's zone for forecast dates, so they line up with planner.today(user)."""
    from .models import DEFAULT_TIMEZONE, PlannerSettings
    name = DEFAULT_TIMEZONE
    try:
        if user is not None:
            name = PlannerSettings.for_user(user).timezone or DEFAULT_TIMEZONE
        ZoneInfo(name)
        return name
    except (ZoneInfoNotFoundError, ValueError, Exception):
        return "auto"


def forecasts(lat, lon, tz=None):
    """{date: Weather} for PAST_DAYS through the next FORECAST_DAYS at this point.
    Empty on any failure. tz: IANA name (the viewer's zone); default 'auto'."""
    tz = tz or "auto"
    try:
        pair = coords(lat, lon)
        if not pair:
            return {}
        key = _cache_key(pair[0], pair[1], tz)
        raw = cache.get(key)
        if raw is not None:
            return {} if raw.get("_empty") else _parse(raw)
        raw = _fetch(pair[0], pair[1], tz)
        if raw is None:
            cache.set(key, {"_empty": True}, EMPTY_CACHE_SECONDS)
            return {}
        cache.set(key, raw, CACHE_SECONDS)
        return _parse(raw)
    except Exception:
        return {}


def _fetch(lat, lon, tz="auto"):
    try:
        response = requests.get(
            FORECAST_URL,
            params={
                "latitude": float(lat),
                "longitude": float(lon),
                "daily": "weather_code,temperature_2m_max,temperature_2m_min",
                "temperature_unit": "fahrenheit",
                "timezone": tz or "auto",
                "forecast_days": FORECAST_DAYS,
                "past_days": PAST_DAYS,
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


def _num(values, i):
    try:
        return int(round(float(values[i])))
    except (IndexError, TypeError, ValueError):
        return None


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
        high, low = _num(highs, i), _num(lows, i)
        if high is None and low is None:
            continue
        if high is None:
            high = low
        if low is None:
            low = high
        try:
            code = codes[i]
        except IndexError:
            code = None
        label, icon = describe(code)
        out[day] = Weather(day, high, low, label, icon)
    return out


def home(prefs):
    """(label, lat, lon) for the place Day/Week should forecast: the last GPS
    fix while weather_auto is on, otherwise the typed home. None if neither."""
    if prefs is None:
        return None
    if getattr(prefs, "weather_auto", False):
        auto = _place(getattr(prefs, "weather_auto_label", "") or "Current location",
                      getattr(prefs, "weather_auto_lat", None),
                      getattr(prefs, "weather_auto_lon", None))
        if auto:
            return auto
    return _place(prefs.weather_label or "Home", prefs.weather_lat, prefs.weather_lon)


def _place(label, lat, lon):
    if lat is None or lon is None:
        return None
    pair = coords(lat, lon)
    if not pair:
        return None
    return (label or "Home", pair[0], pair[1])


def item_place(item):
    """(label, lat, lon) if the item has its own location, else None."""
    if item is None or getattr(item, "location_lat", None) is None or getattr(item, "location_lon", None) is None:
        return None
    return _place(item.location_label or "Away", item.location_lat, item.location_lon)


def home_label(user):
    from .models import PlannerSettings
    found = home(PlannerSettings.for_user(user))
    return found[0] if found else ""


def _window(user):
    from . import planner
    start = planner.today(user) - timedelta(days=PAST_DAYS)
    return start, start + timedelta(days=PAST_DAYS + FORECAST_DAYS - 1)


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
        tz = _zone_name(user)
        wanted = {}

        def need(place):
            if place:
                wanted[_cache_key(place[1], place[2], tz)] = (place[1], place[2])

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

        fetched = {key: forecasts(lat, lon, tz) for key, (lat, lon) in wanted.items()}

        def lookup(place, day):
            if not place or not _in_window(day, start, end):
                return None
            return fetched.get(_cache_key(place[1], place[2], tz), {}).get(day)

        for o in items:
            own = item_place(o.item)
            place = own or home_place
            o.weather = lookup(place, o.date)
            o.weather_place = ""
            if o.weather and own:
                if not home_place or _place_key(own[1], own[2]) != _place_key(home_place[1], home_place[2]):
                    o.weather_place = own[0]

        if home_place:
            daily = fetched.get(_cache_key(home_place[1], home_place[2], tz), {})
            for day in days or []:
                wx = daily.get(day) if _in_window(day, start, end) else None
                if wx:
                    home_wx[day] = wx
    except Exception:
        pass
    return home_wx
