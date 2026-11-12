"""Eat out (idea #78): a short questionnaire and a ranked shortlist of nearby
places to eat, fetched from OpenStreetMap Overpass.

Location reuses the Planner weather helpers and PlannerSettings: current
location (weather_auto_*) when weather follows the device, then Home, then a
typed place through weather.geocode (a city name or a lat, lon pair). A
browser geolocation post lands on the same weather_auto_* fields the Planner
already uses, through this app's own endpoint so a Meals-only account can
reach it.

Places come from overpass-api.de (allowlisted, no key). The raw nearby set is
cached for a few hours per rounded lat/lon; filters and ranking run in Python
so changing a chip does not refetch. A down, slow, or empty API never fails
the page. Hours and prices are shown only when they are tagged; there are no
star ratings (OpenStreetMap has none).
"""
from __future__ import annotations

import math
import re
from collections import namedtuple
from datetime import datetime
from decimal import Decimal
from urllib.parse import urlencode

import requests
from django.core.cache import cache

from albright_planner import weather as wx

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
USER_AGENT = "AlbrightMeals/1.0 (https://natealbright36.pythonanywhere.com)"
TIMEOUT = 15
CACHE_SECONDS = 4 * 3600
EMPTY_CACHE_SECONDS = 10 * 60
# Always fetch the 30-minute radius and filter in Python, so tightening
# "how far" doesn't need a new Overpass call.
FETCH_MILES = 12
FETCH_METERS = 19312
COORD_PLACES = 2
SHORTLIST = 5
# Amenities that are actually places to eat.
AMENITIES = frozenset({"restaurant", "fast_food", "cafe", "food_court"})

CRAVINGS = (
    ("tacos", "Tacos"),
    ("mexican", "Mexican"),
    ("burgers", "Burgers"),
    ("pizza", "Pizza"),
    ("other", "Something else"),
)
BUDGETS = (("1", "$"), ("2", "$$"), ("3", "$$$"))
DISTANCES = (("walk", "Walking"), ("15", "15 minutes"), ("30", "30 minutes"))
MODES = (("dinein", "Dine-in"), ("takeout", "Takeout"), ("either", "Either"))
# Walking: 1 mile. 15 minutes: 5 miles (about 20 mph). 30 minutes: 12 miles.
RADIUS_MILES = {"walk": 1.0, "15": 5.0, "30": 12.0}
# Cuisine tags (and name words) that count as a match for each chip.
CRAVING_TAGS = {
    "tacos": frozenset({"taco", "tacos", "mexican", "tex-mex", "texmex"}),
    "mexican": frozenset({"mexican", "taco", "tacos", "tex-mex", "texmex", "latin", "new_mexican"}),
    "burgers": frozenset({"burger", "burgers", "american"}),
    "pizza": frozenset({"pizza", "italian"}),
}
CUISINE_LABELS = {
    "taco": "Tacos",
    "tacos": "Tacos",
    "mexican": "Mexican",
    "tex-mex": "Tex-Mex",
    "texmex": "Tex-Mex",
    "burger": "Burgers",
    "burgers": "Burgers",
    "pizza": "Pizza",
    "italian": "Italian",
    "american": "American",
    "latin": "Latin",
    "chinese": "Chinese",
    "japanese": "Japanese",
    "thai": "Thai",
    "indian": "Indian",
    "korean": "Korean",
    "vietnamese": "Vietnamese",
    "mediterranean": "Mediterranean",
    "greek": "Greek",
    "sushi": "Sushi",
    "bbq": "BBQ",
    "barbecue": "Barbecue",
    "seafood": "Seafood",
    "sandwich": "Sandwiches",
    "coffee": "Coffee",
    "breakfast": "Breakfast",
    "asian": "Asian",
}
STYLE = {
    "restaurant": "sit-down",
    "fast_food": "quick",
    "cafe": "cafe",
    "food_court": "quick",
}
DAYS = ("Mo", "Tu", "We", "Th", "Fr", "Sa", "Su")
DAY_INDEX = {name: i for i, name in enumerate(DAYS)}
MONTH_RE = re.compile(
    r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec|week\s*\d|easter|SH)\b",
    re.I,
)
COMMENT_RE = re.compile(r'"[^"]*"')

