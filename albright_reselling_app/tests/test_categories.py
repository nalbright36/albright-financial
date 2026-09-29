"""Tests for the valuers.classify() wiring into pipeline.evaluate()/run_scan():
jewelry valuation via CATEGORY_FEES, jewelry/games leads, category "none",
and multi-category keyword scanning. All HTTP is mocked - nothing here ever
hits ShopGoodwill or MaxSold."""
from decimal import Decimal
from unittest import mock

from django.conf import settings
from django.test import TestCase

from albright_reselling_app.scanner import pipeline
from albright_reselling_app.scanner.adapters.base import RawLot
from albright_reselling_app.scanner.adapters.shopgoodwill import ShopGoodwillAdapter
from albright_reselling_app.scanner.coins import estimate_resale
from albright_reselling_app.scanner.jewelry import parse_jewelry_text
from albright_reselling_app.scanner.max_bid import BuyCosts, SellFees, max_bid as compute_max_bid
from albright_reselling_app.scanner_models import ScanRun, SourcedLot

SPOT = {"silver": 30.0, "gold": 2500.0}


def _make_lot(title, current_price=10.0, source="shopgoodwill", external_id=None):
    return SourcedLot.objects.create(
        source=source, external_id=external_id or title[:80], url="https://example.com/lot",
        title=title, current_price=Decimal(str(current_price)),
    )


class EvaluateCategoryTests(TestCase):
    def test_jewelry_with_weight_gets_max_bid_using_category_fees(self):
        lot = _make_lot("14k Gold Chain Necklace 10g", current_price=50.0)

        evaluation, _ = pipeline.evaluate(lot, SPOT, llm_budget=0, use_llm=False)

        self.assertEqual(evaluation.category, "jewelry")
        self.assertGreater(evaluation.max_bid, 0)
        self.assertFalse(evaluation.is_lead)  # weight was stated - no review needed

        cfg = settings.RESELLING_SCANNER
        src = cfg["SOURCES"]["shopgoodwill"]
        parse = parse_jewelry_text(lot.title)
        _, expected_sale = estimate_resale(parse, SPOT, cfg["RESALE_MULTIPLIERS"])
        buy = BuyCosts(src["buyer_premium_pct"], src["sales_tax_pct"], src["default_inbound_shipping"])

        jewelry_fees = {**cfg["FEES"], **cfg["CATEGORY_FEES"]["jewelry"]}
        expected_with_category_fees = compute_max_bid(expected_sale, SellFees(**jewelry_fees), buy)
        self.assertAlmostEqual(float(evaluation.max_bid), expected_with_category_fees, places=2)

        # Prove the CATEGORY_FEES override actually matters: plain coin-style
        # fees (real eBay fee/shipping/packaging) would allow a lower bid.
        plain_fee_max_bid = compute_max_bid(expected_sale, SellFees(**cfg["FEES"]), buy)
        self.assertGreater(expected_with_category_fees, plain_fee_max_bid)

    def test_jewelry_with_no_weight_is_lead_with_no_max_bid(self):
        lot = _make_lot("Vintage 14k Gold Diamond Ring Size 6", current_price=20.0)

        evaluation, _ = pipeline.evaluate(lot, SPOT, llm_budget=0, use_llm=False)

        self.assertEqual(evaluation.category, "jewelry")
        self.assertEqual(evaluation.max_bid, 0)
        self.assertFalse(evaluation.is_candidate)
        self.assertTrue(evaluation.is_lead)
        self.assertIn("no weight", evaluation.lead_reason)

    def test_game_lot_under_limit_is_lead_with_zero_max_bid(self):
        lot = _make_lot("Lot of 8 Nintendo 64 N64 Games Mario Kart Zelda", current_price=25.0)

        evaluation, _ = pipeline.evaluate(lot, SPOT, llm_budget=0, use_llm=False)

        self.assertEqual(evaluation.category, "games")
        self.assertEqual(evaluation.max_bid, 0)
        self.assertEqual(evaluation.melt_value, 0)
        self.assertEqual(evaluation.headroom, 0)
        self.assertFalse(evaluation.is_candidate)
        self.assertTrue(evaluation.is_lead)
        self.assertIn("bulk lot", evaluation.flags)

    def test_non_matching_lot_gets_category_none_with_reason(self):
        lot = _make_lot("Travando Men's Black Bifold Wallet", current_price=2.0)

        evaluation, _ = pipeline.evaluate(lot, SPOT, llm_budget=0, use_llm=False)

        self.assertEqual(evaluation.category, "none")
        self.assertTrue(evaluation.excluded_reason)
        self.assertFalse(evaluation.is_lead)
        self.assertFalse(evaluation.is_candidate)
        self.assertEqual(evaluation.max_bid, 0)
        self.assertEqual(evaluation.coin_keys, "")


