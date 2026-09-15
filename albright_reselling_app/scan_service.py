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
import re
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


def _find_first_raw_lot(payload):
    """Like _extract_lots, but returns the first raw matching dict as-is
    (not run through _normalize_lot) so we can inspect fields our current
    normalization doesn't know about yet."""
    result = {"node": None}

    def walk(node):
        if result["node"] is not None:
            return
        if isinstance(node, dict):
            if _looks_like_lot(node):
                result["node"] = node
                return
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(payload)
    return result["node"]


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
    debug_state = {"printed": False}

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

        # --- TEMPORARY DEBUG: category-browse pages seem to return a
        # different lot shape than the catalog page we originally mapped
        # (bid/category/resale are coming back empty on this page type).
        # Print just the top-level keys plus the specific fields we care
        # about, skipping the bulky nested "auction" object that ate the
        # whole output budget last time.
        if not debug_state["printed"]:
            raw = _find_first_raw_lot(payload)
            if raw:
                print("DEBUG: raw lot top-level keys:", list(raw.keys()))
                for field in ("category", "lotState", "bidAmount", "bidCount", "lead", "lotNumber", "site"):
                    if field in raw:
                        print(f"DEBUG:   raw['{field}'] = {json.dumps(raw[field], default=str)[:500]}")
                debug_state["printed"] = True
        # --- end debug ---

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
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        time.sleep(2)  # give the page's initial JS a moment to fire its first data fetch

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

CRITICAL: resale_low and resale_high are the ONLY place the resale range is recorded —
nothing reads your reason text for numbers. If your reason mentions any specific price
or price range, that exact range MUST also appear in resale_low/resale_high. Never
describe a resale value in the reason while leaving resale_low/resale_high null.

Respond ONLY with compact JSON, in exactly this field order: \
{{"resale_low": <number or null>, "resale_high": <number or null>, \
"interest_score": <0-100>, "reason": "<one sentence>"}}
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
        reason = str(parsed.get("reason", ""))
        resale_low = parsed.get("resale_low")
        resale_high = parsed.get("resale_high")

        # Safety net: if the model still left these null but the reason
        # text contains a dollar range (e.g. "$800-1200" or "$800 to
        # $1,200"), pull it out with a regex rather than losing the
        # estimate the model clearly already formed.
        if resale_low is None or resale_high is None:
            match = re.search(
                r"\$\s?([\d,]+(?:\.\d+)?)\s*(?:-|to|\u2013)\s*\$?\s?([\d,]+(?:\.\d+)?)",
                reason,
            )
            if match:
                resale_low = resale_low if resale_low is not None else float(match.group(1).replace(",", ""))
                resale_high = resale_high if resale_high is not None else float(match.group(2).replace(",", ""))

        return {
            "interest_score": int(parsed.get("interest_score", 0)),
            "reason": reason,
            "resale_low": resale_low,
            "resale_high": resale_high,
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