Filters = namedtuple("Filters", "cravings other budget far mode open_now")
Location = namedtuple("Location", "label lat lon source")
# source: "current", "home", "typed"
Place = namedtuple(
    "Place",
    "osm_id name cuisine_label style address why miles drive_min walk_min "
    "distance_label open_state hours_label map_url directions_url website "
    "lat lon score",
)
Search = namedtuple(
    "Search",
    "places offline offline_message empty_message attribution",
)


# ---------------------------------------------------------------------------
# Filters and query strings
# ---------------------------------------------------------------------------

def filters_from_get(get):
    """Read the GET form. Open now defaults on; distance defaults to 15 minutes;
    dine-in/takeout defaults to Either. Budget and craving start empty."""
    known = {key for key, _label in CRAVINGS}
    cravings = [value for value in get.getlist("craving") if value in known]
    other = (get.get("other") or "").strip()[:80]
    budget = get.get("budget") if get.get("budget") in {"1", "2", "3"} else ""
    far = get.get("far") if get.get("far") in RADIUS_MILES else "15"
    mode = get.get("mode") if get.get("mode") in {"dinein", "takeout", "either"} else "either"
    if "open" in get:
        # Hidden 0 + checkbox 1: QueryDict.get returns the last value.
        open_now = get.get("open") == "1"
    else:
        open_now = True
    return Filters(tuple(cravings), other, budget, far, mode, open_now)


def steps_done(filters):
    """Five flags for the progress bar: craving, budget, how far, dine-in or
    takeout, open now. How far, dine-in/takeout and open now have defaults, so
    those bars start filled; craving and budget fill when the person picks one.
    """
    return (
        bool(filters.cravings or filters.other),
        bool(filters.budget),
        True,
        True,
        True,
    )


def summary_text(filters):
    """The short line under 'N places, ranked'."""
    parts = []
    labels = {key: label for key, label in CRAVINGS}
    names = [labels[key] for key in filters.cravings if key != "other"]
    if filters.other:
        names.append(filters.other)
    if names:
        if len(names) == 1:
            parts.append(names[0])
        elif len(names) == 2:
            parts.append(f"{names[0]} or {names[1]}")
        else:
            parts.append(", ".join(names[:-1]) + f" or {names[-1]}")
    if filters.budget:
        parts.append({key: label for key, label in BUDGETS}[filters.budget])
    if filters.mode == "dinein":
        parts.append("dine-in")
    elif filters.mode == "takeout":
        parts.append("takeout")
    if filters.open_now:
        parts.append("open now")
    parts.append({"walk": "walking distance", "15": "within 15 minutes",
                  "30": "within 30 minutes"}[filters.far])
    return " · ".join(parts)


def filter_params(filters, loc=None, pick=None):
    """GET params that reproduce the current form (for hidden fields and links)."""
    params = []
    for craving in filters.cravings:
        params.append(("craving", craving))
    if filters.other:
        params.append(("other", filters.other))
    if filters.budget:
        params.append(("budget", filters.budget))
    params.append(("far", filters.far))
    params.append(("mode", filters.mode))
    params.append(("open", "1" if filters.open_now else "0"))
    if loc is not None:
        if loc.source == "typed":
            params.append(("lat", _fmt_coord(loc.lat)))
            params.append(("lon", _fmt_coord(loc.lon)))
            if loc.label:
                params.append(("label", loc.label))
        elif loc.source in {"current", "home"}:
            params.append(("src", loc.source))
    if pick:
        params.append(("pick", str(pick)))
    return params


def query_string(filters, loc=None, pick=None):
    return urlencode(filter_params(filters, loc, pick))


def _fmt_coord(value):
    return f"{float(value):.5f}"


# ---------------------------------------------------------------------------
# Location
# ---------------------------------------------------------------------------

def typed_home(prefs):
    """The typed Home location, even when weather_auto is on. None if unset."""
    if prefs is None:
        return None
    return wx._place(
        getattr(prefs, "weather_label", "") or "Home",
        getattr(prefs, "weather_lat", None),
        getattr(prefs, "weather_lon", None),
    )


def current_location(prefs):
    """The last GPS fix, if weather follows the device and a point is stored."""
    if prefs is None or not getattr(prefs, "weather_auto", False):
        return None
    return wx._place(
        getattr(prefs, "weather_auto_label", "") or "Current location",
        getattr(prefs, "weather_auto_lat", None),
        getattr(prefs, "weather_auto_lon", None),
    )


def _as_location(place, source):
    if not place:
        return None
    label, lat, lon = place
    return Location(label, lat, lon, source)


