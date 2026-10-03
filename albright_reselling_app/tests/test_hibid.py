"""HiBid adapter, using response shapes captured from the live API. All HTTP mocked."""
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase, override_settings

from albright_reselling_app.scanner.adapters.base import SourceBlocked, SourceUnavailable
from albright_reselling_app.scanner.adapters.hibid import HiBidAdapter, parse_end_time, parse_premium

CFG = {"hibid": {"home_zip": "33602", "radius_miles": 30, "page_length": 100, "max_pages_per_pass": 2,
                 "default_buyer_premium_pct": 0.20, "auction_timezone": "America/New_York",
                 "include_pickup": True, "include_shipping": True, "sort_order": None}}


def lot(id_, title, high=40.0, bids=5, left=684563.4, ships=True, closed=False, status="OPEN",
        rate=1.13, bp_text="13% B.P. on every purchase", realized=0):
    return {"id": id_, "itemId": 999, "lead": title, "description": "desc", "lotNumber": "12",
            "pictureCount": 6, "shippingOffered": ships, "distanceMiles": None,
            "featuredPicture": {"thumbnailLocation": "https://img/t.jpg", "fullSizeLocation": "https://img/f.jpg"},
            "lotState": {"highBid": high, "bidCount": bids, "isClosed": closed, "priceRealized": realized,
                         "timeLeftSeconds": left, "status": status},
            "auction": {"id": 777, "eventName": "Saturday Showcase", "buyerPremium": bp_text,
                        "buyerPremiumRate": rate, "bidCloseDateTime": "2026-10-10T09:00:00",
                        "eventCity": "Geneva", "eventState": "NY", "eventZip": "14456", "distanceMiles": None,
                        "auctionOptions": {"shippingType": "SHIPPING_OFFERED_ALL"},
                        "auctioneer": {"id": 55, "name": "Hessney Auction Co."}}}


def response(results, total=None, status=200, ctype="application/json; charset=utf-8", errors=None):
    r = MagicMock(status_code=status, headers={"content-type": ctype})
    body = {"data": {"lotSearch": {"pagedResults": {"totalCount": len(results) if total is None else total,
                                                    "results": results}}}}
    if errors:
        body["errors"] = errors
    r.json.return_value = body
    return r


