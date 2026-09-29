"""MaxSold adapter (local-pickup estate/downsizing auctions).

Uses the lot search the MaxSold website itself calls (captured in DevTools 2026-09-28):
    GET https://api.maxsold.com/listings/search?query=...&lat=...&lng=...&radiusMetres=...
It is not a documented public API, so fields can change. The search is meaning-based
(similarity ranked), so loose matches come back - the coin parser filters them.

Pickup details are stored in raw["_pickup"] so the pipeline can price the drive
without any model changes:
    {"distance_miles", "auction_id", "auction_title", "city", "has_shipping"}
"""
import html
import logging
import time
from datetime import datetime

import requests
from django.conf import settings

from .base import BaseAdapter, RawLot, SourceBlocked, SourceUnavailable

log = logging.getLogger(__name__)

SEARCH_URL = "https://api.maxsold.com/listings/search"
# VERIFY: open any lot on maxsold.com and compare its address-bar URL with this pattern.
LOT_URL = "https://maxsold.com/listing/{lot_id}/{slug}"
METERS_PER_MILE = 1609.344

BASE_PARAMS = {             # copied from DevTools; query/lat/lng/radius/page filled per request
    "paginationType": "pagination",
    "similarityThreshold": "0.71",
    "limit": "24",
    "days": "",
    "lotState": "open",
    "total": "true",
    "closedLimit": "",
    "sort": "cosine_distance_asc",
    "country": "usa",
    "valid": "true",
}


def _parse_dt(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))  # UTC, timezone-aware
    except ValueError:
        return None


class MaxSoldAdapter(BaseAdapter):
    source = "maxsold"
    max_pages_per_keyword = 3

    def __init__(self):
        cfg = settings.RESELLING_SCANNER["SOURCES"]["maxsold"]
        self.lat, self.lng = cfg["home_lat"], cfg["home_lng"]
        self.radius_m = int(cfg["radius_miles"] * METERS_PER_MILE)
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "Mozilla/5.0 (personal deal-finder; low volume)"})

    def _get(self, params, keyword):
        for attempt in (1, 2):
            try:
                resp = self.session.get(SEARCH_URL, params=params, timeout=30)
                break
            except (requests.Timeout, requests.ConnectionError) as exc:
                if attempt == 2:
                    raise SourceUnavailable(f"MaxSold '{keyword}': {exc}") from exc
                log.warning("MaxSold request failed (%s); retrying once in 15s", exc)
                time.sleep(15)
        if resp.status_code in (401, 403, 429):
            raise SourceBlocked(f"MaxSold returned {resp.status_code}")
        resp.raise_for_status()
        return resp.json()

    def _search(self, keyword: str, extra_params: dict):
        """Shared pagination/request/retry loop for both open and closed
        searches - extra_params overrides just the fields that differ
        between them (e.g. lotState)."""
        seen = 0
        for page in range(self.max_pages_per_keyword):
            params = {**BASE_PARAMS, **extra_params, "query": keyword, "lat": self.lat, "lng": self.lng,
                      "radiusMetres": self.radius_m, "pageNumber": page}
            data = self._get(params, keyword)
            items = data.get("listings") or []
            for item in items:
                lot = self._parse_item(item)
                if lot:
                    yield lot
            seen += len(items)
            if not items or seen >= (data.get("total") or 0):
                return
            self.pause()

    def search(self, keyword: str):
        yield from self._search(keyword, {})

    def search_closed(self, keyword: str):
        """Closed (sold) MaxSold lots. currentBid.amount on a closed lot is
        the final price it sold for - _parse_item() already reads that
        field into RawLot.current_price, so no special parsing is needed."""
        yield from self._search(keyword, {"lotState": "closed"})

    def _parse_item(self, item: dict) -> RawLot | None:
        lot_id = item.get("amLotId")
        title = html.unescape(item.get("title") or "")
        if not lot_id or not title:
            log.debug("Skipping MaxSold item with unexpected shape: %s", list(item)[:10])
            return None

        slug = (item.get("generatedDetails") or {}).get("slug") or ""
        images = [u for u in (item.get("imageUrls") or []) if isinstance(u, str) and u]
        meters = item.get("distanceMeters")
        item["_pickup"] = {
            "distance_miles": round(meters / METERS_PER_MILE, 1) if meters is not None else None,
            "auction_id": item.get("amAuctionId"),
            "auction_title": html.unescape(item.get("auctionTitle") or ""),
            "city": (item.get("address") or {}).get("city") or "",
            "has_shipping": bool(item.get("hasShipping")),
        }
        return RawLot(
            source=self.source,
            external_id=str(lot_id),
            url=LOT_URL.format(lot_id=lot_id, slug=slug),
            title=title,
            description=html.unescape(item.get("description") or ""),
            current_price=float((item.get("currentBid") or {}).get("amount") or 0),
            image_url=images[0] if images else "",
            bid_count=item.get("amBidCount"),
            end_time=_parse_dt(item.get("closeTime")),
            raw=item,
        )