def resolve_location(user, get):
    """Pick a place to search around.

    Returns (location, error, home, current, redirect_to).
    `redirect_to` is a {label, lat, lon} from a successful typed-place
    geocode; the view redirects to lat/lon/label so the next load is stable.
    `error` is a short message, or ''.
    """
    from albright_planner.models import PlannerSettings

    prefs = PlannerSettings.for_user(user)
    home = _as_location(typed_home(prefs), "home")
    current = _as_location(current_location(prefs), "current")
    query = (get.get("q") or "").strip()[:200]
    src = (get.get("src") or "").strip()
    # An explicit "Use current/Home" click wins over leftover text in the box.
    if query and src not in {"home", "current"}:
        try:
            found = wx.geocode(query)
        except wx.LookupDown:
            fallback = current or home
            return fallback, "Couldn't look that up right now. Try again in a moment.", home, current, None
        if not found:
            fallback = current or home
            return fallback, f"No match for “{query}”.", home, current, None
        return (
            Location(found["label"], found["lat"], found["lon"], "typed"),
            "",
            home,
            current,
            found,
        )
    pair = wx.coords(get.get("lat"), get.get("lon"))
    if pair:
        label = (get.get("label") or "").strip()[:200] or wx._coord_label(*pair)
        return Location(label, pair[0], pair[1], "typed"), "", home, current, None
    if src == "home" and home:
        return home, "", home, current, None
    if src == "current" and current:
        return current, "", home, current, None
    if src == "current" and not current:
        if home:
            return home, "No current location yet — using Home. Allow location, or type a place.", home, current, None
        return None, "No current location yet. Allow location, or type a place.", home, current, None
    if current:
        return current, "", home, current, None
    if home:
        return home, "", home, current, None
    return None, "", home, current, None


def source_caption(source):
    return {
        "current": "Using current location",
        "home": "Using your Home",
        "typed": "Using this place",
    }.get(source, "")


def set_current_location(user, lat, lon):
    """Save a browser GPS fix onto PlannerSettings.weather_auto_* — the same
    fields the Planner uses — only while weather_auto is on. Returns a dict
    for the JSON response. Never raises."""
    from albright_planner.models import PlannerSettings

    pair = wx.rounded(lat, lon)
    if not pair:
        return {"ok": False, "error": "Invalid coordinates."}, 400
    prefs = PlannerSettings.for_user(user)
    if not prefs.weather_auto:
        return {"ok": True, "auto": False, "changed": False, "label": prefs.weather_label}, 200
    if not wx.moved(prefs.weather_auto_lat, prefs.weather_auto_lon, pair[0], pair[1]):
        return {
            "ok": True, "auto": True, "changed": False,
            "label": prefs.weather_auto_label,
            "lat": float(prefs.weather_auto_lat),
            "lon": float(prefs.weather_auto_lon),
        }, 200
    found = wx.reverse_geocode(pair[0], pair[1]) or {
        "label": f"{float(pair[0]):.2f}, {float(pair[1]):.2f}", "lat": pair[0], "lon": pair[1],
    }
    prefs.weather_auto_label = found["label"]
    prefs.weather_auto_lat, prefs.weather_auto_lon = found["lat"], found["lon"]
    prefs.save(update_fields=["weather_auto_label", "weather_auto_lat", "weather_auto_lon"])
    return {
        "ok": True, "auto": True, "changed": True,
        "label": prefs.weather_auto_label,
        "lat": float(prefs.weather_auto_lat),
        "lon": float(prefs.weather_auto_lon),
    }, 200


def weather_follows_device(user):
    from albright_planner.models import PlannerSettings
    return bool(PlannerSettings.for_user(user).weather_auto)


# ---------------------------------------------------------------------------
# Opening hours
# ---------------------------------------------------------------------------

