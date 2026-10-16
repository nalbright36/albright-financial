"""Tests for "My resale estimate" (LotUserEstimate): saving it from the
Calculator dialog (calculator_views.lot_estimate/clear_lot_estimate),
the server-computed my_max_bid (scanner/simple_calc.py math only),
prefill/reset/clear payloads, the Watchlist's My est./My max columns and
headroom basis, "Use as my max bid", dashboard row notes, Telegram
watchlist alerts, "I won this" as a "manual" prediction, and manual as
its own source on Insights and the Scorecard. No network calls."""
import json
import os
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import dateformat, timezone

from albright_reselling_app import ledger_metrics
from albright_reselling_app.calculator_views import source_defaults
from albright_reselling_app.models import LedgerEntry
from albright_reselling_app.scanner import insights
from albright_reselling_app.scanner.alerts import send_watchlist_alerts
from albright_reselling_app.scanner.dashboard import _watched_lots
from albright_reselling_app.scanner.simple_calc import EBAY_PRESETS, buy_cost, ebay_profit, max_bid_for_total
from albright_reselling_app.scanner_models import (
    AIReview, BidWatch, LotEvaluation, LotUserEstimate, SourcedLot,
)

TELEGRAM_POST = "albright_reselling_app.scanner.alerts.requests.post"
CREDS = {"TELEGRAM_BOT_TOKEN": "test-token", "TELEGRAM_CHAT_ID": "12345"}

BUY = {
    "source": "custom", "bid": "10.00", "premium_pct": "15", "tax_pct": "7", "shipping": "5", "other_fees": "1",
    "tax_on_premium": True, "tax_on_shipping": False,
}
EBAY = {
    "category": "most", "shipping_charged": "0", "shipping_cost": "6", "packaging": "1", "other_costs": "0",
    "buyer_tax_pct": "7", "promoted_pct": "0", "target_profit": "20",
}
# By hand: eBay fee base 100 * 1.07 = 107 -> 13.6% = 14.552 + $0.40 per-order
# = 14.952 in fees; 100 - 14.952 - 6 shipping - 1 packaging = 78.048 left
# before paying for the lot; less the $20 target = 58.048 out-the-door
# budget; minus $6 fixed (shipping + other fees), / 1.2305 per bid dollar
# (1 + 15% premium + 7% tax on bid and premium) = 42.29.
EXPECTED_MAX = Decimal("42.29")


def _make_lot(external_id, current_price="10.00", end_in=timedelta(hours=5), source="shopgoodwill"):
    return SourcedLot.objects.create(
        source=source, external_id=external_id, url=f"https://example.com/{external_id}",
        title=f"Estimate Lot {external_id}", current_price=Decimal(current_price),
        end_time=timezone.now() + end_in, raw={},
    )


def _make_evaluation(lot, max_bid="30.00", expected_sale="50.00", is_candidate=True):
    return LotEvaluation.objects.create(
        lot=lot, category="coins", is_candidate=is_candidate, max_bid=Decimal(max_bid),
        headroom=Decimal(max_bid) - lot.current_price, confidence="high", expected_sale=Decimal(expected_sale),
    )


def _make_estimate(lot, resale_estimate="100.00", my_max_bid="42.29"):
    return LotUserEstimate.objects.create(
        lot=lot, resale_estimate=Decimal(resale_estimate), my_max_bid=Decimal(my_max_bid),
        calc_inputs={"buy": BUY, "ebay": EBAY},
    )


class LoggedInTestCase(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="tester", password="pw-not-real-12345")
        self.client.login(username="tester", password="pw-not-real-12345")

    def post_json(self, url, data):
        return self.client.post(url, data=json.dumps(data), content_type="application/json")

    def save_estimate(self, lot, **extra):
        data = {"resale_estimate": "100", "notes": "Clean, boxed", "buy": BUY, "ebay": EBAY, **extra}
        return self.post_json(reverse("albright_reselling_app:lot_estimate", args=[lot.pk]), data)