def _recording_search(searched_keywords):
    """Replaces adapter.search(keyword): records the keyword, yields nothing."""
    def fake_search(self, keyword):
        searched_keywords.append(keyword)
        return
        yield  # pragma: no cover - makes this a generator function
    return fake_search


class RunScanCategoryTests(TestCase):
    @mock.patch("albright_reselling_app.scanner.adapters.base.time.sleep")
    @mock.patch("albright_reselling_app.scanner.pipeline.get_all_spot", return_value=SPOT)
    def test_scans_every_category_by_default(self, mock_spot, mock_sleep):
        searched = []
        with mock.patch.object(ShopGoodwillAdapter, "search", _recording_search(searched)):
            pipeline.run_scan("shopgoodwill")

        cfg = settings.RESELLING_SCANNER
        for category in ("coins", "jewelry", "games", "cards"):
            for keyword in cfg["KEYWORDS"][category]:
                self.assertIn(keyword, searched)

    @mock.patch("albright_reselling_app.scanner.adapters.base.time.sleep")
    @mock.patch("albright_reselling_app.scanner.pipeline.get_all_spot", return_value=SPOT)
    def test_category_option_limits_to_one_category(self, mock_spot, mock_sleep):
        searched = []
        with mock.patch.object(ShopGoodwillAdapter, "search", _recording_search(searched)):
            pipeline.run_scan("shopgoodwill", category="jewelry")

        cfg = settings.RESELLING_SCANNER
        self.assertEqual(sorted(searched), sorted(cfg["KEYWORDS"]["jewelry"]))


class ScanRunLeadsTests(TestCase):
    @mock.patch("albright_reselling_app.management.commands.scan_lots.run_scan")
    def test_scan_run_records_leads(self, mock_run_scan):
        from io import StringIO

        from django.core.management import call_command

        mock_run_scan.return_value = {
            "seen": 5, "candidates": [], "leads": [mock.Mock(), mock.Mock(), mock.Mock()],
            "spot": {"silver": 30.0, "gold": 2500.0}, "failed_keywords": [],
            "keywords_scanned": ["14k gold"], "llm_calls": 0,
        }

        call_command("scan_lots", "--no-llm", stdout=StringIO())

        run = ScanRun.objects.get()
        self.assertEqual(run.leads, 3)


class RunScanDeduplicationTests(TestCase):
    """The same lot can legitimately match more than one search keyword
    (e.g. "silver eagle" and "1 oz silver") - it should still only be
    counted/listed once."""

    @mock.patch("albright_reselling_app.scanner.adapters.base.time.sleep")
    @mock.patch("albright_reselling_app.scanner.pipeline.get_all_spot", return_value=SPOT)
    def test_candidate_matched_by_two_keywords_counted_once(self, mock_spot, mock_sleep):
        def fake_search(self, keyword):
            # Same external_id every time - both keywords "find" the same lot.
            yield RawLot(source="shopgoodwill", external_id="dup-candidate", url="https://example.com/dup",
                         title="Silver Eagle Coin", current_price=1.0)

        with mock.patch.object(ShopGoodwillAdapter, "search", fake_search):
            result = pipeline.run_scan("shopgoodwill", keywords=["silver eagle", "1 oz silver"])

        self.assertEqual(result["seen"], 2)  # both keyword hits were still evaluated
        self.assertEqual(len(result["candidates"]), 1)  # but only listed once
        self.assertEqual(SourcedLot.objects.filter(external_id="dup-candidate").count(), 1)

    @mock.patch("albright_reselling_app.scanner.adapters.base.time.sleep")
    @mock.patch("albright_reselling_app.scanner.pipeline.get_all_spot", return_value=SPOT)
    def test_lead_matched_by_two_keywords_counted_once(self, mock_spot, mock_sleep):
        def fake_search(self, keyword):
            yield RawLot(source="shopgoodwill", external_id="dup-lead", url="https://example.com/dup-lead",
                         title="Lot of 8 Nintendo 64 N64 Games Mario Kart Zelda", current_price=25.0)

        with mock.patch.object(ShopGoodwillAdapter, "search", fake_search):
            result = pipeline.run_scan("shopgoodwill", keywords=["nintendo 64", "n64 games"])

        self.assertEqual(len(result["leads"]), 1)