def opening_status(spec, now):
    """(state, label) for an OSM opening_hours string at `now`.

    state is 'open', 'closed', or 'unknown'. The label is honest: 'Open until
    10 PM', 'Open 24 hours', 'Opens at 5 PM', 'Closed', or 'Hours unknown'.
    Unrecognized syntax (seasonal ranges, holidays with hours) is unknown,
    never a guess.
    """
    if now is None or not spec or not str(spec).strip():
        return "unknown", "Hours unknown"
    text = COMMENT_RE.sub("", str(spec)).strip()
    if not text:
        return "unknown", "Hours unknown"
    if text == "24/7":
        return "open", "Open 24 hours"
    if MONTH_RE.search(text):
        return "unknown", "Hours unknown"
    rules = [part.strip() for part in text.split(";") if part.strip()]
    if not rules:
        return "unknown", "Hours unknown"
    # day index 0-6 (Mo-Su) -> list of (start_min, end_min), end may be >= 1440
    schedule = {i: [] for i in range(7)}
    assigned = set()
    for rule in rules:
        parsed = _parse_rule(rule)
        if parsed is None:
            return "unknown", "Hours unknown"
        days, intervals = parsed
        for day in days:
            schedule[day] = list(intervals)
            assigned.add(day)
    weekday = now.weekday()  # Mon=0, matches DAYS
    minutes = now.hour * 60 + now.minute
    # Overnight from yesterday.
    yesterday = (weekday - 1) % 7
    for start, end in schedule[yesterday]:
        if end > 1440 and minutes < (end - 1440) and minutes + 1440 >= start:
            return "open", f"Open until {_clock(end)}"
    for start, end in schedule[weekday]:
        if start <= minutes < min(end, 1440) or (end > 1440 and minutes >= start):
            return "open", f"Open until {_clock(end)}"
    # Closed: is there a later opening today?
    later = [start for start, end in schedule[weekday] if start > minutes]
    if later:
        return "closed", f"Opens at {_clock(min(later))}"
    return "closed", "Closed"


def _parse_rule(rule):
    """(set of day indexes, list of (start, end) minutes) or None if unknown.

    'PH off' and similar holiday-only rules are skipped (empty days). A rule
    we cannot read makes the whole spec unknown (the caller handles None).
    """
    rule = rule.strip()
    if not rule:
        return None
    if re.match(r"^(PH|SH)\b", rule):
        return set(), []
    if rule == "off":
        return set(range(7)), []
    if rule == "open" or rule == "24/7":
        return set(range(7)), [(0, 1440)]
    body = rule
    days = set(range(7))
    # Times always contain a colon. Days are Mo, Tu, ... optionally ranged.
    time_match = re.search(r"\d{1,2}:\d{2}", rule)
    off = bool(re.search(r"\boff\b", rule, re.I))
    if time_match:
        day_part = rule[:time_match.start()].strip().rstrip(",")
        body = rule[time_match.start():].strip()
        if day_part:
            days = _parse_days(day_part)
            if days is None:
                return None
    elif off:
        day_part = re.sub(r"\boff\b", "", rule, flags=re.I).strip().rstrip(",")
        if day_part:
            days = _parse_days(day_part)
            if days is None:
                return None
        return days, []
    else:
        return None
    if off and not time_match:
        return days, []
    intervals = []
    for chunk in body.split(","):
        chunk = chunk.strip()
        if not chunk or chunk.lower() == "off":
            continue
        # "11:00+" (open end) is not a real close: unknown.
        if chunk.endswith("+") or "+" in chunk:
            return None
        interval = _parse_interval(chunk)
        if interval is None:
            return None
        intervals.append(interval)
    return days, intervals


def _parse_days(text):
    result = set()
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            left, right = part.split("-", 1)
            left, right = left.strip(), right.strip()
            if left not in DAY_INDEX or right not in DAY_INDEX:
                return None
            i, j = DAY_INDEX[left], DAY_INDEX[right]
            if i <= j:
                result.update(range(i, j + 1))
            else:
                result.update(list(range(i, 7)) + list(range(0, j + 1)))
        else:
            if part not in DAY_INDEX:
                return None
            result.add(DAY_INDEX[part])
    return result if result else None


def _parse_interval(text):
    if "-" not in text:
        return None
    left, right = text.split("-", 1)
    start, end = _parse_minutes(left), _parse_minutes(right)
    if start is None or end is None:
        return None
    if end == start:
        return None
    if end < start:
        end += 1440
    return start, end


def _parse_minutes(text):
    match = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", text or "")
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour == 24 and minute == 0:
        return 1440
    if hour > 23 or minute > 59:
        return None
    return hour * 60 + minute


def _clock(minutes):
    minutes = int(minutes) % 1440
    hour, minute = divmod(minutes, 60)
    suffix = "AM" if hour < 12 else "PM"
    hour12 = hour % 12 or 12
    if minute:
        return f"{hour12}:{minute:02d} {suffix}"
    return f"{hour12} {suffix}"


# ---------------------------------------------------------------------------
# Distance
# ---------------------------------------------------------------------------

