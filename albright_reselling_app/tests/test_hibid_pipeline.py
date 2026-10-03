"""HiBid integration with the rest of the scanner: per-lot buyer's premium
(raw["_costs"]) overrides the source's flat default in both the pipeline's
own max bid and the ledger's "I won this" cost prefill; shipping vs. pickup
cost selection; HiBid's own (short) keyword lists are used for hibid scans;
track_closed --source hibid updates an existing lot. All HTTP mocked."""
from datetime import timedelta
from decimal import Decimal
from io import StringIO
from unittest import mock

from django.conf import settings
from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from albright_reselling_app.scanner import pipeline
from albright_reselling_app.scanner.adapters.base import RawLot
from albright_reselling_app.scanner.adapters.hibid import HiBidAdapter
from albright_reselling_app.scanner.alerts import run_alerts
from albright_reselling_app.scanner.pipeline import _buyer_premium_pct, _inbound_shipping
from albright_reselling_app.scanner_models import AlertSent, LotEvaluation, SourcedLot

SPOT = {"silver": 30.0, "gold": 2500.0}


def _hibid_raw(premium_pct=0.21, ships=True, miles=None, distance_estimated=False, end_time_source="time_left",
                reserve_not_met=False):
    raw = {
        "_costs": {"buyer_premium_pct": premium_pct, "premium_text": f"{premium_pct * 100:.0f}%"},
        "_hibid": {
            "pass": "shipping" if ships else "pickup", "end_time_source": end_time_source, "ships": ships,
            "shipping_type": "SHIPPING_OFFERED_ALL", "auction_id": 777, "auction_title": "Saturday Showcase",
            "auctioneer_id": 55, "auctioneer": "Hessney Auction Co.", "city": "Geneva", "state": "NY",
            "zip": "14456", "lot_number": "12", "picture_count": 6, "status": "OPEN",
            "reserve_not_met": reserve_not_met,
        },
    }
    if not ships:
        raw["_pickup"] = {
            "distance_miles": miles if miles is not None else 30.0, "distance_estimated": distance_estimated,
            "auction_id": 777, "auction_title": "Saturday Showcase", "city": "Geneva, NY", "has_shipping": False,
        }
    return raw


def _make_hibid_lot(external_id, title="10 oz .999 Fine Silver Bar", current_price=40.0, **raw_kwargs):
    return SourcedLot.objects.create(
        source="hibid", external_id=external_id, url=f"https://hibid.com/lot/{external_id}",
        title=title, current_price=Decimal(str(current_price)), end_time=timezone.now() + timedelta(hours=5),
        raw=_hibid_raw(**raw_kwargs),
    )


class BuyerPremiumHelperTests(TestCase):
    def test_per_lot_premium_from_costs_wins(self):
        src = settings.RESELLING_SCANNER["SOURCES"]["hibid"]
        self.assertEqual(_buyer_premium_pct({"_costs": {"buyer_premium_pct": 0.21}}, src), 0.21)

    def test_falls_back_to_source_default_without_per_lot_costs(self):
        src = settings.RESELLING_SCANNER["SOURCES"]["shopgoodwill"]
        self.assertEqual(_buyer_premium_pct({}, src), src["buyer_premium_pct"])
        self.assertEqual(_buyer_premium_pct(None, src), src["buyer_premium_pct"])

    def test_hibid_source_has_no_flat_buyer_premium_pct_key(self):
        """HiBid's own default lives under default_buyer_premium_pct and is
        always baked into raw["_costs"] by the adapter - the flat
        "buyer_premium_pct" fallback path only matters for ShopGoodwill/
        MaxSold, confirmed here so a future settings refactor can't quietly
        break the fallback without a test catching it."""
        src = settings.RESELLING_SCANNER["SOURCES"]["hibid"]
        self.assertNotIn("buyer_premium_pct", src)
        self.assertEqual(_buyer_premium_pct({}, src), 0.0)  # the bare .get() fallback, never actually hit


