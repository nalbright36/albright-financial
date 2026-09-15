"""
scan_service.py — scrapes a HiBid URL and scores each lot, returning a list
of dicts ready to pass into ScannedLot.objects.create(scan_request=scan, **lot).

NOTE: This file currently has TEMPORARY DEBUG LOGGING in _scrape() to help
figure out HiBid's actual JSON field names, since the first real scan came
back with 0 lots matched. Once we've corrected _looks_like_lot/_normalize_lot
based on what the debug output shows, the debug prints should be removed.
"""

import base64
import json
import os
import time

import requests
from playwright.sync_api import sync_playwright

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
TEXT_MODEL = "claude-haiku-4-5-20251001"

LOT_ID_KEYS = ("lotId", "lotID", "id", "lotNumber")
TITLE_KEYS = ("lotTitle", "title", "name")
DESC_KEYS = ("lotDescription", "description", "desc")
BID_KEYS = ("currentBid", "currentPrice", "bidAmount", "highBid")
IMAGE_KEYS = ("images", "image", "imageUrl", "thumbnail", "photoUrl")
URL_KEYS = ("lotUrl", "url", "permalink")
CATEGORY_KEYS = ("category", "categoryName", "breadcrumb")
BIDCOUNT_KEYS = ("bidCount", "numBids", "bids")


def _first(d, keys, default=None):
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return default


def _looks_like_lot(item):
    if not isinstance(item, dict):
        return False
    return any(k in item for k in TITLE_KEYS) and any(k in item for k in BID_KEYS + ("lotNumber",))


def _normalize_lot(item):
    image = _first(item, IMAGE_KEYS)
    if isinstance(image, list) and image:
        image = image[0]
    return {
        "lot_id": str(_first(item, LOT_ID_KEYS, "")),
        "title": (_first(item, TITLE_KEYS, "") or "").strip(),
        "description": (_first(item, DESC_KEYS, "") or "").strip(),
        "current_bid": _first(item, BID_KEYS),
        "bid_count": _first(item, BIDCOUNT_KEYS),
        "category": _first(item, CATEGORY_KEYS, "") or "",
        "image_url": image or "",
        "lot_url": _first(item, URL_KEYS, "") or "",
    }


def _extract_lots(payload):
    found = []

    def walk(node):
        if isinstance(node, dict):
            if _looks_like_lot(node):
                found.append(_normalize_lot(node))
                return
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(payload)
    return found


def _debug_dump(payload, prefix="payload", max_depth=3):
    """Recursively prints the shape of a JSON blob, up to max_depth levels,
    so we can find where lot arrays live even when nested inside wrapper
    keys like {"data": {...}} (GraphQL / general API envelopes)."""
    if max_depth <= 0 or not isinstance(payload, dict):
        return
    for k, v in payload.items():
        path = f"{prefix}['{k}']"
        if isinstance(v, list):
            if v and isinstance(v[0], dict):
                print(f"DEBUG:   {path} = list of {len(v)} dicts, first item keys: {list(v[0].keys())}")
            else:
                print(f"DEBUG:   {path} = list of {len(v)} items")
        elif isinstance(v, dict):
            print(f"DEBUG:   {path} = dict with keys: {list(v.keys())}")
            _debug_dump(v, path, max_depth - 1)


# Only these URL fragments get the verbose debug dump — everything else
# (language files, analytics beacons, etc.) is skipped to keep the log
# readable while we're still figuring out HiBid's real API shape.
DEBUG_URL_FILTERS = ("hibid-api.io", "hibid.com/graphql")


