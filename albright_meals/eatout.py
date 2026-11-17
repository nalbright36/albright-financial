"""Eat out (idea #78): filters, strict matching and a ranked list of nearby places to eat, from OpenStreetMap.

Provider: the public Overpass servers (OpenStreetMap - no API key; every host in OVERPASS_URLS is on PythonAnywhere's
free-plan allowlist). OpenStreetMap has names, place kinds (restaurant, fast_food, cafe, bar, pub, ...), cuisine,
opening_hours and a long tail of optional tags (outdoor_seating, reservation, wheelchair, diet:vegetarian, brand, ...).
It has NO star ratings, review counts, photos (bar the odd image tag) or booking links, and prices are rarely tagged.
So:
  - Every filter is checked here, in Python, on the fetched set (Overpass can't filter on most of it): one query per
    location and radius asks for every named place to eat, cached CACHE_SECONDS per ~0.5 mi grid cell and radius - a
    wider cached set also serves a narrower search, so changing a filter never refetches.
  - Each check is yes / no / unknown. A filter the person chose never lets "unknown" through unless "Include places
    with unknown info" is on.
  - place_type() is the one mapping from tags to a Type of place; a hard-coded list of fast-food chains is always Fast
    food whatever the tags say, and of fast-casual chains always Fast casual.
  - Ranking: a Bayesian rating when the provider gives ratings (OpenStreetMap never does, so it's neutral), distance
    scaled to the radius, how many chosen filters it matches, the craving words it matches and the household's own
    ratings (RestaurantVisit - Favorite / Staple get a bonus; Won't eat again / Not for us are hidden).
  - Location reuses the Planner weather helpers and PlannerSettings: current location (weather_auto_*) when weather
    follows the device, then Home, then a typed place. Opening hours are read in the place's own zone: the person's
    zone when searching around them, else the zone Open-Meteo gives for the typed place.
A failure is logged and shown as "unavailable", never as "no results".
"""
from __future__ import annotations

import logging
import math
import random
import re
from collections import namedtuple
from concurrent.futures import ThreadPoolExecutor, as_completed
from concurrent.futures import TimeoutError as FuturesTimeout
from datetime import datetime
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import requests
from django.conf import settings
from django.core.cache import cache

from albright_planner import weather as wx

logger = logging.getLogger(__name__)

# Asked in parallel; settings.EAT_OUT_OVERPASS_URLS overrides. overpass.openstreetmap.fr
# answered in seconds when overpass-api.de was at capacity (2026-11-16).
OVERPASS_URLS = (
    "https://overpass.openstreetmap.fr/api/interpreter",
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
)
USER_AGENT = "AlbrightMeals/1.0 (https://natealbright36.pythonanywhere.com)"
TIMEOUT = 25            # seconds per host for up to 5 miles (also the server-side [timeout:]); hosts are asked in parallel
CACHE_SECONDS = 30 * 60
STALE_SECONDS = 7 * 86400   # last-good copy, used only when every host fails (OpenStreetMap's licence allows keeping it)
ZONE_SECONDS = 30 * 86400
METERS_PER_MILE = 1609.344
GRID_DEGREES = 0.007        # ~0.5 mi: searches this close share one cached fetch
SNAP_MILES = 0.5            # the fetch reaches this much past the radius, so a snapped centre still covers it
RESULTS = 20
PICK_FROM = 5
PRIOR_RATING, PRIOR_WEIGHT = 4.0, 50     # Bayesian rating: 50 imaginary 4.0-star reviews
UNAVAILABLE = "Restaurant search is unavailable right now. Try again in a few minutes."
NO_MATCHES = "No places matched these filters."

# Every named place to eat, drink coffee or get dessert that Overpass is asked for.
AMENITIES = frozenset({"restaurant", "fast_food", "cafe", "food_court", "bar", "pub", "biergarten", "ice_cream"})
SHOPS = frozenset({"bakery", "pastry", "confectionery", "chocolate"})

MILES = ("1", "3", "5", "10", "15", "25")
RADIUS_MILES = {key: float(key) for key in MILES}
DEFAULT_MILES = "5"

TYPES = (
    ("sitdown", "Sit-down", "A server comes to your table"),
    ("fastcasual", "Fast casual", "Order at the counter - Chipotle, Panera, poke bowls"),
    ("fastfood", "Fast food / drive-thru", ""),
    ("fine", "Fine dining", ""),
    ("bar", "Bar / pub / brewery", ""),
    ("cafe", "Café / bakery / dessert", ""),
)
TYPE_LABELS = {key: label for key, label, _help in TYPES}
TYPE_SHORT = {"sitdown": "Sit-down", "fastcasual": "Fast casual", "fastfood": "Fast food", "fine": "Fine dining",
              "bar": "Bar", "cafe": "Café"}
ALL_TYPES = tuple(key for key, _l, _h in TYPES)

CUISINES = (
    ("mexican", "Mexican", {"mexican", "tex-mex", "texmex", "taco", "tacos", "burrito", "new_mexican", "latin_american"}),
    ("italian", "Italian", {"italian", "pasta"}),
    ("pizza", "Pizza", {"pizza"}),
    ("burgers", "Burgers", {"burger", "burgers"}),
    ("american", "American", {"american", "diner", "southern", "soul_food"}),
    ("chinese", "Chinese", {"chinese", "dim_sum", "cantonese", "szechuan", "sichuan"}),
    ("japanese", "Japanese / sushi", {"japanese", "sushi", "ramen"}),
    ("thai", "Thai", {"thai"}),
    ("indian", "Indian", {"indian", "nepalese", "pakistani"}),
    ("korean", "Korean", {"korean"}),
    ("vietnamese", "Vietnamese", {"vietnamese", "pho"}),
    ("mediterranean", "Mediterranean / Greek", {"mediterranean", "greek", "lebanese", "middle_eastern", "falafel",
                                                "turkish", "kebab"}),
    ("bbq", "BBQ", {"bbq", "barbecue"}),
    ("seafood", "Seafood", {"seafood", "fish", "oyster", "fish_and_chips"}),
    ("steak", "Steakhouse", {"steak_house", "steakhouse", "steak"}),
    ("breakfast", "Breakfast / brunch", {"breakfast", "brunch", "pancake", "waffle"}),
    ("sandwich", "Sandwiches / deli", {"sandwich", "deli", "sub", "bagel"}),
    ("chicken", "Chicken / wings", {"chicken", "wings", "fried_chicken"}),
)
CUISINE_TOKENS = {key: tokens for key, _label, tokens in CUISINES}
CUISINE_LABELS = {key: label for key, label, _tokens in CUISINES}
TOKEN_LABELS = {"taco": "Tacos", "tacos": "Tacos", "tex-mex": "Tex-Mex", "texmex": "Tex-Mex", "burger": "Burgers",
                "sushi": "Sushi", "bbq": "BBQ", "barbecue": "Barbecue", "coffee_shop": "Coffee",
                "steak_house": "Steakhouse", "ice_cream": "Ice cream", "sandwich": "Sandwiches"}

PRICES = (("1", "$"), ("2", "$$"), ("3", "$$$"), ("4", "$$$$"))
WHENS = (("now", "Open now"), ("at", "Open at…"), ("any", "Any time"))
DAYS_FULL = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
MEALS = (("breakfast", "Breakfast"), ("brunch", "Brunch"), ("lunch", "Lunch"), ("dinner", "Dinner"),
         ("late", "Late night"))
