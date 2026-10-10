"""Tests for the dashboard's Auction Scanner section: the data function
directly (get_scanner_dashboard_context), and the dashboard view end to end
via the test client. No HTTP call to any external site happens here - only
ORM fixtures."""
import os
import re
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from albright_reselling_app.models import LedgerEntry, LedgerSale
from albright_reselling_app.scanner.dashboard import get_scanner_dashboard_context
from albright_reselling_app.scanner_models import (
    AIReview, AlertSent, BidWatch, LotEvaluation, ScanRun, SourcedLot, SpotPrice,
)


def _make_lot(external_id, end_time, current_price=10.0, source="shopgoodwill", raw=None):
    return SourcedLot.objects.create(
        source=source, external_id=external_id, url=f"https://shopgoodwill.com/item/{external_id}",
        title=f"Morgan Silver Dollar {external_id}", current_price=current_price, end_time=end_time,
        raw=raw or {},
    )


def _make_maxsold_lot(external_id, end_time, auction_id, city="Riverview", distance_miles=8.2,
                       auction_title="Estate of J. Smith", current_price=10.0):
    return _make_lot(
        external_id, end_time, current_price=current_price, source="maxsold",
        raw={"_pickup": {"distance_miles": distance_miles, "auction_id": auction_id,
                          "auction_title": auction_title, "city": city, "has_shipping": False}},
    )


def _make_hibid_lot(external_id, end_time, current_price=40.0, ships=True, miles=None,
                     distance_estimated=False, end_time_source="time_left", premium_pct=0.21):
    raw = {
        "_costs": {"buyer_premium_pct": premium_pct, "premium_text": f"{premium_pct * 100:.0f}%"},
        "_hibid": {
            "pass": "shipping" if ships else "pickup", "end_time_source": end_time_source, "ships": ships,
            "shipping_type": "SHIPPING_OFFERED_ALL", "auction_id": 777, "auction_title": "Saturday Showcase",
            "auctioneer_id": 55, "auctioneer": "Hessney Auction Co.", "city": "Geneva", "state": "NY",
            "zip": "14456", "lot_number": "12", "picture_count": 6, "status": "OPEN",
        },
    }
    if not ships:
        raw["_pickup"] = {
            "distance_miles": miles if miles is not None else 30.0, "distance_estimated": distance_estimated,
            "auction_id": 777, "auction_title": "Saturday Showcase", "city": "Geneva, NY", "has_shipping": False,
        }
    return SourcedLot.objects.create(
        source="hibid", external_id=external_id, url=f"https://hibid.com/lot/{external_id}",
        title=f"10 oz .999 Fine Silver Bar {external_id}", current_price=current_price, end_time=end_time, raw=raw,
    )


def _make_evaluation(lot, is_candidate, max_bid=20.0, headroom=5.0, confidence="high"):
    return LotEvaluation.objects.create(
        lot=lot, is_candidate=is_candidate, max_bid=Decimal(str(max_bid)), headroom=Decimal(str(headroom)),
        confidence=confidence, coin_keys="morgan_dollar", silver_oz=Decimal("0.7734"),
    )


def _make_lead_evaluation(lot, category="games", lead_reason="n64: bulk lot"):
    return LotEvaluation.objects.create(
        lot=lot, category=category, is_candidate=False, is_lead=True, lead_reason=lead_reason,
    )


