"""One request: asks HiBid's GraphQL API which sort orders and lot statuses it accepts.
Run:  python hibid_enums.py   and paste the output back into the chat."""
import json

import requests

QUERY = """{ sort: __type(name: "EventItemSortOrder") { enumValues { name } }
  status: __type(name: "AuctionLotStatus") { enumValues { name } } }"""

resp = requests.post(
    "https://hibid.com/graphql",
    headers={"Content-Type": "application/json", "SITE_SUBDOMAIN": "hibid.com",
             "Origin": "https://hibid.com", "User-Agent": "Mozilla/5.0 (personal deal-finder; low volume)"},
    json={"query": QUERY}, timeout=30)
print("HTTP status:", resp.status_code)
print(json.dumps(resp.json(), indent=2)[:3000])