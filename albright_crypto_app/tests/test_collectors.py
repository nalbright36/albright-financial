"""Collectors with every HTTP call mocked: GitHub search (fields stored,
token header, is:issue, pagination, skip-already-stored), RSS via
feedparser, and the polite rules - one retry on timeouts/5xx, a hard stop
(no retry) on 403/429."""
from unittest import mock

import requests
from django.test import TestCase, override_settings

from albright_crypto_app.collectors.base import SourceBlocked, SourceUnavailable
from albright_crypto_app.collectors.github import GitHubCollector
from albright_crypto_app.collectors.rss import RssCollector
from albright_crypto_app.models import Opportunity, Source
from albright_crypto_app.pipeline import scan_sources

GET = "albright_crypto_app.collectors.base.requests.get"
NO_SLEEP = mock.patch("albright_crypto_app.collectors.base.time.sleep")


def _resp(status=200, json_data=None, content=b"", headers=None):
    resp = mock.Mock(status_code=status, headers=headers or {}, content=content)
    resp.json.return_value = json_data or {}
    resp.raise_for_status = mock.Mock(side_effect=None if status < 400 else requests.HTTPError(status))
    return resp


def _issue(n, body="Fix the parser. Bounty: $200 USDC paid on merge."):
    return {"id": n, "html_url": f"https://github.com/acme/widget/issues/{n}", "title": f"Bounty issue {n}",
            "body": body, "labels": [{"name": "bounty"}, {"name": "good first issue"}],
            "repository_url": "https://api.github.com/repos/acme/widget", "state": "open",
            "created_at": "2026-10-01T12:00:00Z", "updated_at": "2026-10-02T08:30:00Z"}


