"""Tests for the pipeline's upsert writing listing features and
first_seen_price - exercised through run_scan() against a fake adapter, the
same pattern test_scanner_resilience.py uses, so SourcedLot rows are
checked directly. No real HTTP happens here."""
from unittest import mock

from django.test import TestCase

from albright_reselling_app.scanner import pipeline
from albright_reselling_app.scanner.adapters.base import RawLot
from albright_reselling_app.scanner.adapters.shopgoodwill import ShopGoodwillAdapter
from albright_reselling_app.scanner_models import SourcedLot

BASE_SLEEP = "albright_reselling_app.scanner.adapters.base.time.sleep"

RAW_ITEM = {
    "itemId": "feat-1", "title": "14K Gold Ring 5.2 Grams", "currentPrice": 10.0,
    "catFullName": "Jewelry", "views": 50, "sellerId": 42,
}


def _lot(external_id, price, raw=None):
    return RawLot(source="shopgoodwill", external_id=external_id,
                  url=f"https://shopgoodwill.com/item/{external_id}",
                  title="14K Gold Ring 5.2 Grams", current_price=price, raw=raw or dict(RAW_ITEM))


class PipelineFeaturesTests(TestCase):
    def setUp(self):
        patcher = mock.patch("albright_reselling_app.scanner.pipeline.get_all_spot",
                              return_value={"silver": 30.0, "gold": 2500.0})
        patcher.start()
        self.addCleanup(patcher.stop)
        sleep_patcher = mock.patch(BASE_SLEEP)
        sleep_patcher.start()
        self.addCleanup(sleep_patcher.stop)

    def test_features_saved_on_first_scan(self):
        def fake_search(self, keyword):
            yield _lot("feat-1", 10.0)

        with mock.patch.object(ShopGoodwillAdapter, "search", fake_search):
            pipeline.run_scan("shopgoodwill", keywords=["gold ring"])

        lot = SourcedLot.objects.get(external_id="feat-1")
        self.assertTrue(lot.features["title_has_weight"])
        self.assertTrue(lot.features["title_has_karat_or_purity"])
        self.assertEqual(lot.features["views"], 50)
        self.assertEqual(lot.features["seller_id"], 42)
        self.assertEqual(lot.features["seller_category"], "Jewelry")
        self.assertEqual(lot.features["photo_count"], 0)  # no imageURL in RAW_ITEM

    def test_features_refreshed_on_every_scan(self):
        def fake_search_v1(self, keyword):
            yield _lot("feat-2", 10.0, raw={"itemId": "feat-2", "title": "Gold Ring"})

        def fake_search_v2(self, keyword):
            yield _lot("feat-2", 12.0, raw={"itemId": "feat-2", "title": "Gold Ring", "views": 99})

        with mock.patch.object(ShopGoodwillAdapter, "search", fake_search_v1):
            pipeline.run_scan("shopgoodwill", keywords=["gold ring"])
        with mock.patch.object(ShopGoodwillAdapter, "search", fake_search_v2):
            pipeline.run_scan("shopgoodwill", keywords=["gold ring"])

        lot = SourcedLot.objects.get(external_id="feat-2")
        self.assertEqual(lot.features["views"], 99)

    def test_first_seen_price_set_once_and_not_overwritten(self):
        def fake_search_v1(self, keyword):
            yield _lot("feat-3", 10.0)

        def fake_search_v2(self, keyword):
            yield _lot("feat-3", 25.0)

        with mock.patch.object(ShopGoodwillAdapter, "search", fake_search_v1):
            pipeline.run_scan("shopgoodwill", keywords=["gold ring"])

        lot = SourcedLot.objects.get(external_id="feat-3")
        self.assertEqual(lot.first_seen_price, lot.current_price)
        self.assertEqual(float(lot.first_seen_price), 10.0)

        with mock.patch.object(ShopGoodwillAdapter, "search", fake_search_v2):
            pipeline.run_scan("shopgoodwill", keywords=["gold ring"])

        lot.refresh_from_db()
        self.assertEqual(float(lot.first_seen_price), 10.0)  # unchanged
        self.assertEqual(float(lot.current_price), 25.0)  # current price still updates