def _scrape(url, max_lots=300):
    collected = {}

    def handle_response(response):
        ct = response.headers.get("content-type", "")
        if "json" not in ct:
            return
        try:
            payload = response.json()
        except Exception:
            return

        # --- TEMPORARY DEBUG: dump the shape of the interesting responses ---
        if any(f in response.url for f in DEBUG_URL_FILTERS):
            print(f"DEBUG: JSON response from {response.url}")
            if isinstance(payload, dict):
                print(f"DEBUG: top-level keys: {list(payload.keys())}")
                _debug_dump(payload)
            elif isinstance(payload, list) and payload:
                first = payload[0]
                if isinstance(first, dict):
                    print(f"DEBUG: list of {len(payload)} items, first item keys: {list(first.keys())}")
        # --- end debug ---

        for lot in _extract_lots(payload):
            key = lot["lot_id"] or lot["lot_url"] or lot["title"]
            if key and key not in collected:
                collected[key] = lot
        if any(f in response.url for f in DEBUG_URL_FILTERS):
            print(f"DEBUG: {len(collected)} lots matched so far")

    with sync_playwright() as p:
        # PythonAnywhere-specific: playwright install doesn't work here, so
        # point at the Chromium PythonAnywhere already has installed, with
        # these extra launch args. See:
        # https://help.pythonanywhere.com/pages/Playwright
        browser = p.chromium.launch(
            executable_path="/usr/bin/chromium",
            headless=True,
            args=["--disable-gpu", "--no-sandbox", "--headless"],
        )
        page = browser.new_page()
        page.on("response", handle_response)
        page.goto(url, wait_until="networkidle", timeout=60000)
        time.sleep(1.2)

        stagnant = 0
        last_count = 0
        for _ in range(60):
            if len(collected) >= max_lots:
                break
            page.mouse.wheel(0, 4000)
            time.sleep(1.2)
            for label in ("Load More", "Next", "Show More"):
                try:
                    btn = page.get_by_text(label, exact=False)
                    if btn.count() > 0 and btn.first.is_visible():
                        btn.first.click(timeout=2000)
                        time.sleep(1.2)
                except Exception:
                    pass
            if len(collected) == last_count:
                stagnant += 1
            else:
                stagnant = 0
            last_count = len(collected)
            if stagnant >= 5:
                break

        browser.close()

    return list(collected.values())[:max_lots]


def _score_lot_text(lot):
    prompt = f"""You are helping a reseller spot auction lots that are worth a closer look \
because the listing undersells or mislabels what's actually there.

Lot title: {lot['title']}
Lot description: {lot['description'] or '(no description given)'}
Category listed: {lot['category'] or '(none listed)'}
Current bid: {lot['current_bid'] or 'unknown'}

Look for a vague/generic title hiding something valuable, details in the description \
(maker's marks, materials, hallmarks, brand names, "sterling", "14k", signed, vintage) \
not reflected in the title/category, or a category mismatch.

Respond ONLY with compact JSON: {{"interest_score": <0-100>, "reason": "<one sentence>"}}
"""
    headers = {
        "x-api-key": os.environ["ANTHROPIC_API_KEY"],
        "anthropic-version": ANTHROPIC_VERSION,
        "content-type": "application/json",
    }
    body = {"model": TEXT_MODEL, "max_tokens": 150, "messages": [{"role": "user", "content": prompt}]}
    try:
        resp = requests.post(ANTHROPIC_API_URL, headers=headers, json=body, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text").strip()
        if text.startswith("```"):
            text = text.strip("`").split("\n", 1)[-1]
        parsed = json.loads(text)
        return int(parsed.get("interest_score", 0)), str(parsed.get("reason", ""))
    except Exception as e:
        return 0, f"scoring failed: {e}"


def scrape_and_score(url, max_lots=300):
    """Main entry point called by the worker. Returns a list of dicts matching
    the ScannedLot model's fields (minus scan_request, which the caller sets)."""
    lots = _scrape(url, max_lots=max_lots)
    results = []
    for lot in lots:
        score, reason = _score_lot_text(lot)
        lot["interest_score"] = score
        lot["score_reasons"] = reason
        results.append(lot)
        time.sleep(0.3)  # light rate-limit pacing
    return results