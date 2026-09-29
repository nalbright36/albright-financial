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
from albright_reselling_app.scanner_models import LotEvaluation, ScanRun, SourcedLot, SpotPrice


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

        self.assertTrue(any("ago" in w for w in context["scanner_health"]["shopgoodwill"]["warnings"]))

    def test_stale_run_not_flagged_for_maxsold(self):
        run = ScanRun.objects.create(source="maxsold", lots_seen=5, candidates=1)
        ScanRun.objects.filter(pk=run.pk).update(started_at=timezone.now() - timedelta(hours=3))

        context = get_scanner_dashboard_context()

        self.assertFalse(any("ago" in w for w in context["scanner_health"]["maxsold"]["warnings"]))

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


class DashboardViewTests(TestCase):
    """Integration-level: hits the actual dashboard view/template."""

    def setUp(self):
        self.user = User.objects.create_user(username="tester", password="pw-not-real-12345")
        self.client.login(username="tester", password="pw-not-real-12345")

    def test_dashboard_loads_with_no_scanner_data(self):
        response = self.client.get(reverse("albright_reselling_app:dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "No scans have run yet")
        self.assertContains(response, "No live candidates right now")
        self.assertContains(response, "No leads ending soon")
        self.assertContains(response, "No close calls right now")
        self.assertContains(response, "No runs recorded yet")
        self.assertContains(response, "No MaxSold candidates right now")

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
