"""USDA FoodData Central (https://api.nal.usda.gov/fdc/v1/) - the official, public food composition database.

Two calls: search (/foods/search - Foundation, SR Legacy, Survey (FNDDS) and Branded foods) and details
(/food/{fdcId}). Every food fetched is cached in the Food table (nutrients per 100 g, household portions in grams),
so a repeat lookup never calls the API; a search's results are remembered for a day too.

The key is USDA_API_KEY (.env). It's sent in the X-Api-Key header - never in a URL, never logged or shown. Without
it, search() and food() raise NotConfigured and the pages say "USDA key not set"; everything else keeps working.

Polite to their limits (about 1,000 requests an hour per key): short timeouts, at most two retries on 429 / 5xx /
network errors, waiting as long as Retry-After asks (capped).
"""
import hashlib
import re
import time
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

import requests
from django.conf import settings
from django.core.cache import cache

from .models import NUTRIENTS, Food

BASE = "https://api.nal.usda.gov/fdc/v1/"
DATA_TYPES = ["Foundation", "SR Legacy", "Survey (FNDDS)", "Branded"]
GENERIC_TYPES = ["Foundation", "SR Legacy", "Survey (FNDDS)"]
TIMEOUT = (5, 12)                   # connect, read (seconds)
RETRIES = 2
MAX_WAIT = 8                        # longest Retry-After we honour (seconds)
SEARCH_CACHE_SECONDS = 24 * 3600
PAGE_SIZE = 20
NOT_SET = "USDA key not set - ask the site admin to add USDA_API_KEY. You can still use My foods, Recent and Quick add."

# FoodData Central nutrient ids -> our names. Energy: kcal (1008), else the Atwater figures Foundation foods use.
NUTRIENT_IDS = {1008: "calories", 2047: "calories_atwater", 2048: "calories_specific", 1003: "protein", 1005: "carbs",
                1004: "fat", 1079: "fiber", 2000: "sugar", 1063: "sugar_nlea", 1093: "sodium"}
NUTRIENT_NUMBERS = {"208": 1008, "957": 2047, "958": 2048, "203": 1003, "205": 1005, "204": 1004, "291": 1079,
                    "269": 2000, "307": 1093}


class UsdaError(Exception):
    def __init__(self, message):
        super().__init__(message)
        self.message = message


class NotConfigured(UsdaError):
    def __init__(self):
        super().__init__(NOT_SET)


def configured():
    return bool(getattr(settings, "USDA_API_KEY", ""))


def _get(path, params, session=None, sleep=None):
    """One GET to the API -> its JSON. Retries 429 / 5xx / network errors (at most RETRIES times)."""
    key = getattr(settings, "USDA_API_KEY", "")
    if not key:
        raise NotConfigured()
    session = session or requests
    sleep = sleep or time.sleep
    headers = {"X-Api-Key": key, "Accept": "application/json", "User-Agent": "AlbrightFitness/1.0"}
    for attempt in range(RETRIES + 1):
        try:
            response = session.get(BASE + path, params=params, headers=headers, timeout=TIMEOUT)
        except requests.RequestException as exc:
            if attempt == RETRIES:
                raise UsdaError("Couldn't reach USDA FoodData Central - try again in a minute.") from exc
            sleep(1 + attempt)
            continue
        if response.status_code == 429 or response.status_code >= 500:
            if attempt == RETRIES:
                raise UsdaError("USDA FoodData Central is busy right now - try again in a minute.")
            retry_after = response.headers.get("Retry-After", "")
            sleep(min(int(retry_after), MAX_WAIT) if retry_after.isdigit() else 1 + attempt)
            continue
        if response.status_code == 404:
            raise UsdaError("USDA doesn't have that food any more.")
        if response.status_code in (401, 403):
            raise UsdaError("USDA turned the key down - ask the site admin to check USDA_API_KEY.")
        if response.status_code >= 400:
            raise UsdaError(f"USDA FoodData Central answered with an error ({response.status_code}).")
        try:
            return response.json()
        except ValueError as exc:
            raise UsdaError("USDA FoodData Central sent something unexpected - try again.") from exc
    raise UsdaError("Couldn't reach USDA FoodData Central.")         # not reached


def _dec(value, places="0.01"):
    try:
        return Decimal(str(value)).quantize(Decimal(places), rounding=ROUND_HALF_UP)
    except (InvalidOperation, TypeError, ValueError):
        return None


def _nutrients(rows):
    """FoodData Central nutrient rows (search or details shape) -> {calories, protein, ...} per 100 g."""
    found = {}
    for row in rows or []:
        nutrient = row.get("nutrient") or {}
        nid = row.get("nutrientId") or nutrient.get("id")
        number = str(row.get("nutrientNumber") or nutrient.get("number") or "")
        nid = nid if nid in NUTRIENT_IDS else NUTRIENT_NUMBERS.get(number)
        name = NUTRIENT_IDS.get(nid)
        unit = (row.get("unitName") or nutrient.get("unitName") or "").lower()
        value = row.get("value", row.get("amount"))
        if not name or value is None or (name.startswith("calories") and unit and unit != "kcal"):
            continue
        found.setdefault(name, _dec(value))
    out = {n: found.get(n) for n in NUTRIENTS}
    out["calories"] = out["calories"] or found.get("calories_atwater") or found.get("calories_specific")
    out["sugar"] = out["sugar"] or found.get("sugar_nlea")
    return out