class ScannerDashboardContextTests(TestCase):
    """Unit-level: exercises get_scanner_dashboard_context() directly."""

    def test_empty_state_has_no_data(self):
        context = get_scanner_dashboard_context()

        self.assertEqual(context["live_candidates_ending_soon"], [])
        self.assertEqual(context["live_candidates_ending_later"], [])
        self.assertEqual(context["check_by_hand"], [])
        self.assertEqual(context["closest_calls"], [])
        self.assertEqual(context["recent_runs"], [])
        self.assertEqual(context["maxsold_estates"], [])
        self.assertEqual(context["closed_results_summary"], [])
        self.assertEqual(context["closed_results_rows"], [])
        self.assertIsNone(context["scanner_health"]["shopgoodwill"]["last_run"])
        self.assertIn("No shopgoodwill scans have run yet", context["scanner_health"]["shopgoodwill"]["warnings"][0])
        self.assertIsNone(context["scanner_health"]["maxsold"]["last_run"])
        self.assertIsNone(context["spot_prices"]["silver"])
        self.assertIsNone(context["spot_prices"]["gold"])

    def test_live_candidate_appears(self):
        lot = _make_lot("live-1", timezone.now() + timedelta(hours=3))
        _make_evaluation(lot, is_candidate=True)

        context = get_scanner_dashboard_context()

        self.assertEqual(len(context["live_candidates_ending_soon"]), 1)
        self.assertEqual(context["live_candidates_ending_soon"][0]["title"], lot.title)
        self.assertEqual(context["live_candidates_ending_later"], [])

    def test_ended_candidate_does_not_appear(self):
        lot = _make_lot("ended-1", timezone.now() - timedelta(hours=1))
        _make_evaluation(lot, is_candidate=True)

        context = get_scanner_dashboard_context()

        self.assertEqual(context["live_candidates_ending_soon"], [])
        self.assertEqual(context["live_candidates_ending_later"], [])

    def test_stale_run_warning_for_shopgoodwill(self):
        run = ScanRun.objects.create(source="shopgoodwill", lots_seen=5, candidates=1)
        ScanRun.objects.filter(pk=run.pk).update(started_at=timezone.now() - timedelta(hours=3))

        context = get_scanner_dashboard_context()

        health = context["scanner_health"]["shopgoodwill"]
        self.assertTrue(any("ago" in w for w in health["warnings"]))
        self.assertEqual(health["status"], "warn")

    def test_stale_run_flagged_for_maxsold_too(self):
        """Staleness is now checked per-source (expected_interval_hours+1,
        default 1h -> 2h threshold) rather than gated by a hardcoded
        whitelist - MaxSold gets the same default threshold as
        ShopGoodwill unless its own settings say otherwise."""
        run = ScanRun.objects.create(source="maxsold", lots_seen=5, candidates=1)
        ScanRun.objects.filter(pk=run.pk).update(started_at=timezone.now() - timedelta(hours=3))

        context = get_scanner_dashboard_context()

        health = context["scanner_health"]["maxsold"]
        self.assertTrue(any("ago" in w for w in health["warnings"]))
        self.assertEqual(health["status"], "warn")

    def test_hibid_not_flagged_stale_within_its_longer_interval(self):
        """HiBid's expected_interval_hours=6 gives it a 7h threshold
        (6+1), so a 5h-old run isn't stale even though that would already
        be stale for ShopGoodwill/MaxSold's default 2h threshold."""
        run = ScanRun.objects.create(source="hibid", lots_seen=5, candidates=1)
        ScanRun.objects.filter(pk=run.pk).update(started_at=timezone.now() - timedelta(hours=5))

        context = get_scanner_dashboard_context()

        health = context["scanner_health"]["hibid"]
        self.assertFalse(any("ago" in w for w in health["warnings"]))
        self.assertEqual(health["status"], "ok")

    def test_hibid_flagged_stale_past_its_longer_interval(self):
        run = ScanRun.objects.create(source="hibid", lots_seen=5, candidates=1)
        ScanRun.objects.filter(pk=run.pk).update(started_at=timezone.now() - timedelta(hours=8))

        context = get_scanner_dashboard_context()

        health = context["scanner_health"]["hibid"]
        self.assertTrue(any("ago" in w for w in health["warnings"]))
        self.assertEqual(health["status"], "warn")

    def test_healthy_run_has_ok_status_and_no_warnings(self):
        ScanRun.objects.create(source="shopgoodwill", lots_seen=5, candidates=1)

        context = get_scanner_dashboard_context()

        health = context["scanner_health"]["shopgoodwill"]
        self.assertEqual(health["status"], "ok")
        self.assertEqual(health["warnings"], [])
        self.assertIsNotNone(health["last_run_relative"])

    def test_never_run_has_error_status(self):
        context = get_scanner_dashboard_context()

        self.assertEqual(context["scanner_health"]["shopgoodwill"]["status"], "error")
        self.assertIsNone(context["scanner_health"]["shopgoodwill"]["last_run_relative"])

    def test_failed_run_has_error_status(self):
        ScanRun.objects.create(source="shopgoodwill", lots_seen=5, candidates=1, error="boom")

        context = get_scanner_dashboard_context()

        self.assertEqual(context["scanner_health"]["shopgoodwill"]["status"], "error")

    def test_failed_keywords_warning_has_warn_status_not_error(self):
        ScanRun.objects.create(source="shopgoodwill", lots_seen=5, candidates=1, failed_keywords=["peace dollar"])

        context = get_scanner_dashboard_context()

        health = context["scanner_health"]["shopgoodwill"]
        self.assertEqual(health["status"], "warn")
        self.assertTrue(any("failed keywords" in w for w in health["warnings"]))

    def test_stale_spot_warning(self):
        price = SpotPrice.objects.create(metal="silver", price_usd=Decimal("30.00"), source="goldapi")
        SpotPrice.objects.filter(pk=price.pk).update(fetched_at=timezone.now() - timedelta(days=10))

        context = get_scanner_dashboard_context()

        self.assertTrue(context["spot_prices"]["silver"]["is_stale"])

    def test_maxsold_pickup_info_on_row(self):
        lot = _make_maxsold_lot("ms-1", timezone.now() + timedelta(hours=5), auction_id=42,
                                 city="Riverview", distance_miles=8.2, auction_title="Estate of J. Smith")
        _make_evaluation(lot, is_candidate=True)

        context = get_scanner_dashboard_context()

        row = context["live_candidates_ending_soon"][0]
        self.assertEqual(row["source"], "maxsold")
        self.assertEqual(row["pickup"]["city"], "Riverview")
        self.assertEqual(row["pickup"]["distance_miles"], 8.2)
        self.assertEqual(row["pickup"]["auction_title"], "Estate of J. Smith")

    def test_maxsold_estates_grouping(self):
        lot1 = _make_maxsold_lot("ms-a", timezone.now() + timedelta(hours=2), auction_id=7, city="Tampa")
        lot2 = _make_maxsold_lot("ms-b", timezone.now() + timedelta(hours=3), auction_id=7, city="Tampa")
        other_estate_lot = _make_maxsold_lot("ms-c", timezone.now() + timedelta(hours=4), auction_id=9,
                                              city="Brandon")
        _make_evaluation(lot1, is_candidate=True, headroom=5.0)
        _make_evaluation(lot2, is_candidate=True, headroom=7.5)
        _make_evaluation(other_estate_lot, is_candidate=True, headroom=2.0)

        context = get_scanner_dashboard_context()

        estates = {e["auction_id"]: e for e in context["maxsold_estates"]}
        self.assertEqual(estates[7]["lot_count"], 2)
        self.assertEqual(estates[7]["total_headroom"], Decimal("12.5"))
        self.assertEqual(estates[9]["lot_count"], 1)
        self.assertEqual(estates[9]["total_headroom"], Decimal("2.0"))

    def test_closed_results_counts_under_and_over_max(self):
        under_lot = _make_lot("closed-under", timezone.now() - timedelta(hours=1))
        under_lot.is_closed = True
        under_lot.final_price = Decimal("15.00")
        under_lot.save()
        LotEvaluation.objects.create(
            lot=under_lot, category="coins", max_bid=Decimal("20.00"), melt_value=Decimal("18.00"),
        )

        over_lot = _make_lot("closed-over", timezone.now() - timedelta(hours=1))
        over_lot.is_closed = True
        over_lot.final_price = Decimal("25.00")
        over_lot.save()
        LotEvaluation.objects.create(
            lot=over_lot, category="coins", max_bid=Decimal("20.00"), melt_value=Decimal("18.00"),
        )

        # No max bid - shouldn't count toward the summary at all.
        no_bid_lot = _make_lot("closed-no-bid", timezone.now() - timedelta(hours=1))
        no_bid_lot.is_closed = True
        no_bid_lot.final_price = Decimal("5.00")
        no_bid_lot.save()
        LotEvaluation.objects.create(lot=no_bid_lot, category="coins", max_bid=Decimal("0"))

        context = get_scanner_dashboard_context()

        summary = {s["category"]: s for s in context["closed_results_summary"]}
        self.assertEqual(summary["coins"]["under"], 1)
        self.assertEqual(summary["coins"]["over"], 1)
        self.assertEqual(len(context["closed_results_rows"]), 2)

    def test_candidate_window_split(self):
        soon_lot = _make_lot("soon", timezone.now() + timedelta(hours=5))
        later_lot = _make_lot("later", timezone.now() + timedelta(days=3))
        _make_evaluation(soon_lot, is_candidate=True)
        _make_evaluation(later_lot, is_candidate=True)

        context = get_scanner_dashboard_context()

        soon_titles = [row["title"] for row in context["live_candidates_ending_soon"]]
        later_titles = [row["title"] for row in context["live_candidates_ending_later"]]
        self.assertEqual(soon_titles, [soon_lot.title])
        self.assertEqual(later_titles, [later_lot.title])

    def test_lead_ending_within_window_appears(self):
        lot = _make_lot("lead-soon", timezone.now() + timedelta(hours=10))
        _make_lead_evaluation(lot, category="games", lead_reason="n64: bulk lot")

        context = get_scanner_dashboard_context()

        self.assertEqual(len(context["leads_to_review"]), 1)
        self.assertEqual(context["leads_to_review"][0]["title"], lot.title)
        self.assertEqual(context["lead_counts_by_category"], {"games": 1})

    def test_lead_ending_far_out_does_not_appear(self):
        lot = _make_lot("lead-far", timezone.now() + timedelta(days=5))
        _make_lead_evaluation(lot, category="games", lead_reason="n64: bulk lot")

        context = get_scanner_dashboard_context()

        self.assertEqual(context["leads_to_review"], [])
        self.assertEqual(context["lead_counts_by_category"], {})

    def test_low_confidence_under_max_in_check_by_hand_not_closest_calls(self):
        lot = _make_lot("low-conf-1", timezone.now() + timedelta(hours=5))
        _make_evaluation(lot, is_candidate=False, max_bid=20.0, headroom=5.0, confidence="low")

        context = get_scanner_dashboard_context()

        self.assertEqual([r["title"] for r in context["check_by_hand"]], [lot.title])
        self.assertEqual(context["closest_calls"], [])

    def test_over_max_lot_only_in_closest_calls(self):
        lot = _make_lot("over-max-1", timezone.now() + timedelta(hours=5))
        _make_evaluation(lot, is_candidate=False, max_bid=20.0, headroom=-5.0, confidence="high")

        context = get_scanner_dashboard_context()

        self.assertEqual([r["title"] for r in context["closest_calls"]], [lot.title])
        self.assertEqual(context["check_by_hand"], [])

    def test_low_confidence_over_max_is_closest_call_not_check_by_hand(self):
        """Closest Calls is purely about headroom (over max), independent of
        confidence; Check by hand requires headroom >= 0 (affordable), so a
        low-confidence, over-max lot lands in Closest Calls only."""
        lot = _make_lot("low-conf-over-max", timezone.now() + timedelta(hours=5))
        _make_evaluation(lot, is_candidate=False, max_bid=20.0, headroom=-5.0, confidence="low")

        context = get_scanner_dashboard_context()

        self.assertEqual([r["title"] for r in context["closest_calls"]], [lot.title])
        self.assertEqual(context["check_by_hand"], [])

    def test_item_and_metal_labels_for_coin(self):
        lot = _make_lot("labels-coin", timezone.now() + timedelta(hours=5))
        LotEvaluation.objects.create(
            lot=lot, is_candidate=True, max_bid=Decimal("20.00"), headroom=Decimal("5.00"),
            confidence="high", coin_keys="morgan_dollar", silver_oz=Decimal("0.7734"), gold_oz=Decimal("0"),
        )

        context = get_scanner_dashboard_context()

        row = context["live_candidates_ending_soon"][0]
        self.assertEqual(row["item_label"], "Morgan dollar")
        self.assertEqual(row["metal_label"], "0.77 oz Ag")

    def test_item_and_metal_labels_for_jewelry(self):
        lot = _make_lot("labels-jewelry", timezone.now() + timedelta(hours=5))
        LotEvaluation.objects.create(
            lot=lot, is_candidate=True, max_bid=Decimal("20.00"), headroom=Decimal("5.00"),
            confidence="high", coin_keys="jewelry_gold_14k", gold_oz=Decimal("0.6400"),
        )

        context = get_scanner_dashboard_context()

        row = context["live_candidates_ending_soon"][0]
        self.assertEqual(row["item_label"], "14k gold jewelry")
        self.assertEqual(row["metal_label"], "0.64 oz Au")

    def test_ai_review_badge_data_for_recent_successful_review(self):
        lot = _make_lot("ai-badge-1", timezone.now() + timedelta(hours=3))
        _make_evaluation(lot, is_candidate=True)
        AIReview.objects.create(
            lot=lot, status="done", resale_low=Decimal("95.00"), resale_high=Decimal("130.00"),
            confidence="high", cost_usd=Decimal("0.05"),
        )

        context = get_scanner_dashboard_context()

        row = context["live_candidates_ending_soon"][0]
        self.assertIsNotNone(row["ai_review"])
        self.assertEqual(row["ai_review"].resale_low, Decimal("95.00"))

    def test_no_ai_review_badge_without_a_review(self):
        lot = _make_lot("no-ai-badge", timezone.now() + timedelta(hours=3))
        _make_evaluation(lot, is_candidate=True)

        context = get_scanner_dashboard_context()

        self.assertIsNone(context["live_candidates_ending_soon"][0]["ai_review"])

    def test_alerts_status_reflects_missing_env_vars(self):
        with mock.patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "", "TELEGRAM_CHAT_ID": ""}):
            context = get_scanner_dashboard_context()

        self.assertFalse(context["alerts_status"]["configured"])

    def test_alerts_status_reflects_configured_env_vars(self):
        with mock.patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "tok", "TELEGRAM_CHAT_ID": "123"}):
            context = get_scanner_dashboard_context()

        self.assertTrue(context["alerts_status"]["configured"])

    def test_alerts_status_counts_todays_alerts(self):
        lot = _make_lot("alert-count-1", timezone.now() + timedelta(hours=3))
        AlertSent.objects.create(lot=lot, kind="candidate")

        context = get_scanner_dashboard_context()

        self.assertEqual(context["alerts_status"]["today_count"], 1)

    def test_ai_review_stats_reflect_todays_reviews(self):
        lot = _make_lot("ai-stats-1", timezone.now() + timedelta(hours=3))
        AIReview.objects.create(lot=lot, status="done", cost_usd=Decimal("0.05"))
        AIReview.objects.create(lot=lot, status="error", cost_usd=Decimal("0"))

        context = get_scanner_dashboard_context()

        self.assertEqual(context["ai_review_stats"]["today_count"], 2)
        self.assertEqual(context["ai_review_stats"]["month_cost"], Decimal("0.05"))

    def test_matched_keyword_on_lot_and_lead_rows(self):
        lot = _make_lot("matched-kw", timezone.now() + timedelta(hours=3))
        lot.matched_keyword = "morgan dollar"
        lot.save()
        _make_evaluation(lot, is_candidate=True)

        lead_lot = _make_lot("matched-kw-lead", timezone.now() + timedelta(hours=10))
        lead_lot.matched_keyword = "n64 games"
        lead_lot.save()
        _make_lead_evaluation(lead_lot)

        context = get_scanner_dashboard_context()

        self.assertEqual(context["live_candidates_ending_soon"][0]["matched_keyword"], "morgan dollar")
        self.assertEqual(context["leads_to_review"][0]["matched_keyword"], "n64 games")

    def test_closest_calls_get_ai_review_attached(self):
        lot = _make_lot("closest-ai", timezone.now() + timedelta(hours=5))
        _make_evaluation(lot, is_candidate=False, max_bid=20.0, headroom=-5.0, confidence="high")
        AIReview.objects.create(
            lot=lot, status="done", resale_low=Decimal("95.00"), resale_high=Decimal("130.00"),
            confidence="high", cost_usd=Decimal("0.05"),
        )

        context = get_scanner_dashboard_context()

        self.assertIsNotNone(context["closest_calls"][0]["ai_review"])

    def test_tab_counts_group_tables_correctly(self):
        soon_lot = _make_lot("tab-soon", timezone.now() + timedelta(hours=5))
        _make_evaluation(soon_lot, is_candidate=True)
        later_lot = _make_lot("tab-later", timezone.now() + timedelta(days=3))
        _make_evaluation(later_lot, is_candidate=True)
        check_lot = _make_lot("tab-check", timezone.now() + timedelta(hours=5))
        _make_evaluation(check_lot, is_candidate=False, max_bid=20.0, headroom=5.0, confidence="low")
        lead_lot = _make_lot("tab-lead", timezone.now() + timedelta(hours=10))
        _make_lead_evaluation(lead_lot)
        closest_lot = _make_lot("tab-closest", timezone.now() + timedelta(hours=5))
        _make_evaluation(closest_lot, is_candidate=False, max_bid=20.0, headroom=-5.0, confidence="high")

        context = get_scanner_dashboard_context()

        # Act now: candidates ending soon (1) + check by hand (1) + leads (1)
        self.assertEqual(context["tab_counts"]["act_now"], 3)
        # Watch: candidates ending later (1) + closest calls (1) + maxsold estates (0)
        self.assertEqual(context["tab_counts"]["watch"], 2)
        self.assertEqual(context["tab_counts"]["performance"], 0)

    def test_tab_counts_empty_state(self):
        context = get_scanner_dashboard_context()

        self.assertEqual(context["tab_counts"], {"act_now": 0, "watch": 0, "performance": 0})

    def test_is_relisted_true_on_row_when_features_flags_it(self):
        lot = _make_lot("relisted-ctx-1", timezone.now() + timedelta(hours=3))
        lot.features = {"relist_id": 99, "is_relisted": True}
        lot.save()
        _make_evaluation(lot, is_candidate=True)

        context = get_scanner_dashboard_context()

        self.assertTrue(context["live_candidates_ending_soon"][0]["is_relisted"])

    def test_is_relisted_false_by_default(self):
        lot = _make_lot("relisted-ctx-2", timezone.now() + timedelta(hours=3))
        _make_evaluation(lot, is_candidate=True)

        context = get_scanner_dashboard_context()

        self.assertFalse(context["live_candidates_ending_soon"][0]["is_relisted"])

    def test_hibid_row_carries_auctioneer_premium_and_city_state(self):
        lot = _make_hibid_lot("hibid-ctx-1", timezone.now() + timedelta(hours=3), premium_pct=0.21)
        _make_evaluation(lot, is_candidate=True)

        context = get_scanner_dashboard_context()
        row = context["live_candidates_ending_soon"][0]

        self.assertEqual(row["auctioneer"], "Hessney Auction Co.")
        self.assertEqual(row["hibid_city_state"], "Geneva, NY")
        self.assertEqual(row["premium_pct"], 21.0)
        self.assertFalse(row["end_time_estimated"])

    def test_hibid_pickup_lot_carries_pickup_and_estimate_marker(self):
        lot = _make_hibid_lot(
            "hibid-ctx-2", timezone.now() + timedelta(hours=3), ships=False, distance_estimated=True,
        )
        _make_evaluation(lot, is_candidate=True)

        context = get_scanner_dashboard_context()
        row = context["live_candidates_ending_soon"][0]

        self.assertIsNotNone(row["pickup"])
        self.assertTrue(row["pickup"]["distance_estimated"])

    def test_hibid_auction_close_end_time_flagged_estimated(self):
        lot = _make_hibid_lot("hibid-ctx-3", timezone.now() + timedelta(hours=3), end_time_source="auction_close")
        _make_evaluation(lot, is_candidate=True)

        context = get_scanner_dashboard_context()
        row = context["live_candidates_ending_soon"][0]

        self.assertTrue(row["end_time_estimated"])

    def test_non_hibid_row_has_no_hibid_detail_fields(self):
        lot = _make_lot("non-hibid-ctx", timezone.now() + timedelta(hours=3))
        _make_evaluation(lot, is_candidate=True)

        context = get_scanner_dashboard_context()
        row = context["live_candidates_ending_soon"][0]

        self.assertNotIn("auctioneer", row)

    def test_pickup_column_shown_for_hibid_pickup_only_lots(self):
        """_has_maxsold (the Pickup-column gate) keys on real pickup data,
        not source == "maxsold" specifically - a watched HiBid pickup lot
        must still get the column, same as MaxSold always has."""
        lot = _make_hibid_lot("hibid-ctx-4", timezone.now() + timedelta(hours=3), ships=False)
        _make_evaluation(lot, is_candidate=True)

        context = get_scanner_dashboard_context()

        self.assertTrue(context["live_candidates_ending_soon_has_maxsold"])

    def test_reserve_not_met_flag_true_on_row(self):
        # confidence="low" + affordable (headroom>=0) lands this in
        # check_by_hand regardless of is_candidate - exactly the kind of
        # row a reserve-not-met lot (forced out of the candidate tables)
        # would actually show up in.
        lot = _make_hibid_lot("reserve-ctx-1", timezone.now() + timedelta(hours=3))
        LotEvaluation.objects.create(
            lot=lot, is_candidate=False, max_bid=Decimal("20.0"), headroom=Decimal("5.0"),
            confidence="low", flags=["reserve_not_met"],
        )

        context = get_scanner_dashboard_context()

        self.assertEqual(len(context["check_by_hand"]), 1)
        self.assertTrue(context["check_by_hand"][0]["reserve_not_met"])

    def test_reserve_not_met_flag_false_by_default(self):
        lot = _make_hibid_lot("reserve-ctx-2", timezone.now() + timedelta(hours=3))
        _make_evaluation(lot, is_candidate=True)

        context = get_scanner_dashboard_context()

        self.assertFalse(context["live_candidates_ending_soon"][0]["reserve_not_met"])


