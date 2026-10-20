"""RSS/Atom feeds (official project and program feeds only - which feeds
run is up to the Settings page's list). The feed is fetched with the same
polite GET as every collector (so a 403/429 stops it), then parsed with
feedparser."""
from datetime import datetime, timezone as dt_timezone

import feedparser

from ..links import is_web_url
from .base import BaseCollector, RawOpportunity


def _published(entry):
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if not parsed:
        return None
    return datetime(*parsed[:6], tzinfo=dt_timezone.utc)


class RssCollector(BaseCollector):
    kind = "rss"
    max_entries = 50

    def collect(self, source):
        resp = self.get(source.url)
        feed = feedparser.parse(resp.content)
        for entry in (feed.entries or [])[: self.max_entries]:
            link = entry.get("link") or ""
            if not is_web_url(link):
                continue  # no usable link (feedparser can fall back to a bare guid like "q2")
            summary = entry.get("summary") or ""
            content = " ".join(part.get("value", "") for part in entry.get("content") or [])
            yield RawOpportunity(
                external_id=f"rss:{entry.get('id') or link}"[:300],
                url=link,
                title=(entry.get("title") or link)[:500],
                text=(content or summary).strip(),
                metadata={"feed": source.url, "published": entry.get("published") or entry.get("updated") or ""},
                published=_published(entry),
            )