def _amount_text(amount):
    amount = _dec(amount, "0.01")
    if amount is None:
        return ""
    return f"{amount.normalize():f}"


def _portions(data):
    """Household portions in grams: [{"label": "1 cup, chopped", "grams": 160.0}, ...] (both shapes)."""
    portions = []
    for p in data.get("foodPortions") or []:
        grams = _dec(p.get("gramWeight"), "0.1")
        if not grams or grams <= 0:
            continue
        unit = ((p.get("measureUnit") or {}).get("name") or "").strip()
        unit = "" if unit.lower() in ("undetermined", "") else unit
        description = (p.get("portionDescription") or "").strip()
        modifier = (p.get("modifier") or "").strip()
        if description and description.lower() != "quantity not specified":
            label = description
        else:
            amount = _amount_text(p.get("amount") or 1)
            words = " ".join(w for w in (unit, modifier) if w and not w.isdigit())
            label = f"{amount} {words}".strip() if words else ""
        if label:
            portions.append({"label": label[:60], "grams": float(grams)})
    for m in data.get("foodMeasures") or []:                       # search results' shape
        grams = _dec(m.get("gramWeight"), "0.1")
        label = (m.get("disseminationText") or "").strip()
        if grams and grams > 0 and label and label.lower() != "quantity not specified":
            portions.append({"label": label[:60], "grams": float(grams)})
    serving, unit = data.get("servingSize"), (data.get("servingSizeUnit") or "").lower()
    if serving and unit in ("g", "grm", "gram", "grams"):
        text = (data.get("householdServingFullText") or "").strip()
        grams = _dec(serving, "0.1")
        portions.append({"label": (f"{text} ({grams.normalize():f} g)" if text else f"1 serving ({grams.normalize():f} g)")[:60],
                         "grams": float(grams)})
    seen, unique = set(), []
    for p in portions:
        if p["label"].lower() not in seen:
            seen.add(p["label"].lower())
            unique.append(p)
    return unique[:12]


def _save(data, detailed):
    """One food (search or details shape) into the cache -> the Food row."""
    fdc_id = data.get("fdcId")
    if not fdc_id:
        return None
    fields = {"name": (data.get("description") or "Unnamed food").strip()[:255],
              "brand": (data.get("brandName") or data.get("brandOwner") or "").strip()[:120],
              "data_type": (data.get("dataType") or "")[:30], **_nutrients(data.get("foodNutrients"))}
    portions = _portions(data)
    existing = Food.objects.filter(fdc_id=fdc_id).first()
    if existing and existing.detailed and not detailed:
        return existing                                     # never replace a full record with a search summary
    if portions or not existing:
        fields["portions"] = portions
    fields["detailed"] = detailed or bool(existing and existing.detailed)
    food, _created = Food.objects.update_or_create(fdc_id=fdc_id, defaults=fields)
    return food


def _search_key(query, data_types):
    raw = f"{query.lower()}|{','.join(data_types)}"
    return "usda:search:" + hashlib.sha256(raw.encode()).hexdigest()[:32]


def clean_query(text):
    return " ".join(re.sub(r"[^\w\s,%'-]+", " ", text or "").split())[:100]


def search(query, data_types=None, session=None, sleep=None):
    """Foods matching the words -> Food rows (cached), best first. The same search within a day uses the cache."""
    query = clean_query(query)
    if not query:
        return []
    if not configured():
        raise NotConfigured()
    data_types = data_types or DATA_TYPES
    key = _search_key(query, data_types)
    ids = cache.get(key)
    if ids is None:
        data = _get("foods/search", {"query": query, "dataType": ",".join(data_types), "pageSize": PAGE_SIZE},
                    session=session, sleep=sleep)
        foods = [f for f in (_save(row, detailed=False) for row in data.get("foods") or []) if f]
        ids = [f.fdc_id for f in foods]
        cache.set(key, ids, SEARCH_CACHE_SECONDS)
    by_id = {f.fdc_id: f for f in Food.objects.filter(fdc_id__in=ids)}
    return [by_id[i] for i in ids if i in by_id]


def food(fdc_id, session=None, sleep=None):
    """One food with its household portions. From the cache when it's been fetched before (no API call)."""
    cached = Food.objects.filter(fdc_id=fdc_id).first()
    if cached and cached.detailed:
        return cached
    if not configured():
        if cached:
            return cached                                   # a search result is still usable (grams work)
        raise NotConfigured()
    data = _get(f"food/{int(fdc_id)}", {}, session=session, sleep=sleep)
    return _save(data, detailed=True)