# Open at this local time on the day = good for that meal. Late night: open after 10 PM.
MEAL_CHECK = {"breakfast": 8 * 60, "brunch": 11 * 60, "lunch": 12 * 60 + 30, "dinner": 19 * 60, "late": 22 * 60 + 30}
MEAL_LABELS = dict(MEALS)

# (key, label, chip shown on a card) - only what OpenStreetMap actually tags. Good for groups and parking aren't
# tagged on places to eat at all, so they aren't offered.
FEATURES = (
    ("reservations", "Takes reservations", "Reservations"),
    ("outdoor", "Outdoor seating", "Outdoor"),
    ("kids", "Good for kids", "Kids"),
    ("cocktails", "Serves cocktails", "Cocktails"),
    ("beer_wine", "Serves beer / wine", "Beer & wine"),
    ("vegetarian", "Vegetarian options", "Vegetarian"),
    ("dogs", "Dog friendly", "Dogs OK"),
    ("wheelchair", "Wheelchair accessible", "Accessible"),
    ("live_music", "Live music", "Live music"),
    ("happy_hour", "Happy hour", "Happy hour"),
)
FEATURE_LABELS = {key: label for key, label, _chip in FEATURES}
FEATURE_CHIPS = {key: chip for key, _label, chip in FEATURES}
SERVICES = (("dinein", "Dine-in"), ("takeout", "Takeout"), ("delivery", "Delivery"))
SERVICE_LABELS = dict(SERVICES)
SEENS = (("", "Anywhere"), ("new", "Somewhere new"), ("favorites", "Our favorites"))
SORTS = (("best", "Best match"), ("rating", "Rating"), ("distance", "Distance"), ("price", "Price"))
MIN_RATINGS = (("0", "Any"), ("4.0", "4.0+"), ("4.3", "4.3+"), ("4.5", "4.5+"))
DEFAULT_MIN_REVIEWS = 50


# ---------------------------------------------------------------------------
# Chains
# ---------------------------------------------------------------------------

def _norm(text):
    """'McDonald's #4412' -> 'mcdonalds 4412'; '&' is 'and'."""
    text = (text or "").lower().replace("&", " and ").replace("’", "'")
    text = re.sub(r"['.]", "", text)
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text).split())


FAST_FOOD_CHAINS = frozenset(_norm(n) for n in (
    "McDonald's", "Wendy's", "Burger King", "Taco Bell", "Chick-fil-A", "Popeyes", "Popeyes Louisiana Kitchen",
    "Subway", "KFC", "Kentucky Fried Chicken", "Arby's", "Sonic", "Sonic Drive-In", "Jack in the Box", "Carl's Jr",
    "Hardee's", "Dairy Queen", "Whataburger", "In-N-Out Burger", "In-N-Out", "Culver's", "Raising Cane's",
    "Del Taco", "White Castle", "Krystal", "Checkers", "Rally's", "Bojangles", "Church's Chicken", "Church's Texas Chicken",
    "El Pollo Loco", "Panda Express", "Little Caesars", "Domino's", "Domino's Pizza", "Pizza Hut", "Papa John's",
    "Papa Murphy's", "Jimmy John's", "Jersey Mike's", "Jersey Mike's Subs", "Firehouse Subs", "Wingstop", "Zaxby's",
    "Long John Silver's", "A&W", "Steak 'n Shake", "Freddy's", "Freddy's Frozen Custard & Steakburgers",
    "Taco John's", "Five Guys", "Wienerschnitzel", "Captain D's", "Jack's", "Krispy Krunchy Chicken",
))
FAST_CASUAL_CHAINS = frozenset(_norm(n) for n in (
    "Chipotle", "Chipotle Mexican Grill", "Panera", "Panera Bread", "Qdoba", "Qdoba Mexican Eats", "Moe's",
    "Moe's Southwest Grill", "Cava", "Sweetgreen", "Noodles & Company", "Shake Shack", "Smashburger", "Potbelly",
    "Jason's Deli", "McAlister's Deli", "Zoës Kitchen", "Zoes Kitchen", "Cafe Rio", "Costa Vida", "Chopt",
    "Honeygrow", "Pei Wei", "Blaze Pizza", "MOD Pizza", "Pieology", "Tropical Smoothie Cafe", "Freshii",
    "Garbanzo", "Garbanzo Mediterranean Fresh", "Roti", "Pokeworks", "Fuzzy's Taco Shop", "Torchy's Tacos",
    "Tender Greens", "Corner Bakery", "Corner Bakery Cafe", "Einstein Bros", "Einstein Bros Bagels", "Which Wich",
    "Firebirds Wood Fired Grill", "Teriyaki Madness", "Modern Market", "Snooze", "Illegal Pete's",
))
# Fast food that isn't a chain: these cuisines without a drive-through are order-at-the-counter fast casual.
FAST_CASUAL_CUISINES = frozenset({
    "poke", "salad", "bowl", "mediterranean", "falafel", "greek", "burrito", "mexican", "taco", "tacos", "noodle",
    "ramen", "asian", "thai", "indian", "korean", "vietnamese", "pho", "deli", "bagel", "healthy", "vegan",
    "vegetarian", "lebanese", "middle_eastern", "sushi", "japanese", "chinese",
})
CAFE_CUISINES = frozenset({"coffee_shop", "coffee", "dessert", "ice_cream", "frozen_yogurt", "donut", "doughnut",
                           "bakery", "cake", "cupcake", "crepe", "bubble_tea", "tea", "juice", "pastry", "gelato",
                           "chocolate", "cookie"})
FINE_CUISINES = frozenset({"fine_dining", "gourmet", "haute_cuisine"})


def _chain_name(name, chains):
    n = _norm(name)
    return bool(n) and any(n == chain or n.startswith(chain + " ") for chain in chains)


def is_chain(tags, name=""):
    """A chain: OpenStreetMap's brand tags, or a name on one of the chain lists."""
    if tags.get("brand") or tags.get("brand:wikidata"):
        return True
    return _chain_name(name, FAST_FOOD_CHAINS) or _chain_name(name, FAST_CASUAL_CHAINS) \
        or _chain_name(tags.get("brand", ""), FAST_FOOD_CHAINS)


# ---------------------------------------------------------------------------
# What a place is: type, cuisine, price, features, service
# ---------------------------------------------------------------------------

def _yes(value):
    return (value or "").strip().lower() in {"yes", "only", "true", "1"}


def _no(value):
    return (value or "").strip().lower() in {"no", "none", "false", "0"}


def _cuisine_tokens(tags, name=""):
    raw = (tags.get("cuisine") or "").lower().replace(";", ",").replace("/", ",")
    tokens = {part.strip().replace(" ", "_") for part in raw.split(",") if part.strip()}
    # Name words help "Fuzzy's Taco Shop" match tacos even if cuisine is unset.
    for word in re.findall(r"[a-z]+", (name or "").lower()):
        if word in {"taco", "tacos", "pizza", "pizzeria", "burger", "burgers", "sushi", "thai", "indian", "mexican",
                    "ramen", "pho", "bbq", "poke", "taqueria", "steakhouse", "deli", "bagel", "bagels", "wings"}:
            tokens.add({"pizzeria": "pizza", "taqueria": "mexican", "steakhouse": "steak_house", "bagels": "bagel"}
                       .get(word, word))
    return tokens