class DashboardViewTests(TestCase):
    """Integration-level: hits the actual dashboard view/template."""

    def setUp(self):
        self.user = User.objects.create_user(username="tester", password="pw-not-real-12345")
        self.client.login(username="tester", password="pw-not-real-12345")

    def test_dashboard_loads_with_no_scanner_data(self):
        response = self.client.get(reverse("albright_reselling_app:dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Never run")
        self.assertContains(response, "Nothing ending within")
        self.assertContains(response, "Nothing low-confidence and affordable right now")
        self.assertContains(response, "No leads ending within")
        self.assertContains(response, "No close calls right now")
        self.assertContains(response, "No runs recorded yet")
        self.assertContains(response, "No MaxSold candidates right now")

    def test_alerts_card_shows_configured_state_and_todays_count(self):
        lot = _make_lot("alert-card-1", timezone.now() + timedelta(hours=3))
        AlertSent.objects.create(lot=lot, kind="candidate")

        with mock.patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "tok", "TELEGRAM_CHAT_ID": "123"}):
            response = self.client.get(reverse("albright_reselling_app:dashboard"))

        self.assertContains(response, "Telegram configured")
        self.assertContains(response, "1 sent today")

    def test_alerts_card_shows_not_configured_state(self):
        with mock.patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "", "TELEGRAM_CHAT_ID": ""}):
            response = self.client.get(reverse("albright_reselling_app:dashboard"))

        self.assertContains(response, "Telegram not configured")

    def test_status_bar_shows_error_dot_when_never_run(self):
        response = self.client.get(reverse("albright_reselling_app:dashboard"))

        self.assertContains(response, "status-dot--error")

    def test_status_bar_shows_ok_dot_for_healthy_run(self):
        ScanRun.objects.create(source="shopgoodwill", lots_seen=5, candidates=1)
        ScanRun.objects.create(source="maxsold", lots_seen=5, candidates=1)
        ScanRun.objects.create(source="hibid", lots_seen=5, candidates=1)

        response = self.client.get(reverse("albright_reselling_app:dashboard"))

        self.assertContains(response, "status-dot--ok")
        self.assertNotContains(response, "status-dot--error")

    def test_status_bar_shows_warn_dot_for_stale_run(self):
        run = ScanRun.objects.create(source="shopgoodwill", lots_seen=5, candidates=1)
        ScanRun.objects.filter(pk=run.pk).update(started_at=timezone.now() - timedelta(hours=3))
        ScanRun.objects.create(source="maxsold", lots_seen=5, candidates=1)

        response = self.client.get(reverse("albright_reselling_app:dashboard"))

        self.assertContains(response, "status-dot--warn")

    def test_tabs_render_with_badge_counts(self):
        lot = _make_lot("tab-page-soon", timezone.now() + timedelta(hours=3))
        _make_evaluation(lot, is_candidate=True)

        response = self.client.get(reverse("albright_reselling_app:dashboard"))

        self.assertContains(response, 'id="tab-badge-act-now">1<')
        self.assertContains(response, 'id="tab-badge-watch">0<')
        self.assertContains(response, 'id="tab-badge-performance">0<')
        # All three panels render stacked (the no-JS fallback) - "watch" and
        # "performance" just carry the hidden attribute for JS to toggle.
        self.assertContains(response, 'id="act-now"')
        self.assertContains(response, 'id="watch" hidden')
        self.assertContains(response, 'id="performance" hidden')

    def test_candidate_in_act_now_tab_not_watch(self):
        soon_lot = _make_lot("tab-page-act", timezone.now() + timedelta(hours=3))
        _make_evaluation(soon_lot, is_candidate=True)

        response = self.client.get(reverse("albright_reselling_app:dashboard"))
        content = response.content.decode()

        act_now_html = content.split('id="watch"')[0]
        watch_html = content.split('id="watch"')[1].split('id="performance"')[0]
        self.assertIn(soon_lot.title, act_now_html)
        self.assertNotIn(soon_lot.title, watch_html)

    def test_live_candidate_appears_on_page(self):
        lot = _make_lot("live-page-1", timezone.now() + timedelta(hours=3))
        _make_evaluation(lot, is_candidate=True)

        response = self.client.get(reverse("albright_reselling_app:dashboard"))

        self.assertContains(response, lot.title)

    def test_ended_candidate_not_on_page(self):
        lot = _make_lot("ended-page-1", timezone.now() - timedelta(hours=1))
        _make_evaluation(lot, is_candidate=True)

        response = self.client.get(reverse("albright_reselling_app:dashboard"))

        self.assertNotContains(response, lot.title)

    def test_maxsold_pickup_info_and_estate_grouping_on_page(self):
        lot1 = _make_maxsold_lot("ms-page-a", timezone.now() + timedelta(hours=2), auction_id=11,
                                  city="Riverview", distance_miles=8.2, auction_title="Estate of J. Smith")
        lot2 = _make_maxsold_lot("ms-page-b", timezone.now() + timedelta(hours=3), auction_id=11,
                                  city="Riverview", distance_miles=8.2, auction_title="Estate of J. Smith")
        _make_evaluation(lot1, is_candidate=True)
        _make_evaluation(lot2, is_candidate=True)

        response = self.client.get(reverse("albright_reselling_app:dashboard"))

        self.assertContains(response, "Riverview")
        self.assertContains(response, "Estate of J. Smith")
        self.assertContains(response, "8.2")

    def test_ai_review_badge_on_page(self):
        lot = _make_lot("ai-badge-page", timezone.now() + timedelta(hours=3))
        _make_evaluation(lot, is_candidate=True)
        AIReview.objects.create(
            lot=lot, status="done", resale_low=Decimal("95.00"), resale_high=Decimal("130.00"),
            confidence="high", cost_usd=Decimal("0.05"),
        )

        response = self.client.get(reverse("albright_reselling_app:dashboard"))

        self.assertContains(response, "AI: $95")
        self.assertContains(response, "AI review")  # the button is still offered too

    def test_ai_review_button_without_existing_review(self):
        lot = _make_lot("ai-button-only", timezone.now() + timedelta(hours=3))
        _make_evaluation(lot, is_candidate=True)

        response = self.client.get(reverse("albright_reselling_app:dashboard"))

        self.assertContains(response, "AI review")
        self.assertNotContains(response, "AI: $")

    def test_relisted_badge_shown_for_relisted_candidate(self):
        lot = _make_lot("relisted-badge-1", timezone.now() + timedelta(hours=3))
        lot.features = {"relist_id": 42, "is_relisted": True}
        lot.save()
        _make_evaluation(lot, is_candidate=True)

        response = self.client.get(reverse("albright_reselling_app:dashboard"))

        self.assertContains(response, "Relisted")

    def test_no_relisted_badge_for_non_relisted_candidate(self):
        lot = _make_lot("relisted-badge-2", timezone.now() + timedelta(hours=3))
        _make_evaluation(lot, is_candidate=True)

        response = self.client.get(reverse("albright_reselling_app:dashboard"))

        self.assertNotContains(response, "Relisted")

    def test_reserve_not_met_badge_shown(self):
        lot = _make_hibid_lot("reserve-page-1", timezone.now() + timedelta(hours=3))
        LotEvaluation.objects.create(
            lot=lot, is_candidate=False, max_bid=Decimal("20.0"), headroom=Decimal("5.0"),
            confidence="low", flags=["reserve_not_met"],
        )

        response = self.client.get(reverse("albright_reselling_app:dashboard"))

        self.assertContains(response, "Reserve not met")

    def test_no_reserve_not_met_badge_without_the_flag(self):
        lot = _make_hibid_lot("reserve-page-2", timezone.now() + timedelta(hours=3))
        _make_evaluation(lot, is_candidate=True)

        response = self.client.get(reverse("albright_reselling_app:dashboard"))

        self.assertNotContains(response, "Reserve not met")

    def test_hibid_source_filter_option_present(self):
        response = self.client.get(reverse("albright_reselling_app:dashboard"))

        self.assertContains(response, '<option value="hibid">HiBid</option>')

    def test_hibid_detail_shows_auctioneer_premium_and_city_state(self):
        lot = _make_hibid_lot("hibid-page-1", timezone.now() + timedelta(hours=3), premium_pct=0.21)
        _make_evaluation(lot, is_candidate=True)

        response = self.client.get(reverse("albright_reselling_app:dashboard"))

        self.assertContains(response, "Hessney Auction Co.")
        self.assertContains(response, "Geneva, NY")
        self.assertContains(response, "21% premium")

    def test_hibid_pickup_lot_shows_pickup_and_estimate_badge(self):
        lot = _make_hibid_lot(
            "hibid-page-2", timezone.now() + timedelta(hours=3), ships=False, distance_estimated=True,
        )
        _make_evaluation(lot, is_candidate=True)

        response = self.client.get(reverse("albright_reselling_app:dashboard"))

        self.assertContains(response, "Pickup:")
        self.assertContains(response, "est.")

    def test_hibid_auction_close_end_time_shows_estimate_badge(self):
        lot = _make_hibid_lot("hibid-page-3", timezone.now() + timedelta(hours=3), end_time_source="auction_close")
        _make_evaluation(lot, is_candidate=True)

        response = self.client.get(reverse("albright_reselling_app:dashboard"))

        self.assertContains(response, "est. end time")

    def test_hibid_in_health_status_bar(self):
        response = self.client.get(reverse("albright_reselling_app:dashboard"))

        self.assertContains(response, "HiBid")


