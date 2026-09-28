"""Tests for the dashboard's ShopGoodwill Coin Scanner section: the data
function directly (get_scanner_dashboard_context), and the dashboard view
end to end via the test client. No HTTP call to any external site happens
here - only ORM fixtures."""
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from albright_reselling_app.scanner.dashboard import get_scanner_dashboard_context
from albright_reselling_app.scanner_models import LotEvaluation, ScanRun, SourcedLot, SpotPrice


def _make_lot(external_id, end_time, current_price=10.0):
    return SourcedLot.objects.create(
        source="shopgoodwill", external_id=external_id, url=f"https://shopgoodwill.com/item/{external_id}",
        title=f"Morgan Silver Dollar {external_id}", current_price=current_price, end_time=end_time,
    )


def _make_evaluation(lot, is_candidate, max_bid=20.0, headroom=5.0, confidence="high"):
    return LotEvaluation.objects.create(
        lot=lot, is_candidate=is_candidate, max_bid=Decimal(str(max_bid)), headroom=Decimal(str(headroom)),
        confidence=confidence, coin_keys="morgan_dollar", silver_oz=Decimal("0.7734"),
    )


class ScannerDashboardContextTests(TestCase):
    """Unit-level: exercises get_scanner_dashboard_context() directly."""

    def test_empty_state_has_no_data(self):
        context = get_scanner_dashboard_context()

        self.assertEqual(context["live_candidates"], [])
        self.assertEqual(context["closest_calls"], [])
        self.assertEqual(context["recent_runs"], [])
        self.assertIsNone(context["scanner_health"]["last_run"])
        self.assertIn("No scans have run yet", context["scanner_health"]["warnings"][0])
        self.assertIsNone(context["spot_prices"]["silver"])
        self.assertIsNone(context["spot_prices"]["gold"])

    def test_live_candidate_appears(self):
        lot = _make_lot("live-1", timezone.now() + timedelta(hours=3))
        _make_evaluation(lot, is_candidate=True)

        context = get_scanner_dashboard_context()

        self.assertEqual(len(context["live_candidates"]), 1)
        self.assertEqual(context["live_candidates"][0]["title"], lot.title)

    def test_ended_candidate_does_not_appear(self):
        lot = _make_lot("ended-1", timezone.now() - timedelta(hours=1))
        _make_evaluation(lot, is_candidate=True)

        context = get_scanner_dashboard_context()

        self.assertEqual(context["live_candidates"], [])

    def test_stale_run_warning(self):
        run = ScanRun.objects.create(source="shopgoodwill", lots_seen=5, candidates=1)
        ScanRun.objects.filter(pk=run.pk).update(started_at=timezone.now() - timedelta(hours=3))

        context = get_scanner_dashboard_context()

        self.assertTrue(any("ago" in w for w in context["scanner_health"]["warnings"]))

    def test_stale_spot_warning(self):
        price = SpotPrice.objects.create(metal="silver", price_usd=Decimal("30.00"), source="goldapi")
        SpotPrice.objects.filter(pk=price.pk).update(fetched_at=timezone.now() - timedelta(days=10))

        context = get_scanner_dashboard_context()

        self.assertTrue(context["spot_prices"]["silver"]["is_stale"])


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
        self.assertContains(response, "No close calls right now")
        self.assertContains(response, "No runs recorded yet")

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
