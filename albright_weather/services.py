"""Weather-app fetches: a richer Open-Meteo forecast, US AQI, and NWS alerts.

Reuses albright_planner.weather helpers (geocode, describe, coords, the
viewer's time zone, timeouts, User-Agent) but keeps its own cache keys so
Planner's planner.wx2 / planner.wxcur stay unchanged. Every fetch is
timeout-wrapped; a down or slow API returns empty rather than raising, and
never breaks the page.
"""
from collections import namedtuple
from datetime import datetime, timedelta
from decimal import Decimal
from xml.sax.saxutils import escape

import requests
from django.core.cache import cache
from django.utils.safestring import mark_safe

from albright_planner import weather as wx

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
AQI_URL = "https://air-quality-api.open-meteo.com/v1/air-quality"
ALERTS_URL = "https://api.weather.gov/alerts/active"
RAINVIEWER_URL = "https://www.rainviewer.com/"
ZOOM_EARTH_URL = "https://zoom.earth/"
NHC_URL = "https://www.nhc.noaa.gov/"
CERA_URL = "https://cera.coastalrisk.live/"
ZOOM_EARTH_STORMS_URL = "https://zoom.earth/storms/"

FORECAST_CACHE_SECONDS = 60 * 60
AQI_CACHE_SECONDS = 30 * 60
ALERTS_CACHE_SECONDS = 10 * 60
EMPTY_CACHE_SECONDS = wx.EMPTY_CACHE_SECONDS
TIMEOUT = wx.TIMEOUT
USER_AGENT = wx.USER_AGENT
HOURS = 48
FORECAST_DAYS = 16

# cache.get sentinel so a cached empty payload isn't confused with a miss
_MISSING = object()

Current = namedtuple(
    "Current",
    "temp feels label icon high low wind wind_dir humidity",
)
Details = namedtuple(
    "Details",
    "uv uv_label dew pressure sunrise sunset aqi aqi_label aqi_tone",
)
HourPoint = namedtuple("HourPoint", "when hour label temp pop wind")
DayOutlook = namedtuple(
    "DayOutlook",
    "date weekday is_today label icon high low pop bar_left bar_width",
)
Alert = namedtuple("Alert", "event headline severity onset ends")
PlaceChip = namedtuple("PlaceChip", "key label kind href selected removable pk")
Chosen = namedtuple("Chosen", "key label lat lon kind pk")

_COMPASS = (
    "N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
    "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW",
)
_AQI = (
    (50, "Good", "good"),
    (100, "Moderate", "moderate"),
    (150, "Unhealthy for sensitive groups", "usg"),
    (200, "Unhealthy", "unhealthy"),
    (300, "Very unhealthy", "very"),
    (None, "Hazardous", "hazardous"),
)
_UV = (
    (2, "Low"),
    (5, "Moderate"),
    (7, "High"),
    (10, "Very high"),
    (None, "Extreme"),
)


def _round_key(lat, lon):
    return f"{wx._round(lat):.2f}:{wx._round(lon):.2f}"


def _fetch(url, params=None, timeout=TIMEOUT, headers=None):
    """GET json, or None on any failure. Never raises."""
    hdrs = {"Accept": "application/json", "User-Agent": USER_AGENT}
    if headers:
        hdrs.update(headers)
    try:
        response = requests.get(url, params=params, headers=hdrs, timeout=timeout)
    except requests.RequestException:
        return None
    if response.status_code >= 400:
        return None
    try:
        data = response.json()
    except ValueError:
        return None
    return data if isinstance(data, dict) or isinstance(data, list) else None


def _cached(key, seconds, loader):
    cached = cache.get(key, _MISSING)
    if cached is not _MISSING:
        return None if isinstance(cached, dict) and cached.get("_empty") else cached
    data = loader()
    cache.set(key, data if data else {"_empty": True}, seconds if data else EMPTY_CACHE_SECONDS)
    return data


