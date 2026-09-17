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
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

import requests
from playwright.sync_api import sync_playwright

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
TEXT_MODEL = "claude-haiku-4-5-20251001"
DEEP_DIVE_MODEL = "claude-sonnet-5"   # stronger model for the deliberate, per-lot deep dive
MAX_DEEP_DIVE_IMAGES = 5

# --- Max hammer assumptions (shared constants so they're easy to tune later) ---
AUCTION_PREMIUM_RATE = 0.20   # buyer's premium assumption
CC_FEE_RATE = 0.03            # credit card processing fee assumption
EBAY_FEE_RATE = 0.14          # eBay final value fee, applied to resale price
MIN_PROFIT_MARGIN = 0.10      # required net profit as a fraction of resale price
SMALL_ITEM_SHIPPING = 12.00
STANDARD_SHIPPING = 20.00
# Keyword match against category (or title, if category is blank — some
# HiBid page types don't return a category at all) to guess small-item
# shipping vs standard. Rough heuristic, not exact.
SMALL_ITEM_KEYWORDS = ("jewelry", "coin", "watch", "stamp", "gem", "currency", "ring", "earring")

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


def _auctioneer_info(item):
    """Pulls auctioneer name + close datetime from a lot's own nested
    'auction' object (present on category-browse pages, where each lot
    carries its full auction info). Returns ('', None) if not present —
    the caller falls back to the top-level auction info captured
    separately for page types that don't nest it per-lot."""
    auction_obj = item.get("auction") or {}
    auctioneer = (auction_obj.get("auctioneer") or {}).get("name", "")
    close_dt = auction_obj.get("bidCloseDateTime")
    return auctioneer, close_dt


def _normalize_lot(item):
    lot_state = item.get("lotState") or {}
    featured = item.get("featuredPicture") or {}
    lot_id = item.get("id")
    item_id = item.get("itemId")
    auctioneer_name, auction_close_datetime = _auctioneer_info(item)

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
        "auctioneer_name": auctioneer_name,
        "auction_close_datetime": auction_close_datetime,
    }


def _find_raw_lot_node(payload):
    """Like _extract_lots, but returns the first raw matching dict as-is
    (not run through _normalize_lot). Used for reconciliation, where we
    want lotState.isClosed/priceRealized — fields the listing-page
    ScannedLot schema doesn't carry, so _normalize_lot doesn't expose them."""
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


def _extract_raw_lots(payload):
    """Recursively finds every raw lot-shaped dict in a JSON blob, without
    normalizing them. Shared by both the live scanner (which normalizes
    via _normalize_lot) and the historical harvester (which normalizes via
    _normalize_historical_lot), so both paths look for lots the same way."""
    found = []

    def walk(node):
        if isinstance(node, dict):
            if _looks_like_lot(node):
                found.append(node)
                return
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(payload)
    return found


def _extract_lots(payload):
    return [_normalize_lot(raw) for raw in _extract_raw_lots(payload)]


def _find_paging_info(payload):
    """Finds a pagedResults-shaped node ({pageNumber, totalPages, results})
    anywhere in the payload, so we can tell how many pages a category-browse
    or search-style URL actually has. Returns (page_number, total_pages) or
    None if this response doesn't carry paging info (e.g. a single-auction
    catalog page, which doesn't paginate this way)."""
    result = {"info": None}

    def walk(node):
        if result["info"] is not None:
            return
        if isinstance(node, dict):
            if "totalPages" in node and "pageNumber" in node and "results" in node:
                result["info"] = (node.get("pageNumber"), node.get("totalPages"))
                return
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(payload)
    return result["info"]


def _set_apage(url, page_number):
    """Returns url with its apage query param set to page_number, adding
    the param if it wasn't already present."""
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    qs["apage"] = [str(page_number)]
    new_query = urlencode(qs, doseq=True)
    return urlunparse(parsed._replace(query=new_query))


