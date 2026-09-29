"""Tests for the MaxSold adapter and its wiring into the pipeline
(registration, per-source keywords, per-lot pickup cost). All HTTP is
mocked - nothing here ever hits maxsold.com."""
from unittest import mock

import requests
from django.test import SimpleTestCase, override_settings

from albright_reselling_app.scanner.adapters.base import SourceBlocked, SourceUnavailable
from albright_reselling_app.scanner.adapters.maxsold import METERS_PER_MILE, MaxSoldAdapter
from albright_reselling_app.scanner.pipeline import ADAPTERS, _inbound_shipping, _keywords_for

SAMPLE_ITEM = {
    "amLotId": 12345,
    "amAuctionId": 777,
    "amBidCount": 3,
    "auctionTitle": "Men&#39;s Estate Auction",
    "closeTime": "2026-10-02T00:14:00Z",
    "currentBid": {"code": "usd", "amount": 2},
    "description": "A nice lot of Men&#39;s items",
    "distanceMeters": 13277,
    "generatedDetails": {"slug": "mens-estate-auction-lot-1"},
    "hasShipping": False,
    "imageUrls": ["https://example.com/img1.jpg"],
    "title": "Men&#39;s Coin Collection",
    "address": {"city": "Riverview"},
}

MAXSOLD_SOURCE_CFG = {
    "home_lat": 27.9517, "home_lng": -82.4588, "radius_miles": 30,
    "buyer_premium_pct": 0.15, "sales_tax_pct": 0.07, "default_inbound_shipping": 0.0,
    "mileage_rate": 0.70, "pickup_fixed_cost": 5.0,
    "keywords": {"coins": ["silver coins", "gold coins"]},
}


def _resp(status_code=200, json_data=None):
    resp = mock.Mock()
    resp.status_code = status_code
    resp.raise_for_status.return_value = None
    resp.json.return_value = json_data or {}
    return resp


@override_settings(RESELLING_SCANNER={
    "SOURCES": {"maxsold": MAXSOLD_SOURCE_CFG},
    "KEYWORDS": {"coins": ["fallback keyword"]},
})
class MaxSoldAdapterTests(SimpleTestCase):
    def test_parses_sample_listing(self):
        adapter = MaxSoldAdapter()
        adapter.session.get = mock.Mock(return_value=_resp(200, {"listings": [SAMPLE_ITEM], "total": 1}))

        lots = list(adapter.search("coin collection"))

        self.assertEqual(len(lots), 1)
        lot = lots[0]
        self.assertEqual(lot.title, "Men's Coin Collection")  # HTML entities decoded
        self.assertEqual(lot.description, "A nice lot of Men's items")
        self.assertEqual(lot.current_price, 2.0)  # currentBid.amount
        self.assertIsNotNone(lot.end_time)
        self.assertEqual(lot.end_time.utcoffset().total_seconds(), 0)  # timezone-aware UTC
        self.assertEqual(lot.end_time.year, 2026)
        self.assertEqual(lot.raw["_pickup"]["distance_miles"], round(13277 / METERS_PER_MILE, 1))
        self.assertEqual(lot.raw["_pickup"]["auction_id"], 777)
        self.assertEqual(lot.raw["_pickup"]["auction_title"], "Men's Estate Auction")
        self.assertEqual(lot.raw["_pickup"]["city"], "Riverview")
        self.assertFalse(lot.raw["_pickup"]["has_shipping"])

    def test_radius_converted_to_meters_in_request_params(self):
        adapter = MaxSoldAdapter()
        adapter.session.get = mock.Mock(return_value=_resp(200, {"listings": [], "total": 0}))

        list(adapter.search("coin collection"))

        params = adapter.session.get.call_args.kwargs["params"]
        self.assertEqual(params["radiusMetres"], int(30 * METERS_PER_MILE))

    def test_pagination_stops_when_total_reached(self):
        adapter = MaxSoldAdapter()
        adapter.session.get = mock.Mock(return_value=_resp(200, {"listings": [SAMPLE_ITEM], "total": 1}))

        list(adapter.search("coin collection"))

        self.assertEqual(adapter.session.get.call_count, 1)  # total (1) reached after page 1

    def test_pagination_stops_on_empty_page(self):
        adapter = MaxSoldAdapter()
        adapter.session.get = mock.Mock(return_value=_resp(200, {"listings": [], "total": 50}))

        list(adapter.search("coin collection"))

        self.assertEqual(adapter.session.get.call_count, 1)  # empty page, even though total says more

    @mock.patch("albright_reselling_app.scanner.adapters.maxsold.time.sleep")
    def test_403_raises_source_blocked_without_retry(self, mock_sleep):
        adapter = MaxSoldAdapter()
        adapter.session.get = mock.Mock(return_value=_resp(403))

        with self.assertRaises(SourceBlocked):
            list(adapter.search("coin collection"))

        self.assertEqual(adapter.session.get.call_count, 1)
        mock_sleep.assert_not_called()

    @mock.patch("albright_reselling_app.scanner.adapters.maxsold.time.sleep")
    def test_two_timeouts_raise_source_unavailable(self, mock_sleep):
        adapter = MaxSoldAdapter()
        adapter.session.get = mock.Mock(
            side_effect=[requests.Timeout("first timeout"), requests.ConnectionError("second failure")]
        )

        with self.assertRaises(SourceUnavailable):
            list(adapter.search("coin collection"))

        self.assertEqual(adapter.session.get.call_count, 2)
        mock_sleep.assert_called_once_with(15)  # one polite retry, no hammering


