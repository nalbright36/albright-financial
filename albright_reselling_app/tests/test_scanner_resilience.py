"""Tests for ShopGoodwill timeout/connection-error resilience: the adapter's
retry-once behavior, and the pipeline's per-keyword failure handling. Every
HTTP call is mocked - nothing here ever hits shopgoodwill.com, and
time.sleep is mocked so retries/pauses don't actually wait."""
from unittest import mock

import requests
from django.test import SimpleTestCase, TestCase

from albright_reselling_app.scanner import pipeline
from albright_reselling_app.scanner.adapters.base import RawLot, SourceBlocked, SourceUnavailable
from albright_reselling_app.scanner.adapters.shopgoodwill import ShopGoodwillAdapter
from albright_reselling_app.scanner_models import SourcedLot

ADAPTER_SLEEP = "albright_reselling_app.scanner.adapters.shopgoodwill.time.sleep"
BASE_SLEEP = "albright_reselling_app.scanner.adapters.base.time.sleep"  # used by BaseAdapter.pause()


def _resp(status_code=200, json_data=None):
    resp = mock.Mock()
    resp.status_code = status_code
    resp.raise_for_status.return_value = None
    resp.json.return_value = json_data or {}
    return resp


def _empty_page():
    return _resp(200, {"searchResults": {"items": []}})


class ShopGoodwillRetryTests(SimpleTestCase):
    """Adapter-level: no DB needed here."""

    @mock.patch(ADAPTER_SLEEP)
    def test_timeout_then_success_retries_once(self, mock_sleep):
        adapter = ShopGoodwillAdapter()
        item = {"itemId": 1, "title": "Morgan Dollar", "currentPrice": 10.0}
        page1_ok = _resp(200, {"searchResults": {"items": [item]}})
        adapter.session.post = mock.Mock(side_effect=[requests.Timeout("read timed out"), page1_ok, _empty_page()])

        lots = list(adapter.search("morgan dollar"))

        self.assertEqual([lot.external_id for lot in lots], ["1"])
        self.assertEqual(adapter.session.post.call_count, 3)  # failed page1, retried page1, page2 (empty, stops)
        # sleep(15) is the retry delay; sleep(3.0) is the normal pause() between page1 and page2 -
        # both go through time.sleep, which base.py and shopgoodwill.py share via the same import.
        self.assertEqual(mock_sleep.call_args_list, [mock.call(15), mock.call(3.0)])

    @mock.patch(ADAPTER_SLEEP)
    def test_two_timeouts_raises_source_unavailable(self, mock_sleep):
        adapter = ShopGoodwillAdapter()
        adapter.session.post = mock.Mock(
            side_effect=[requests.Timeout("first timeout"), requests.ConnectionError("second failure")]
        )

        with self.assertRaises(SourceUnavailable) as ctx:
            list(adapter.search("morgan dollar"))

        self.assertIn("morgan dollar", str(ctx.exception))
        self.assertEqual(adapter.session.post.call_count, 2)
        mock_sleep.assert_called_once_with(15)  # only one retry - no hammering

    @mock.patch(ADAPTER_SLEEP)
    def test_403_stops_immediately_without_retry(self, mock_sleep):
        adapter = ShopGoodwillAdapter()
        adapter.session.post = mock.Mock(return_value=_resp(403))

        with self.assertRaises(SourceBlocked):
            list(adapter.search("morgan dollar"))

        self.assertEqual(adapter.session.post.call_count, 1)
        mock_sleep.assert_not_called()


def _lot(tag, price=10.0):
    return RawLot(source="shopgoodwill", external_id=tag, url=f"https://shopgoodwill.com/item/{tag}",
                  title="Morgan Silver Dollar", current_price=price)


class RunScanKeywordFailureTests(TestCase):
    """Pipeline-level: exercises run_scan() against a fake adapter.search()
    and a real (but tiny) DB write, so SourcedLot rows are checked directly."""

    def setUp(self):
        patcher = mock.patch("albright_reselling_app.scanner.pipeline.get_all_spot",
                              return_value={"silver": 30.0, "gold": 2500.0})
        patcher.start()
        self.addCleanup(patcher.stop)
        sleep_patcher = mock.patch(BASE_SLEEP)
        sleep_patcher.start()
        self.addCleanup(sleep_patcher.stop)

    def test_skips_failed_keyword_and_keeps_others(self):
        def fake_search(self, keyword):
            if keyword == "peace dollar":
                raise SourceUnavailable(f"ShopGoodwill unavailable for keyword {keyword!r}: boom")
            yield _lot(keyword)

        with mock.patch.object(ShopGoodwillAdapter, "search", fake_search):
            result = pipeline.run_scan("shopgoodwill",
                                        keywords=["morgan dollar", "peace dollar", "silver eagle"])

        self.assertEqual(result["failed_keywords"], ["peace dollar"])
        self.assertEqual(result["seen"], 2)
        self.assertTrue(SourcedLot.objects.filter(external_id="morgan dollar").exists())
        self.assertTrue(SourcedLot.objects.filter(external_id="silver eagle").exists())
        self.assertFalse(SourcedLot.objects.filter(external_id="peace dollar").exists())

    def test_two_consecutive_failures_stop_scan(self):
        def fake_search(self, keyword):
            if keyword in ("peace dollar", "silver eagle"):
                raise SourceUnavailable(f"ShopGoodwill unavailable for keyword {keyword!r}: boom")
            yield _lot(keyword)

        with mock.patch.object(ShopGoodwillAdapter, "search", fake_search):
            result = pipeline.run_scan(
                "shopgoodwill",
                keywords=["morgan dollar", "peace dollar", "silver eagle", "silver bar"],
            )

        self.assertEqual(result["failed_keywords"], ["peace dollar", "silver eagle"])
        self.assertEqual(result["seen"], 1)  # only "morgan dollar" was processed before the stop
        self.assertFalse(SourcedLot.objects.filter(external_id="silver bar").exists())  # never reached