def _find_top_level_auction_info(payload):
    """Single-auction catalog pages return auction info once, separately,
    at payload.data.auction — not nested inside each lot the way
    category-browse pages do. Returns (auctioneer_name, close_datetime,
    auction_text) or (None, None, "") if this response isn't that shape.
    auction_text is the auction's own eventName + description, used as a
    fallback signal for guessing a category on page types (like this one)
    that don't expose per-lot category at all."""
    try:
        auction = payload["data"]["auction"]
        auctioneer = (auction.get("auctioneer") or {}).get("name", "")
        close_dt = auction.get("bidCloseDateTime")
        text = " ".join(filter(None, [auction.get("eventName", ""), auction.get("description", "")]))
        if auctioneer or close_dt or text:
            return auctioneer, close_dt, text
    except (KeyError, TypeError, AttributeError):
        pass
    return None, None, ""


def _collect_raw_lots(url, max_lots=300, max_pages=40):
    """Shared pagination/scroll engine: visits url (and subsequent apage=N
    pages, if the response carries paging info) via a real browser, and
    returns (raw_lot_dicts, top_level_auction_info). Not normalized — the
    caller decides how to interpret the raw lot objects, since the live
    scanner and the historical harvester need different fields out of the
    same underlying data. Keeping the pagination logic here in one place
    means both paths automatically benefit from the same fixes (timeout
    handling, apage pagination, auctioneer fallback) without duplicating
    or risking drift between two copies of this code."""
    collected = {}
    paging_state = {"total_pages": None}
    top_level_auction = {"name": None, "close_dt": None, "text": ""}

    def handle_response(response):
        ct = response.headers.get("content-type", "")
        if "json" not in ct:
            return
        try:
            payload = response.json()
        except Exception:
            return

        for raw in _extract_raw_lots(payload):
            key = str(raw.get("id") or raw.get("itemId") or raw.get("lead") or id(raw))
            if key not in collected:
                collected[key] = raw

        info = _find_paging_info(payload)
        if info and info[1]:
            paging_state["total_pages"] = info[1]
            # --- TEMPORARY DEBUG: figure out why harvesting isn't paginating ---
            print(f"DEBUG: paging info seen — pageNumber={info[0]}, totalPages={info[1]}, "
                  f"collected so far={len(collected)}")
            # --- end debug ---

        name, close_dt, auction_text = _find_top_level_auction_info(payload)
        if name or close_dt or auction_text:
            top_level_auction["name"] = name
            top_level_auction["close_dt"] = close_dt
            top_level_auction["text"] = auction_text

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

        current_page_num = 1
        current_url = url
        pages_visited = 0

        while True:
            count_before_page = len(collected)
            page.goto(current_url, wait_until="domcontentloaded", timeout=60000)
            time.sleep(2)  # give the page's initial JS a moment to fire its first data fetch

            # Existing scroll/"Load More" handling — still needed for
            # single-auction catalog pages, which use infinite scroll
            # within one page rather than an apage query param.
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

            pages_visited += 1
            new_this_page = len(collected) - count_before_page
            print(f"DEBUG: finished page {current_page_num} (visit #{pages_visited}) — "
                  f"collected={len(collected)}, new_this_page={new_this_page}, stagnant_rounds={stagnant}")

            if len(collected) >= max_lots:
                print(f"DEBUG: stopping — hit max_lots ({max_lots})")
                break
            if pages_visited >= max_pages:
                print(f"DEBUG: stopping — hit max_pages ({max_pages})")
                break

            total_pages = paging_state["total_pages"]
            if total_pages and current_page_num >= total_pages:
                print(f"DEBUG: stopping — reached known total_pages ({total_pages})")
                break

            # We don't require totalPages to be known before trying the next
            # apage — some page types (this catalog type, apparently) never
            # report it via GraphQL even though apage navigation still
            # works. Instead: always attempt the next page once, and only
            # stop if that attempt genuinely added nothing new — meaning
            # either we've gone past the real last page, or apage isn't
            # respected at all for this URL shape (in which case one wasted
            # extra page load is a small cost for correctness elsewhere).
            if pages_visited > 1 and new_this_page == 0:
                print(f"DEBUG: stopping — apage={current_page_num} added 0 new lots "
                      f"(either past the last real page, or apage isn't supported for this URL)")
                break

            current_page_num += 1
            current_url = _set_apage(url, current_page_num)
            print(f"DEBUG: advancing to page {current_page_num}: {current_url}")

        browser.close()

    return list(collected.values())[:max_lots], top_level_auction


