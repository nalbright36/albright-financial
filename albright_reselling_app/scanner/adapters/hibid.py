"""HiBid adapter (thousands of auction houses; mix of shipping and local pickup).

Uses the LotSearch GraphQL query the HiBid website itself sends (captured 2026-10-02,
verified with plain requests: no login, no browser). Not a documented public API.

Two passes per keyword:
  - pickup:   lots within your radius (zip + miles), priced with a round-trip drive cost
  - shipping: lots that ship, nationwide, capped by page limits (there can be thousands)

Per-auction costs go in raw["_costs"], pickup details in raw["_pickup"] (same shape as
MaxSold, so the existing pickup-cost helper works).
"""
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests
from django.conf import settings

from .base import BaseAdapter, RawLot, SourceBlocked, SourceUnavailable

log = logging.getLogger(__name__)

URL = "https://hibid.com/graphql"
LOT_URL = "https://hibid.com/lot/{lot_id}"
HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/plain, */*",
    "SITE_SUBDOMAIN": "hibid.com",
    "Origin": "https://hibid.com",
    "Referer": "https://hibid.com/lots",
    "User-Agent": "Mozilla/5.0 (personal deal-finder; low volume)",
}
OPEN_STATUSES = {"OPEN", "POSTED"}

QUERY = """query LotSearch($pageNumber: Int!, $pageLength: Int!, $searchText: String = null,
  $zip: String = null, $miles: Int = null, $shippingOffered: Boolean = false,
  $status: AuctionLotStatus = null, $sortOrder: EventItemSortOrder = null,
  $isArchive: Boolean = false, $countAsView: Boolean = false) {
  lotSearch(input: {searchText: $searchText, zip: $zip, miles: $miles, shippingOffered: $shippingOffered,
                    status: $status, sortOrder: $sortOrder, isArchive: $isArchive, countAsView: $countAsView},
            pageNumber: $pageNumber, pageLength: $pageLength, sortDirection: SORT_DIRECTION) {
    pagedResults {
      totalCount
      results {
        id lead description lotNumber pictureCount shippingOffered distanceMiles
        featuredPicture { thumbnailLocation fullSizeLocation }
        lotState { highBid minBid bidCount isClosed priceRealized timeLeftSeconds status reserveSatisfied showReserveStatus }
        auction {
          id eventName buyerPremium buyerPremiumRate bidCloseDateTime
          eventCity eventState eventZip distanceMiles
          auctionOptions { shippingType }
          auctioneer { id name }
        }
      }
    }
  }
}"""


def parse_premium(rate, text, default: float) -> float:
    """HiBid gives buyerPremiumRate as a multiplier (1.13 = 13%), but it's sometimes 1
    while the text says otherwise ("A 15% fee is added"). Text percentages are summed
    ("18% + 3% Credit Card Fee" = 21%). Use whichever is higher; default if neither."""
    from_rate = (float(rate) - 1.0) if isinstance(rate, (int, float)) and 1.0 < float(rate) < 2.0 else 0.0
    pcts = [float(p) for p in re.findall(r"(\d+(?:\.\d+)?)\s*%", text or "")]
    from_text = min(sum(pcts), 50.0) / 100 if pcts else 0.0
    best = max(from_rate, from_text)
    return round(best, 4) if best > 0 else default


def parse_end_time(time_left_seconds, bid_close_local: str | None, now: datetime, auction_tz: str):
    """Lot end time (UTC). timeLeftSeconds when positive; otherwise the auction's close
    time (local wall time, interpreted in auction_tz). Returns (end_time, source)."""
    if isinstance(time_left_seconds, (int, float)) and time_left_seconds > 0:
        return now + timedelta(seconds=float(time_left_seconds)), "time_left"
    if bid_close_local:
        try:
            naive = datetime.fromisoformat(str(bid_close_local).replace("Z", ""))
            if naive.tzinfo is None:
                naive = naive.replace(tzinfo=ZoneInfo(auction_tz))
            return naive.astimezone(timezone.utc), "auction_close"
        except ValueError:
            pass
    return None, "unknown"


class HiBidAdapter(BaseAdapter):
    source = "hibid"

    def __init__(self):
        cfg = settings.RESELLING_SCANNER["SOURCES"]["hibid"]
        self.cfg = cfg
        self.page_length = cfg.get("page_length", 100)
        self.max_pages = cfg.get("max_pages_per_pass", 2)
        direction = cfg.get("sort_direction")   # None = don't send it (as the website does when sorting)
        if direction is None:
            self.query = QUERY.replace(", sortDirection: SORT_DIRECTION", "")
        elif direction in ("ASC", "DESC"):
            self.query = QUERY.replace("SORT_DIRECTION", direction)
        else:
            raise ValueError("HiBid sort_direction must be None, 'ASC' or 'DESC'")
        self.session = requests.Session()
        self.session.headers.update(HEADERS)

    # -- HTTP --------------------------------------------------------------
    def _post(self, variables: dict, keyword: str) -> dict:
        payload = {"operationName": "LotSearch", "variables": variables, "query": self.query}
        for attempt in (1, 2):
            try:
                resp = self.session.post(URL, json=payload, timeout=30)
                break
            except (requests.Timeout, requests.ConnectionError) as exc:
                if attempt == 2:
                    raise SourceUnavailable(f"HiBid '{keyword}': {exc}") from exc
                log.warning("HiBid request failed (%s); retrying once in 15s", exc)
                time.sleep(15)
        if resp.status_code in (401, 403, 429) or "text/html" in (resp.headers.get("content-type") or ""):
            raise SourceBlocked(f"HiBid returned {resp.status_code} ({resp.headers.get('content-type')})")
        resp.raise_for_status()
        data = resp.json()
        if data.get("errors"):
            raise SourceUnavailable(f"HiBid GraphQL error for '{keyword}': {str(data['errors'])[:300]}")
        return ((data.get("data") or {}).get("lotSearch") or {}).get("pagedResults") or {}

    def _passes(self, keyword: str, archive: bool = False):
        base = {"searchText": keyword, "pageLength": self.page_length, "countAsView": False}
        if archive:
            base["isArchive"] = True
        else:
            base["status"] = "OPEN"   # without this, sorted results start with ended lots
            if self.cfg.get("sort_order"):
                base["sortOrder"] = self.cfg["sort_order"]
        if self.cfg.get("include_pickup", True):
            yield "pickup", {**base, "zip": self.cfg["home_zip"], "miles": self.cfg["radius_miles"]}
        if self.cfg.get("include_shipping", True):
            yield "shipping", {**base, "shippingOffered": True}

    def _iter(self, keyword: str, archive: bool):
        seen = set()
        for pass_name, variables in self._passes(keyword, archive):
            fetched = 0
            for page in range(1, self.max_pages + 1):
                paged = self._post({**variables, "pageNumber": page}, keyword)
                results = paged.get("results") or []
                for item in results:
                    lot = self._parse_item(item, pass_name)
                    if lot and lot.external_id not in seen:
                        seen.add(lot.external_id)
                        yield lot, item
                fetched += len(results)
                if not results or fetched >= (paged.get("totalCount") or 0):
                    break
                self.pause()
            self.pause()

    # -- public API ---------------------------------------------------------
    def search(self, keyword: str):
        for lot, item in self._iter(keyword, archive=False):
            state = item.get("lotState") or {}
            if state.get("isClosed") or (state.get("status") and state["status"] not in OPEN_STATUSES):
                continue
            yield lot

    def search_closed(self, keyword: str, days_back: int = 2):
        """Closed lots (final price in current_price). days_back kept for interface parity."""
        for lot, item in self._iter(keyword, archive=True):
            state = item.get("lotState") or {}
            if state.get("isClosed"):
                lot.current_price = float(state.get("priceRealized") or state.get("highBid") or 0)
                yield lot

    # -- parsing ------------------------------------------------------------
    def _parse_item(self, item: dict, pass_name: str) -> RawLot | None:
        lot_id, title = item.get("id"), (item.get("lead") or "").strip()  # use id, not itemId
        if not lot_id or not title:
            return None
        auction = item.get("auction") or {}
        state = item.get("lotState") or {}
        picture = item.get("featuredPicture") or {}
        ships = bool(item.get("shippingOffered"))

        end_time, end_source = parse_end_time(
            state.get("timeLeftSeconds"), auction.get("bidCloseDateTime"),
            datetime.now(timezone.utc), self.cfg.get("auction_timezone", "America/New_York"))
        premium = parse_premium(auction.get("buyerPremiumRate"), auction.get("buyerPremium"),
                                self.cfg.get("default_buyer_premium_pct", 0.20))
        miles = item.get("distanceMiles") or auction.get("distanceMiles")

        item["_costs"] = {"buyer_premium_pct": premium, "premium_text": auction.get("buyerPremium") or ""}
        # Lots that ship are priced with shipping; pickup-only lots with the drive cost.
        if not ships:
            item["_pickup"] = {
                "distance_miles": round(float(miles), 1) if miles else float(self.cfg["radius_miles"]),
                "distance_estimated": not miles,
                "auction_id": auction.get("id"),
                "auction_title": auction.get("eventName") or "",
                "city": f"{auction.get('eventCity') or ''}, {auction.get('eventState') or ''}".strip(", "),
                "has_shipping": False,
            }
        item["_hibid"] = {
            "pass": pass_name, "end_time_source": end_source, "ships": ships,
            "shipping_type": (auction.get("auctionOptions") or {}).get("shippingType"),
            "auction_id": auction.get("id"), "auction_title": auction.get("eventName") or "",
            "auctioneer_id": (auction.get("auctioneer") or {}).get("id"),
            "auctioneer": (auction.get("auctioneer") or {}).get("name") or "",
            "city": auction.get("eventCity"), "state": auction.get("eventState"), "zip": auction.get("eventZip"),
            "lot_number": item.get("lotNumber"), "picture_count": item.get("pictureCount"),
            "status": state.get("status"),
            "high_bid": state.get("highBid"), "min_bid": state.get("minBid"),
            # reserve_not_met: the lot may not sell at all, even above our max bid
            "reserve_not_met": bool(state.get("showReserveStatus")) and state.get("reserveSatisfied") is False,
        }
        return RawLot(
            source=self.source,
            external_id=str(lot_id),
            url=LOT_URL.format(lot_id=lot_id),
            title=title,
            description=item.get("description") or "",
            # The price you'd actually pay: the high bid, but never below the opening bid.
            current_price=max(float(state.get("highBid") or 0), float(state.get("minBid") or 0)),
            image_url=picture.get("fullSizeLocation") or picture.get("thumbnailLocation") or "",
            bid_count=state.get("bidCount"),
            end_time=end_time,
            raw=item,
        )