def haversine_miles(lat1, lon1, lat2, lon2):
    radius = 3958.8
    phi1, phi2 = math.radians(float(lat1)), math.radians(float(lat2))
    dphi = math.radians(float(lat2) - float(lat1))
    dlmb = math.radians(float(lon2) - float(lon1))
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlmb / 2) ** 2
    return 2 * radius * math.asin(min(1.0, math.sqrt(a)))


def drive_minutes(miles):
    """Rough suburban drive time at 20 mph, at least 1 minute."""
    return max(1, int(round(float(miles) / 20.0 * 60.0)))


def walk_minutes(miles):
    """Walking time at 3 mph, at least 1 minute."""
    return max(1, int(round(float(miles) / 3.0 * 60.0)))


def distance_label(miles, far):
    miles = float(miles)
    if miles < 0.1:
        feet = max(50, int(round(miles * 5280 / 50.0) * 50))
        length = f"{feet} ft"
    else:
        length = f"{miles:.1f} mi"
    if far == "walk" or miles <= 0.6:
        return f"{length} · {walk_minutes(miles)} min walk"
    return f"{length} · {drive_minutes(miles)} min drive"


# ---------------------------------------------------------------------------
# Overpass
# ---------------------------------------------------------------------------

def _cache_key(lat, lon):
    return f"meals.eatout:{round(float(lat), COORD_PLACES):.2f}:{round(float(lon), COORD_PLACES):.2f}"


def _query(lat, lon):
    return (
        f"[out:json][timeout:{TIMEOUT}];\n"
        f"(\n"
        f'  node["amenity"~"^(restaurant|fast_food|cafe|food_court)$"]'
        f"(around:{FETCH_METERS},{float(lat):.5f},{float(lon):.5f});\n"
        f'  way["amenity"~"^(restaurant|fast_food|cafe|food_court)$"]'
        f"(around:{FETCH_METERS},{float(lat):.5f},{float(lon):.5f});\n"
        f'  relation["amenity"~"^(restaurant|fast_food|cafe|food_court)$"]'
        f"(around:{FETCH_METERS},{float(lat):.5f},{float(lon):.5f});\n"
        f");\n"
        f"out center tags;"
    )


def fetch_places(lat, lon):
    """Raw Overpass elements around (lat, lon), or None if the API is down.

    An empty list means a successful empty result (nothing tagged nearby).
    None means a network/HTTP/parse failure — the page should say so.
    Never raises.
    """
    key = _cache_key(lat, lon)
    cached = cache.get(key)
    if cached is not None:
        return cached
    try:
        response = requests.post(
            OVERPASS_URL,
            data={"data": _query(lat, lon)},
            headers={"Accept": "application/json", "User-Agent": USER_AGENT},
            timeout=TIMEOUT,
        )
    except requests.RequestException:
        cache.set(key, None, EMPTY_CACHE_SECONDS)
        return None
    if response.status_code >= 400:
        cache.set(key, None, EMPTY_CACHE_SECONDS)
        return None
    try:
        payload = response.json()
    except ValueError:
        cache.set(key, None, EMPTY_CACHE_SECONDS)
        return None
    if not isinstance(payload, dict):
        cache.set(key, None, EMPTY_CACHE_SECONDS)
        return None
    elements = payload.get("elements")
    if not isinstance(elements, list):
        cache.set(key, None, EMPTY_CACHE_SECONDS)
        return None
    cache.set(key, elements, CACHE_SECONDS)
    return elements


def _element_point(element):
    if not isinstance(element, dict):
        return None
    if element.get("lat") is not None and element.get("lon") is not None:
        return wx.coords(element.get("lat"), element.get("lon"))
    center = element.get("center") or {}
    return wx.coords(center.get("lat"), center.get("lon"))


def _tags(element):
    tags = element.get("tags") if isinstance(element, dict) else None
    return tags if isinstance(tags, dict) else {}


def _cuisine_tokens(tags, name=""):
    raw = (tags.get("cuisine") or "").lower().replace(";", ",").replace("/", ",")
    tokens = {part.strip().replace(" ", "_") for part in raw.split(",") if part.strip()}
    # Name words help "Fuzzy's Taco Shop" match tacos even if cuisine is unset.
    for word in re.findall(r"[a-z]+", (name or "").lower()):
        if word in {"taco", "tacos", "pizza", "burger", "burgers", "sushi",
                    "thai", "indian", "mexican", "chipotle"}:
            tokens.add(word if word != "chipotle" else "mexican")
    return tokens