class InboundShippingHelperTests(TestCase):
    def test_shipping_lot_uses_source_default(self):
        src = settings.RESELLING_SCANNER["SOURCES"]["hibid"]
        raw = _hibid_raw(ships=True)
        self.assertEqual(_inbound_shipping(raw, src), src["default_inbound_shipping"])

    def test_pickup_lot_uses_mileage_cost(self):
        src = settings.RESELLING_SCANNER["SOURCES"]["hibid"]
        raw = _hibid_raw(ships=False, miles=20.0)
        expected = (2 * 20.0 * src["mileage_rate"]) + src["pickup_fixed_cost"]
        self.assertEqual(_inbound_shipping(raw, src), expected)


class PerLotPremiumMaxBidTests(TestCase):
    def test_higher_per_lot_premium_lowers_max_bid(self):
        low_premium_lot = _make_hibid_lot("premium-low", current_price=40.0, premium_pct=0.10)
        high_premium_lot = _make_hibid_lot("premium-high", current_price=40.0, premium_pct=0.30)

        low_ev, _ = pipeline.evaluate(low_premium_lot, SPOT, llm_budget=0, use_llm=False)
        high_ev, _ = pipeline.evaluate(high_premium_lot, SPOT, llm_budget=0, use_llm=False)

        self.assertEqual(low_ev.category, "coins")
        self.assertGreater(low_ev.max_bid, 0)
        self.assertGreater(low_ev.max_bid, high_ev.max_bid)

    def test_no_costs_on_raw_falls_back_to_zero_not_hibid_default(self):
        """A hibid lot with no "_costs" at all (shouldn't happen via the
        real adapter, which always sets it) hits BuyCosts' flat-rate
        fallback - which has no "buyer_premium_pct" key for hibid, so it's
        0.0, not "default_buyer_premium_pct". That fallback only really
        matters for ShopGoodwill/MaxSold; for hibid it's a defensive
        floor, not a meaningful default (see BuyerPremiumHelperTests)."""
        no_costs_lot = SourcedLot.objects.create(
            source="hibid", external_id="premium-none", url="https://hibid.com/lot/premium-none",
            title="10 oz .999 Fine Silver Bar", current_price=Decimal("40.0"),
            end_time=timezone.now() + timedelta(hours=5), raw={},
        )
        zero_premium_lot = _make_hibid_lot("premium-zero", current_price=40.0, premium_pct=0.0)

        no_costs_ev, _ = pipeline.evaluate(no_costs_lot, SPOT, llm_budget=0, use_llm=False)
        zero_premium_ev, _ = pipeline.evaluate(zero_premium_lot, SPOT, llm_budget=0, use_llm=False)

        self.assertEqual(no_costs_ev.max_bid, zero_premium_ev.max_bid)


