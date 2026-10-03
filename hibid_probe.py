"""One-off HiBid probe. Makes at most 3 requests, no login, then prints what it found.

Run from your project folder (any Python with `requests`):
    python hibid_probe.py "14k gold" 33602 30

Arguments: search text, your zip code, radius in miles.
It answers: does HiBid accept plain requests? What format are buyer's premium,
end time and status? Paste the whole output back into the chat.
"""
import json
import sys
from datetime import datetime, timedelta, timezone

import requests

URL = "https://hibid.com/graphql"
HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/plain, */*",
    "SITE_SUBDOMAIN": "hibid.com",
    "Origin": "https://hibid.com",
    "Referer": "https://hibid.com/lots",
    "User-Agent": "Mozilla/5.0 (personal deal-finder; low volume)",
}

QUERY = """query LotSearch($pageNumber: Int!, $pageLength: Int!, $searchText: String = null,
  $zip: String = null, $miles: Int = null, $shippingOffered: Boolean = false,
  $status: AuctionLotStatus = null, $isArchive: Boolean = false, $countAsView: Boolean = false) {
  lotSearch(input: {searchText: $searchText, zip: $zip, miles: $miles, shippingOffered: $shippingOffered,
                    status: $status, isArchive: $isArchive, countAsView: $countAsView},
            pageNumber: $pageNumber, pageLength: $pageLength, sortDirection: DESC) {
    pagedResults {
      totalCount filteredCount
      results {
        id itemId lead description lotNumber pictureCount shippingOffered distanceMiles
        featuredPicture { thumbnailLocation fullSizeLocation }
        lotState { highBid bidCount isClosed priceRealized timeLeftSeconds status minBid }
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


def run(label, variables):
    print(f"\n=== {label} ===")
    payload = {"operationName": "LotSearch", "variables": variables, "query": QUERY}
    try:
        resp = requests.post(URL, headers=HEADERS, json=payload, timeout=30)
    except requests.RequestException as exc:
        print("REQUEST FAILED:", exc)
        return
    print("HTTP status:", resp.status_code, "| content-type:", resp.headers.get("content-type"))
    if resp.status_code != 200:
        print("Body starts with:", resp.text[:300].replace("\n", " "))
        return
    data = resp.json()
    if data.get("errors"):
        print("GraphQL errors:", json.dumps(data["errors"])[:600])
    paged = (((data.get("data") or {}).get("lotSearch") or {}).get("pagedResults")) or {}
    results = paged.get("results") or []
    print("totalCount:", paged.get("totalCount"), "| returned:", len(results))
    statuses = sorted({str((r.get("lotState") or {}).get("status")) for r in results})
    print("lotState.status values seen:", statuses)
    for r in results[:2]:
        a, s = r.get("auction") or {}, r.get("lotState") or {}
        left = s.get("timeLeftSeconds")
        est_end = (datetime.now(timezone.utc) + timedelta(seconds=left)).isoformat() if left else None
        print(json.dumps({
            "id": r.get("id"), "title": r.get("lead"), "highBid": s.get("highBid"),
            "bidCount": s.get("bidCount"), "isClosed": s.get("isClosed"),
            "priceRealized": s.get("priceRealized"), "timeLeftSeconds": left, "estimated_end_utc": est_end,
            "pictureCount": r.get("pictureCount"), "shippingOffered": r.get("shippingOffered"),
            "distanceMiles": r.get("distanceMiles"), "auction.buyerPremium": a.get("buyerPremium"),
            "auction.buyerPremiumRate": a.get("buyerPremiumRate"),
            "auction.bidCloseDateTime": a.get("bidCloseDateTime"),
            "auction.city/state": f"{a.get('eventCity')}, {a.get('eventState')}",
            "auction.shippingType": (a.get("auctionOptions") or {}).get("shippingType"),
            "auctioneer": (a.get("auctioneer") or {}).get("name"),
        }, indent=2))


if __name__ == "__main__":
    text = sys.argv[1] if len(sys.argv) > 1 else "14k gold"
    zip_code = sys.argv[2] if len(sys.argv) > 2 else None
    miles = int(sys.argv[3]) if len(sys.argv) > 3 else 30
    base = {"searchText": text, "pageNumber": 1, "pageLength": 10, "countAsView": False}
    run("1. Open lots near you (pickup)", {**base, "zip": zip_code, "miles": miles, "status": "OPEN"})
    run("2. Open lots that ship", {**base, "shippingOffered": True, "status": "OPEN"})
    run("3. Closed/archived lots", {**base, "isArchive": True})