class SaveEstimateTests(LoggedInTestCase):
    def test_save_computes_and_stores_my_max_bid(self):
        lot = _make_lot("save-1")

        response = self.save_estimate(lot)

        self.assertEqual(response.status_code, 200)
        estimate = LotUserEstimate.objects.get(lot=lot)
        self.assertEqual(estimate.resale_estimate, Decimal("100.00"))
        self.assertEqual(estimate.my_max_bid, EXPECTED_MAX)
        self.assertEqual(response.json()["saved"]["my_max_bid"], float(EXPECTED_MAX))

    def test_my_max_bid_is_the_highest_bid_that_hits_target_profit(self):
        """Checked straight against simple_calc: bidding My max leaves at
        least the $20 target after every buy/sell cost; one cent more doesn't."""
        lot = _make_lot("save-2")
        self.save_estimate(lot)
        max_bid = float(LotUserEstimate.objects.get(lot=lot).my_max_bid)

        def profit_at(bid):
            total = buy_cost(bid, premium_pct=15, tax_pct=7, shipping=5, other_fees=1).total
            return ebay_profit(total, 100, shipping_cost=6, packaging=1, tiers=EBAY_PRESETS["most"]["tiers"],
                               buyer_tax_pct=7).profit

        self.assertGreaterEqual(profit_at(max_bid), 20)
        # +2 cents, not +1: max_bid_for_total() stays strictly under the
        # unrounded budget, so one cent over can still round to $20.00.
        self.assertLess(profit_at(max_bid + 0.02), 20)

    def test_max_bid_is_computed_server_side_not_trusted_from_browser(self):
        lot = _make_lot("save-3")

        self.save_estimate(lot, my_max_bid="999.00")

        self.assertEqual(LotUserEstimate.objects.get(lot=lot).my_max_bid, EXPECTED_MAX)

    def test_stores_calc_inputs_and_notes_dropping_unknown_keys(self):
        lot = _make_lot("save-4")

        self.save_estimate(lot, buy={**BUY, "tab": "buy", "budget": "50", "junk": 1}, ebay={**EBAY, "tab": "ebay"})

        estimate = LotUserEstimate.objects.get(lot=lot)
        self.assertEqual(estimate.notes, "Clean, boxed")
        self.assertEqual(estimate.calc_inputs["buy"], BUY)
        self.assertEqual(estimate.calc_inputs["ebay"], EBAY)

    def test_resave_updates_the_one_estimate(self):
        lot = _make_lot("save-5")
        self.save_estimate(lot)

        self.save_estimate(lot, resale_estimate="150", ebay={**EBAY, "target_profit": "30"})

        self.assertEqual(LotUserEstimate.objects.filter(lot=lot).count(), 1)
        estimate = LotUserEstimate.objects.get(lot=lot)
        self.assertEqual(estimate.resale_estimate, Decimal("150.00"))
        self.assertGreater(estimate.my_max_bid, EXPECTED_MAX)

    def test_rejects_missing_zero_or_garbage_estimate(self):
        lot = _make_lot("save-6")
        for bad in ("", "0", "-5", "abc", "NaN", "Infinity", None):
            with self.subTest(bad=bad):
                response = self.save_estimate(lot, resale_estimate=bad)
                self.assertEqual(response.status_code, 400)
        self.assertFalse(LotUserEstimate.objects.filter(lot=lot).exists())

    def test_huge_budget_shortfall_floors_max_bid_at_zero(self):
        lot = _make_lot("save-7")

        self.save_estimate(lot, resale_estimate="5")

        self.assertEqual(LotUserEstimate.objects.get(lot=lot).my_max_bid, Decimal("0.00"))

    def test_live_preview_matches_saved_value(self):
        response = self.post_json(reverse("albright_reselling_app:calculator_calculate"), {
            "tab": "estimate", "resale_estimate": "100", "buy": BUY, "ebay": EBAY,
        })

        self.assertEqual(response.json()["my_max_bid"], float(EXPECTED_MAX))

    def test_requires_login(self):
        lot = _make_lot("save-8")
        self.client.logout()

        response = self.save_estimate(lot)

        self.assertEqual(response.status_code, 302)
        self.assertFalse(LotUserEstimate.objects.exists())