def _apply_top_level_auction_fallback(lots, top_level_auction):
    """For page types that don't nest auction info per-lot (single-auction
    catalogs), apply the top-level auction info captured separately to any
    lot dict that didn't already get it from its own nested object."""
    if top_level_auction["name"] or top_level_auction["close_dt"]:
        for lot in lots:
            if not lot.get("auctioneer_name"):
                lot["auctioneer_name"] = top_level_auction["name"] or ""
            if not lot.get("auction_close_datetime"):
                lot["auction_close_datetime"] = top_level_auction["close_dt"]
    return lots


def _scrape(url, max_lots=300, max_pages=40):
    raw_lots, top_level_auction = _collect_raw_lots(url, max_lots=max_lots, max_pages=max_pages)
    lots = [_normalize_lot(raw) for raw in raw_lots]
    return _apply_top_level_auction_fallback(lots, top_level_auction)


def _normalize_historical_lot(item):
    """Like _normalize_lot, but for the bulk historical harvester: pulls
    the FINAL closed-auction fields (priceRealized, closing bidCount)
    directly from lotState instead of the live highBid/bidCount used for
    still-open lots, and returns None for anything that isn't actually
    closed yet (in case a status=CLOSED search URL lets a stray still-open
    lot through — better to skip it than record a false "final" price)."""
    lot_state = item.get("lotState") or {}
    if not lot_state.get("isClosed"):
        return None

    featured = item.get("featuredPicture") or {}
    lot_id = item.get("id")
    item_id = item.get("itemId")
    auctioneer_name, auction_close_datetime = _auctioneer_info(item)
    price_realized = lot_state.get("priceRealized")

    return {
        "lot_id": str(lot_id or item_id or ""),
        "title": (item.get("lead") or "").strip(),
        "description": (item.get("description") or "").strip(),
        "category": _category_name(item.get("category")),
        "final_price": price_realized if price_realized else None,
        "final_bid_count": lot_state.get("bidCount"),
        "image_url": featured.get("thumbnailLocation") or featured.get("fullSizeLocation") or "",
        "lot_url": f"https://hibid.com/lot/{lot_id}" if lot_id else "",
        "auctioneer_name": auctioneer_name,
        "auction_close_datetime": auction_close_datetime,
    }


CATEGORY_KEYWORDS = [
    ("Coins & Currency", ("coin", "currency", "morgan", "silver dollar", "numismatic")),
    ("Jewelry", ("jewelry", "jewellery", "gemstone", "diamond", "ring", "earring", "necklace", "pendant")),
    ("Watches", ("watch", "timepiece", "rolex")),
    ("Sports Memorabilia", ("sports memorabilia", "trading card", "autograph")),
    ("Antiques & Collectibles", ("antique", "collectible", "vintage")),
    ("Tools", ("tool", "machinery", "equipment")),
    ("Electronics", ("electronic", "computer", "camera")),
    ("Firearms", ("firearm", "ammunition", "ammo")),
    ("Art", ("painting", "sculpture", "artwork")),
    ("Furniture", ("furniture",)),
    ("Toys & Games", ("toy", "pokemon", "yu gi oh", "one piece", "trading card game")),
    ("Stamps", ("stamp collection", "philately")),
    ("Vehicles", ("automobile", "motorcycle", "vehicle")),
]


def _guess_category_from_text(text):
    """Best-effort category guess by keyword match against the auction's
    own title/description — used as a fallback ONLY when a lot has no
    per-lot category at all (single-auction catalog pages don't expose
    one, a known HiBid data gap confirmed during live-scanner work, not a
    bug in our matching). Coarse by nature: applies one whole auction's
    theme to every lot in it, so it can mislabel a stray off-theme lot
    within an otherwise single-category auction — better than a blank
    category, not a substitute for a real per-lot one."""
    if not text:
        return ""
    lowered = text.lower()
    for label, keywords in CATEGORY_KEYWORDS:
        if any(kw in lowered for kw in keywords):
            return label
    return ""