@override_settings(RESELLING_SCANNER={
    "SOURCES": {"maxsold": MAXSOLD_SOURCE_CFG},
    "KEYWORDS": {"coins": ["fallback keyword"]},
})
class MaxSoldSearchClosedTests(SimpleTestCase):
    def test_sends_closed_lot_state(self):
        adapter = MaxSoldAdapter()
        adapter.session.get = mock.Mock(return_value=_resp(200, {"listings": [], "total": 0}))

        list(adapter.search_closed("coin collection"))

        params = adapter.session.get.call_args.kwargs["params"]
        self.assertEqual(params["lotState"], "closed")

    def test_open_search_unaffected(self):
        """search() should still request open lots."""
        adapter = MaxSoldAdapter()
        adapter.session.get = mock.Mock(return_value=_resp(200, {"listings": [], "total": 0}))

        list(adapter.search("coin collection"))

        params = adapter.session.get.call_args.kwargs["params"]
        self.assertEqual(params["lotState"], "open")

    def test_closed_lot_final_price_from_current_bid(self):
        adapter = MaxSoldAdapter()
        adapter.session.get = mock.Mock(return_value=_resp(200, {"listings": [SAMPLE_ITEM], "total": 1}))

        lots = list(adapter.search_closed("coin collection"))

        self.assertEqual(lots[0].current_price, 2.0)  # currentBid.amount


class AdapterRegistrationTests(SimpleTestCase):
    def test_maxsold_registered(self):
        self.assertIs(ADAPTERS["maxsold"], MaxSoldAdapter)


class KeywordResolutionTests(SimpleTestCase):
    def test_maxsold_uses_source_specific_keywords(self):
        cfg = {
            "SOURCES": {"maxsold": {"keywords": {"coins": ["silver coins", "gold coins"]}}},
            "KEYWORDS": {"coins": ["global fallback"]},
        }
        self.assertEqual(_keywords_for(cfg, "maxsold", "coins"), ["silver coins", "gold coins"])

    def test_shopgoodwill_uses_global_keywords(self):
        cfg = {
            "SOURCES": {"shopgoodwill": {}},  # no "keywords" key in its SOURCES entry
            "KEYWORDS": {"coins": ["global fallback"]},
        }
        self.assertEqual(_keywords_for(cfg, "shopgoodwill", "coins"), ["global fallback"])


class InboundShippingHelperTests(SimpleTestCase):
    def test_pickup_cost_from_distance_and_mileage_rate(self):
        src_cfg = {"default_inbound_shipping": 0.0, "mileage_rate": 0.70, "pickup_fixed_cost": 5.0}
        raw = {"_pickup": {"distance_miles": 8.2}}

        self.assertAlmostEqual(_inbound_shipping(raw, src_cfg), 16.48)

    def test_falls_back_to_default_when_no_pickup_info(self):
        src_cfg = {"default_inbound_shipping": 10.0, "mileage_rate": 0.70, "pickup_fixed_cost": 5.0}

        self.assertEqual(_inbound_shipping({}, src_cfg), 10.0)
        self.assertEqual(_inbound_shipping(None, src_cfg), 10.0)
        self.assertEqual(_inbound_shipping({"_pickup": {}}, src_cfg), 10.0)
        self.assertEqual(_inbound_shipping({"_pickup": {"distance_miles": None}}, src_cfg), 10.0)

    def test_falls_back_to_default_when_source_has_no_mileage_rate(self):
        src_cfg = {"default_inbound_shipping": 10.0}  # e.g. ShopGoodwill's config
        raw = {"_pickup": {"distance_miles": 8.2}}  # hypothetically present, still shouldn't be used

        self.assertEqual(_inbound_shipping(raw, src_cfg), 10.0)