class PrefillResetClearTests(LoggedInTestCase):
    def test_reopen_returns_saved_values_for_prefill(self):
        lot = _make_lot("pre-1")
        _make_evaluation(lot, expected_sale="50.00")
        self.save_estimate(lot)

        data = self.client.get(reverse("albright_reselling_app:lot_estimate", args=[lot.pk])).json()

        saved = data["saved"]
        self.assertEqual(saved["resale_estimate"], 100.0)
        self.assertEqual(saved["my_max_bid"], float(EXPECTED_MAX))
        self.assertEqual(saved["notes"], "Clean, boxed")
        self.assertEqual(saved["calc_inputs"], {"buy": BUY, "ebay": EBAY})
        updated = timezone.localtime(LotUserEstimate.objects.get(lot=lot).updated_at)
        self.assertEqual(saved["updated_date"], dateformat.format(updated, "M j, Y"))

    def test_no_saved_estimate_returns_null(self):
        lot = _make_lot("pre-2")

        data = self.client.get(reverse("albright_reselling_app:lot_estimate", args=[lot.pk])).json()

        self.assertIsNone(data["saved"])

    def test_reset_values_are_the_scanners_untouched_by_the_saved_estimate(self):
        """"Reset to scanner values" refills from the response's "scanner"
        block - the source's own defaults and the scanner's expected sale,
        never the saved estimate's 15% premium / $100."""
        lot = _make_lot("pre-3", current_price="12.00")
        _make_evaluation(lot, expected_sale="50.00")
        self.save_estimate(lot)

        scanner = self.client.get(reverse("albright_reselling_app:lot_estimate", args=[lot.pk])).json()["scanner"]

        defaults = source_defaults("shopgoodwill")
        self.assertEqual(scanner["bid"], 12.0)
        self.assertEqual(scanner["source"], "shopgoodwill")
        self.assertEqual(scanner["premium_pct"], defaults["premium_pct"])
        self.assertEqual(scanner["tax_pct"], defaults["tax_pct"])
        self.assertEqual(scanner["shipping"], defaults["shipping"])
        self.assertEqual(scanner["expected_sale"], 50.0)

    def test_reset_does_not_delete_the_saved_estimate(self):
        lot = _make_lot("pre-4")
        self.save_estimate(lot)

        self.client.get(reverse("albright_reselling_app:lot_estimate", args=[lot.pk]))

        self.assertTrue(LotUserEstimate.objects.filter(lot=lot).exists())

    def test_clear_deletes_estimate(self):
        lot = _make_lot("pre-5")
        self.save_estimate(lot)

        response = self.client.post(reverse("albright_reselling_app:clear_lot_estimate", args=[lot.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json()["saved"])
        self.assertFalse(LotUserEstimate.objects.filter(lot=lot).exists())
        data = self.client.get(reverse("albright_reselling_app:lot_estimate", args=[lot.pk])).json()
        self.assertIsNone(data["saved"])

    def test_clear_requires_post(self):
        lot = _make_lot("pre-6")
        self.save_estimate(lot)

        response = self.client.get(reverse("albright_reselling_app:clear_lot_estimate", args=[lot.pk]))

        self.assertEqual(response.status_code, 405)
        self.assertTrue(LotUserEstimate.objects.filter(lot=lot).exists())

    def test_dialog_has_estimate_panel_and_calc_buttons_point_at_lot(self):
        lot = _make_lot("pre-7")
        _make_evaluation(lot)

        response = self.client.get(reverse("albright_reselling_app:dashboard"))

        self.assertContains(response, 'id="calc-estimate"')
        self.assertContains(response, 'id="calc-estimate-save"')
        self.assertContains(response, 'id="calc-estimate-clear"')
        self.assertContains(response, 'id="calc-estimate-reset"')
        self.assertContains(
            response, f'data-estimate-url="{reverse("albright_reselling_app:lot_estimate", args=[lot.pk])}"',
        )


class WatchlistTests(LoggedInTestCase):
    def test_headroom_uses_my_max_when_present(self):
        lot = _make_lot("wl-1", current_price="10.00")
        _make_evaluation(lot, max_bid="30.00")
        _make_estimate(lot, my_max_bid="42.29")
        BidWatch.objects.create(lot=lot, status="watching")

        row = _watched_lots(timezone.now())[0]

        self.assertEqual(row["headroom"], Decimal("32.29"))
        self.assertEqual(row["headroom_basis"], "mine")
        self.assertEqual(row["my_estimate"].resale_estimate, Decimal("100.00"))

    def test_headroom_falls_back_to_scanner_max(self):
        lot = _make_lot("wl-2", current_price="10.00")
        _make_evaluation(lot, max_bid="30.00")
        BidWatch.objects.create(lot=lot, status="watching")

        row = _watched_lots(timezone.now())[0]

        self.assertEqual(row["headroom"], Decimal("20.00"))
        self.assertEqual(row["headroom_basis"], "scanner")
        self.assertIsNone(row["my_estimate"])

    def test_no_max_anywhere_means_no_headroom(self):
        lot = _make_lot("wl-3")
        BidWatch.objects.create(lot=lot, status="watching")

        row = _watched_lots(timezone.now())[0]

        self.assertIsNone(row["headroom"])
        self.assertIsNone(row["headroom_basis"])

    def test_section_renders_my_columns_and_mine_marker(self):
        lot = _make_lot("wl-4")
        _make_evaluation(lot)
        _make_estimate(lot)
        BidWatch.objects.create(lot=lot, status="watching")

        response = self.client.get(reverse("albright_reselling_app:watchlist_section"))

        self.assertContains(response, "My est.</th>", html=False)
        self.assertContains(response, "My max</th>", html=False)
        self.assertContains(response, 'data-myest="100.00"')
        self.assertContains(response, 'data-mymax="42.29"')
        self.assertContains(response, ">mine</span>")
        self.assertContains(response, "$32.29")

    def test_no_mine_marker_on_scanner_headroom(self):
        lot = _make_lot("wl-5")
        _make_evaluation(lot)
        BidWatch.objects.create(lot=lot, status="watching")

        response = self.client.get(reverse("albright_reselling_app:watchlist_section"))

        self.assertNotContains(response, ">mine</span>")

    def test_use_as_my_max_offered_only_when_watch_has_no_max(self):
        lot = _make_lot("wl-6")
        _make_estimate(lot)
        watch = BidWatch.objects.create(lot=lot, status="watching")
        use_url = reverse("albright_reselling_app:use_my_max_bid", args=[lot.pk])

        self.assertContains(self.client.get(reverse("albright_reselling_app:watchlist_section")), use_url)

        watch.my_max_bid = Decimal("25.00")
        watch.save()
        self.assertNotContains(self.client.get(reverse("albright_reselling_app:watchlist_section")), use_url)

    def test_use_as_my_max_copies_my_max_bid(self):
        lot = _make_lot("wl-7")
        _make_estimate(lot, my_max_bid="42.29")
        watch = BidWatch.objects.create(lot=lot, status="watching")

        response = self.client.post(reverse("albright_reselling_app:use_my_max_bid", args=[lot.pk]))

        self.assertRedirects(response, reverse("albright_reselling_app:dashboard"))
        watch.refresh_from_db()
        self.assertEqual(watch.my_max_bid, Decimal("42.29"))

    def test_use_as_my_max_json_and_never_overwrites(self):
        lot = _make_lot("wl-8")
        _make_estimate(lot, my_max_bid="42.29")
        watch = BidWatch.objects.create(lot=lot, status="watching", my_max_bid=Decimal("25.00"))

        response = self.client.post(
            reverse("albright_reselling_app:use_my_max_bid", args=[lot.pk]), HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(response.status_code, 400)
        watch.refresh_from_db()
        self.assertEqual(watch.my_max_bid, Decimal("25.00"))

    def test_use_as_my_max_without_estimate_or_watch(self):
        lot = _make_lot("wl-9")
        url = reverse("albright_reselling_app:use_my_max_bid", args=[lot.pk])
        self.assertEqual(self.client.post(url, HTTP_X_REQUESTED_WITH="XMLHttpRequest").status_code, 400)
        _make_estimate(lot)
        self.assertEqual(self.client.post(url, HTTP_X_REQUESTED_WITH="XMLHttpRequest").status_code, 400)

    def test_dialog_payload_reports_watch_state(self):
        lot = _make_lot("wl-10")
        BidWatch.objects.create(lot=lot, status="watching")

        watch = self.save_estimate(lot).json()["watch"]

        self.assertTrue(watch["watching"])
        self.assertIsNone(watch["watch_max_bid"])
        self.assertEqual(watch["use_my_max_url"], reverse("albright_reselling_app:use_my_max_bid", args=[lot.pk]))


class DashboardNoteTests(LoggedInTestCase):
    def test_candidate_row_shows_my_estimate_under_title(self):
        lot = _make_lot("dash-1")
        _make_evaluation(lot)
        _make_estimate(lot)

        response = self.client.get(reverse("albright_reselling_app:dashboard"))

        # Once under the title, once in the expandable detail row.
        self.assertContains(response, "My est. $100.00 &middot; My max $42.29", count=2)

    def test_note_hidden_when_no_estimate(self):
        lot = _make_lot("dash-2")
        _make_evaluation(lot)

        response = self.client.get(reverse("albright_reselling_app:dashboard"))

        self.assertNotContains(response, "My est. $")
        self.assertContains(response, f'class="js-my-est" data-lot-id="{lot.pk}"')


class WatchlistAlertTests(TestCase):
    @mock.patch(TELEGRAM_POST)
    @mock.patch.dict(os.environ, CREDS)
    def test_alert_includes_my_estimate_max_and_headroom(self, mock_post):
        mock_post.return_value = mock.Mock(raise_for_status=mock.Mock(return_value=None))
        lot = _make_lot("alert-1", current_price="10.00", end_in=timedelta(minutes=10))
        _make_evaluation(lot)
        _make_estimate(lot, my_max_bid="42.29")
        BidWatch.objects.create(lot=lot, status="watching")

        send_watchlist_alerts()

        message = mock_post.call_args.kwargs["json"]["text"]
        self.assertIn("My est.: $100.00 · My max: $42.29 · headroom vs my max: $32.29", message)

    @mock.patch(TELEGRAM_POST)
    @mock.patch.dict(os.environ, CREDS)
    def test_alert_omits_my_line_without_estimate(self, mock_post):
        mock_post.return_value = mock.Mock(raise_for_status=mock.Mock(return_value=None))
        lot = _make_lot("alert-2", end_in=timedelta(minutes=10))
        BidWatch.objects.create(lot=lot, status="watching")

        send_watchlist_alerts()

        message = mock_post.call_args.kwargs["json"]["text"]
        self.assertNotIn("My est.", message)


class WinLotManualPredictionTests(LoggedInTestCase):
    WIN_POST = {
        "category": "coins", "cost": "12.00", "buyer_premium": "0", "sales_tax": "0", "buy_fees": "0",
        "inbound_shipping": "0", "purchase_hours": "0", "source": "shopgoodwill",
    }

    def _win(self, lot, query=""):
        return self.client.post(
            reverse("albright_reselling_app:ledger_win_lot", args=[lot.pk]) + query,
            {**self.WIN_POST, "item": lot.title, "listing_url": lot.url,
             "purchase_date": timezone.localdate().isoformat()},
        )

    def test_won_lot_uses_manual_estimate_over_ai_review_and_scanner(self):
        lot = _make_lot("win-1")
        _make_evaluation(lot, expected_sale="50.00")
        review = AIReview.objects.create(
            lot=lot, status="done", resale_low=Decimal("60.00"), resale_high=Decimal("80.00"),
            cost_usd=Decimal("0.05"),
        )
        _make_estimate(lot, resale_estimate="100.00")

        response = self._win(lot, f"?review={review.pk}")

        self.assertRedirects(response, reverse("albright_reselling_app:ledger_resale"))
        entry = LedgerEntry.objects.get(scanner_lot=lot)
        self.assertEqual(entry.predicted_sale_low, Decimal("100.00"))
        self.assertEqual(entry.predicted_sale_high, Decimal("100.00"))
        self.assertEqual(entry.predicted_sale_source, "manual")
        self.assertEqual(entry.get_predicted_sale_source_display(), "Manual")

    def test_without_estimate_falls_back_to_scanner(self):
        lot = _make_lot("win-2")
        _make_evaluation(lot, expected_sale="50.00")

        self._win(lot)

        self.assertEqual(LedgerEntry.objects.get(scanner_lot=lot).predicted_sale_source, "scanner")

    def test_form_says_it_will_use_saved_estimate(self):
        lot = _make_lot("win-3")
        _make_estimate(lot)

        response = self.client.get(reverse("albright_reselling_app:ledger_win_lot", args=[lot.pk]))

        self.assertContains(response, "Prediction will use your saved estimate ($100.00)")


class ManualAccuracyTests(LoggedInTestCase):
    def _sold_entry(self, source, predicted, sold_for):
        return LedgerEntry.objects.create(
            owner=self.user, item=f"{source} item", cost=Decimal("10.00"), status="sold_out",
            sold_for=Decimal(sold_for), sold_date=timezone.localdate(),
            predicted_sale_low=Decimal(predicted), predicted_sale_high=Decimal(predicted),
            predicted_sale_source=source,
        )

    def test_insights_reports_manual_separately(self):
        self._sold_entry("manual", "100.00", "110.00")   # +10%
        self._sold_entry("manual", "100.00", "70.00")    # -30%
        self._sold_entry("scanner", "50.00", "100.00")   # +100%

        rows = {row["source"]: row for row in insights.prediction_accuracy_by_source()}

        self.assertEqual(set(rows), {"scanner", "ai_review", "manual"})
        self.assertEqual(rows["manual"]["label"], "Manual")
        self.assertEqual(rows["manual"]["count"], 2)
        self.assertAlmostEqual(rows["manual"]["median_error_pct"], -10.0)
        self.assertAlmostEqual(rows["manual"]["median_abs_error_pct"], 20.0)
        self.assertEqual(rows["scanner"]["count"], 1)
        self.assertAlmostEqual(rows["scanner"]["median_error_pct"], 100.0)
        self.assertEqual(rows["ai_review"]["count"], 0)
        self.assertIsNone(rows["ai_review"]["median_error_pct"])

    def test_unsold_and_written_off_entries_excluded(self):
        LedgerEntry.objects.create(
            owner=self.user, item="Holding", cost=Decimal("10.00"), status="holding",
            predicted_sale_low=Decimal("100"), predicted_sale_high=Decimal("100"), predicted_sale_source="manual",
        )
        LedgerEntry.objects.create(
            owner=self.user, item="Gone", cost=Decimal("10.00"), status="written_off",
            predicted_sale_low=Decimal("100"), predicted_sale_high=Decimal("100"), predicted_sale_source="manual",
        )

        rows = {row["source"]: row for row in insights.prediction_accuracy_by_source()}

        self.assertEqual(rows["manual"]["count"], 0)

    def test_insights_page_shows_manual_row(self):
        self._sold_entry("manual", "100.00", "110.00")

        response = self.client.get(reverse("albright_reselling_app:insights"))

        self.assertContains(response, "Prediction Accuracy by Source")
        self.assertContains(response, 'data-prediction-source="manual"')
        self.assertContains(response, "<td>Manual</td>", html=False)

    def test_scorecard_groups_manual_prediction_error_separately(self):
        self._sold_entry("manual", "100.00", "110.00")
        self._sold_entry("scanner", "50.00", "100.00")

        groups = {row["label"]: row for row in ledger_metrics.scorecard_context(self.user)["by_prediction_source"]}

        self.assertIn("Manual", groups)
        self.assertIn("Scanner", groups)
        self.assertAlmostEqual(groups["Manual"]["median_prediction_error_pct"], 10.0)
        self.assertAlmostEqual(groups["Scanner"]["median_prediction_error_pct"], 100.0)