class ReserveNotMetTests(TestCase):
    """raw["_hibid"]["reserve_not_met"] forces is_candidate False and adds
    the "reserve_not_met" flag, regardless of what the valuation math says -
    the current bid on a reserve auction that hasn't hit reserve yet isn't
    a real price signal."""

    def test_reserve_not_met_forces_not_a_candidate_and_flags_it(self):
        lot = _make_hibid_lot("reserve-1", current_price=40.0, premium_pct=0.10, reserve_not_met=True)

        ev, _ = pipeline.evaluate(lot, SPOT, llm_budget=0, use_llm=False)

        self.assertEqual(ev.category, "coins")
        self.assertGreater(ev.max_bid, 0)  # the valuation itself still ran and still looks affordable...
        self.assertFalse(ev.is_candidate)  # ...but it's still not a candidate
        self.assertIn("reserve_not_met", ev.flags)

    def test_otherwise_identical_lot_without_the_flag_is_a_candidate(self):
        """Same price/premium as the reserve-not-met lot above - proves the
        valuation math alone would have made this a candidate, and the
        flag is what's actually suppressing it."""
        lot = _make_hibid_lot("reserve-2", current_price=40.0, premium_pct=0.10, reserve_not_met=False)

        ev, _ = pipeline.evaluate(lot, SPOT, llm_budget=0, use_llm=False)

        self.assertTrue(ev.is_candidate)
        self.assertNotIn("reserve_not_met", ev.flags)

    def test_reserve_not_met_does_not_duplicate_flag_on_rescan(self):
        lot = _make_hibid_lot("reserve-3", current_price=40.0, reserve_not_met=True)

        pipeline.evaluate(lot, SPOT, llm_budget=0, use_llm=False)
        ev, _ = pipeline.evaluate(lot, SPOT, llm_budget=0, use_llm=False)

        self.assertEqual(ev.flags.count("reserve_not_met"), 1)

    def test_non_hibid_lot_unaffected(self):
        """ShopGoodwill/MaxSold raw dicts never carry "_hibid" at all, so
        this is a pure no-op for them - confirms there's no accidental
        source-agnostic side effect."""
        lot = SourcedLot.objects.create(
            source="shopgoodwill", external_id="reserve-sgw-1", url="https://example.com/reserve-sgw-1",
            title="10 oz .999 Fine Silver Bar", current_price=Decimal("40.0"),
            end_time=timezone.now() + timedelta(hours=5), raw={},
        )

        ev, _ = pipeline.evaluate(lot, SPOT, llm_budget=0, use_llm=False)

        self.assertTrue(ev.is_candidate)
        self.assertNotIn("reserve_not_met", ev.flags)

    def test_reserve_not_met_suppresses_candidate_alert(self):
        """run_alerts()'s candidate query filters on is_candidate=True, so
        setting it False here is enough to stop the candidate alert from
        firing - no separate change needed in scanner/alerts.py."""
        lot = _make_hibid_lot("reserve-alert-1", current_price=40.0, premium_pct=0.10, reserve_not_met=True)
        pipeline.evaluate(lot, SPOT, llm_budget=0, use_llm=False)

        cfg = {**settings.RESELLING_SCANNER, "ALERTS": {
            "enabled": True, "window_minutes": 600, "kinds": ["candidate"], "min_headroom": -1000.0,
            "candidate_confidence": ["high", "medium", "low", ""], "max_per_run": 10,
        }}
        with override_settings(RESELLING_SCANNER=cfg):
            sent = run_alerts(now=timezone.now())

        self.assertEqual(sent, [])
        self.assertFalse(AlertSent.objects.filter(lot=lot, kind="candidate").exists())


class WinLotHibidPremiumTests(TestCase):
    """The "I won this" form's cost prefill (ledger_views._win_lot_initial,
    via the view) must use the same per-lot premium the scanner's own max
    bid used, not the source's flat default."""

    def setUp(self):
        self.user = User.objects.create_user(username="tester", password="pw-not-real-12345")
        self.client.login(username="tester", password="pw-not-real-12345")

    def test_prefilled_premium_uses_per_lot_rate(self):
        lot = _make_hibid_lot("win-hibid-1", current_price=100.0, premium_pct=0.21)
        LotEvaluation.objects.create(lot=lot, category="coins", max_bid=Decimal("50.00"))

        response = self.client.get(reverse("albright_reselling_app:ledger_win_lot", args=[lot.pk]))

        # hammer=100, premium 21% -> premium dollar amount = 21.00
        self.assertAlmostEqual(float(response.context["form"].initial["buyer_premium"]), 21.00, places=2)

    def test_different_lots_different_premiums(self):
        low_lot = _make_hibid_lot("win-hibid-low", current_price=100.0, premium_pct=0.10)
        high_lot = _make_hibid_lot("win-hibid-high", current_price=100.0, premium_pct=0.30)
        LotEvaluation.objects.create(lot=low_lot, category="coins", max_bid=Decimal("50.00"))
        LotEvaluation.objects.create(lot=high_lot, category="coins", max_bid=Decimal("50.00"))

        low_premium = self.client.get(
            reverse("albright_reselling_app:ledger_win_lot", args=[low_lot.pk])
        ).context["form"].initial["buyer_premium"]
        high_premium = self.client.get(
            reverse("albright_reselling_app:ledger_win_lot", args=[high_lot.pk])
        ).context["form"].initial["buyer_premium"]

        self.assertLess(float(low_premium), float(high_premium))