def cuisine_label(tags, name=""):
    tokens = _cuisine_tokens(tags, name)
    # Prefer a chip-related label when present, else the first tagged cuisine.
    raw = (tags.get("cuisine") or "").replace(";", ",").split(",")
    ordered = [part.strip().lower().replace(" ", "_") for part in raw if part.strip()]
    for token in ordered:
        if token in CUISINE_LABELS:
            return CUISINE_LABELS[token]
        if token:
            return token.replace("_", " ").title()
    for token in tokens:
        if token in CUISINE_LABELS:
            return CUISINE_LABELS[token]
    return ""


def _address(tags):
    street = " ".join(
        part for part in (tags.get("addr:housenumber") or "", tags.get("addr:street") or "") if part
    ).strip()
    if street:
        return street
    for key in ("addr:place", "addr:full", "addr:suburb", "addr:city"):
        if tags.get(key):
            return tags[key]
    return ""


def _website(tags):
    url = (tags.get("website") or tags.get("contact:website") or "").strip()
    if url and not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    return url[:300]


def map_url(lat, lon):
    return f"https://www.openstreetmap.org/?mlat={float(lat):.5f}&mlon={float(lon):.5f}#map=18/{float(lat):.5f}/{float(lon):.5f}"


def directions_url(lat, lon, name=""):
    dest = f"{float(lat):.5f},{float(lon):.5f}"
    return "https://www.google.com/maps/dir/?api=1&" + urlencode({"destination": dest})


def _match_craving(tokens, name, filters):
    """0 (no match), 1 (related / name), 2 (direct cuisine tag)."""
    wanted = []
    for key in filters.cravings:
        if key == "other":
            continue
        wanted.append((key, CRAVING_TAGS.get(key, frozenset())))
    if filters.other:
        word = filters.other.lower().strip().replace(" ", "_")
        wanted.append(("other", frozenset({word, word.replace("_", "-"), filters.other.lower()})))
    if not wanted:
        return 0, False  # no craving asked; not a 'miss'
    best = 0
    for key, tags in wanted:
        if tokens & tags:
            # Direct if the chip's primary word is in the cuisine tags.
            primary = {
                "tacos": {"taco", "tacos"},
                "mexican": {"mexican"},
                "burgers": {"burger", "burgers"},
                "pizza": {"pizza"},
            }.get(key, tags)
            best = max(best, 2 if tokens & primary else 1)
        elif any(tag in (name or "").lower() for tag in tags):
            best = max(best, 1)
    return best, True  # asked=True


def _mode_fit(tags, amenity, filters):
    """1 good, 0 neutral, -1 mismatch. Exclude only a hard conflict."""
    takeaway = (tags.get("takeaway") or tags.get("takeaway:covid19") or "").lower()
    seating = (tags.get("indoor_seating") or tags.get("outdoor_seating") or "").lower()
    if filters.mode == "dinein":
        if takeaway == "only":
            return -1
        if amenity == "restaurant" or seating == "yes":
            return 1
        if amenity == "fast_food":
            return 0
        return 0
    if filters.mode == "takeout":
        if takeaway in {"no", "none"}:
            return -1
        if takeaway in {"yes", "only"} or amenity == "fast_food":
            return 1
        return 0
    return 0


def _why(place_bits):
    return ", ".join(bit for bit in place_bits if bit)


def _score(miles, radius, craving_level, open_state, mode_fit, filters):
    score = 0.0
    # Closer is better, scaled to the chosen radius.
    score += max(0.0, 1.0 - (miles / max(radius, 0.2))) * 25
    if craving_level == 2:
        score += 50
    elif craving_level == 1:
        score += 30
    if filters.open_now:
        if open_state == "open":
            score += 20
        elif open_state == "unknown":
            score += 4
        elif open_state == "closed":
            score -= 30
    else:
        if open_state == "open":
            score += 5
    if mode_fit == 1:
        score += 15
    elif mode_fit == -1:
        score -= 20
    return score


