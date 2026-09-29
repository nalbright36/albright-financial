"""Tests for the ShopGoodwill search_closed() adapter method and the
track_closed management command. All HTTP is mocked - nothing here ever
hits shopgoodwill.com."""
from datetime import timedelta
from decimal import Decimal
from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from albright_reselling_app.scanner.adapters.base import RawLot
from albright_reselling_app.scanner.adapters.maxsold import MaxSoldAdapter
from albright_reselling_app.scanner.adapters.shopgoodwill import ShopGoodwillAdapter
from albright_reselling_app.scanner_models import LotEvaluation, SourcedLot


def _resp(status_code=200, json_data=None):
    resp = mock.Mock()
    resp.status_code = status_code
    resp.raise_for_status.return_value = None
    resp.json.return_value = json_data or {}
    return resp


class SearchClosedTests(SimpleTestCase):
    def test_sends_closed_auction_params(self):
        adapter = ShopGoodwillAdapter()
        adapter.session.post = mock.Mock(return_value=_resp(200, {"items": [], "total": 0}))

        list(adapter.search_closed("morgan dollar", days_back=5))

        payload = adapter.session.post.call_args.kwargs["json"]
        self.assertEqual(payload["searchClosedAuctions"], "true")
        self.assertEqual(payload["closedAuctionDaysBack"], "5")
        self.assertEqual(payload["searchText"], "morgan dollar")

    def test_default_days_back_is_two(self):
        adapter = ShopGoodwillAdapter()
        adapter.session.post = mock.Mock(return_value=_resp(200, {"items": [], "total": 0}))

        list(adapter.search_closed("morgan dollar"))

        payload = adapter.session.post.call_args.kwargs["json"]
        self.assertEqual(payload["closedAuctionDaysBack"], "2")

    def test_open_search_unaffected(self):
        """search() should still send the original (non-closed) payload."""
        adapter = ShopGoodwillAdapter()
        adapter.session.post = mock.Mock(return_value=_resp(200, {"items": [], "total": 0}))

        list(adapter.search("morgan dollar"))

        payload = adapter.session.post.call_args.kwargs["json"]
        self.assertEqual(payload["searchClosedAuctions"], "false")


def _make_lot(external_id, title="Morgan Silver Dollar", max_bid="20.00", melt_value="18.00", category="coins",
              source="shopgoodwill"):
    lot = SourcedLot.objects.create(
        source=source, external_id=external_id, url=f"https://example.com/{external_id}",
        title=title, current_price=Decimal("10.00"), end_time=timezone.now() - timedelta(hours=2),
    )
    LotEvaluation.objects.create(
        lot=lot, category=category, max_bid=Decimal(max_bid), melt_value=Decimal(melt_value),
    )
    return lot