def _recording_search(searched_keywords):
    def fake_search(self, keyword):
        searched_keywords.append(keyword)
        return
        yield  # pragma: no cover - makes this a generator function
    return fake_search


class HibidKeywordsUsedTests(TestCase):
    @mock.patch("albright_reselling_app.scanner.adapters.base.time.sleep")
    @mock.patch("albright_reselling_app.scanner.pipeline.get_all_spot", return_value=SPOT)
    def test_hibid_scan_uses_hibid_own_short_keyword_list_not_global(self, mock_spot, mock_sleep):
        searched = []
        with mock.patch.object(HiBidAdapter, "search", _recording_search(searched)):
            pipeline.run_scan("hibid")

        cfg = settings.RESELLING_SCANNER
        hibid_keywords = {kw for kws in cfg["SOURCES"]["hibid"]["keywords"].values() for kw in kws}
        global_keywords = {kw for kws in cfg["KEYWORDS"].values() for kw in kws}

        self.assertEqual(set(searched), hibid_keywords)
        # HiBid's list is deliberately short - prove it's actually smaller
        # than (and not a superset reusing) the global per-category lists.
        self.assertLess(len(hibid_keywords), len(global_keywords))


TRACK_CLOSED_SEARCH_CLOSED = "albright_reselling_app.scanner.adapters.hibid.HiBidAdapter.search_closed"
TRACK_CLOSED_PAUSE = "albright_reselling_app.scanner.adapters.hibid.HiBidAdapter.pause"


class TrackClosedHibidTests(TestCase):
    @mock.patch(TRACK_CLOSED_PAUSE)
    def test_source_hibid_updates_existing_lot_final_price(self, mock_pause):
        lot = _make_hibid_lot("closed-hibid-1", current_price=40.0)
        self.assertFalse(lot.is_closed)

        closed = RawLot(
            source="hibid", external_id="closed-hibid-1", url=lot.url, title=lot.title,
            current_price=55.0, bid_count=7,
        )
        with mock.patch(TRACK_CLOSED_SEARCH_CLOSED, side_effect=lambda *a, **k: iter([closed])):
            call_command("track_closed", "--source", "hibid", "--category", "coins", stdout=StringIO())

        lot.refresh_from_db()
        self.assertTrue(lot.is_closed)
        self.assertEqual(lot.final_price, Decimal("55.00"))
        self.assertEqual(lot.bid_count_at_close, 7)

    @mock.patch(TRACK_CLOSED_PAUSE)
    def test_source_hibid_passes_days_back(self, mock_pause):
        _make_hibid_lot("closed-hibid-2", current_price=40.0)

        with mock.patch(TRACK_CLOSED_SEARCH_CLOSED, return_value=iter([])) as mock_search:
            call_command(
                "track_closed", "--source", "hibid", "--category", "coins", "--days-back", "9", stdout=StringIO(),
            )

        for call in mock_search.call_args_list:
            self.assertEqual(call.kwargs.get("days_back"), 9)

    @mock.patch(TRACK_CLOSED_PAUSE)
    def test_already_closed_lot_not_updated_again(self, mock_pause):
        lot = _make_hibid_lot("closed-hibid-3", current_price=40.0)
        lot.is_closed = True
        lot.final_price = Decimal("30.00")
        lot.save()

        closed = RawLot(
            source="hibid", external_id="closed-hibid-3", url=lot.url, title=lot.title, current_price=999.0,
        )
        with mock.patch(TRACK_CLOSED_SEARCH_CLOSED, side_effect=lambda *a, **k: iter([closed])):
            call_command("track_closed", "--source", "hibid", "--category", "coins", stdout=StringIO())

        lot.refresh_from_db()
        self.assertEqual(lot.final_price, Decimal("30.00"))  # untouched
