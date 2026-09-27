"""ShopGoodwill adapter.

ShopGoodwill's website loads search results from a JSON endpoint. It is NOT a
documented public API, so the URL and payload below can change without notice.

BEFORE FIRST RUN - verify against the live site:
  1. Open shopgoodwill.com, press F12 -> Network tab -> filter "Fetch/XHR".
  2. Search for "morgan dollar".
  3. Click the search request. Compare its URL with SEARCH_URL and its
     "Payload" with SEARCH_PAYLOAD below; paste in anything that differs.
  4. Check the "Response" field names against _parse_item().
Also read ShopGoodwill's Terms of Use. Keep polling slow, and if the site
blocks us we stop - no proxies, no workarounds.
"""
import logging
from datetime import datetime

import requests

from .base import BaseAdapter, RawLot, SourceBlocked

log = logging.getLogger(__name__)

SEARCH_URL = "https://buyerapi.shopgoodwill.com/api/Search/ItemListing"
ITEM_URL = "https://shopgoodwill.com/item/{item_id}"

SEARCH_PAYLOAD = {           # copied from DevTools, 2026-09-25
    "isSize": False, "isWeddingCatagory": "false", "isMultipleCategoryIds": False,
    "isFromHeaderMenuTab": False, "layout": "", "isFromHomePage": False,
    "searchText": "", "selectedGroup": "", "selectedCategoryIds": "", "selectedSellerIds": "",
    "lowPrice": "0", "highPrice": "999999", "searchBuyNowOnly": "", "searchPickupOnly": "false",
    "searchNoPickupOnly": "false", "searchOneCentShippingOnly": "false", "searchDescriptions": "false",
    "searchClosedAuctions": "false", "closedAuctionEndingDate": "", "closedAuctionDaysBack": "7",
    "searchCanadaShipping": "false", "searchInternationalShippingOnly": "false",
    "sortColumn": "1", "page": "1", "pageSize": "40", "sortDescending": "false",
    "savedSearchId": 0, "useBuyerPrefs": "true", "searchUSOnlyShipping": "false",
    "categoryLevelNo": "1", "partNumber": "", "catIds": "", "categoryLevel": 1, "categoryId": 0,
}


def _first(d: dict, *keys, default=None):
    for k in keys:
        if d.get(k) not in (None, ""):
            return d[k]
    return default


def _parse_dt(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


class ShopGoodwillAdapter(BaseAdapter):
    source = "shopgoodwill"

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (personal deal-finder; low volume)",
        })

    def search(self, keyword: str):
        for page in range(1, self.max_pages_per_keyword + 1):
            today = datetime.now()
            payload = {**SEARCH_PAYLOAD, "searchText": keyword, "page": str(page),
                       "closedAuctionEndingDate": f"{today.month}/{today.day}/{today.year}"}
            resp = self.session.post(SEARCH_URL, json=payload, timeout=20)
            if resp.status_code in (401, 403, 429):
                raise SourceBlocked(f"ShopGoodwill returned {resp.status_code}")
            resp.raise_for_status()

            data = resp.json()
            items = (data.get("searchResults") or {}).get("items") or data.get("items") or []
            if not items:
                return
            for item in items:
                lot = self._parse_item(item)
                if lot:
                    yield lot
            self.pause()

    def _parse_item(self, item: dict) -> RawLot | None:
        item_id = _first(item, "itemId", "itemID", "id")
        title = _first(item, "title", "itemTitle", default="")
        if not item_id or not title:
            log.debug("Skipping item with unexpected shape: %s", list(item)[:10])
            return None
        return RawLot(
            source=self.source,
            external_id=str(item_id),
            url=ITEM_URL.format(item_id=item_id),
            title=title,
            current_price=float(_first(item, "currentPrice", default=0) or 0),
            image_url=(_first(item, "imageURL", "imageUrl", default="") or "").replace("\\", "/"),
            bid_count=_first(item, "numBids", "bidCount"),
            end_time=_parse_dt(_first(item, "endTime", "endDate")),
            raw=item,
        )