def price_level(tags):
    """1-4 ($-$$$$) from a price tag, or None - OpenStreetMap rarely has one."""
    for key in ("price_range", "price:range", "price_level", "price"):
        value = (tags.get(key) or "").strip()
        if not value:
            continue
        dollars = value.count("$")
        if 1 <= dollars <= 4:
            return dollars
        if value.isdigit() and 1 <= int(value) <= 4:
            return int(value)
    return None


def _fine_signals(tags, tokens):
    return bool(tokens & FINE_CUISINES or tags.get("michelin") or tags.get("stars")
                or (tags.get("reservation") or "").lower() == "required" or tags.get("dress_code")
                or price_level(tags) == 4)


def place_type(tags, name=""):
    """The one mapping from OpenStreetMap tags to a Type of place (a TYPES key).

      - A name on FAST_FOOD_CHAINS is always fastfood; on FAST_CASUAL_CHAINS always fastcasual - whatever the tags.
      - bar / pub / biergarten, or a restaurant brewing its own beer (microbrewery, craft=brewery): bar.
      - cafe / ice_cream, a bakery or pastry shop, or a dessert / coffee cuisine: cafe.
      - fast_food / food_court: fastfood - unless it has no drive-through and a fast-casual cuisine (poke, salad,
        burritos, noodles, ...): fastcasual.
      - restaurant: takeaway=only (takeout only - Google's meal_takeaway) is fastfood; fine-dining signals (cuisine
        fine_dining, Michelin / stars, reservation=required, a dress code, $$$$) fine; otherwise sitdown - a
        restaurant type that isn't fast food, takeout-only or a café. (OpenStreetMap has no dine-in / reservable
        flags to require on top; indoor_seating / reservation only count as the supporting "why" chips.)
    """
    amenity = (tags.get("amenity") or "").strip().lower()
    shop = (tags.get("shop") or "").strip().lower()
    tokens = _cuisine_tokens(tags, name)
    brand = tags.get("brand", "")
    if _chain_name(name, FAST_FOOD_CHAINS) or _chain_name(brand, FAST_FOOD_CHAINS):
        return "fastfood"
    if _chain_name(name, FAST_CASUAL_CHAINS) or _chain_name(brand, FAST_CASUAL_CHAINS):
        return "fastcasual"
    if amenity in {"bar", "pub", "biergarten"} or _yes(tags.get("microbrewery")) or tags.get("craft") == "brewery":
        return "bar"
    if amenity in {"cafe", "ice_cream"} or shop in SHOPS:
        return "cafe"
    if amenity in {"fast_food", "food_court"}:
        if tokens & CAFE_CUISINES and not tokens - CAFE_CUISINES:
            return "cafe"
        if not _yes(tags.get("drive_through")) and tokens & FAST_CASUAL_CUISINES:
            return "fastcasual"
        return "fastfood"
    if amenity == "restaurant":
        if (tags.get("takeaway") or "").strip().lower() == "only":
            return "fastfood"
        if _fine_signals(tags, tokens):
            return "fine"
        if tokens and tokens <= CAFE_CUISINES:
            return "cafe"
        return "sitdown"
    return "fastfood"


def features(tags):
    """{feature key: True / False / None (not tagged)} for FEATURES."""
    amenity = (tags.get("amenity") or "").lower()

    def flag(*keys, yes=None, no=None):
        yes = yes or {"yes", "only", "limited", "recommended", "required"}
        values = [(tags.get(k) or "").strip().lower() for k in keys]
        if any(v in yes for v in values):
            return True
        if any(v in (no or {"no", "none"}) for v in values):
            return False
        return None

    beer = flag("drink:beer", "drink:wine", "microbrewery", "drink:craft_beer")
    if beer is None and amenity in {"bar", "pub", "biergarten"}:
        beer = True
    if beer is None and _no(tags.get("alcohol")):
        beer = False
    cocktails = flag("cocktails", "drink:cocktail", "drink:cocktails")
    if cocktails is None and amenity == "bar":
        cocktails = True
    happy = tags.get("happy_hours") or tags.get("happy_hour")
    return {
        "reservations": flag("reservation"),
        "outdoor": flag("outdoor_seating", yes={"yes", "garden", "terrace", "patio", "roof", "veranda", "sidewalk"}),
        "kids": flag("kids_area", "highchair", "kids_menu", "changing_table"),
        "cocktails": cocktails,
        "beer_wine": beer,
        "vegetarian": flag("diet:vegetarian", "diet:vegan"),
        "dogs": flag("dog", yes={"yes", "leashed", "outside", "unleashed"}),
        "wheelchair": flag("wheelchair", yes={"yes", "designated"}, no={"no", "limited"}),
        "live_music": flag("live_music"),
        "happy_hour": None if not happy else (False if _no(happy) else True),
    }


def services(tags, kind):
    """{"dinein", "takeout", "delivery": True / False / None}."""
    amenity = (tags.get("amenity") or "").lower()
    takeaway = (tags.get("takeaway") or "").strip().lower()
    indoor, outdoor = (tags.get("indoor_seating") or "").lower(), (tags.get("outdoor_seating") or "").lower()
    if takeaway == "only" or (_no(indoor) and _no(outdoor)):
        dinein = False
    elif _yes(indoor) or (outdoor and not _no(outdoor)) or amenity in {"restaurant", "bar", "pub", "biergarten",
                                                                        "food_court"}:
        dinein = True
    else:
        dinein = None
    if takeaway in {"yes", "only"}:
        takeout = True
    elif _no(takeaway):
        takeout = False
    else:
        takeout = True if kind in {"fastfood", "fastcasual"} or amenity == "fast_food" else None
    delivery = (tags.get("delivery") or "").strip().lower()
    return {"dinein": dinein, "takeout": takeout,
            "delivery": True if delivery in {"yes", "only"} else False if _no(delivery) else None}


def cuisine_keys(tokens):
    return {key for key, wanted in CUISINE_TOKENS.items() if tokens & wanted}


def cuisine_label(tags, name=""):
    """"Mexican", "Tacos", "Coffee" - the first tagged cuisine, else one read from the name; "" if none."""
    raw = [p.strip().lower().replace(" ", "_") for p in (tags.get("cuisine") or "").replace(";", ",").split(",")]
    first = next((p for p in raw if p), "")
    if first:
        if first in TOKEN_LABELS:
            return TOKEN_LABELS[first]
        if first in CUISINE_LABELS:
            return CUISINE_LABELS[first].split(" / ")[0]
        return first.replace("_", " ").title()
    keys = sorted(cuisine_keys(_cuisine_tokens(tags, name)))
    return CUISINE_LABELS[keys[0]].split(" / ")[0] if keys else ""


# ---------------------------------------------------------------------------
# Filters and query strings
# ---------------------------------------------------------------------------

Filters = namedtuple("Filters", "types cuisines prices miles when at_day at_time meal min_rating min_reviews "
                                "features services no_chains seen craving include_unknown sort")