@override_settings(RESELLING_SCANNER={"SOURCES": CFG})
class HiBidTests(SimpleTestCase):
    def adapter(self, **overrides):
        a = HiBidAdapter()
        a.cfg = {**a.cfg, **overrides}
        a.pause = lambda: None
        return a

    def test_premium_parsing(self):
        self.assertEqual(parse_premium(1.13, "13% B.P. on every purchase", 0.2), 0.13)
        self.assertEqual(parse_premium(1, "A 15% fee is added to your total", 0.2), 0.15)
        self.assertEqual(parse_premium(1.18, "Buyers Premium 18% + 3% Credit Card Fee", 0.2), 0.21)
        self.assertEqual(parse_premium(None, "", 0.2), 0.2)

    def test_end_time(self):
        now = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)
        end, src = parse_end_time(3600, "2026-10-11T18:00:00", now, "America/New_York")
        self.assertEqual((end, src), (datetime(2026, 10, 2, 13, 0, tzinfo=timezone.utc), "time_left"))
        end, src = parse_end_time(-194903.4, "2026-10-11T18:00:00", now, "America/New_York")
        self.assertEqual((end, src), (datetime(2026, 10, 11, 22, 0, tzinfo=timezone.utc), "auction_close"))

    def test_parse_shipping_lot(self):
        a = self.adapter(include_pickup=False)
        with patch.object(a.session, "post", return_value=response([lot(323573008, "2009 .585 Gold (14K) Washington")])):
            lots = list(a.search("14k gold"))
        l = lots[0]
        self.assertEqual((l.external_id, l.url, l.current_price, l.bid_count),
                         ("323573008", "https://hibid.com/lot/323573008", 40.0, 5))
        self.assertEqual(l.raw["_costs"]["buyer_premium_pct"], 0.13)
        self.assertNotIn("_pickup", l.raw)
        self.assertEqual(l.raw["_hibid"]["auctioneer"], "Hessney Auction Co.")

    def test_pickup_lot_gets_drive_info(self):
        a = self.adapter(include_shipping=False)
        with patch.object(a.session, "post", return_value=response([lot(1, "14k ring 3g", ships=False)])) as p:
            l = list(a.search("14k gold"))[0]
        self.assertEqual(p.call_args.kwargs["json"]["variables"]["zip"], "33602")
        self.assertEqual(l.raw["_pickup"]["distance_miles"], 30.0)
        self.assertTrue(l.raw["_pickup"]["distance_estimated"])

    def test_closed_and_unknown_status_skipped_in_search(self):
        a = self.adapter(include_pickup=False)
        items = [lot(1, "open"), lot(2, "closed", closed=True), lot(3, "posted", status="POSTED"),
                 lot(4, "weird", status="CLOSED")]
        with patch.object(a.session, "post", return_value=response(items)):
            self.assertEqual([l.external_id for l in a.search("x")], ["1", "3"])

    def test_dedupe_across_passes_and_paging_stops(self):
        a = self.adapter()
        with patch.object(a.session, "post", side_effect=[response([lot(1, "a")], total=1),
                                                         response([lot(1, "a"), lot(2, "b")], total=2)]) as p:
            ids = [l.external_id for l in a.search("x")]
        self.assertEqual((ids, p.call_count), (["1", "2"], 2))

    def test_search_closed_uses_price_realized(self):
        a = self.adapter(include_pickup=False)
        item = lot(5, "14kt ring", high=500.0, closed=True, status="CLOSED", realized=500.0, left=0)
        with patch.object(a.session, "post", return_value=response([item])) as p:
            l = list(a.search_closed("14k gold"))[0]
        self.assertTrue(p.call_args.kwargs["json"]["variables"]["isArchive"])
        self.assertEqual(l.current_price, 500.0)

    def test_sort_settings_sent(self):
        a = self.adapter(include_pickup=False, sort_order="TIME_LEFT")
        with patch.object(a.session, "post", return_value=response([])) as p:
            list(a.search("x"))
        body = p.call_args.kwargs["json"]
        self.assertEqual(body["variables"]["sortOrder"], "TIME_LEFT")
        self.assertEqual(body["variables"]["status"], "OPEN")
        self.assertNotIn("sortDirection", body["query"])  # default: not sent, like the website

    def test_sort_direction_setting(self):
        with override_settings(RESELLING_SCANNER={"SOURCES": {"hibid": {**CFG["hibid"], "sort_direction": "ASC"}}}):
            self.assertIn("sortDirection: ASC", HiBidAdapter().query)
        with override_settings(RESELLING_SCANNER={"SOURCES": {"hibid": {**CFG["hibid"], "sort_direction": None}}}):
            self.assertNotIn("sortDirection", HiBidAdapter().query)
        with override_settings(RESELLING_SCANNER={"SOURCES": {"hibid": {**CFG["hibid"], "sort_direction": "x"}}}):
            self.assertRaises(ValueError, HiBidAdapter)

    def test_opening_bid_used_when_no_bids(self):
        a = self.adapter(include_pickup=False)
        item = lot(9, "14k ring 3g", high=0.0, bids=0)
        item["lotState"].update(minBid=75.0, reserveSatisfied=False, showReserveStatus=True)
        with patch.object(a.session, "post", return_value=response([item])):
            l = list(a.search("x"))[0]
        self.assertEqual(l.current_price, 75.0)
        self.assertTrue(l.raw["_hibid"]["reserve_not_met"])

    def test_blocked_and_graphql_errors(self):
        a = self.adapter(include_pickup=False)
        with patch.object(a.session, "post", return_value=response([], status=403)):
            self.assertRaises(SourceBlocked, lambda: list(a.search("x")))
        with patch.object(a.session, "post", return_value=response([], ctype="text/html")):
            self.assertRaises(SourceBlocked, lambda: list(a.search("x")))
        with patch.object(a.session, "post", return_value=response([], errors=[{"message": "bad enum"}])):
            self.assertRaises(SourceUnavailable, lambda: list(a.search("x")))