class TrackClosedCommandTests(TestCase):
    @mock.patch("albright_reselling_app.scanner.adapters.base.time.sleep")
    def test_updates_existing_lot_and_marks_closed(self, mock_sleep):
        lot = _make_lot("lot-1")

        def fake_search_closed(self, keyword, days_back=2):
            yield RawLot(source="shopgoodwill", external_id="lot-1", url=lot.url, title=lot.title,
                         current_price=15.0)

        out = StringIO()
        with mock.patch.object(ShopGoodwillAdapter, "search_closed", fake_search_closed):
            call_command("track_closed", "--category", "coins", stdout=out)

        lot.refresh_from_db()
        self.assertEqual(lot.final_price, Decimal("15.00"))
        self.assertTrue(lot.is_closed)
        self.assertIsNotNone(lot.final_checked_at)
        self.assertIn("Lots updated: 1", out.getvalue())

    @mock.patch("albright_reselling_app.scanner.adapters.base.time.sleep")
    def test_ignores_items_not_already_in_database(self, mock_sleep):
        def fake_search_closed(self, keyword, days_back=2):
            yield RawLot(source="shopgoodwill", external_id="ghost-1", url="https://example.com/ghost-1",
                         title="Never Scanned Lot", current_price=15.0)

        out = StringIO()
        with mock.patch.object(ShopGoodwillAdapter, "search_closed", fake_search_closed):
            call_command("track_closed", "--category", "coins", stdout=out)

        self.assertFalse(SourcedLot.objects.filter(external_id="ghost-1").exists())
        self.assertIn("Lots updated: 0", out.getvalue())

    @mock.patch("albright_reselling_app.scanner.adapters.base.time.sleep")
    def test_skips_already_closed_lot(self, mock_sleep):
        lot = _make_lot("lot-already-closed")
        lot.is_closed = True
        lot.final_price = Decimal("12.00")
        lot.save()

        def fake_search_closed(self, keyword, days_back=2):
            yield RawLot(source="shopgoodwill", external_id="lot-already-closed", url=lot.url, title=lot.title,
                         current_price=99.0)

        out = StringIO()
        with mock.patch.object(ShopGoodwillAdapter, "search_closed", fake_search_closed):
            call_command("track_closed", "--category", "coins", stdout=out)

        lot.refresh_from_db()
        self.assertEqual(lot.final_price, Decimal("12.00"))  # unchanged
        self.assertIn("Lots updated: 0", out.getvalue())

    @mock.patch("albright_reselling_app.scanner.adapters.base.time.sleep")
    def test_summary_counts_under_and_over_max(self, mock_sleep):
        lot_under = _make_lot("under-1", title="Morgan Silver Dollar A")
        lot_over = _make_lot("over-1", title="Morgan Silver Dollar B")

        def fake_search_closed(self, keyword, days_back=2):
            yield RawLot(source="shopgoodwill", external_id="under-1", url=lot_under.url,
                         title=lot_under.title, current_price=15.0)  # under the $20 max bid
            yield RawLot(source="shopgoodwill", external_id="over-1", url=lot_over.url,
                         title=lot_over.title, current_price=25.0)  # over the $20 max bid

        out = StringIO()
        with mock.patch.object(ShopGoodwillAdapter, "search_closed", fake_search_closed):
            call_command("track_closed", "--category", "coins", stdout=out)

        output = out.getvalue()
        self.assertIn("Lots updated: 2", output)
        self.assertIn("coins: 1 at/under max, 1 over max", output)


class TrackClosedMaxSoldSourceTests(TestCase):
    @mock.patch("albright_reselling_app.scanner.adapters.base.time.sleep")
    def test_updates_existing_maxsold_lot(self, mock_sleep):
        lot = _make_lot("ms-lot-1", source="maxsold")

        def fake_search_closed(self, keyword):
            yield RawLot(source="maxsold", external_id="ms-lot-1", url=lot.url, title=lot.title,
                         current_price=15.0)

        out = StringIO()
        with mock.patch.object(MaxSoldAdapter, "search_closed", fake_search_closed):
            call_command("track_closed", "--source", "maxsold", "--category", "coins", stdout=out)

        lot.refresh_from_db()
        self.assertEqual(lot.final_price, Decimal("15.00"))
        self.assertTrue(lot.is_closed)
        self.assertIn("Lots updated: 1", out.getvalue())

    @mock.patch("albright_reselling_app.scanner.adapters.base.time.sleep")
    def test_ignores_unknown_maxsold_lot(self, mock_sleep):
        def fake_search_closed(self, keyword):
            yield RawLot(source="maxsold", external_id="ms-ghost", url="https://maxsold.com/listing/ms-ghost",
                         title="Never Scanned Lot", current_price=15.0)

        out = StringIO()
        with mock.patch.object(MaxSoldAdapter, "search_closed", fake_search_closed):
            call_command("track_closed", "--source", "maxsold", "--category", "coins", stdout=out)

        self.assertFalse(SourcedLot.objects.filter(external_id="ms-ghost").exists())
        self.assertIn("Lots updated: 0", out.getvalue())