DEFAULTS = Filters(types=ALL_TYPES, cuisines=(), prices=(), miles=DEFAULT_MILES, when="now", at_day=None,
                   at_time="", meal="", min_rating=0.0, min_reviews=DEFAULT_MIN_REVIEWS, features=(), services=(),
                   no_chains=False, seen="", craving="", include_unknown=False, sort="best")
# GET names: type cuisine price (repeated) mi when day time meal min_rating min_reviews feat svc (repeated) nochain
# seen crave unknown sort; f=1 marks a submitted form (the last-used filters are saved then).
FILTER_KEYS = frozenset({"type", "cuisine", "price", "mi", "when", "day", "time", "meal", "min_rating",
                         "min_reviews", "feat", "svc", "nochain", "seen", "crave", "unknown", "sort", "f"})
TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


def _pick(get, key, choices):
    return tuple(v for v in dict.fromkeys(get.getlist(key)) if v in choices)


def _flag(get, key):
    """Hidden 0 + checkbox 1: the last value wins."""
    return get.get(key) == "1"


def has_filters(get):
    return any(key in get for key in FILTER_KEYS)


def filters_from_get(get):
    """Read the GET form; anything missing or not a choice takes its default (filter_errors says so)."""
    types = _pick(get, "type", ALL_TYPES) or ALL_TYPES
    try:
        day = int(get.get("day", ""))
        day = day if 0 <= day <= 6 else None
    except ValueError:
        day = None
    at_time = (get.get("time") or "").strip()
    at_time = at_time if TIME_RE.match(at_time) else ""
    when = get.get("when") if get.get("when") in dict(WHENS) else "now"
    if when == "at" and not at_time:
        when = "now"
    try:
        min_rating = float(get.get("min_rating") or 0)
        min_rating = min_rating if str(get.get("min_rating") or "0") in dict(MIN_RATINGS) else 0.0
    except ValueError:
        min_rating = 0.0
    try:
        min_reviews = max(0, min(10000, int(get.get("min_reviews") or DEFAULT_MIN_REVIEWS)))
    except ValueError:
        min_reviews = DEFAULT_MIN_REVIEWS
    return Filters(
        types=types,
        cuisines=_pick(get, "cuisine", CUISINE_TOKENS),
        prices=_pick(get, "price", dict(PRICES)),
        miles=get.get("mi") if get.get("mi") in RADIUS_MILES else DEFAULT_MILES,
        when=when, at_day=day, at_time=at_time,
        meal=get.get("meal") if get.get("meal") in MEAL_CHECK else "",
        min_rating=min_rating, min_reviews=min_reviews,
        features=_pick(get, "feat", FEATURE_LABELS),
        services=_pick(get, "svc", SERVICE_LABELS),
        no_chains=_flag(get, "nochain"),
        seen=get.get("seen") if get.get("seen") in {"new", "favorites"} else "",
        craving=" ".join((get.get("crave") or "").split())[:80],
        include_unknown=_flag(get, "unknown"),
        sort=get.get("sort") if get.get("sort") in dict(SORTS) else "best",
    )


def filter_errors(get):
    """{field: message} for values that were sent but aren't a choice. The search still runs on the defaults."""
    errors = {}
    if get.get("f") == "1" and not _pick(get, "type", ALL_TYPES):
        errors["type"] = "Pick at least one type of place - showing every type."
    elif any(v not in ALL_TYPES for v in get.getlist("type")):
        errors["type"] = "Pick a type of place from the list."
    if any(v not in CUISINE_TOKENS for v in get.getlist("cuisine")):
        errors["cuisine"] = "Pick a cuisine from the list."
    if any(v not in dict(PRICES) for v in get.getlist("price")):
        errors["price"] = "Pick $, $$, $$$ or $$$$."
    if get.get("mi") not in (None, "") and get.get("mi") not in RADIUS_MILES:
        errors["mi"] = f"Pick a distance from the list - showing {DEFAULT_MILES} mi."
    if get.get("when") == "at" and not TIME_RE.match((get.get("time") or "").strip()):
        errors["when"] = "Pick a time - showing places open now."
    if any(v not in FEATURE_LABELS for v in get.getlist("feat")):
        errors["feat"] = "Pick features from the list."
    if len((get.get("crave") or "").strip()) > 80:
        errors["crave"] = "Keep it under 80 characters."
    return errors


def filter_params(filters, loc=None, pick=None):
    """GET params that reproduce the filters (and the place) - for links, relax buttons and presets."""
    p = [("f", "1")]
    p += [("type", t) for t in filters.types]
    p += [("cuisine", c) for c in filters.cuisines]
    p += [("price", v) for v in filters.prices]
    p.append(("mi", filters.miles))
    p.append(("when", filters.when))
    if filters.when == "at":
        if filters.at_day is not None:
            p.append(("day", str(filters.at_day)))
        p.append(("time", filters.at_time))
    if filters.meal:
        p.append(("meal", filters.meal))
    if filters.min_rating:
        p.append(("min_rating", f"{filters.min_rating:.1f}"))
    if filters.min_reviews != DEFAULT_MIN_REVIEWS:
        p.append(("min_reviews", str(filters.min_reviews)))
    p += [("feat", v) for v in filters.features]
    p += [("svc", v) for v in filters.services]
    if filters.no_chains:
        p.append(("nochain", "1"))
    if filters.seen:
        p.append(("seen", filters.seen))
    if filters.craving:
        p.append(("crave", filters.craving))
    if filters.include_unknown:
        p.append(("unknown", "1"))
    if filters.sort != "best":
        p.append(("sort", filters.sort))
    p += location_params(loc)
    if pick:
        p.append(("pick", str(pick)))
    return p


def location_params(loc):
    """The GET params that keep searching around this place."""
    if loc is None:
        return []
    if loc.source == "typed":
        return [("lat", _fmt_coord(loc.lat)), ("lon", _fmt_coord(loc.lon))] + ([("label", loc.label)] if loc.label else [])
    return [("src", loc.source)] if loc.source in {"current", "home"} else []


def query_string(filters, loc=None, pick=None):
    return urlencode(filter_params(filters, loc, pick))


def _fmt_coord(value):
    return f"{float(value):.5f}"


def more_active(filters):
    """Any of the "More filters" set - the section starts open then."""
    return bool(filters.cuisines or filters.prices or filters.meal or filters.features or filters.services
                or filters.no_chains or filters.seen or filters.include_unknown or filters.min_rating
                or filters.miles != DEFAULT_MILES or filters.when != "now" or filters.sort != "best")


def summary_text(filters):
    """The short line under the results heading."""
    parts = []
    if set(filters.types) != set(ALL_TYPES):
        parts.append(", ".join(TYPE_SHORT[t] for t in filters.types))
    if filters.cuisines:
        parts.append(" or ".join(CUISINE_LABELS[c] for c in filters.cuisines))
    if filters.craving:
        parts.append(f"“{filters.craving}”")
    if filters.prices:
        parts.append("/".join(dict(PRICES)[p] for p in filters.prices))
    if filters.when == "now":
        parts.append("open now")
    elif filters.when == "at":
        day = DAYS_FULL[filters.at_day] if filters.at_day is not None else "today"
        parts.append(f"open {day} at {_clock(_minutes(filters.at_time))}")
    if filters.meal:
        parts.append(MEAL_LABELS[filters.meal].lower())
    parts += [FEATURE_LABELS[f].lower() for f in filters.features]
    parts += [SERVICE_LABELS[s].lower() for s in filters.services]
    if filters.no_chains:
        parts.append("local spots")
    if filters.seen:
        parts.append(dict(SEENS)[filters.seen].lower())
    parts.append(f"within {filters.miles} mi")
    return " · ".join(parts)


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


