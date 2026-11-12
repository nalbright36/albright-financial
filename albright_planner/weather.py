"""Weather for the Planner (idea #77): forecasts from Open-Meteo (no API key;
api.open-meteo.com and geocoding-api.open-meteo.com are on PythonAnywhere's
free-account whitelist). Reverse-geocoding a GPS fix uses Nominatim
(nominatim.openstreetmap.org, also on the whitelist), at city level only.

A person has a home location on PlannerSettings (weather_label / _lat / _lon).
When weather_auto is on, the browser posts a rough location to planner/location/
and that fix (weather_auto_*) is used instead; the typed home is the fallback
if they say no or turn the box off. An item can have its own place when it
won't happen at home. show_weather (default on) hides every forecast and
skips the fetches when off.

Day and Week show the home forecast at the top (the Day header and each Week
column): high/low, rain chance, and, on today, the current temperature. The
Day view also has an hourly temperature and rain-chance strip (today and
future days), with a Now marker and ticks for timed events. Items at home
don't repeat the forecast; an item at another place keeps a small pin and
that place's high. The item view panel still shows the full forecast.

Forecasts cover about the past week through the next 16 days, in the viewer's
time zone. Daily and hourly data is cached a few hours per rounded lat/lon;
the current temperature uses a shorter (20-minute) cache of its own. Past
that window, nothing is shown. Every fetch is wrapped: a down or slow API
never fails the page.
"""
import re
from collections import namedtuple
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests
from django.core.cache import cache

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
REVERSE_URL = "https://nominatim.openstreetmap.org/reverse"
CACHE_SECONDS = 4 * 3600
CURRENT_CACHE_SECONDS = 20 * 60
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
# Default hourly strip: 6 AM through 10 PM, expanded to include Now and events.
HOUR_FIRST = 6
HOUR_LAST = 22
# Rain-chance styling: "likely" at this percent or above.
POP_LIKELY = 50

# pop: daily chance of precipitation (0-100), or None if the API omitted it.
# temp / feels / current_hour: current conditions, only on the day that
# matches the API's current.time. hourly: a tuple of Hour for that date.
Weather = namedtuple(
    "Weather",
    "date high low label icon pop temp feels hourly current_hour",
    defaults=(None, None, None, (), None),
)
# hour: 0-23. events: short titles of timed items starting in this hour.
# label: "6a", "12p". is_now: the current hour on today.
Hour = namedtuple("Hour", "hour temp pop is_now events label", defaults=(False, (), ""))


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
    # wx2: daily + hourly payload (the previous planner.wx: key was daily only).
    return f"planner.wx2:{_round(lat):.2f}:{_round(lon):.2f}:{tz}"


def _current_key(lat, lon, tz="auto"):
    return f"planner.wxcur:{_round(lat):.2f}:{_round(lon):.2f}:{tz}"


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


def local_now(user):
    """The current datetime in the viewer's Planner zone (UTC if the zone is unknown)."""
    from django.utils import timezone as djtz
    name = _zone_name(user)
    try:
        tz = ZoneInfo("UTC") if name == "auto" else ZoneInfo(name)
    except ZoneInfoNotFoundError:
        tz = ZoneInfo("UTC")
    return djtz.now().astimezone(tz)


def forecasts(lat, lon, tz=None):
    """{date: Weather} for PAST_DAYS through the next FORECAST_DAYS at this point.
    Empty on any failure. tz: IANA name (the viewer's zone); default 'auto'.
    Daily and hourly data is cached CACHE_SECONDS; current temperature uses a
    separate CURRENT_CACHE_SECONDS key. One Open-Meteo call fills both; a later
    current-only refresh does not refetch the forecast."""
    tz = tz or "auto"
    try:
        pair = coords(lat, lon)
        if not pair:
            return {}
        fkey = _cache_key(pair[0], pair[1], tz)
        ckey = _current_key(pair[0], pair[1], tz)
        raw = cache.get(fkey)
        current = cache.get(ckey)
        if raw is None:
            payload = _fetch(pair[0], pair[1], tz, what="full")
            if payload is None:
                cache.set(fkey, {"_empty": True}, EMPTY_CACHE_SECONDS)
                return {}
            raw, current = _split_payload(payload)
            cache.set(fkey, raw, CACHE_SECONDS)
            cache.set(ckey, current if current else {"_empty": True}, CURRENT_CACHE_SECONDS)
        elif raw.get("_empty"):
            return {}
        elif current is None:
            payload = _fetch(pair[0], pair[1], tz, what="current")
            current = (
                payload.get("current")
                if isinstance(payload, dict) and isinstance(payload.get("current"), dict)
                else None
            )
            cache.set(ckey, current if current else {"_empty": True}, CURRENT_CACHE_SECONDS)
        if isinstance(current, dict) and current.get("_empty"):
            current = None
        return _parse(raw, current)
    except Exception:
        return {}


