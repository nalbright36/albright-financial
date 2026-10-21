"""Every opportunity source gets one collector that yields RawOpportunity
objects - same adapter pattern as the reseller scanner's
scanner/adapters/base.py. Polite by default: a pause between requests,
one retry on a timeout or server error, and a hard stop (never a retry) on
403/429 - a refusal or rate limit means "back off", not "try harder".

Collectors only ever READ public data (HTTP GET). Nothing here logs in,
solves CAPTCHAs, or gets around access controls.
"""
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime

import requests

log = logging.getLogger(__name__)

USER_AGENT = "AlbrightCryptoFinder/1.0 (personal research; polite; contact via site owner)"


@dataclass
class RawOpportunity:
    external_id: str
    url: str
    title: str
    text: str = ""
    metadata: dict = field(default_factory=dict)
    published: datetime | None = None
    # Facts stated in a structured API's own fields (location, tags...), saved
    # as-is by pipeline.store - only the keys in pipeline.STRUCTURED_FACTS.
    facts: dict = field(default_factory=dict)


class SourceBlocked(Exception):
    """The site refused us (403/429). Stop this source - never retry around it."""


class SourceUnavailable(Exception):
    """Timed out / connection refused / 5xx, even after one polite retry."""


class BaseCollector:
    kind = "base"
    min_interval = None   # timedelta: the site's minimum time between fetches (pipeline.scan_sources)
    request_delay_seconds = 2.0
    timeout_seconds = 20
    retries = 1

    def collect(self, source):
        """Yield RawOpportunity objects for one Source row."""
        raise NotImplementedError

    def pause(self):
        time.sleep(self.request_delay_seconds)

    def headers(self):
        return {"User-Agent": USER_AGENT}

    def get(self, url, params=None):
        """One GET with the polite rules above. Returns the response."""
        last_exc = None
        for attempt in range(self.retries + 1):
            try:
                resp = requests.get(url, params=params, headers=self.headers(), timeout=self.timeout_seconds)
            except (requests.Timeout, requests.ConnectionError) as exc:
                last_exc = exc
            else:
                if resp.status_code in (403, 429):
                    raise SourceBlocked(f"{url} answered {resp.status_code} - stopping this source")
                if resp.status_code >= 500:
                    last_exc = SourceUnavailable(f"{url} answered {resp.status_code}")
                else:
                    resp.raise_for_status()
                    return resp
            if attempt < self.retries:
                self.pause()
        raise SourceUnavailable(str(last_exc))