RSS = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>Program</title>
<item><title>New quest: 25 USDC</title><link>https://example.org/quest/1</link><guid>q1</guid>
<description>Complete the docs survey to earn 25 USDC.</description><pubDate>Mon, 19 Oct 2026 10:00:00 GMT</pubDate></item>
<item><title>No link item</title><guid>q2</guid></item>
</channel></rss>"""


@NO_SLEEP
class GitHubCollectorTests(TestCase):
    def setUp(self):
        self.source = Source.objects.create(kind="github", name="bounties", url="label:bounty state:open")

    @mock.patch(GET)
    def test_stores_url_title_body_labels_repo_and_dates(self, mock_get, _sleep):
        mock_get.return_value = _resp(json_data={"items": [_issue(1)]})

        raws = list(GitHubCollector().collect(self.source))

        self.assertEqual(len(raws), 1)
        raw = raws[0]
        self.assertEqual(raw.external_id, "github:1")
        self.assertEqual(raw.url, "https://github.com/acme/widget/issues/1")
        self.assertEqual(raw.title, "Bounty issue 1")
        self.assertIn("$200 USDC", raw.text)
        self.assertEqual(raw.metadata["labels"], ["bounty", "good first issue"])
        self.assertEqual(raw.metadata["repo"], "acme/widget")
        self.assertEqual((raw.metadata["created_at"], raw.metadata["updated_at"]),
                         ("2026-10-01T12:00:00Z", "2026-10-02T08:30:00Z"))

    @mock.patch(GET)
    def test_official_search_endpoint_issues_only(self, mock_get, _sleep):
        mock_get.return_value = _resp(json_data={"items": []})

        list(GitHubCollector().collect(self.source))

        url = mock_get.call_args.args[0]
        params = mock_get.call_args.kwargs["params"]
        self.assertEqual(url, "https://api.github.com/search/issues")
        self.assertEqual(params["q"], "label:bounty state:open is:issue")
        self.assertEqual(mock_get.call_args.kwargs["headers"]["Accept"], "application/vnd.github+json")

    @mock.patch(GET)
    def test_token_is_optional_and_sent_when_set(self, mock_get, _sleep):
        mock_get.return_value = _resp(json_data={"items": []})
        with override_settings(GITHUB_TOKEN=""):
            list(GitHubCollector().collect(self.source))
        self.assertNotIn("Authorization", mock_get.call_args.kwargs["headers"])
        with override_settings(GITHUB_TOKEN="ghp_readonly"):
            list(GitHubCollector().collect(self.source))
        self.assertEqual(mock_get.call_args.kwargs["headers"]["Authorization"], "Bearer ghp_readonly")

    @mock.patch(GET)
    def test_403_and_429_stop_without_retrying(self, mock_get, _sleep):
        for status in (403, 429):
            mock_get.reset_mock()
            mock_get.return_value = _resp(status=status)
            with self.assertRaises(SourceBlocked):
                list(GitHubCollector().collect(self.source))
            self.assertEqual(mock_get.call_count, 1)

    @mock.patch(GET)
    def test_one_polite_retry_on_timeout_then_unavailable(self, mock_get, sleep):
        mock_get.side_effect = requests.Timeout("slow")

        with self.assertRaises(SourceUnavailable):
            list(GitHubCollector().collect(self.source))

        self.assertEqual(mock_get.call_count, 2)
        sleep.assert_called()

    @mock.patch(GET)
    def test_retry_recovers_after_a_5xx(self, mock_get, _sleep):
        mock_get.side_effect = [_resp(status=502), _resp(json_data={"items": [_issue(7)]})]

        raws = list(GitHubCollector().collect(self.source))

        self.assertEqual([r.external_id for r in raws], ["github:7"])

    @mock.patch(GET)
    def test_skips_issues_already_stored(self, mock_get, _sleep):
        mock_get.return_value = _resp(json_data={"items": [_issue(1), _issue(2)]})
        first, _ = scan_sources([self.source], use_ai=False)
        second, _ = scan_sources([self.source], use_ai=False)

        self.assertEqual(len(first), 2)
        self.assertEqual(second, [])
        self.assertEqual(Opportunity.objects.count(), 2)

    @mock.patch(GET)
    def test_rate_limit_exhausted_stops_after_using_the_page(self, mock_get, _sleep):
        mock_get.return_value = _resp(json_data={"items": [_issue(3)]}, headers={"X-RateLimit-Remaining": "0"})

        new, errors = scan_sources([self.source], use_ai=False)

        self.assertEqual(len(new), 1)
        self.assertEqual(len(errors), 1)
        self.assertIn("blocked", errors[0])


@NO_SLEEP
class RssCollectorTests(TestCase):
    @mock.patch(GET)
    def test_parses_feed_entries_with_links(self, mock_get, _sleep):
        mock_get.return_value = _resp(content=RSS)
        source = Source.objects.create(kind="rss", name="Program feed", url="https://example.org/feed.xml")

        raws = list(RssCollector().collect(source))

        self.assertEqual(len(raws), 1)   # the entry without a link is skipped
        self.assertEqual(raws[0].url, "https://example.org/quest/1")
        self.assertEqual(raws[0].title, "New quest: 25 USDC")
        self.assertIn("25 USDC", raws[0].text)
        self.assertEqual(raws[0].external_id, "rss:q1")
        self.assertIsNotNone(raws[0].published)
        self.assertEqual(mock_get.call_args.args[0], "https://example.org/feed.xml")

    @mock.patch(GET)
    def test_blocked_feed_doesnt_stop_other_sources(self, mock_get, _sleep):
        good = Source.objects.create(kind="rss", name="good", url="https://good.example/feed")
        bad = Source.objects.create(kind="rss", name="bad", url="https://bad.example/feed")
        mock_get.side_effect = lambda url, **kw: _resp(status=429) if "bad" in url else _resp(content=RSS)

        new, errors = scan_sources([bad, good], use_ai=False)

        self.assertEqual(len(new), 1)
        self.assertEqual(len(errors), 1)
        self.assertIn("bad", errors[0])