def harvest_closed_lots(url, max_lots=1000, max_pages=100):
    """Bulk-harvests raw closed-auction data — no AI scoring, pure hard
    data (price, bids, category, auctioneer) — from a HiBid search or
    category URL. Intended to be given a status=CLOSED (or similar)
    filtered URL, so results already carry their final price/bid count at
    scrape time with no separate reconciliation step needed.

    Reuses the exact same pagination engine as the live scanner
    (_collect_raw_lots), so the timeout fix, apage pagination, and
    auctioneer-fallback logic already debugged there apply here too.
    Returns a list of dicts ready for
    HistoricalLot.objects.create(harvest_request=..., **lot).
    """
    raw_lots, top_level_auction = _collect_raw_lots(url, max_lots=max_lots, max_pages=max_pages)

    results = []
    debug_printed = False
    for raw in raw_lots:
        normalized = _normalize_historical_lot(raw)
        if normalized is None:
            continue  # not actually closed — skip

        # --- TEMPORARY DEBUG: bid count isn't coming through on catalog
        # harvests — dump one raw closed lot's lotState to see whether the
        # field is genuinely missing here too (same root cause as category)
        # or named/shaped differently than expected.
        if not debug_printed:
            print(f"DEBUG: raw lotState for first closed lot: {json.dumps(raw.get('lotState'), default=str)}")
            debug_printed = True
        # --- end debug ---

        if not normalized.get("category"):
            normalized["category"] = _guess_category_from_text(top_level_auction.get("text", ""))
        results.append(normalized)

    return _apply_top_level_auction_fallback(results, top_level_auction)


def fetch_lot_status(lot_url):
    """Loads a single HiBid lot page and reports whether it has closed and,
    if so, its final realized price. Used by the reconciliation feature to
    check back on lots after their auction ends, comparing the actual
    outcome against what the scanner estimated at scan time.

    A single lot's own page fires a different GraphQL query than the
    listing pages (something like "lotDetail" rather than "lotSearch"),
    but the lot object it returns still has the same lotState shape, so
    the same generic matcher (_looks_like_lot/_find_raw_lot_node) works
    here without modification.
    """
    captured = {"lot_node": None}

    def handle_response(response):
        ct = response.headers.get("content-type", "")
        if "json" not in ct:
            return
        try:
            payload = response.json()
        except Exception:
            return
        node = _find_raw_lot_node(payload)
        if node:
            captured["lot_node"] = node

    with sync_playwright() as p:
        browser = p.chromium.launch(
            executable_path="/usr/bin/chromium",
            headless=True,
            args=["--disable-gpu", "--no-sandbox", "--headless"],
        )
        page = browser.new_page()
        page.on("response", handle_response)
        page.goto(lot_url, wait_until="domcontentloaded", timeout=60000)
        time.sleep(2)
        browser.close()

    node = captured["lot_node"]
    if not node:
        raise ValueError(f"Could not find lot data at {lot_url}")

    lot_state = node.get("lotState") or {}
    is_closed = bool(lot_state.get("isClosed"))
    price_realized = lot_state.get("priceRealized")
    final_bid_count = lot_state.get("bidCount")
    auctioneer_name, _ = _auctioneer_info(node)

    return {
        "is_closed": is_closed,
        # priceRealized is only meaningful once the lot has actually
        # closed — a value of 0 on an open lot just means "no sale yet",
        # not a real $0 outcome.
        "price_realized": price_realized if is_closed and price_realized else None,
        "final_bid_count": final_bid_count,
        "auctioneer_name": auctioneer_name,
    }


