"""GitHub bounty issues via the official REST search API
(GET /search/issues). An optional GITHUB_TOKEN (read-only, public data)
only raises the rate limit; without one, search allows ~10 requests/minute,
which the request delay below stays well under."""
from datetime import datetime

from django.conf import settings

from .base import BaseCollector, RawOpportunity, SourceBlocked

SEARCH_URL = "https://api.github.com/search/issues"


def _parse_dt(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


class GitHubCollector(BaseCollector):
    kind = "github"
    request_delay_seconds = 7.0     # unauthenticated search: 10 requests/minute
    per_page = 50
    max_pages = 2

    def headers(self):
        headers = {**super().headers(), "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        token = getattr(settings, "GITHUB_TOKEN", "")
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    @staticmethod
    def query_for(source):
        """The Source's url holds the search query; issues only (not PRs)."""
        query = source.url.strip()
        return query if "is:issue" in query or "type:issue" in query else f"{query} is:issue"

    def collect(self, source):
        query = self.query_for(source)
        for page in range(1, self.max_pages + 1):
            resp = self.get(SEARCH_URL, params={"q": query, "sort": "created", "order": "desc",
                                                 "per_page": self.per_page, "page": page})
            if resp.headers.get("X-RateLimit-Remaining") == "0":
                # Out of quota after this page: use it, then stop rather than hit a 403.
                yield from self._items(resp.json())
                raise SourceBlocked("GitHub search rate limit used up - stopping until it resets")
            data = resp.json()
            items = data.get("items") or []
            yield from self._items(data)
            if len(items) < self.per_page:
                return
            self.pause()

    @staticmethod
    def _items(data):
        for item in data.get("items") or []:
            repo_url = item.get("repository_url") or ""
            repo = "/".join(repo_url.rstrip("/").split("/")[-2:]) if repo_url else ""
            yield RawOpportunity(
                external_id=f"github:{item.get('id')}",
                url=item.get("html_url") or "",
                title=(item.get("title") or "")[:500],
                text=item.get("body") or "",
                metadata={
                    "labels": [label.get("name") for label in item.get("labels") or [] if label.get("name")],
                    "repo": repo,
                    "created_at": item.get("created_at"),
                    "updated_at": item.get("updated_at"),
                    "state": item.get("state"),
                },
                published=_parse_dt(item.get("created_at")),
            )
