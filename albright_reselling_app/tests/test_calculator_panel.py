"""Tests for the dashboard/Today pages' slide-in Calculator panel
(_calculator_panel.html, calculator_views.panel_context(), static/js/
calculator.js): the panel markup itself renders on both pages, the
"Calc" button's prefill data attributes on Act Now/Watch/My Bids rows
carry the right values, and the full Calculators page (which shares the
same _calculator_tabs.html partial) still renders and calculates -
beyond what test_bid_calculator_views.py already covers in detail."""
import json
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from albright_reselling_app.scanner_models import BidWatch, LotEvaluation, SourcedLot


def _make_lot(external_id, end_time, current_price="10.00", source="shopgoodwill"):
    return SourcedLot.objects.create(
        source=source, external_id=external_id, url=f"https://example.com/{external_id}",
        title=f"Panel Test Lot {external_id}", current_price=Decimal(current_price), end_time=end_time,
    )


def _make_evaluation(lot, is_candidate=True, max_bid="30.00", headroom="20.00", confidence="high",
                      expected_sale="50.00", category="coins"):
    return LotEvaluation.objects.create(
        lot=lot, category=category, is_candidate=is_candidate, max_bid=Decimal(max_bid),
        headroom=Decimal(headroom), confidence=confidence, expected_sale=Decimal(expected_sale),
    )


def _make_lead(lot, category="games", lead_reason="n64: bulk lot", expected_sale="0"):
    return LotEvaluation.objects.create(
        lot=lot, category=category, is_lead=True, lead_reason=lead_reason, expected_sale=Decimal(expected_sale),
    )


class PanelMarkupTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="tester", password="pw-not-real-12345")
        self.client.login(username="tester", password="pw-not-real-12345")

    def test_panel_renders_on_dashboard(self):
        response = self.client.get(reverse("albright_reselling_app:dashboard"))
        content = response.content.decode()

        self.assertContains(response, 'id="calc-panel-toggle"')
        self.assertContains(response, 'id="calc-panel"')
        self.assertContains(response, 'id="calc-panel-close"')
        self.assertContains(response, 'id="calc-tabs"')
        self.assertContains(response, 'id="calc-data"')
        self.assertIn("calculator.js", content)

    def test_panel_renders_on_today_page(self):
        response = self.client.get(reverse("albright_reselling_app:today"))

        self.assertContains(response, 'id="calc-panel-toggle"')
        self.assertContains(response, 'id="calc-panel"')
        self.assertContains(response, 'id="calc-tabs"')
        self.assertContains(response, 'id="calc-data"')

    def test_panel_not_present_on_full_calculators_page(self):
        response = self.client.get(reverse("albright_reselling_app:calculator"))

        self.assertNotContains(response, 'id="calc-panel-toggle"')
        self.assertNotContains(response, 'id="calc-panel"')
        self.assertContains(response, 'id="calc-tabs"')

    def test_panel_calc_data_has_no_lot_prefill(self):
        """Unlike the full page's ?lot= GET flow, the panel's prefill is
        entirely client-side (the "Calc" button's own data attributes) -
        its server-rendered calc-data always has an empty lotPrefill."""
        response = self.client.get(reverse("albright_reselling_app:dashboard"))
        content = response.content.decode()

        start = content.index('id="calc-data">') + len('id="calc-data">')
        end = content.index("</script>", start)
        calc_data = json.loads(content[start:end])

        self.assertEqual(calc_data["lotPrefill"], {})


class CalcButtonPrefillDataTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="tester", password="pw-not-real-12345")
        self.client.login(username="tester", password="pw-not-real-12345")

    def test_candidate_row_carries_prefill_attributes(self):
        lot = _make_lot("calc-cand-1", timezone.now() + timedelta(hours=3), current_price="22.50")
        _make_evaluation(lot, is_candidate=True, expected_sale="75.00")

        response = self.client.get(reverse("albright_reselling_app:dashboard"))
        content = response.content.decode()

        self.assertIn(f'data-lot-id="{lot.pk}"', content)
        self.assertIn('data-title="Panel Test Lot calc-cand-1"', content)
        self.assertIn('data-bid="22.50"', content)
        self.assertIn('data-source="shopgoodwill"', content)
        self.assertIn('data-expected-sale="75.00"', content)

    def test_check_by_hand_row_carries_prefill_attributes(self):
        lot = _make_lot("calc-cbh-1", timezone.now() + timedelta(hours=3), current_price="5.00")
        _make_evaluation(lot, is_candidate=False, confidence="low", max_bid="20.00", headroom="15.00",
                          expected_sale="40.00")

        response = self.client.get(reverse("albright_reselling_app:dashboard"))
        content = response.content.decode()

        self.assertIn('data-bid="5.00"', content)
        self.assertIn('data-expected-sale="40.00"', content)

    def test_lead_row_carries_prefill_attributes(self):
        lot = _make_lot("calc-lead-1", timezone.now() + timedelta(hours=3), current_price="8.00")
        _make_lead(lot)

        response = self.client.get(reverse("albright_reselling_app:dashboard"))
        content = response.content.decode()

        self.assertIn(f'data-lot-id="{lot.pk}"', content)
        self.assertIn('data-bid="8.00"', content)

    def test_candidates_ending_later_row_carries_prefill_attributes(self):
        lot = _make_lot("calc-later-1", timezone.now() + timedelta(days=2), current_price="12.00")
        _make_evaluation(lot, is_candidate=True, expected_sale="33.00")

        response = self.client.get(reverse("albright_reselling_app:dashboard"))
        content = response.content.decode()

        watch_section = content.split('id="watch"')[1].split('id="performance"')[0]
        self.assertIn('data-bid="12.00"', watch_section)
        self.assertIn('data-expected-sale="33.00"', watch_section)

    def test_closest_calls_row_carries_prefill_attributes(self):
        lot = _make_lot("calc-closest-1", timezone.now() + timedelta(hours=3), current_price="45.00")
        _make_evaluation(lot, is_candidate=False, max_bid="20.00", headroom="-25.00", expected_sale="55.00")

        response = self.client.get(reverse("albright_reselling_app:dashboard"))
        content = response.content.decode()

        watch_section = content.split('id="watch"')[1].split('id="performance"')[0]
        self.assertIn('data-bid="45.00"', watch_section)
        self.assertIn('data-expected-sale="55.00"', watch_section)

    def test_my_bids_row_carries_prefill_attributes(self):
        lot = _make_lot("calc-watch-1", timezone.now() + timedelta(hours=3), current_price="18.00")
        _make_evaluation(lot, expected_sale="60.00")
        BidWatch.objects.create(lot=lot, my_max_bid=Decimal("25.00"), status="watching")

        response = self.client.get(reverse("albright_reselling_app:dashboard"))
        content = response.content.decode()

        my_bids_section = content.split('id="my-bids"')[1].split('<div class="tab-panel" id="watch"')[0]
        self.assertIn('data-bid="18.00"', my_bids_section)
        self.assertIn('data-expected-sale="60.00"', my_bids_section)

    def test_calc_button_absent_from_results_table(self):
        """Results/Performance is explicitly out of scope for the Calc
        button."""
        lot = _make_lot("calc-results-1", timezone.now() - timedelta(days=1), current_price="10.00")
        lot.is_closed = True
        lot.final_price = Decimal("15.00")
        lot.save()
        LotEvaluation.objects.create(lot=lot, category="coins", max_bid=Decimal("20.00"))

        response = self.client.get(reverse("albright_reselling_app:dashboard"))
        content = response.content.decode()

        performance_section = content.split('id="performance"')[1]
        results_section = performance_section.split('id="table-results"')[1].split("</table>")[0]
        self.assertNotIn("js-calc-from-lot", results_section)

class FullCalculatorPageStillWorksTests(TestCase):
    """Confirms the shared-partial/shared-JS refactor didn't break the
    full Calculators page - test_bid_calculator_views.py covers the
    calculation math itself in depth; this just checks the page/endpoint
    still work end to end through the new shared plumbing."""

    def setUp(self):
        self.user = User.objects.create_user(username="tester", password="pw-not-real-12345")
        self.client.login(username="tester", password="pw-not-real-12345")

    def test_calculators_page_renders_all_three_tabs(self):
        response = self.client.get(reverse("albright_reselling_app:calculator"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Buy cost")
        self.assertContains(response, "eBay profit")
        self.assertContains(response, "Melt value")

    def test_calculate_api_still_computes(self):
        response = self.client.post(
            reverse("albright_reselling_app:calculator_calculate"),
            data=json.dumps({"tab": "buy", "bid": "100", "premium_pct": "18", "tax_pct": "7", "shipping": "10"}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertAlmostEqual(response.json()["total"], 136.26, places=2)