def _fetch_all_lot_images(lot_url):
    """Loads a lot's own page and returns (raw_lot_node, list_of_photo_urls)
    — every available photo, not just the single featured thumbnail stored
    at scan time.

    NOTE: the 'pictures' field's exact shape is unverified against a real
    response (only its key name was seen in an earlier truncated debug
    dump). If this comes back with 0 extra images beyond the featured one,
    that's the first thing to check — the field name or item shape here is
    likely slightly off, the same kind of fix we made for lot_url earlier.
    """
    captured = {"lot_node": None}

    def handle_response(response):
        ct = response.headers.get("content-type", "")
        if "json" not in ct:
            return
        try:
            payload = response.json()
        except Exception:
            return
        node = _find_raw_lot_node(payload)
        if node:
            captured["lot_node"] = node

    with sync_playwright() as p:
        browser = p.chromium.launch(
            executable_path="/usr/bin/chromium",
            headless=True,
            args=["--disable-gpu", "--no-sandbox", "--headless"],
        )
        page = browser.new_page()
        page.on("response", handle_response)
        page.goto(lot_url, wait_until="domcontentloaded", timeout=60000)
        time.sleep(2)
        browser.close()

    node = captured["lot_node"]
    if not node:
        raise ValueError(f"Could not find lot data at {lot_url}")

    urls = []
    pictures = node.get("pictures")
    if isinstance(pictures, list):
        for pic in pictures:
            if isinstance(pic, dict):
                pic_url = pic.get("fullSizeLocation") or pic.get("thumbnailLocation")
                if pic_url:
                    urls.append(pic_url)
            elif isinstance(pic, str):
                urls.append(pic)

    if not urls:
        featured = node.get("featuredPicture") or {}
        pic_url = featured.get("fullSizeLocation") or featured.get("thumbnailLocation")
        if pic_url:
            urls.append(pic_url)

    return node, urls[:MAX_DEEP_DIVE_IMAGES]


def _image_to_b64(image_url):
    resp = requests.get(image_url, timeout=15)
    resp.raise_for_status()
    media_type = resp.headers.get("content-type", "image/jpeg").split(";")[0]
    return base64.standard_b64encode(resp.content).decode("utf-8"), media_type


def run_deep_dive(lot):
    """lot is a ScannedLot instance. Fetches every available photo for the
    lot plus its full text, and asks a stronger, vision-capable model for a
    more careful resale estimate, confidence level, and reasoning than the
    cheap bulk text-only scoring pass used at scan time.

    Deliberately conservative: no real eBay comps are pulled here (eBay no
    longer allows unauthenticated access to sold-listing data), so this is
    still an AI estimate, not verified market data — just a more careful
    one, with an explicit confidence label so a "low confidence" result
    reads as a flag to research further, not as a firm number to bid to.
    """
    node, image_urls = _fetch_all_lot_images(lot.lot_url)

    content = []
    for url in image_urls:
        try:
            b64, media_type = _image_to_b64(url)
            content.append({"type": "image", "source": {"type": "base64", "media_type": media_type, "data": b64}})
        except Exception:
            continue  # skip any single image that fails to load; keep the rest

    prompt = f"""You are doing a careful, deliberate resale evaluation of a single auction lot for a \
reseller who has been overpaying on quick, exciting-looking flags and wants a harder, more skeptical \
second look before committing real money. You do NOT have access to real eBay sold comps for this — \
base your estimate on the photos, the listing text, and your general knowledge, and be explicit about \
where your confidence is genuinely low rather than projecting false precision.

Lot title: {lot.title}
Full description: {lot.description or '(no description given)'}
Category: {lot.category or '(none listed)'}
Current bid: {lot.current_bid if lot.current_bid is not None else 'unknown'}
Initial quick-scan estimate: {f"${lot.estimated_resale_low}-${lot.estimated_resale_high}" if lot.estimated_resale_low else 'none given'}

Look carefully at the attached photos for anything that changes the picture: visible wear or damage, \
hallmarks or stamps, materials that look inconsistent with the listing text, condition issues not \
mentioned in the description, or signs the item is lab-grown/synthetic/reproduction rather than what \
the title implies. If a certification number is visible in a photo, note it, but do not assume it has \
been verified — you cannot check GIA/IGI's database yourself.

Respond ONLY with compact JSON in this exact shape:
{{"resale_low": <number>, "resale_high": <number>, "confidence": "<high|medium|low>", \
"analysis": "<2-4 sentences explaining your reasoning, referencing specific things you saw in the photos or text>"}}
"""
    content.append({"type": "text", "text": prompt})

    headers = {
        "x-api-key": os.environ["ANTHROPIC_API_KEY"],
        "anthropic-version": ANTHROPIC_VERSION,
        "content-type": "application/json",
    }
    body = {"model": DEEP_DIVE_MODEL, "max_tokens": 500, "messages": [{"role": "user", "content": content}]}
    resp = requests.post(ANTHROPIC_API_URL, headers=headers, json=body, timeout=60)
    resp.raise_for_status()
    data = resp.json()
    text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text").strip()
    if text.startswith("```"):
        text = text.strip("`").split("\n", 1)[-1]
    parsed = json.loads(text)

    resale_low = parsed.get("resale_low")
    shipping = _estimate_shipping({"category": lot.category, "title": lot.title})
    max_hammer = _calc_max_hammer(resale_low, shipping)

    return {
        "resale_low": resale_low,
        "resale_high": parsed.get("resale_high"),
        "confidence": str(parsed.get("confidence", "")).lower(),
        "analysis": str(parsed.get("analysis", "")),
        "max_hammer": max_hammer,
        "images_analyzed": len(image_urls),
    }


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


