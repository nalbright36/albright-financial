"""Tests for the dashboard's Auction Scanner section: the data function
directly (get_scanner_dashboard_context), and the dashboard view end to end
via the test client. No HTTP call to any external site happens here - only
ORM fixtures."""
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from albright_reselling_app.scanner.dashboard import get_scanner_dashboard_context
from albright_reselling_app.scanner_models import AIReview, LotEvaluation, ScanRun, SourcedLot, SpotPrice


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

    def test_stale_run_not_flagged_for_maxsold(self):
        run = ScanRun.objects.create(source="maxsold", lots_seen=5, candidates=1)
        ScanRun.objects.filter(pk=run.pk).update(started_at=timezone.now() - timedelta(hours=3))

        context = get_scanner_dashboard_context()

        health = context["scanner_health"]["maxsold"]
        self.assertFalse(any("ago" in w for w in health["warnings"]))
        self.assertEqual(health["status"], "ok")

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

    def test_status_bar_shows_error_dot_when_never_run(self):
        response = self.client.get(reverse("albright_reselling_app:dashboard"))

        self.assertContains(response, "status-dot--error")

    def test_status_bar_shows_ok_dot_for_healthy_run(self):
        ScanRun.objects.create(source="shopgoodwill", lots_seen=5, candidates=1)
        ScanRun.objects.create(source="maxsold", lots_seen=5, candidates=1)

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