def forecast_payload(lat, lon, tz):
    """Raw Open-Meteo body for this point (cached), or None."""
    key = f"weather.app.fc:{_round_key(lat, lon)}:{tz}"

    def load():
        return _fetch(FORECAST_URL, {
            "latitude": float(lat),
            "longitude": float(lon),
            "current": ",".join((
                "temperature_2m", "apparent_temperature", "weather_code",
                "relative_humidity_2m", "wind_speed_10m", "wind_direction_10m",
                "surface_pressure",
            )),
            "hourly": ",".join((
                "temperature_2m", "precipitation_probability", "weather_code",
                "wind_speed_10m", "dew_point_2m", "relative_humidity_2m",
                "surface_pressure", "uv_index",
            )),
            "daily": ",".join((
                "weather_code", "temperature_2m_max", "temperature_2m_min",
                "precipitation_probability_max", "sunrise", "sunset", "uv_index_max",
            )),
            "temperature_unit": "fahrenheit",
            "wind_speed_unit": "mph",
            "timezone": tz or "auto",
            "forecast_days": FORECAST_DAYS,
        })

    return _cached(key, FORECAST_CACHE_SECONDS, load)


def aqi_payload(lat, lon, tz):
    """Open-Meteo air-quality body (cached), or None."""
    key = f"weather.app.aqi:{_round_key(lat, lon)}:{tz}"

    def load():
        return _fetch(AQI_URL, {
            "latitude": float(lat),
            "longitude": float(lon),
            "current": "us_aqi,pm2_5,pm10",
            "timezone": tz or "auto",
        })

    return _cached(key, AQI_CACHE_SECONDS, load)


def alerts_payload(lat, lon):
    """NWS active alerts GeoJSON (cached), or None. Failures are cached empty."""
    key = f"weather.app.nws:{_round_key(lat, lon)}"

    def load():
        return _fetch(
            ALERTS_URL,
            {"point": f"{float(lat):.4f},{float(lon):.4f}"},
            timeout=TIMEOUT,
            headers={"Accept": "application/geo+json", "User-Agent": USER_AGENT},
        )

    return _cached(key, ALERTS_CACHE_SECONDS, load)