def _estimate_shipping(lot):
    """Rough $12 vs $20 shipping guess based on keyword match against
    category (falling back to title, since some page types don't return
    category at all)."""
    haystack = f"{lot.get('category', '')} {lot.get('title', '')}".lower()
    if any(kw in haystack for kw in SMALL_ITEM_KEYWORDS):
        return SMALL_ITEM_SHIPPING
    return STANDARD_SHIPPING


def _calc_max_hammer(resale_low, shipping):
    """Solves for the highest hammer (winning bid) price that still leaves
    at least MIN_PROFIT_MARGIN net profit (as a fraction of resale_low)
    after buyer's premium, credit card fee, shipping, and eBay's final
    value fee are all deducted from the resale price.

    profit = resale - (hammer*(1+AUCTION_PREMIUM_RATE+CC_FEE_RATE)
                        + shipping + resale*EBAY_FEE_RATE)
    profit >= MIN_PROFIT_MARGIN * resale
    => hammer <= (resale*(1 - EBAY_FEE_RATE - MIN_PROFIT_MARGIN) - shipping)
                 / (1 + AUCTION_PREMIUM_RATE + CC_FEE_RATE)

    Returns None if resale_low is None (nothing to base the estimate on),
    or 0 if fixed costs alone already exceed what the margin allows (i.e.
    this item isn't profitable at any bid under these assumptions).
    """
    if resale_low is None:
        return None
    numerator = resale_low * (1 - EBAY_FEE_RATE - MIN_PROFIT_MARGIN) - shipping
    denominator = 1 + AUCTION_PREMIUM_RATE + CC_FEE_RATE
    max_hammer = numerator / denominator
    return round(max(max_hammer, 0), 2)


def scrape_and_score(url, max_lots=300, max_pages=40):
    """Main entry point called by the worker. Returns a list of dicts matching
    the ScannedLot model's fields (minus scan_request, which the caller sets).

    max_pages caps how many apage=N pages a category-browse/search URL will
    walk through (single-auction catalog URLs ignore this — they don't
    paginate via apage, so they just run once as before)."""
    lots = _scrape(url, max_lots=max_lots, max_pages=max_pages)
    results = []
    for lot in lots:
        scored = _score_lot_text(lot)
        lot["interest_score"] = scored["interest_score"]
        lot["score_reasons"] = scored["reason"]
        lot["estimated_resale_low"] = scored["resale_low"]
        lot["estimated_resale_high"] = scored["resale_high"]
        shipping = _estimate_shipping(lot)
        lot["max_hammer"] = _calc_max_hammer(scored["resale_low"], shipping)
        results.append(lot)
        time.sleep(0.3)  # light rate-limit pacing
    return results