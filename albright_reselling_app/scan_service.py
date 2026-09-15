"""
scan_service.py — scrapes a HiBid URL and scores each lot, returning a list
of dicts ready to pass into ScannedLot.objects.create(scan_request=scan, **lot).

Field mapping notes (reverse-engineered from HiBid's GraphQL response at
hibid.com/graphql, query "lotSearch" -> pagedResults.results):
  - title        <- lead
  - description  <- description
  - current_bid  <- lotState.highBid  (NOT the top-level bidAmount field,
                     which doesn't match the real current price)
  - bid_count    <- lotState.bidCount
  - category     <- category.categoryName (category can be dict or string)
  - image_url    <- featuredPicture.thumbnailLocation
  - lot_url      <- https://hibid.com/lot/{id} — confirmed real HiBid URL
                     pattern (slug after the id is optional); note this
                     uses the top-level "id" field, NOT "itemId" (they are
                     different HiBid identifiers — id is the lot page id).
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

LOT_MARKER_KEYS = ("bidList", "lotNumber", "lead")  # unique enough to real lot records


def _looks_like_lot(item):
    if not isinstance(item, dict):
        return False
    return all(k in item for k in LOT_MARKER_KEYS)


def _category_name(category):
    if isinstance(category, dict):
        return category.get("categoryName") or category.get("fullCategory") or ""
    if isinstance(category, str):
        return category
    return ""


def _normalize_lot(item):
    lot_state = item.get("lotState") or {}
    featured = item.get("featuredPicture") or {}
    lot_id = item.get("id")
    item_id = item.get("itemId")

    return {
        "lot_id": str(lot_id or item_id or ""),
        "title": (item.get("lead") or "").strip(),
        "description": (item.get("description") or "").strip(),
        "current_bid": lot_state.get("highBid"),
        "bid_count": lot_state.get("bidCount"),
        "category": _category_name(item.get("category")),
        "image_url": featured.get("thumbnailLocation") or featured.get("fullSizeLocation") or "",
        # Best-guess deep link based on lot id — HiBid didn't return a
        # direct URL for this lot (links/linkTypes came back empty), but
        # https://hibid.com/lot/{id} is HiBid's confirmed real URL pattern
        # (the slug after the id is optional).
        "lot_url": f"https://hibid.com/lot/{lot_id}" if lot_id else "",
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

        for lot in _extract_lots(payload):
            key = lot["lot_id"] or lot["lot_url"] or lot["title"]
            if key and key not in collected:
                collected[key] = lot

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

Also give a rough estimated resale range (used-item resale, not retail) based on what \
this item typically sells for, if you have any reasonable basis to estimate one. If \
there's truly not enough information to estimate (e.g. a vague "misc box lot" with no \
identifiable contents), return null for both bounds rather than guessing.

Respond ONLY with compact JSON: {{"interest_score": <0-100>, "reason": "<one sentence>", \
"resale_low": <number or null>, "resale_high": <number or null>}}
"""
    headers = {
        "x-api-key": os.environ["ANTHROPIC_API_KEY"],
        "anthropic-version": ANTHROPIC_VERSION,
        "content-type": "application/json",
    }
    body = {"model": TEXT_MODEL, "max_tokens": 200, "messages": [{"role": "user", "content": prompt}]}
    try:
        resp = requests.post(ANTHROPIC_API_URL, headers=headers, json=body, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text").strip()
        if text.startswith("```"):
            text = text.strip("`").split("\n", 1)[-1]
        parsed = json.loads(text)
        return {
            "interest_score": int(parsed.get("interest_score", 0)),
            "reason": str(parsed.get("reason", "")),
            "resale_low": parsed.get("resale_low"),
            "resale_high": parsed.get("resale_high"),
        }
    except Exception as e:
        return {"interest_score": 0, "reason": f"scoring failed: {e}", "resale_low": None, "resale_high": None}


def scrape_and_score(url, max_lots=300):
    """Main entry point called by the worker. Returns a list of dicts matching
    the ScannedLot model's fields (minus scan_request, which the caller sets)."""
    lots = _scrape(url, max_lots=max_lots)
    results = []
    for lot in lots:
        scored = _score_lot_text(lot)
        lot["interest_score"] = scored["interest_score"]
        lot["score_reasons"] = scored["reason"]
        lot["estimated_resale_low"] = scored["resale_low"]
        lot["estimated_resale_high"] = scored["resale_high"]
        results.append(lot)
        time.sleep(0.3)  # light rate-limit pacing
    return results