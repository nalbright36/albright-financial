"""eBay Browse API (official) - current ACTIVE listings for a search query.

Optional: used only if EBAY_CLIENT_ID and EBAY_CLIENT_SECRET are set (free eBay
developer account, Production keyset). Sold prices are not available through this
API, which is why the AI review still searches the web for sold comps.
"""
import base64
import os
import re
import time

import requests

TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"
SEARCH_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"
SCOPE = "https://api.ebay.com/oauth/api_scope"
_token_cache = {"token": None, "expires": 0.0}

FILLER = r"l@@k|\b(lot|of|vintage|antique|estate|nice|rare|look|wow|untested|as is|see photos|bundle)\b"


def is_configured() -> bool:
    return bool(os.environ.get("EBAY_CLIENT_ID") and os.environ.get("EBAY_CLIENT_SECRET"))


def _app_token() -> str:
    if _token_cache["token"] and time.time() < _token_cache["expires"] - 60:
        return _token_cache["token"]
    creds = f"{os.environ['EBAY_CLIENT_ID']}:{os.environ['EBAY_CLIENT_SECRET']}".encode()
    resp = requests.post(
        TOKEN_URL,
        headers={"Authorization": "Basic " + base64.b64encode(creds).decode(),
                 "Content-Type": "application/x-www-form-urlencoded"},
        data={"grant_type": "client_credentials", "scope": SCOPE},
        timeout=20,
    )
    resp.raise_for_status()
    data = resp.json()
    _token_cache.update(token=data["access_token"], expires=time.time() + int(data.get("expires_in", 7200)))
    return _token_cache["token"]


def query_from_title(title: str) -> str:
    q = re.sub(FILLER, " ", title.lower())
    q = re.sub(r"[^\w\s/.-]", " ", q)
    return re.sub(r"\s+", " ", q).strip()[:100]


def search_active(query: str, limit: int = 10) -> list:
    resp = requests.get(
        SEARCH_URL,
        headers={"Authorization": f"Bearer {_app_token()}", "X-EBAY-C-MARKETPLACE-ID": "EBAY_US"},
        params={"q": query, "limit": limit},
        timeout=20,
    )
    resp.raise_for_status()
    out = []
    for item in resp.json().get("itemSummaries") or []:
        price = (item.get("price") or {}).get("value")
        if item.get("itemWebUrl") and price:
            out.append({"title": item.get("title", ""), "price": float(price),
                        "condition": item.get("condition", ""), "url": item["itemWebUrl"]})
    return out