Location = namedtuple("Location", "label lat lon source")   # source: "current", "home", "typed"


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
        return Location(found["label"], found["lat"], found["lon"], "typed"), "", home, current, found
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
    return {"current": "Using current location", "home": "Using your Home", "typed": "Using this place"}.get(source, "")


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


def lookup_zone(lat, lon):
    """The IANA zone at a point (Open-Meteo's timezone=auto), cached ZONE_SECONDS; None on any failure."""
    key = f"meals.eatout.zone:{round(float(lat), 1):.1f}:{round(float(lon), 1):.1f}"
    found = cache.get(key)
    if found is not None:
        return found or None
    name = ""
    try:
        response = requests.get(wx.FORECAST_URL, params={"latitude": float(lat), "longitude": float(lon),
                                                         "timezone": "auto", "current": "weather_code"},
                                headers={"Accept": "application/json", "User-Agent": USER_AGENT}, timeout=5)
        if response.status_code < 400:
            name = str((response.json() or {}).get("timezone") or "")
            ZoneInfo(name)
    except Exception:
        name = ""
    cache.set(key, name, ZONE_SECONDS if name else 3600)
    return name or None


def place_zone(loc, user):
    """The zone the places' hours are in: the person's own around them (current location, Home); a typed place's
    own zone (lookup_zone), falling back to the person's."""
    own = wx._zone_name(user) if user is not None else "UTC"
    if loc is not None and loc.source == "typed":
        own = lookup_zone(loc.lat, loc.lon) or own
    try:
        return ZoneInfo("UTC" if own == "auto" else own)
    except Exception:
        return ZoneInfo("UTC")


# ---------------------------------------------------------------------------
# Opening hours
# ---------------------------------------------------------------------------

DAYS = ("Mo", "Tu", "We", "Th", "Fr", "Sa", "Su")
DAY_INDEX = {name: i for i, name in enumerate(DAYS)}
MONTH_RE = re.compile(r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec|week\s*\d|easter|SH)\b", re.I)
COMMENT_RE = re.compile(r'"[^"]*"')


def parse_hours(spec):
    """{day 0-6: [(start, end) minutes - end may pass 1440]} for an OSM opening_hours string, or None when it's
    missing or uses syntax we don't read (seasonal ranges, holidays with hours) - never a guess."""
    if not spec or not str(spec).strip():
        return None
    text = COMMENT_RE.sub("", str(spec)).strip()
    if not text:
        return None
    if text == "24/7":
        return {i: [(0, 1440)] for i in range(7)}
    if MONTH_RE.search(text):
        return None
    rules = [part.strip() for part in text.split(";") if part.strip()]
    if not rules:
        return None
    schedule = {i: [] for i in range(7)}
    for rule in rules:
        parsed = _parse_rule(rule)
        if parsed is None:
            return None
        days, intervals = parsed
        for day in days:
            schedule[day] = list(intervals)
    return schedule


def open_at(schedule, weekday, minutes):
    """(open?, closing minute or None) at a weekday and minute of the day; schedule from parse_hours()."""
    yesterday = (weekday - 1) % 7
    for start, end in schedule[yesterday]:
        if end > 1440 and minutes < (end - 1440) and minutes + 1440 >= start:
            return True, end
    for start, end in schedule[weekday]:
        if start <= minutes < min(end, 1440) or (end > 1440 and minutes >= start):
            return True, end
    return False, None


def opening_status(spec, now):
    """(state, label) for an OSM opening_hours string at `now`: 'open' / 'closed' / 'unknown' and 'Open until
    10 PM', 'Open 24 hours', 'Opens at 5 PM', 'Closed' or 'Hours unknown'."""
    schedule = parse_hours(spec) if now is not None else None
    if schedule is None:
        return "unknown", "Hours unknown"
    return opening_status_from(schedule, now)


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
    intervals = []
    for chunk in body.split(","):
        chunk = chunk.strip()
        if not chunk or chunk.lower() == "off":
            continue
        if "+" in chunk:                                 # "11:00+" (open end) is not a real close: unknown
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
            left, right = (s.strip() for s in part.split("-", 1))
            if left not in DAY_INDEX or right not in DAY_INDEX:
                return None
            i, j = DAY_INDEX[left], DAY_INDEX[right]
            result.update(range(i, j + 1) if i <= j else list(range(i, 7)) + list(range(0, j + 1)))
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
    if start is None or end is None or end == start:
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


def _minutes(hhmm):
    hour, minute = hhmm.split(":")
    return int(hour) * 60 + int(minute)


def _clock(minutes):
    minutes = int(minutes) % 1440
    hour, minute = divmod(minutes, 60)
    suffix = "AM" if hour < 12 else "PM"
    hour12 = hour % 12 or 12
    return f"{hour12}:{minute:02d} {suffix}" if minute else f"{hour12} {suffix}"


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


def distance_label(miles):
    miles = float(miles)
    length = f"{max(50, int(round(miles * 5280 / 50.0) * 50))} ft" if miles < 0.1 else f"{miles:.1f} mi"
    if miles <= 0.6:
        return f"{length} · {walk_minutes(miles)} min walk"
    return f"{length} · {drive_minutes(miles)} min drive"


# ---------------------------------------------------------------------------
# Overpass
# ---------------------------------------------------------------------------

def snap(lat, lon):
    """The ~0.5 mi grid point a search is fetched (and cached) around."""
    return (round(round(float(lat) / GRID_DEGREES) * GRID_DEGREES, 5),
            round(round(float(lon) / GRID_DEGREES) * GRID_DEGREES, 5))


def fetch_meters(far):
    """The Overpass search radius, in meters: the chosen miles plus SNAP_MILES for the snapped centre."""
    return int(round((RADIUS_MILES.get(far, RADIUS_MILES[DEFAULT_MILES]) + SNAP_MILES) * METERS_PER_MILE))


def timeout_for(far):
    miles = RADIUS_MILES.get(far, 5.0)
    return TIMEOUT if miles <= 5 else 40 if miles <= 15 else 60


def _cache_key(lat, lon, far):
    s_lat, s_lon = snap(lat, lon)
    return f"meals.eatout2:{s_lat:.3f}:{s_lon:.3f}:{far}"


def _query(lat, lon, far=DEFAULT_MILES):
    s_lat, s_lon = snap(lat, lon)
    around = f"(around:{fetch_meters(far)},{s_lat:.5f},{s_lon:.5f})"
    return (
        f"[out:json][timeout:{timeout_for(far)}];\n"
        f'(nwr["amenity"~"^({"|".join(sorted(AMENITIES))})$"]["name"]{around};\n'
        f' nwr["shop"~"^({"|".join(sorted(SHOPS))})$"]["name"]{around};);\n'
        f"out center tags;"
    )