class RangeFilterTests(TestCase):
    """The filter bar's Bid/Max bid/Headroom min-max inputs and headroom
    presets (static/js/auction_scanner.js's initFilters()) - and the raw
    numeric data-* attributes on each row those filters read from, since
    a formatted "$12.50" there would silently break every comparison."""

    def setUp(self):
        self.user = User.objects.create_user(username="tester", password="pw-not-real-12345")
        self.client.login(username="tester", password="pw-not-real-12345")

    def test_range_filter_inputs_render(self):
        response = self.client.get(reverse("albright_reselling_app:dashboard"))

        for field_id in ("af-bid-min", "af-bid-max", "af-maxbid-min", "af-maxbid-max",
                          "af-headroom-min", "af-headroom-max"):
            self.assertContains(response, f'id="{field_id}"')
        self.assertContains(response, "Under max")
        self.assertContains(response, "$25+ headroom")
        self.assertContains(response, "Close calls")
        self.assertContains(response, 'id="af-clear"')
        self.assertContains(response, "Clear filters")

    def test_candidate_row_has_raw_numeric_bid_maxbid_headroom(self):
        lot = _make_lot("range-filter-1", timezone.now() + timedelta(hours=3), current_price=Decimal("12.50"))
        _make_evaluation(lot, is_candidate=True, max_bid=20.0, headroom=7.5)

        response = self.client.get(reverse("albright_reselling_app:dashboard"))
        content = response.content.decode()

        bid = re.search(r'data-bid="([^"]*)"', content)
        maxbid = re.search(r'data-maxbid="([^"]*)"', content)
        headroom = re.search(r'data-headroom="([^"]*)"', content)
        self.assertIsNotNone(bid)
        self.assertIsNotNone(maxbid)
        self.assertIsNotNone(headroom)
        self.assertEqual(float(bid.group(1)), 12.5)
        self.assertEqual(float(maxbid.group(1)), 20.0)
        self.assertEqual(float(headroom.group(1)), 7.5)
        # raw numbers, not formatted currency - the JS filter compares these
        # with parseFloat(), which "$12.50" would silently fail on.
        self.assertNotIn("$", bid.group(1))
        self.assertNotIn("$", maxbid.group(1))
        self.assertNotIn("$", headroom.group(1))

    def test_lead_row_has_bid_but_no_maxbid_or_headroom(self):
        lot = _make_lot("range-filter-lead-1", timezone.now() + timedelta(hours=3), current_price=Decimal("8.00"))
        _make_lead_evaluation(lot)

        response = self.client.get(reverse("albright_reselling_app:dashboard"))
        content = response.content.decode()
        table_start = content.index('id="table-leads"')
        leads_table = content[table_start:content.index("</table>", table_start)]

        bid = re.search(r'data-bid="([^"]*)"', leads_table)
        self.assertIsNotNone(bid)
        self.assertEqual(float(bid.group(1)), 8.0)
        self.assertNotIn("data-maxbid=", leads_table)
        self.assertNotIn("data-headroom=", leads_table)

    def test_query_string_params_documented_in_shared_js(self):
        """applyFromURL()/updateURL() in the shared JS file read/write the
        same element ids and query-param names the dashboard renders -
        the only way to check this without a JS test runner."""
        js_path = os.path.join(
            os.path.dirname(__file__), "..", "..", "albright_trading_app", "static", "js", "auction_scanner.js",
        )
        with open(js_path, encoding="utf-8") as f:
            js_source = f.read()

        for element_id, param in (
            ("af-bid-min", "bid_min"), ("af-bid-max", "bid_max"),
            ("af-maxbid-min", "maxbid_min"), ("af-maxbid-max", "maxbid_max"),
            ("af-headroom-min", "headroom_min"), ("af-headroom-max", "headroom_max"),
        ):
            self.assertIn(element_id, js_source)
            self.assertIn(param, js_source)

    def test_script_tag_is_cache_busted(self):
        """The shared JS file is loaded through a ?v=<mtime> query string
        (templatetags/cache_bust.py) - otherwise editing it has no visible
        effect for a browser (or CDN) already holding an old copy cached
        under the same stable /static/... URL, which is exactly what made
        this feature look completely broken even though the code was
        correct."""
        response = self.client.get(reverse("albright_reselling_app:dashboard"))

        match = re.search(r'<script src="([^"]*auction_scanner\.js\?v=\d+)"', response.content.decode())
        self.assertIsNotNone(match)