def _build_place(element, loc, filters, now):
    tags = _tags(element)
    name = (tags.get("name") or tags.get("name:en") or "").strip()
    if not name:
        return None
    amenity = (tags.get("amenity") or "").strip()
    if amenity not in AMENITIES:
        return None
    point = _element_point(element)
    if not point:
        return None
    lat, lon = point
    miles = haversine_miles(loc.lat, loc.lon, lat, lon)
    radius = RADIUS_MILES[filters.far]
    if miles > radius + 0.05:
        return None
    tokens = _cuisine_tokens(tags, name)
    craving_level, craving_asked = _match_craving(tokens, name, filters)
    mode_fit = _mode_fit(tags, amenity, filters)
    if mode_fit == -1:
        return None
    open_state, hours_label = opening_status(tags.get("opening_hours") or "", now)
    if filters.open_now and open_state == "closed":
        return None
    cuisine = cuisine_label(tags, name)
    style = STYLE.get(amenity, "")
    why_bits = []
    if style == "sit-down" and cuisine:
        why_bits.append(f"Sit-down {cuisine}")
    elif cuisine:
        why_bits.append(cuisine)
    elif style:
        why_bits.append(style.capitalize())
    if open_state == "open":
        why_bits.append("open now")
    elif open_state == "unknown" and filters.open_now:
        why_bits.append("hours unknown")
    if filters.far == "walk" or miles <= 0.6:
        why_bits.append(f"{walk_minutes(miles)} min walk")
    else:
        why_bits.append(f"{drive_minutes(miles)} min away")
    osm_id = f"{element.get('type', 'node')}/{element.get('id', '')}"
    score = _score(miles, radius, craving_level, open_state, mode_fit, filters)
    if craving_asked and craving_level == 0:
        # Keep as a filler only: lower score, honest why.
        score -= 25
        why_bits.insert(0, "Nearby, different cuisine")
    return {
        "place": Place(
            osm_id=osm_id,
            name=name[:120],
            cuisine_label=cuisine,
            style=style,
            address=_address(tags),
            why=_why(why_bits),
            miles=round(miles, 2),
            drive_min=drive_minutes(miles),
            walk_min=walk_minutes(miles),
            distance_label=distance_label(miles, filters.far),
            open_state=open_state,
            hours_label=hours_label,
            map_url=map_url(lat, lon),
            directions_url=directions_url(lat, lon, name),
            website=_website(tags),
            lat=lat,
            lon=lon,
            score=score,
        ),
        "craving_level": craving_level,
        "craving_asked": craving_asked,
    }


def rank_places(elements, loc, filters, now, limit=SHORTLIST):
    """Turn Overpass elements into a short ranked list of Place."""
    built = []
    seen = set()
    for element in elements or []:
        row = _build_place(element, loc, filters, now)
        if not row:
            continue
        place = row["place"]
        key = (place.name.lower(), round(float(place.lat), 4), round(float(place.lon), 4))
        if key in seen:
            continue
        seen.add(key)
        built.append(row)
    built.sort(key=lambda row: (-row["place"].score, row["place"].miles, row["place"].name.lower()))
    asked = any(row["craving_asked"] for row in built) or bool(
        (filters.cravings and any(c != "other" for c in filters.cravings)) or filters.other
    )
    if asked:
        matches = [row for row in built if row["craving_level"] > 0]
        if len(matches) >= 3:
            built = matches
        else:
            # Fill with nearby others so the list isn't empty, matches first.
            extras = [row for row in built if row["craving_level"] == 0]
            built = matches + extras
    return [row["place"] for row in built[:limit]]


def search(loc, filters, user=None):
    """Fetch and rank. Never raises. offline is True when Overpass failed."""
    if loc is None:
        return Search([], False, "", "", _attribution())
    now = wx.local_now(user) if user is not None else None
    if now is None:
        now = datetime.now()
    try:
        elements = fetch_places(loc.lat, loc.lon)
    except Exception:
        elements = None
    if elements is None:
        return Search(
            [],
            True,
            "We couldn't reach the places list just now. Try again in a moment.",
            "",
            _attribution(),
        )
    try:
        places = rank_places(elements, loc, filters, now)
    except Exception:
        return Search(
            [],
            True,
            "We couldn't rank places just now. Try again in a moment.",
            "",
            _attribution(),
        )
    empty = ""
    if not places:
        empty = "No places matched. Try a wider area or a different craving."
    return Search(places, False, "", empty, _attribution())


def _attribution():
    return "Places from OpenStreetMap. Hours only when tagged; prices and ratings aren't listed."


def pick_index(places, raw):
    """1-based pick from the GET param, wrapped into range, or 0 if none."""
    if not places:
        return 0
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 0
    if value <= 0:
        return 0
    return ((value - 1) % len(places)) + 1