def wind_dir(degrees):
    try:
        deg = float(degrees) % 360
    except (TypeError, ValueError):
        return ""
    return _COMPASS[int((deg + 11.25) // 22.5) % 16]


def _band(value, table):
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None, "", ""
    for cap, *rest in table:
        if cap is None or n <= cap:
            if len(rest) == 2:
                return int(round(n)), rest[0], rest[1]
            return int(round(n)), rest[0], ""
    return int(round(n)), table[-1][1], table[-1][2] if len(table[-1]) > 2 else ""


def _hpa_to_inhg(hpa):
    try:
        return round(float(hpa) * 0.02953, 2)
    except (TypeError, ValueError):
        return None


def _parse_clock(raw, tz):
    parsed = wx._parse_dt(raw)
    if not isinstance(parsed, datetime):
        return None, ""
    if parsed.tzinfo is None and tz and tz != "auto":
        try:
            from zoneinfo import ZoneInfo
            parsed = parsed.replace(tzinfo=ZoneInfo(tz))
        except Exception:
            pass
    # Built by hand: strftime's "%-I" is Linux-only and raises on Windows.
    return parsed, f"{parsed.hour % 12 or 12}:{parsed.minute:02d} {'AM' if parsed.hour < 12 else 'PM'}"


def _hour_from_current(hourly, current):
    """Index of the hourly row that matches current.time, or None."""
    if not isinstance(hourly, dict) or not isinstance(current, dict):
        return None
    want = str(current.get("time") or "")
    if not want:
        return None
    times = hourly.get("time") or []
    # current is often "2026-11-12T07:00"; hourly uses the same shape
    if want in times:
        return times.index(want)
    # fall back to the same calendar hour
    parsed = wx._parse_dt(want)
    if not isinstance(parsed, datetime):
        return None
    for i, raw in enumerate(times):
        other = wx._parse_dt(raw)
        if isinstance(other, datetime) and other.replace(minute=0, second=0, microsecond=0) == parsed.replace(minute=0, second=0, microsecond=0):
            return i
    return None


def _series(block, name):
    values = (block or {}).get(name) if isinstance(block, dict) else None
    return values if isinstance(values, list) else []


def parse_current(payload):
    """Current conditions + today's H/L, or None if nothing usable."""
    if not isinstance(payload, dict):
        return None
    current = payload.get("current") if isinstance(payload.get("current"), dict) else {}
    daily = payload.get("daily") if isinstance(payload.get("daily"), dict) else {}
    temp = wx._num([current.get("temperature_2m")], 0)
    feels = wx._num([current.get("apparent_temperature")], 0)
    if temp is None and feels is None:
        return None
    label, icon = wx.describe(current.get("weather_code"))
    humidity = wx._num([current.get("relative_humidity_2m")], 0)
    wind = wx._num([current.get("wind_speed_10m")], 0)
    direction = wind_dir(current.get("wind_direction_10m"))
    high = wx._num(_series(daily, "temperature_2m_max"), 0)
    low = wx._num(_series(daily, "temperature_2m_min"), 0)
    # If current is missing a temp but hourly has one, leave it; hero still shows H/L.
    return Current(temp, feels, label, icon, high, low, wind, direction, humidity)


def parse_details(payload, aqi_body, tz):
    """UV, dew point, pressure, sunrise/sunset, AQI — any field may be empty."""
    payload = payload if isinstance(payload, dict) else {}
    current = payload.get("current") if isinstance(payload.get("current"), dict) else {}
    hourly = payload.get("hourly") if isinstance(payload.get("hourly"), dict) else {}
    daily = payload.get("daily") if isinstance(payload.get("daily"), dict) else {}
    idx = _hour_from_current(hourly, current)

    def hourly_num(name):
        if idx is None:
            return None
        return wx._num(_series(hourly, name), idx)

    uv = hourly_num("uv_index")
    if uv is None:
        uv = wx._num(_series(daily, "uv_index_max"), 0)
    uv, uv_label, _ = _band(uv, _UV) if uv is not None else (None, "", "")
    dew = hourly_num("dew_point_2m")
    pressure = _hpa_to_inhg(current.get("surface_pressure"))
    if pressure is None:
        pressure = _hpa_to_inhg(hourly_num("surface_pressure"))
    _rise, sunrise = _parse_clock((_series(daily, "sunrise") or [None])[0] if _series(daily, "sunrise") else None, tz)
    _set, sunset = _parse_clock((_series(daily, "sunset") or [None])[0] if _series(daily, "sunset") else None, tz)
    aqi = aqi_label = aqi_tone = None
    if isinstance(aqi_body, dict):
        cur = aqi_body.get("current") if isinstance(aqi_body.get("current"), dict) else {}
        aqi, aqi_label, aqi_tone = _band(cur.get("us_aqi"), _AQI)
        if aqi is None:
            aqi_label, aqi_tone = "", ""
    return Details(uv, uv_label, dew, pressure, sunrise, sunset, aqi, aqi_label, aqi_tone or "")


def parse_hourly(payload, now):
    """The next HOURS hours from `now` (inclusive), skipping hours already past."""
    if not isinstance(payload, dict) or not isinstance(now, datetime):
        return []
    hourly = payload.get("hourly") if isinstance(payload.get("hourly"), dict) else {}
    times = _series(hourly, "time")
    temps = _series(hourly, "temperature_2m")
    pops = _series(hourly, "precipitation_probability")
    winds = _series(hourly, "wind_speed_10m")
    start = now.replace(minute=0, second=0, microsecond=0)
    # Compare timezone-naive ISO times in the forecast zone against `now`'s clock.
    if start.tzinfo is not None:
        start_naive = start.replace(tzinfo=None)
    else:
        start_naive = start
    end_naive = start_naive + timedelta(hours=HOURS)
    out = []
    for i, raw in enumerate(times):
        parsed = wx._parse_dt(raw)
        if not isinstance(parsed, datetime):
            continue
        stamp = parsed.replace(tzinfo=None)
        if stamp < start_naive or stamp >= end_naive:
            continue
        out.append(HourPoint(
            stamp,
            stamp.hour,
            wx._hour_label(stamp.hour),
            wx._num(temps, i),
            wx._num(pops, i),
            wx._num(winds, i),
        ))
        if len(out) >= HOURS:
            break
    return out


def parse_outlook(payload, today):
    """Up to 16 daily rows with range-bar positions, or []."""
    if not isinstance(payload, dict):
        return []
    daily = payload.get("daily") if isinstance(payload.get("daily"), dict) else {}
    times = _series(daily, "time")
    highs, lows = _series(daily, "temperature_2m_max"), _series(daily, "temperature_2m_min")
    pops, codes = _series(daily, "precipitation_probability_max"), _series(daily, "weather_code")
    rows = []
    for i, raw in enumerate(times):
        parsed = wx._parse_dt(raw)
        if parsed is None:
            continue
        day = parsed.date() if isinstance(parsed, datetime) else parsed
        high, low = wx._num(highs, i), wx._num(lows, i)
        if high is None and low is None:
            continue
        if high is None:
            high = low
        if low is None:
            low = high
        label, icon = wx.describe(codes[i] if i < len(codes) else None)
        weekday = "Today" if today and day == today else day.strftime("%a")
        rows.append({
            "date": day, "weekday": weekday, "is_today": bool(today and day == today),
            "label": label, "icon": icon, "high": high, "low": low,
            "pop": wx._num(pops, i),
        })
        if len(rows) >= FORECAST_DAYS:
            break
    if not rows:
        return []
    floor = min(r["low"] for r in rows)
    ceil = max(r["high"] for r in rows)
    span = max(ceil - floor, 1)
    out = []
    for r in rows:
        left = (r["low"] - floor) / span * 100
        width = (r["high"] - r["low"]) / span * 100
        out.append(DayOutlook(
            r["date"], r["weekday"], r["is_today"], r["label"], r["icon"],
            r["high"], r["low"], r["pop"], round(left, 2), max(round(width, 2), 2),
        ))
    return out


def parse_alerts(body):
    """Active NWS alerts, or [] if the payload is missing or unusable."""
    if not isinstance(body, dict):
        return []
    features = body.get("features") or []
    out = []
    for feat in features:
        props = feat.get("properties") if isinstance(feat, dict) else None
        if not isinstance(props, dict):
            continue
        event = (props.get("event") or "").strip()
        if not event:
            continue
        headline = (props.get("headline") or "").strip()
        severity = (props.get("severity") or "").strip()
        onset = _alert_when(props.get("onset") or props.get("effective"))
        ends = _alert_when(props.get("ends") or props.get("expires"))
        out.append(Alert(event, headline, severity, onset, ends))
    return out


def _alert_when(raw):
    parsed = wx._parse_dt(raw)
    if not isinstance(parsed, datetime):
        return ""
    try:
        return parsed.strftime("%a %-I:%M %p")
    except ValueError:
        return parsed.strftime("%a %I:%M %p").replace(" 0", " ")


def hourly_chart(points):
    """A server-rendered SVG (temp line, rain bars, wind labels). Safe HTML."""
    if not points:
        return ""
    width, height = 800, 220
    pad_l, pad_r, pad_t, pad_b = 36, 16, 18, 36
    plot_w = width - pad_l - pad_r
    plot_h = height - pad_t - pad_b
    temps = [p.temp for p in points if p.temp is not None]
    if not temps:
        return ""
    tmin, tmax = min(temps), max(temps)
    if tmin == tmax:
        tmin, tmax = tmin - 5, tmax + 5
    # a little headroom so the line doesn't kiss the frame
    pad = max((tmax - tmin) * 0.12, 2)
    tmin, tmax = tmin - pad, tmax + pad
    n = len(points)
    def x_at(i):
        if n == 1:
            return pad_l + plot_w / 2
        return pad_l + plot_w * i / (n - 1)

    def y_at(temp):
        return pad_t + plot_h * (1 - (temp - tmin) / (tmax - tmin))

    bar_w = max(plot_w / max(n, 1) * 0.45, 2)
    parts = [
        f'<svg class="wx-chart__svg" viewBox="0 0 {width} {height}" role="img" '
        f'aria-label="Hourly temperature, rain chance, and wind for the next {n} hours" '
        f'preserveAspectRatio="none">',
        f'<rect class="wx-chart__bg" x="0" y="0" width="{width}" height="{height}" />',
    ]
    # rain bars sit in the lower third of the plot, 0-100%
    rain_top = pad_t + plot_h * 0.55
    rain_h = plot_h * 0.45
    for i, p in enumerate(points):
        pop = 0 if p.pop is None else max(0, min(100, p.pop))
        if pop <= 0:
            continue
        h = rain_h * (pop / 100)
        x = x_at(i) - bar_w / 2
        y = rain_top + rain_h - h
        parts.append(
            f'<rect class="wx-chart__rain" x="{x:.2f}" y="{y:.2f}" width="{bar_w:.2f}" '
            f'height="{h:.2f}" rx="1"><title>{p.label}: {pop}% chance of rain</title></rect>'
        )
    # temperature line
    coords = []
    for i, p in enumerate(points):
        if p.temp is None:
            continue
        coords.append(f"{x_at(i):.2f},{y_at(p.temp):.2f}")
    if coords:
        parts.append(f'<polyline class="wx-chart__temp" fill="none" points="{" ".join(coords)}" />')
        for i, p in enumerate(points):
            if p.temp is None:
                continue
            parts.append(
                f'<circle class="wx-chart__dot" cx="{x_at(i):.2f}" cy="{y_at(p.temp):.2f}" r="2.2">'
                f'<title>{p.label}: {p.temp}°</title></circle>'
            )
    # y-axis labels (high / mid / low)
    for temp in (tmax, (tmin + tmax) / 2, tmin):
        y = y_at(temp)
        label = str(int(round(temp)))
        parts.append(
            f'<text class="wx-chart__axis" x="{pad_l - 6}" y="{y + 3:.2f}" text-anchor="end">{escape(label)}</text>'
        )
    # x-axis: every 3 hours, plus wind under a sparser cadence
    for i, p in enumerate(points):
        if i % 3 != 0 and i != n - 1:
            continue
        x = x_at(i)
        parts.append(
            f'<text class="wx-chart__axis wx-chart__hour" x="{x:.2f}" y="{height - 18}" '
            f'text-anchor="middle">{escape(p.label)}</text>'
        )
        if p.wind is not None and (i % 6 == 0 or i == 0):
            parts.append(
                f'<text class="wx-chart__wind" x="{x:.2f}" y="{height - 6}" '
                f'text-anchor="middle">{escape(str(p.wind))} mph</text>'
            )
    parts.append("</svg>")
    return mark_safe("".join(parts))


def radar_links(lat, lon):
    """Outbound radar links for this point (Leaflet radar is a later slice)."""
    try:
        lat_f, lon_f = float(lat), float(lon)
        rain = f"https://www.rainviewer.com/map.html?loc={lat_f:.4f},{lon_f:.4f},7"
        zoom = f"https://zoom.earth/maps/radar/#view={lat_f:.4f},{lon_f:.4f},8z"
    except (TypeError, ValueError):
        rain, zoom = RAINVIEWER_URL, ZOOM_EARTH_URL
    return {"rainviewer": rain, "zoom_earth": zoom}


def tropics_links():
    return {"nhc": NHC_URL, "cera": CERA_URL, "zoom_earth": ZOOM_EARTH_STORMS_URL}


def load_place(lat, lon, user):
    """Everything the Weather page needs for one point. Never raises."""
    tz = wx._zone_name(user)
    now = wx.local_now(user)
    today = now.date()
    try:
        payload = forecast_payload(lat, lon, tz)
    except Exception:
        payload = None
    try:
        aqi_body = aqi_payload(lat, lon, tz)
    except Exception:
        aqi_body = None
    try:
        alerts_body = alerts_payload(lat, lon)
    except Exception:
        alerts_body = None
    hours = parse_hourly(payload, now)
    return {
        "current": parse_current(payload),
        "details": parse_details(payload, aqi_body, tz),
        "hours": hours,
        "chart": hourly_chart(hours),
        "outlook": parse_outlook(payload, today),
        "alerts": parse_alerts(alerts_body),
        "radar": radar_links(lat, lon),
        "tropics": tropics_links(),
        "now": now,
        "tz": tz,
        "offline": payload is None,
    }


def typed_home(prefs):
    """The typed Home location, even when weather_auto is on. None if unset."""
    if prefs is None:
        return None
    return wx._place(prefs.weather_label or "Home", prefs.weather_lat, prefs.weather_lon)


def current_location(prefs):
    """The last GPS fix, if weather_auto is on and a point has been stored."""
    if prefs is None or not getattr(prefs, "weather_auto", False):
        return None
    return wx._place(
        getattr(prefs, "weather_auto_label", "") or "Current location",
        getattr(prefs, "weather_auto_lat", None),
        getattr(prefs, "weather_auto_lon", None),
    )


def chips_for(user, selected_key):
    """Home, Current location, and the user's saved places as chips."""
    from albright_planner.models import PlannerSettings
    from django.urls import reverse

    from .models import WeatherPlace

    prefs = PlannerSettings.for_user(user)
    items = []
    home = typed_home(prefs)
    if home:
        items.append(("home", home[0] or "Home", "home", None, home[1], home[2]))
    current = current_location(prefs)
    if current:
        items.append(("current", current[0] or "Current location", "current", None, current[1], current[2]))
    for place in WeatherPlace.objects.filter(user=user):
        items.append((f"p{place.pk}", place.label, "saved", place.pk, place.lat, place.lon))
    if not items:
        return [], None
    valid = {row[0] for row in items}
    key = selected_key if selected_key in valid else None
    if key is None:
        # Prefer Current, then Home, then the first saved place.
        for candidate in ("current", "home"):
            if candidate in valid:
                key = candidate
                break
        if key is None:
            key = items[0][0]
    base = reverse("albright_weather:home")
    chips = []
    chosen = None
    for row_key, label, kind, pk, lat, lon in items:
        selected = row_key == key
        chips.append(PlaceChip(
            row_key, label, kind, f"{base}?place={row_key}", selected, kind == "saved", pk,
        ))
        if selected:
            chosen = Chosen(row_key, label, lat, lon, kind, pk)
    return chips, chosen


def add_place(user, query):
    """Geocode `query` through Open-Meteo and save it. Returns (place, error)."""
    from .models import WeatherPlace

    query = (query or "").strip()
    if not query:
        return None, "Type a city or a pair of coordinates."
    try:
        found = wx.geocode(query)
    except wx.LookupDown:
        return None, "Couldn't look that up right now. Try again in a moment."
    if not found:
        return None, f"No match for “{query}”."
    nxt = (WeatherPlace.objects.filter(user=user).order_by("-sort").values_list("sort", flat=True).first() or 0) + 1
    place = WeatherPlace.objects.create(
        user=user, label=found["label"], lat=found["lat"], lon=found["lon"], sort=nxt,
    )
    return place, None


def tile_line(user):
    """Home tile: the current temperature and today's condition at Home / Current."""
    from albright_planner.models import PlannerSettings

    prefs = PlannerSettings.for_user(user)
    place = current_location(prefs) or typed_home(prefs)
    if not place:
        return {"value": "Set a place", "note": "Home in Planner settings, or save one here",
                "size": "text", "tone": "warn"}
    try:
        payload = forecast_payload(place[1], place[2], wx._zone_name(user))
        current = parse_current(payload)
    except Exception:
        current = None
    if current is None or current.temp is None:
        return {"value": "—", "note": place[0], "size": "text", "tone": "warn"}
    note = current.label or place[0]
    if current.label and place[0]:
        note = f"{current.label} · {place[0]}"
    return {"value": f"{current.temp}°", "note": note, "size": "text", "tone": ""}


def launcher_line(user):
    """A short line for the header launcher tile."""
    info = tile_line(user)
    value = info.get("value") or "—"
    note = info.get("note") or ""
    if value in ("Set a place", "—"):
        return value
    # "39° · Showers later"
    short = note.split(" · ", 1)[0] if note else ""
    return f"{value} · {short}" if short else value