def overpass_urls():
    return tuple(getattr(settings, "EAT_OUT_OVERPASS_URLS", None) or OVERPASS_URLS)


def _host(url):
    return url.split("//", 1)[-1].split("/", 1)[0]


def _provider_message(response):
    """The first readable line of an Overpass error page (markdown or HTML)."""
    text = re.sub(r"<[^>]+>", " ", getattr(response, "text", "") or "")
    for line in text.splitlines():
        line = line.strip(" #\t")
        if line:
            return line[:200]
    return ""


Fetch = namedtuple("Fetch", "elements errors status host")
# elements: a list (possibly empty) on success, None when every host failed.
# errors: ["host: what went wrong", ...]; status: the last HTTP status seen, or None.
# host: the Overpass host that answered, "cache", or "stale" (a last-good copy).


def _ask(url, query, timeout=TIMEOUT):
    """One Overpass host: (elements or None, error text, HTTP status or None). Never raises."""
    host = _host(url)
    try:
        response = requests.post(url, data={"data": query},
                                 headers={"Accept": "application/json", "User-Agent": USER_AGENT}, timeout=timeout)
    except requests.RequestException as exc:
        return None, f"{host}: {type(exc).__name__}", None
    status = response.status_code
    if status >= 400:
        message = _provider_message(response)
        return None, f"{host}: HTTP {status}" + (f" ({message})" if message else ""), status
    try:
        payload = response.json()
    except ValueError:
        payload = None
    elements = payload.get("elements") if isinstance(payload, dict) else None
    if not isinstance(elements, list):
        return None, f"{host}: HTTP {status}, unreadable response", status
    remark = str(payload.get("remark") or "")
    if not elements and "error" in remark.lower():
        # Overpass reports a server-side timeout as 200 + an empty list + a remark.
        return None, f"{host}: {remark[:160]}", status
    return elements, "", status


def fetch(lat, lon, far=DEFAULT_MILES, use_cache=True):
    """Overpass elements around (lat, lon) within `far` miles. Never raises.

    A cached set (CACHE_SECONDS, per ~0.5 mi grid cell) for the same or a wider radius is reused. Otherwise every
    host is asked at once and the first good answer wins. Each failure is logged with its HTTP status and the
    provider's message - there is no key to leak. When all fail, a last-good copy (STALE_SECONDS) is used if any.
    """
    wanted = MILES.index(far) if far in MILES else MILES.index(DEFAULT_MILES)
    radius = MILES[wanted]
    if use_cache:
        for wider in MILES[wanted:]:
            cached = cache.get(_cache_key(lat, lon, wider))
            if cached is not None:
                return Fetch(cached, [], 200, "cache")
    urls = overpass_urls()
    query, timeout = _query(lat, lon, radius), timeout_for(radius)
    errors, status = {}, None
    if urls:
        pool = ThreadPoolExecutor(max_workers=len(urls))
        futures = {pool.submit(_ask, url, query, timeout): url for url in urls}
        try:
            for future in as_completed(futures, timeout=timeout + 3):
                elements, error, code = future.result()
                status = code if code is not None else status
                if elements is not None:
                    cache.set(_cache_key(lat, lon, radius), elements, CACHE_SECONDS)
                    cache.set(_cache_key(lat, lon, radius) + ":stale", elements, STALE_SECONDS)
                    return Fetch(elements, [errors[u] for u in urls if u in errors], code, _host(futures[future]))
                errors[futures[future]] = error
                logger.warning("Eat out: Overpass %s", error)
        except FuturesTimeout:
            for future, url in futures.items():
                if url not in errors:
                    errors[url] = f"{_host(url)}: no answer in {timeout + 3}s"
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
    ordered = [errors[u] for u in urls if u in errors]
    logger.error("Eat out: every Overpass host failed near %.2f, %.2f (%s mi): %s",
                 float(lat), float(lon), radius, "; ".join(ordered) or "no hosts configured")
    if use_cache:
        for wider in MILES[wanted:]:
            stale = cache.get(_cache_key(lat, lon, wider) + ":stale")
            if stale is not None:
                return Fetch(stale, ordered, status, "stale")
    return Fetch(None, ordered, status, "")


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


def _address(tags):
    street = " ".join(p for p in (tags.get("addr:housenumber") or "", tags.get("addr:street") or "") if p).strip()
    if street:
        return street
    for key in ("addr:place", "addr:full", "addr:suburb", "addr:city"):
        if tags.get(key):
            return tags[key]
    return ""


def _url(tags, *keys):
    for key in keys:
        url = (tags.get(key) or "").strip()
        if url:
            if not re.match(r"^https?://", url, re.I):
                url = "https://" + url
            return url[:300]
    return ""


def _photo(tags):
    url = (tags.get("image") or "").strip()
    return url[:400] if re.match(r"^https://\S+\.(jpe?g|png|webp)$", url, re.I) else ""


def _phone(tags):
    raw = (tags.get("phone") or tags.get("contact:phone") or "").split(";")[0].strip()
    digits = re.sub(r"[^\d+]", "", raw)
    return (raw[:40], digits[:20]) if len(digits) >= 7 else ("", "")


def map_url(lat, lon):
    return (f"https://www.openstreetmap.org/?mlat={float(lat):.5f}&mlon={float(lon):.5f}"
            f"#map=18/{float(lat):.5f}/{float(lon):.5f}")


def directions_url(lat, lon):
    return "https://www.google.com/maps/dir/?api=1&" + urlencode({"destination": f"{float(lat):.5f},{float(lon):.5f}"})


# ---------------------------------------------------------------------------
# Matching and ranking
# ---------------------------------------------------------------------------

Place = namedtuple(
    "Place",
    "place_id name kind kind_label cuisine_label price address miles distance_label open_state hours_label why "
    "photo phone phone_digits website menu reserve map_url directions_url rating review_count visit score lat lon",
)
Search = namedtuple("Search", "places offline offline_message empty_message attribution blockers hidden total")
# blockers: [{"key", "label", "count", "relax_label", "relax_qs"}] - which filters removed the most (empty state).

UNKNOWN, YES, NO = None, True, False