def _fetch(lat, lon, tz="auto", what="full"):
    params = {
        "latitude": float(lat),
        "longitude": float(lon),
        "temperature_unit": "fahrenheit",
        "timezone": tz or "auto",
        "current": "temperature_2m,apparent_temperature,weather_code",
    }
    if what != "current":
        params["daily"] = "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max"
        params["hourly"] = "temperature_2m,precipitation_probability,weather_code"
        params["forecast_days"] = FORECAST_DAYS
        params["past_days"] = PAST_DAYS
    try:
        response = requests.get(
            FORECAST_URL,
            params=params,
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
    if not isinstance(data, dict):
        return None
    if what != "current" and not isinstance(data.get("daily"), dict):
        return None
    return data


def _split_payload(payload):
    """((daily, hourly) raw, current dict or None) from a full Open-Meteo body,
    or from an older cache entry that was just the daily dict."""
    if not isinstance(payload, dict):
        return {"_empty": True}, None
    if isinstance(payload.get("daily"), dict):
        hourly = payload.get("hourly") if isinstance(payload.get("hourly"), dict) else {}
        raw = {"daily": payload["daily"], "hourly": hourly}
    elif "time" in payload and ("temperature_2m_max" in payload or "temperature_2m_min" in payload):
        raw = {"daily": payload, "hourly": {}}
    else:
        raw = payload
    current = payload.get("current") if isinstance(payload.get("current"), dict) else None
    return raw, current


def _num(values, i):
    try:
        return int(round(float(values[i])))
    except (IndexError, TypeError, ValueError):
        return None


def _hour_label(hour):
    try:
        hour = int(hour)
    except (TypeError, ValueError):
        return ""
    if hour == 0:
        return "12a"
    if hour < 12:
        return f"{hour}a"
    if hour == 12:
        return "12p"
    return f"{hour - 12}p"


def _parse_dt(raw):
    """A date or datetime from an Open-Meteo time string, or None."""
    if isinstance(raw, datetime):
        return raw
    if isinstance(raw, date) and not isinstance(raw, datetime):
        return raw
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        try:
            return date.fromisoformat(str(raw))
        except (TypeError, ValueError):
            return None


def _parse(raw, current=None):
    if not isinstance(raw, dict):
        return {}
    daily = raw.get("daily") if isinstance(raw.get("daily"), dict) else raw
    times = daily.get("time") or []
    codes = daily.get("weather_code") or daily.get("weathercode") or []
    highs = daily.get("temperature_2m_max") or []
    lows = daily.get("temperature_2m_min") or []
    pops = daily.get("precipitation_probability_max") or daily.get("precipitation_probability") or []
    out = {}
    for i, raw_day in enumerate(times):
        parsed = _parse_dt(raw_day)
        if parsed is None:
            continue
        day = parsed.date() if isinstance(parsed, datetime) else parsed
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
        pop = _num(pops, i)
        out[day] = Weather(day, high, low, label, icon, pop)
    hourly_by_day = _parse_hourly(raw.get("hourly") if isinstance(raw.get("hourly"), dict) else {})
    cur_temp, cur_feels, cur_hour, cur_day = _parse_current(current)
    for day, wx in list(out.items()):
        hours = hourly_by_day.get(day, ())
        temp = feels = current_hour = None
        if cur_day == day:
            temp, feels, current_hour = cur_temp, cur_feels, cur_hour
        out[day] = wx._replace(hourly=hours, temp=temp, feels=feels, current_hour=current_hour)
    return out


def _parse_hourly(hourly):
    """{date: tuple[Hour]} from an Open-Meteo hourly block. Missing values skipped."""
    if not hourly:
        return {}
    times = hourly.get("time") or []
    temps = hourly.get("temperature_2m") or []
    pops = hourly.get("precipitation_probability") or []
    by_day = {}
    for i, raw in enumerate(times):
        parsed = _parse_dt(raw)
        if not isinstance(parsed, datetime):
            continue
        day, hour = parsed.date(), parsed.hour
        temp, pop = _num(temps, i), _num(pops, i)
        if temp is None and pop is None:
            continue
        by_day.setdefault(day, []).append(Hour(hour, temp, pop, False, (), _hour_label(hour)))
    return {day: tuple(rows) for day, rows in by_day.items()}


def _parse_current(current):
    """(temp, feels, hour, date) from an Open-Meteo current block; any field may be None."""
    if not isinstance(current, dict) or current.get("_empty"):
        return None, None, None, None
    parsed = _parse_dt(current.get("time"))
    day = hour = None
    if isinstance(parsed, datetime):
        day, hour = parsed.date(), parsed.hour
    elif isinstance(parsed, date):
        day = parsed
    temp = _num([current.get("temperature_2m")], 0)
    feels = _num([current.get("apparent_temperature")], 0)
    if temp is None and feels is None:
        return None, None, hour, day
    return temp, feels, hour, day


def _short_title(title):
    """A short tick label for the hourly strip (first word, up to 12 characters)."""
    title = " ".join((title or "").split())
    if not title:
        return ""
    word = title.split(" ", 1)[0]
    if len(word) > 12:
        return word[:11] + "…"
    return word


def display_hours(wx, occs=None, now_hour=None, first=HOUR_FIRST, last=HOUR_LAST):
    """The Day view's hourly strip: 6 AM–10 PM, expanded to include Now and
    any timed events. Empty when there is no hourly data. now_hour (0-23) marks
    Now; leave it None on past and future days."""
    if wx is None or not getattr(wx, "hourly", None):
        return []
    hours = {h.hour: h for h in wx.hourly}
    events = {}
    for o in occs or []:
        if getattr(o, "is_meal", False):
            continue
        start = getattr(o, "start_time", None)
        if start is None:
            continue
        title = getattr(o, "title", "") or ""
        if not title and getattr(o, "item", None) is not None:
            title = getattr(o.item, "title", "") or ""
        short = _short_title(title)
        if short:
            events.setdefault(start.hour, []).append(short)
    lo, hi = first, last
    if now_hour is not None:
        lo, hi = min(lo, now_hour), max(hi, now_hour)
    if events:
        lo, hi = min(lo, min(events)), max(hi, max(events))
    lo, hi = max(0, lo), min(23, hi)
    out = []
    for h in range(lo, hi + 1):
        src = hours.get(h)
        ev = tuple(events.get(h, ()))
        is_now = now_hour is not None and h == now_hour
        if src is None:
            if ev or is_now:
                out.append(Hour(h, None, None, is_now, ev, _hour_label(h)))
            continue
        out.append(Hour(src.hour, src.temp, src.pop, is_now, ev, src.label or _hour_label(h)))
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


def enabled(user):
    """False when the person has turned 'Show weather' off (default is on)."""
    from .models import PlannerSettings
    if user is None:
        return True
    try:
        prefs = PlannerSettings.for_user(user)
    except Exception:
        return True
    return bool(getattr(prefs, "show_weather", True))


def _window(user):
    from . import planner
    start = planner.today(user) - timedelta(days=PAST_DAYS)
    return start, start + timedelta(days=PAST_DAYS + FORECAST_DAYS - 1)


def _in_window(day, start, end):
    return day is not None and start <= day <= end


def attach(user, occs, days):
    """Set o.weather (and o.weather_place, when the item is not at home) on
    each occurrence. Returns {date: Weather} for the user's home, for Day and
    Week headers. Never raises. When show_weather is off, returns {} and
    leaves occurrences alone (no fetches)."""
    from .models import PlannerSettings
    home_wx = {}
    try:
        prefs = PlannerSettings.for_user(user)
        if not getattr(prefs, "show_weather", True):
            return {}
        home_place = home(prefs)
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