class LedgerTilesDashboardTests(TestCase):
    """The dashboard's top-of-page row of 5 ledger tiles that replaced the
    old research-pipeline summary (Lots Scanned / Flagged 60+ / etc)."""

    def setUp(self):
        self.user = User.objects.create_user(username="tester", password="pw-not-real-12345")
        self.client.login(username="tester", password="pw-not-real-12345")

    def test_old_tiles_no_longer_render(self):
        response = self.client.get(reverse("albright_reselling_app:dashboard"))

        self.assertNotContains(response, "Lots Scanned")
        self.assertNotContains(response, "Flagged 60+")
        self.assertNotContains(response, "Historical Harvested")
        self.assertNotContains(response, "Lots Analyzed")
        self.assertNotContains(response, "In Inventory</span>")
        self.assertNotContains(response, "Sold (All-Time)")
        self.assertNotContains(response, "Avg Sleeper Margin")
        self.assertNotContains(response, "Reconciled Accuracy")
        self.assertNotContains(response, "Top Sleeper Segment")
        self.assertNotContains(response, "Top Flagged Lots")
        self.assertNotContains(response, "Running Background Jobs")
        self.assertNotContains(response, "Ledger Costs This Month")

    def test_new_tiles_render_with_labels(self):
        response = self.client.get(reverse("albright_reselling_app:dashboard"))

        self.assertContains(response, "Profit This Month")
        self.assertContains(response, "Sales This Month")
        self.assertContains(response, "Inventory")
        self.assertContains(response, "Needs Action")
        self.assertContains(response, "My Bids")

    def test_empty_state_renders_without_errors(self):
        response = self.client.get(reverse("albright_reselling_app:dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "avg ROI")  # the "— avg ROI" fallback, not a crash

    def test_profit_tile_numbers_and_colors(self):
        today = timezone.localdate()
        LedgerEntry.objects.create(
            owner=self.user, item="Sold High", cost=Decimal("10.00"), sold_for=Decimal("40.00"),
            status="sold_out", purchase_date=today, sold_date=today,
        )

        response = self.client.get(reverse("albright_reselling_app:dashboard"))

        self.assertContains(response, "+$30.00")
        self.assertContains(response, "All-time: $30.00")

    def test_inventory_tile_counts_unsold_entries(self):
        LedgerEntry.objects.create(
            owner=self.user, item="Holding Item", cost=Decimal("15.00"), status="holding",
            purchase_date=timezone.localdate(),
        )

        response = self.client.get(reverse("albright_reselling_app:dashboard"))

        self.assertContains(response, "$15.00 at cost")

    def test_my_bids_tile_links_to_my_bids_anchor(self):
        response = self.client.get(reverse("albright_reselling_app:dashboard"))

        self.assertContains(response, f'href="{reverse("albright_reselling_app:dashboard")}#my-bids"')

    def test_inventory_tile_links_to_unsold_ledger_filter(self):
        response = self.client.get(reverse("albright_reselling_app:dashboard"))

        self.assertContains(response, reverse("albright_reselling_app:ledger") + "?status=unsold")

    def test_profit_and_sales_tiles_link_to_scorecard(self):
        response = self.client.get(reverse("albright_reselling_app:dashboard"))

        self.assertContains(response, reverse("albright_reselling_app:ledger_scorecard"))

    def test_needs_action_tile_links_to_unsold_ledger_when_markdowns_pending(self):
        LedgerEntry.objects.create(
            owner=self.user, item="Aged Item", cost=Decimal("10.00"), status="holding",
            purchase_date=timezone.localdate() - timedelta(days=16),
        )

        response = self.client.get(reverse("albright_reselling_app:dashboard"))

        self.assertContains(response, "1 markdown")

    def test_needs_action_tile_shows_nothing_pending_when_empty(self):
        response = self.client.get(reverse("albright_reselling_app:dashboard"))

        self.assertContains(response, "Nothing pending")

    def test_my_bids_watching_count_renders(self):
        now = timezone.now()
        lot = SourcedLot.objects.create(
            source="shopgoodwill", external_id="dash-watch-1", url="https://example.com/dash-watch-1",
            title="Watched Lot", current_price=Decimal("10"), end_time=now + timedelta(hours=5),
        )
        BidWatch.objects.create(lot=lot, my_max_bid=Decimal("20.00"), status="watching")

        response = self.client.get(reverse("albright_reselling_app:dashboard"))
        self.assertContains(response, 'id="my-bids"')