def _num(value, kind):
    try:
        return kind(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def bayesian(rating, count, prior=PRIOR_RATING, weight=PRIOR_WEIGHT):
    """A rating pulled toward `prior` until it has enough reviews: 4.6 from 800 beats 5.0 from 3."""
    if rating is None or not count:
        return None
    return (prior * weight + float(rating) * int(count)) / (weight + int(count))


def craving_words(text):
    stop = {"and", "the", "with", "for", "food", "place", "some", "good", "best", "near", "spot"}
    return [w for w in re.findall(r"[a-z0-9]+", (text or "").lower()) if len(w) >= 3 and w not in stop]


def _craving_hits(words, tags, name, tokens):
    if not words:
        return 0
    text = " ".join([name or "", tags.get("cuisine", ""), tags.get("description", ""), tags.get("brand", ""),
                     " ".join(tokens), " ".join(k for k, v in tags.items() if _yes(v))]).lower().replace("_", " ")
    return sum(1 for w in words if w in text or (w.endswith("s") and w[:-1] in text))


def _hours_check(schedule, filters, now):
    """(when result, meal result, label) - True / False / None (hours unknown)."""
    if schedule is None:
        when = None if filters.when != "any" else True
        meal = None if filters.meal else True
        return when, meal, "Hours unknown"
    day = now.weekday()
    if filters.when == "at":
        day = filters.at_day if filters.at_day is not None else now.weekday()
        is_open, end = open_at(schedule, day, _minutes(filters.at_time))
        label = (f"Open {DAYS_FULL[day][:3]} {_clock(_minutes(filters.at_time))}" if is_open
                 else f"Closed {DAYS_FULL[day][:3]} {_clock(_minutes(filters.at_time))}")
        when = is_open
    else:
        state, label = opening_status_from(schedule, now)
        when = True if filters.when == "any" else state == "open"
    meal = True
    if filters.meal:
        meal = open_at(schedule, day, MEAL_CHECK[filters.meal])[0]
    return when, meal, label


def opening_status_from(schedule, now):
    if all(s == [(0, 1440)] for s in schedule.values()):
        return "open", "Open 24 hours"
    minutes = now.hour * 60 + now.minute
    is_open, end = open_at(schedule, now.weekday(), minutes)
    if is_open:
        return "open", f"Open until {_clock(end)}"
    later = [start for start, _end in schedule[now.weekday()] if start > minutes]
    return "closed", f"Opens at {_clock(min(later))}" if later else "Closed"


def evaluate(element, loc, filters, now, visits=None, words=None):
    """One place against every chosen filter: {"place": Place, "checks": {group: True/False/None}, "matched": n,
    "hidden": bool (rated Won't eat again / Not for us)} - or None when it isn't a named place to eat within the
    radius. Groups: type cuisine price when meal rating feat:<key> svc:<key> chains seen craving."""
    from .models import RestaurantVisit
    tags = _tags(element)
    name = (tags.get("name") or tags.get("name:en") or "").strip()
    amenity = (tags.get("amenity") or "").strip().lower()
    if not name or (amenity not in AMENITIES and (tags.get("shop") or "") not in SHOPS):
        return None
    point = _element_point(element)
    if not point:
        return None
    lat, lon = point
    miles = haversine_miles(loc.lat, loc.lon, lat, lon)
    radius = RADIUS_MILES[filters.miles]
    if miles > radius + 0.05:
        return None
    place_id = f"osm:{element.get('type', 'node')}/{element.get('id', '')}"
    visit = (visits or {}).get(place_id)
    kind = place_type(tags, name)
    tokens = _cuisine_tokens(tags, name)
    price = price_level(tags)
    feats, svcs = features(tags), services(tags, kind)
    schedule = parse_hours(tags.get("opening_hours"))
    when_ok, meal_ok, hours_label = _hours_check(schedule, filters, now)
    open_state = "unknown" if schedule is None else ("open" if opening_status_from(schedule, now)[0] == "open"
                                                     else "closed")
    # Ratings: OpenStreetMap has none. A provider that has them puts "rating" / "review_count" on the element.
    rating, review_count = _num(element.get("rating"), float), _num(element.get("review_count"), int)
    checks = {"type": kind in filters.types}
    if filters.cuisines:
        keys = cuisine_keys(tokens)
        checks["cuisine"] = bool(keys & set(filters.cuisines)) if tokens else UNKNOWN
    if filters.prices:
        checks["price"] = UNKNOWN if price is None else str(price) in filters.prices
    if filters.when != "any":
        checks["when"] = when_ok
    if filters.meal:
        checks["meal"] = meal_ok
    if review_count is not None:
        checks["rating"] = review_count >= filters.min_reviews and (rating or 0) >= filters.min_rating
    elif filters.min_rating:
        checks["rating"] = UNKNOWN
    for key in filters.features:
        checks[f"feat:{key}"] = feats[key]
    for key in filters.services:
        checks[f"svc:{key}"] = svcs[key]
    if filters.no_chains:
        checks["chains"] = not is_chain(tags, name)
    if filters.seen == "new":
        checks["seen"] = visit is None
    elif filters.seen == "favorites":
        checks["seen"] = visit is not None and visit.level >= RestaurantVisit.WILL_EAT
    hits = _craving_hits(words or [], tags, name, tokens)
    if words:
        checks["craving"] = hits > 0
    why = []
    if kind in filters.types and set(filters.types) != set(ALL_TYPES):
        why.append(TYPE_SHORT[kind])
    elif kind == "sitdown" and (_yes(tags.get("indoor_seating")) or feats["reservations"]):
        why.append("Sit-down")
    if checks.get("cuisine"):
        why.append(cuisine_label(tags, name) or "Cuisine")
    if hits:
        why.append(f"“{filters.craving}”" if hits == len(words) else "Craving match")
    why += [FEATURE_CHIPS[k] for k in filters.features if feats[k]]
    why += [SERVICE_LABELS[k] for k in filters.services if svcs[k]]
    if filters.no_chains and checks.get("chains"):
        why.append("Local")
    if checks.get("when") and filters.when == "now":
        why.append("Open now")
    if filters.meal and checks.get("meal"):
        why.append(MEAL_LABELS[filters.meal])
    matched = sum(1 for k, v in checks.items() if v is True and k != "type") + (hits if words else 0)
    place = Place(
        place_id=place_id, name=name[:120], kind=kind, kind_label=TYPE_LABELS[kind], cuisine_label=cuisine_label(tags, name),
        price=price, address=_address(tags), miles=round(miles, 2), distance_label=distance_label(miles),
        open_state=open_state, hours_label=hours_label, why=why, photo=_photo(tags),
        phone=_phone(tags)[0], phone_digits=_phone(tags)[1],
        website=_url(tags, "website", "contact:website", "url"), menu=_url(tags, "website:menu", "menu:url", "menu"),
        reserve=_url(tags, "reservation:url", "reservation:website", "website:booking", "booking"),
        map_url=map_url(lat, lon), directions_url=directions_url(lat, lon), rating=rating, review_count=review_count,
        visit=visit, score=0.0, lat=lat, lon=lon)
    place = place._replace(score=score(place, radius, matched, visit, open_state, filters))
    hidden = visit is not None and visit.level in (RestaurantVisit.WONT_EAT, RestaurantVisit.NOT_FOR_US)
    return {"place": place, "checks": checks, "matched": matched, "hidden": hidden}


def score(place, radius, matched, visit, open_state, filters):
    """Best match: Bayesian rating (neutral without ratings) + distance scaled to the radius + 6 per matched
    filter / craving word + the household's rating (Staple +25, Favorite +20, Will eat again +8, Needs tweaks -5)."""
    from .models import RestaurantVisit
    stars = bayesian(place.rating, place.review_count)
    total = ((stars if stars is not None else PRIOR_RATING) - 3.0) * 20
    total += max(0.0, 1.0 - place.miles / max(radius, 0.2)) * 25
    total += matched * 6
    if visit is not None:
        total += {RestaurantVisit.STAPLE: 25, RestaurantVisit.FAVORITE: 20, RestaurantVisit.WILL_EAT: 8,
                  RestaurantVisit.NEEDS_TWEAKS: -5}.get(visit.level, 0)
    if open_state == "open":
        total += 3
    elif open_state == "unknown":
        total -= 2
    return round(total, 3)


def passes(checks, include_unknown):
    return all(v is True or (v is None and include_unknown) for v in checks.values())


def _sort_key(sort):
    if sort == "distance":
        return lambda p: (p.miles, -p.score)
    if sort == "price":
        return lambda p: (p.price is None, p.price or 0, -p.score)
    if sort == "rating":
        return lambda p: (-(p.visit.level if p.visit else 0), -(bayesian(p.rating, p.review_count) or 0), -p.score)
    return lambda p: (-p.score, p.miles, p.name.lower())


def rank(elements, loc, filters, now, visits=None, limit=RESULTS):
    """(places, blockers, hidden count, candidates) - every filter strict; unknown passes only with include_unknown."""
    words = craving_words(filters.craving)
    rows, seen = [], set()
    for element in elements or []:
        row = evaluate(element, loc, filters, now, visits, words)
        if not row:
            continue
        place = row["place"]
        key = (place.name.lower(), round(float(place.lat), 4), round(float(place.lon), 4))
        if key in seen:
            continue
        seen.add(key)
        rows.append(row)
    hidden = sum(1 for r in rows if r["hidden"])
    rows = [r for r in rows if not r["hidden"]]
    kept = [r["place"] for r in rows if passes(r["checks"], filters.include_unknown)]
    kept.sort(key=_sort_key(filters.sort))
    return kept[:limit], (blockers(rows, filters, loc) if not kept else []), hidden, len(rows)


# ---------------------------------------------------------------------------
# The empty state: which filters removed the most, and one-tap relaxes
# ---------------------------------------------------------------------------

def _group_label(key, filters):
    if key.startswith("feat:"):
        return FEATURE_LABELS[key[5:]]
    if key.startswith("svc:"):
        return SERVICE_LABELS[key[4:]]
    return {"type": "Type of place", "cuisine": "Cuisine", "price": "Price",
            "when": "Open now" if filters.when == "now" else "Open at that time",
            "meal": MEAL_LABELS.get(filters.meal, "Meal"), "rating": "Minimum rating", "chains": "Exclude chains",
            "seen": dict(SEENS).get(filters.seen, ""), "craving": f"“{filters.craving}”", "unknown": "Unknown info"}[key]


def _relaxed(filters, key, rows):
    """(button label, filters with that one group relaxed)."""
    if key == "type":
        missing = {}
        for r in rows:
            if not r["checks"]["type"]:
                missing[r["place"].kind] = missing.get(r["place"].kind, 0) + 1
        best = max(missing, key=missing.get) if missing else "fastcasual"
        return f"Include {TYPE_SHORT[best].lower()}", filters._replace(types=tuple(filters.types) + (best,))
    if key.startswith("feat:"):
        return f"Drop “{FEATURE_LABELS[key[5:]]}”", filters._replace(features=tuple(f for f in filters.features
                                                                                    if f != key[5:]))
    if key.startswith("svc:"):
        return f"Drop “{SERVICE_LABELS[key[4:]]}”", filters._replace(services=tuple(s for s in filters.services
                                                                                    if s != key[4:]))
    return {
        "cuisine": ("Any cuisine", filters._replace(cuisines=())),
        "price": ("Any price", filters._replace(prices=())),
        "when": ("Any time", filters._replace(when="any")),
        "meal": ("Any meal", filters._replace(meal="")),
        "rating": ("Any rating", filters._replace(min_rating=0.0)),
        "chains": ("Include chains", filters._replace(no_chains=False)),
        "seen": ("Anywhere", filters._replace(seen="")),
        "craving": ("Clear the craving", filters._replace(craving="")),
        "unknown": ("Include places with unknown info", filters._replace(include_unknown=True)),
    }[key]


def blockers(rows, filters, loc, top=3):
    """The filters that removed the most places: per group, how many places fail it (no, or unknown while unknown
    is excluded) - plus "unknown info" (places that would pass if unknown counted) and a wider distance."""
    counts = {}
    unknown_only = 0
    for r in rows:
        for key, value in r["checks"].items():
            if value is False or (value is None and not filters.include_unknown):
                counts[key] = counts.get(key, 0) + 1
        if not filters.include_unknown and passes(r["checks"], True):
            unknown_only += 1
    out = []
    if unknown_only:
        label, relaxed = _relaxed(filters, "unknown", rows)
        out.append({"key": "unknown", "label": "Unknown info", "count": unknown_only, "relax_label": label,
                    "relax_qs": query_string(relaxed, loc)})
    for key in sorted(counts, key=lambda k: (-counts[k], k))[:top]:
        label, relaxed = _relaxed(filters, key, rows)
        out.append({"key": key, "label": _group_label(key, filters), "count": counts[key], "relax_label": label,
                    "relax_qs": query_string(relaxed, loc)})
    wider = MILES.index(filters.miles) + 1
    if wider < len(MILES):
        out.append({"key": "distance", "label": f"Within {filters.miles} mi", "count": None,
                    "relax_label": f"Widen to {MILES[wider]} mi",
                    "relax_qs": query_string(filters._replace(miles=MILES[wider]), loc)})
    return out


# ---------------------------------------------------------------------------
# The search
# ---------------------------------------------------------------------------

def _now(zone):
    """The time at the places (their own zone) - one place to fix the clock in tests."""
    return datetime.now(zone)


def household_visits(space):
    """{place_id: RestaurantVisit} for the space being shown."""
    from .models import RestaurantVisit
    if space is None:
        return {}
    return {v.place_id: v for v in RestaurantVisit.objects.filter(space=space)}


def search(loc, filters, user=None, space=None, now=None):
    """Fetch, filter and rank. Never raises. offline is True when no place list could be fetched (or ranking
    broke) - never dressed up as "no matches". Admins also see what went wrong."""
    if loc is None:
        return Search([], False, "", "", _attribution(), [], 0, 0)
    if now is None:
        now = _now(place_zone(loc, user))
    is_admin = bool(getattr(user, "is_superuser", False))
    try:
        result = fetch(loc.lat, loc.lon, filters.miles)
    except Exception:                                # fetch never raises; belt and braces
        logger.exception("Eat out: fetch crashed")
        result = Fetch(None, ["unexpected error (see the error log)"], None, "")
    if result.elements is None:
        message = UNAVAILABLE
        if is_admin:
            detail = "; ".join(result.errors) or "no Overpass hosts configured"
            message += f" Admin detail: {detail}. Run manage.py check_eat_out to test."
        return Search([], True, message, "", _attribution(), [], 0, 0)
    try:
        places, blocked, hidden, total = rank(result.elements, loc, filters, now, household_visits(space))
    except Exception:
        logger.exception("Eat out: ranking %s places failed", len(result.elements))
        message = UNAVAILABLE + (" Admin detail: ranking failed (see the error log)." if is_admin else "")
        return Search([], True, message, "", _attribution(), [], 0, 0)
    return Search(places, False, "", "" if places else NO_MATCHES, _attribution(), blocked, hidden, total)


def _attribution():
    return "Places from OpenStreetMap. Hours, prices and features only when tagged; no star ratings or photos."


def random_pick(places, rng=random):
    """1-based: one of the top PICK_FROM at random ("Pick for us"), or 0."""
    if not places:
        return 0
    return rng.randint(1, min(PICK_FROM, len(places)))
