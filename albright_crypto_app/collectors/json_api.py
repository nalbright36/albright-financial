"""Official JSON job/gig APIs ("API (income)" sources) - public, no login,
added on the Settings page. The response is either a list of items or an
object holding one (under "jobs", "results", "data", "items" ...); each
item's fields are read by their usual names. Same polite GET as every
collector (403/429 stops the source). Nothing is scraped: an item without
an http(s) link of its own is skipped."""
import html
import re
from datetime import datetime, timezone as dt_timezone

from ..links import is_web_url
from .base import BaseCollector, RawOpportunity

LIST_KEYS = ("jobs", "results", "data", "items", "listings", "postings", "opportunities")
ID_KEYS = ("id", "guid", "slug", "uuid")
URL_KEYS = ("url", "link", "apply_url", "job_url", "application_url", "html_url")
TITLE_KEYS = ("title", "position", "name", "job_title")
TEXT_KEYS = ("description", "body", "summary", "content", "details")
DATE_KEYS = ("publication_date", "published_at", "date", "created_at", "posted_at")
# Stated facts shown under the description (they're from the listing itself,
# so quotes from them are still quotes "from the text").
FACT_KEYS = (("company_name", "Company"), ("company", "Company"), ("candidate_required_location", "Location"),
             ("location", "Location"), ("job_type", "Job type"), ("salary", "Salary"), ("tags", "Tags"))


def _first(item, keys):
    for key in keys:
        value = item.get(key)
        if value not in (None, "", []):
            return value
    return None


def _plain(text):
    text = re.sub(r"<(br|/p|/li|/h\d)\s*/?>", "\n", str(text or ""), flags=re.I)
    text = html.unescape(re.sub(r"<[^>]+>", " ", text))
    return re.sub(r"[ \t]+", " ", re.sub(r"\n\s*\n+", "\n\n", text)).strip()


def _items(payload):
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in LIST_KEYS:
            if isinstance(payload.get(key), list):
                return payload[key]
    return []


def _published(value):
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=dt_timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt_timezone.utc)


class JsonApiCollector(BaseCollector):
    kind = "api_income"
    max_entries = 100

    def collect(self, source):
        payload = self.get(source.url).json()
        for item in _items(payload)[: self.max_entries]:
            if not isinstance(item, dict):
                continue  # e.g. a legal notice as the list's first element
            link = str(_first(item, URL_KEYS) or "")
            if not is_web_url(link):
                continue
            facts = []
            seen = set()
            for key, label in FACT_KEYS:
                value = item.get(key)
                if value in (None, "", []) or label in seen:
                    continue
                seen.add(label)
                facts.append(f"{label}: {', '.join(map(str, value)) if isinstance(value, list) else value}")
            text = "\n".join(filter(None, [_plain(_first(item, TEXT_KEYS)), "\n".join(facts)]))
            item_id = _first(item, ID_KEYS) or link
            yield RawOpportunity(
                external_id=f"api:{item_id}"[:300],
                url=link,
                title=_plain(_first(item, TITLE_KEYS) or link)[:500],
                text=text,
                metadata={"api": source.url, "published": str(_first(item, DATE_KEYS) or "")},
                published=_published(_first(item, DATE_KEYS)),
            )
