"""Tests for the Ledger <-> scanner integration: LedgerEntry/LedgerSale
model math, the "I won this" flow, sale recording, status actions, legacy
cost splitting, Scorecard grouping/aging, and the dashboard's Realized
block. No network calls anywhere - pure ORM fixtures."""
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from albright_reselling_app import ledger_metrics
from albright_reselling_app.models import LedgerEntry, LedgerSale
from albright_reselling_app.scanner_models import AIReview, BidWatch, LotEvaluation, SourcedLot


def _make_lot(external_id, source="shopgoodwill", title="Morgan Silver Dollar", current_price="10.00",
              final_price=None, raw=None):
    return SourcedLot.objects.create(
        source=source, external_id=external_id, url=f"https://example.com/{external_id}",
        title=title, current_price=Decimal(current_price), end_time=timezone.now() + timedelta(hours=5),
        final_price=Decimal(str(final_price)) if final_price is not None else None,
        is_closed=final_price is not None, raw=raw or {},
    )


def _make_evaluation(lot, category="coins", max_bid="20.00", melt_value="18.00", expected_sale="19.00"):
    return LotEvaluation.objects.create(
        lot=lot, category=category, max_bid=Decimal(max_bid), melt_value=Decimal(melt_value),
        expected_sale=Decimal(expected_sale),
    )


def _make_review(lot, resale_low="25.00", resale_high="35.00", suggested_max_bid="22.00"):
    return AIReview.objects.create(
        lot=lot, status="done", resale_low=Decimal(resale_low), resale_high=Decimal(resale_high),
        suggested_max_bid=Decimal(suggested_max_bid), confidence="high", cost_usd=Decimal("0.05"),
    )


def _make_entry(owner, item="Test Item", cost="10.00", status="holding", purchase_date=None, **extra):
    entry = LedgerEntry.objects.create(
        owner=owner, item=item, cost=Decimal(cost), status=status,
        purchase_date=purchase_date or timezone.localdate(), **extra,
    )
    # .create() doesn't coerce string kwargs (e.g. sold_for="40.00") to
    # Decimal in memory the way a DB round-trip does - refetch so every
    # field has its real Python type, matching what every other code path
    # (forms, views re-fetching by pk) actually sees.
    entry.refresh_from_db()
    return entry


class LegacyProfitUnchangedTests(TestCase):
    """An entry shaped exactly like one created before this feature (only
    the original fields touched) must compute the exact same total/profit
    it always did, even though those properties were extended."""

    def setUp(self):
        self.owner = User.objects.create_user(username="tester", password="pw-not-real-12345")

    def test_unsold_legacy_row_profit_unchanged(self):
        entry = LedgerEntry.objects.create(
            owner=self.owner, item="Old Item", cost=Decimal("50.00"), fees=Decimal("5.00"),
            shipping=Decimal("3.00"),
        )

        self.assertEqual(entry.total, Decimal("58.00"))
        self.assertEqual(entry.profit, Decimal("-58.00"))

    def test_sold_legacy_row_profit_unchanged(self):
        entry = LedgerEntry.objects.create(
            owner=self.owner, item="Old Sold Item", cost=Decimal("50.00"), fees=Decimal("5.00"),
            shipping=Decimal("3.00"), sold_for=Decimal("90.00"), status="sold_out",
        )

        self.assertEqual(entry.total, Decimal("58.00"))
        self.assertEqual(entry.profit, Decimal("32.00"))  # 90 - 58, exactly as the original formula gave

    def test_legacy_row_has_no_scanner_links_and_still_renders(self):
        LedgerEntry.objects.create(owner=self.owner, item="No Scanner Link", cost=Decimal("20.00"))
        self.client.login(username="tester", password="pw-not-real-12345")

        response = self.client.get(reverse("albright_reselling_app:ledger"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "No Scanner Link")


class WinLotFlowTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="tester", password="pw-not-real-12345")
        self.client.login(username="tester", password="pw-not-real-12345")

    def test_prefill_uses_scanner_expected_sale_when_no_review(self):
        lot = _make_lot("win-scanner-1", current_price="12.00")
        _make_evaluation(lot, category="coins", expected_sale="19.00", melt_value="18.00", max_bid="20.00")

        response = self.client.get(reverse("albright_reselling_app:ledger_win_lot", args=[lot.pk]))

        self.assertEqual(response.status_code, 200)
        form = response.context["form"]
        self.assertEqual(form.initial["item"], lot.title)
        self.assertEqual(form.initial["source"], "shopgoodwill")
        self.assertEqual(form.initial["category"], "coins")
        self.assertEqual(float(form.initial["cost"]), 12.00)  # no final_price yet -> falls back to current bid

    def test_prefill_uses_final_price_when_tracker_has_it(self):
        lot = _make_lot("win-final-1", current_price="12.00", final_price="15.50")
        _make_evaluation(lot)

        response = self.client.get(reverse("albright_reselling_app:ledger_win_lot", args=[lot.pk]))

        self.assertEqual(float(response.context["form"].initial["cost"]), 15.50)

    def test_prefill_computes_buyer_premium_and_tax_like_max_bid(self):
        lot = _make_lot("win-costs-1", source="maxsold", current_price="100.00")
        _make_evaluation(lot, expected_sale="150.00")

        response = self.client.get(reverse("albright_reselling_app:ledger_win_lot", args=[lot.pk]))

        cfg = settings.RESELLING_SCANNER["SOURCES"]["maxsold"]
        hammer = 100.0
        expected_premium = round(hammer * cfg["buyer_premium_pct"], 2)
        expected_tax = round((hammer + expected_premium) * cfg["sales_tax_pct"], 2)

        initial = response.context["form"].initial
        self.assertAlmostEqual(float(initial["buyer_premium"]), expected_premium, places=2)
        self.assertAlmostEqual(float(initial["sales_tax"]), expected_tax, places=2)

    def test_prefers_ai_review_prediction_over_scanner(self):
        lot = _make_lot("win-ai-1", current_price="20.00")
        _make_evaluation(lot, expected_sale="19.00")
        review = _make_review(lot, resale_low="25.00", resale_high="35.00", suggested_max_bid="22.00")

        response = self.client.get(
            reverse("albright_reselling_app:ledger_win_lot", args=[lot.pk]), {"review": review.pk}
        )

        self.assertEqual(response.context["review"].pk, review.pk)

    def test_post_creates_entry_with_snapshot_from_scanner(self):
        lot = _make_lot("win-post-scanner", current_price="12.00")
        _make_evaluation(lot, category="coins", expected_sale="19.00", melt_value="18.00", max_bid="20.00")

        response = self.client.post(reverse("albright_reselling_app:ledger_win_lot", args=[lot.pk]), {
            "item": lot.title, "purchase_date": timezone.localdate().isoformat(), "source": "shopgoodwill",
            "category": "coins", "listing_url": lot.url, "cost": "12.00", "buyer_premium": "0.00",
            "sales_tax": "0.84", "buy_fees": "0.00", "inbound_shipping": "10.00", "purchase_hours": "0.5",
        })

        self.assertRedirects(response, reverse("albright_reselling_app:ledger"))
        entry = LedgerEntry.objects.get(scanner_lot=lot)
        self.assertEqual(entry.owner, self.user)
        self.assertEqual(entry.predicted_sale_source, "scanner")
        self.assertEqual(entry.predicted_sale_low, Decimal("19.00"))
        self.assertEqual(entry.predicted_sale_high, Decimal("19.00"))
        self.assertEqual(entry.scanner_max_bid, Decimal("20.00"))
        self.assertEqual(entry.melt_value, Decimal("18.00"))
        self.assertIsNone(entry.ai_suggested_max_bid)

    def test_post_creates_entry_with_snapshot_from_ai_review(self):
        lot = _make_lot("win-post-ai", current_price="12.00")
        _make_evaluation(lot, category="coins", expected_sale="19.00")
        review = _make_review(lot, resale_low="25.00", resale_high="35.00", suggested_max_bid="22.00")

        response = self.client.post(
            f"{reverse('albright_reselling_app:ledger_win_lot', args=[lot.pk])}?review={review.pk}",
            {
                "item": lot.title, "purchase_date": timezone.localdate().isoformat(), "source": "shopgoodwill",
                "category": "coins", "listing_url": lot.url, "cost": "12.00", "buyer_premium": "0",
                "sales_tax": "0.84", "buy_fees": "0", "inbound_shipping": "10.00", "purchase_hours": "0",
                "review": str(review.pk),
            },
        )

        self.assertRedirects(response, reverse("albright_reselling_app:ledger"))
        entry = LedgerEntry.objects.get(scanner_lot=lot)
        self.assertEqual(entry.ai_review_id, review.pk)
        self.assertEqual(entry.predicted_sale_source, "ai_review")
        self.assertEqual(entry.predicted_sale_low, Decimal("25.00"))
        self.assertEqual(entry.predicted_sale_high, Decimal("35.00"))
        self.assertEqual(entry.ai_suggested_max_bid, Decimal("22.00"))

    def test_duplicate_lot_shows_warning_not_a_second_form(self):
        lot = _make_lot("win-dup-1")
        _make_evaluation(lot)
        existing = _make_entry(self.user, item="Already Logged", scanner_lot=lot)

        response = self.client.get(reverse("albright_reselling_app:ledger_win_lot", args=[lot.pk]))

        self.assertContains(response, "Already Logged")
        self.assertContains(response, f"#entry-{existing.pk}")
        self.assertNotIn("form", response.context)

    def test_duplicate_warning_bypassed_with_confirm(self):
        lot = _make_lot("win-dup-2")
        _make_evaluation(lot)
        _make_entry(self.user, item="Already Logged 2", scanner_lot=lot)

        response = self.client.get(
            reverse("albright_reselling_app:ledger_win_lot", args=[lot.pk]), {"confirm": "1"}
        )

        self.assertIn("form", response.context)
        self.assertNotContains(response, "You already logged this lot")

    def test_confirmed_post_creates_second_entry(self):
        lot = _make_lot("win-dup-3")
        _make_evaluation(lot)
        _make_entry(self.user, item="First Entry", scanner_lot=lot)

        response = self.client.post(
            f"{reverse('albright_reselling_app:ledger_win_lot', args=[lot.pk])}?confirm=1", {
                "item": "Second Entry", "purchase_date": timezone.localdate().isoformat(), "source": "shopgoodwill",
                "category": "coins", "listing_url": lot.url, "cost": "12.00", "buyer_premium": "0",
                "sales_tax": "0", "buy_fees": "0", "inbound_shipping": "0", "purchase_hours": "0",
                "confirm": "1",
            },
        )

        self.assertRedirects(response, reverse("albright_reselling_app:ledger"))
        self.assertEqual(LedgerEntry.objects.filter(scanner_lot=lot).count(), 2)

    def test_requires_login(self):
        self.client.logout()
        lot = _make_lot("win-nologin")

        response = self.client.get(reverse("albright_reselling_app:ledger_win_lot", args=[lot.pk]))

        self.assertEqual(response.status_code, 302)


class RecordSaleTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="tester", password="pw-not-real-12345")
        self.client.login(username="tester", password="pw-not-real-12345")

    def test_recording_sale_promotes_holding_to_partially_sold(self):
        entry = _make_entry(self.user, status="holding")

        response = self.client.post(reverse("albright_reselling_app:ledger_record_sale", args=[entry.pk]), {
            "sale_date": timezone.localdate().isoformat(), "sale_price": "25.00", "channel": "ebay",
            "selling_fees": "2.00", "outbound_shipping": "5.00", "hours_spent": "1.0", "notes": "",
        })

        self.assertRedirects(response, reverse("albright_reselling_app:ledger_manage", args=[entry.pk]))
        entry.refresh_from_db()
        self.assertEqual(entry.status, "partially_sold")
        self.assertEqual(entry.sales.count(), 1)

    def test_itemized_sales_override_legacy_sold_for(self):
        entry = _make_entry(self.user, cost="10.00", sold_for="999.00")
        LedgerSale.objects.create(entry=entry, sale_date=timezone.localdate(), sale_price=Decimal("30.00"))
        LedgerSale.objects.create(entry=entry, sale_date=timezone.localdate(), sale_price=Decimal("15.00"))

        self.assertEqual(entry.total_realized, Decimal("45.00"))
        self.assertTrue(entry.has_conflicting_sale_data)

    def test_no_itemized_sales_uses_sold_for(self):
        entry = _make_entry(self.user, cost="10.00", sold_for="40.00")

        self.assertEqual(entry.total_realized, Decimal("40.00"))
        self.assertFalse(entry.has_conflicting_sale_data)

    def test_second_sale_does_not_change_status_from_partially_sold(self):
        entry = _make_entry(self.user, status="partially_sold")

        self.client.post(reverse("albright_reselling_app:ledger_record_sale", args=[entry.pk]), {
            "sale_date": timezone.localdate().isoformat(), "sale_price": "10.00", "channel": "other",
            "selling_fees": "0", "outbound_shipping": "0", "hours_spent": "0", "notes": "",
        })

        entry.refresh_from_db()
        self.assertEqual(entry.status, "partially_sold")  # not bumped to sold_out automatically


class SetStatusTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="tester", password="pw-not-real-12345")
        self.client.login(username="tester", password="pw-not-real-12345")

    def test_mark_sold_out(self):
        entry = _make_entry(self.user, status="partially_sold")

        self.client.post(reverse("albright_reselling_app:ledger_set_status", args=[entry.pk]), {"status": "sold_out"})

        entry.refresh_from_db()
        self.assertEqual(entry.status, "sold_out")

    def test_mark_written_off_counts_full_buy_cost_as_loss(self):
        entry = _make_entry(self.user, cost="40.00", buy_fees="5.00")

        self.client.post(
            reverse("albright_reselling_app:ledger_set_status", args=[entry.pk]), {"status": "written_off"}
        )

        entry.refresh_from_db()
        self.assertEqual(entry.status, "written_off")
        self.assertEqual(entry.profit, Decimal("-45.00"))
        self.assertTrue(entry.is_realized)

    def test_invalid_status_ignored(self):
        entry = _make_entry(self.user, status="holding")

        self.client.post(reverse("albright_reselling_app:ledger_set_status", args=[entry.pk]), {"status": "bogus"})

        entry.refresh_from_db()
        self.assertEqual(entry.status, "holding")


class SplitLegacyTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="tester", password="pw-not-real-12345")
        self.client.login(username="tester", password="pw-not-real-12345")

    def test_valid_split_zeroes_legacy_and_fills_new_fields(self):
        entry = _make_entry(self.user, fees="12.00", shipping="8.00")

        self.client.post(reverse("albright_reselling_app:ledger_split_legacy", args=[entry.pk]), {
            "new_buy_fees": "7.00", "new_sell_fees": "5.00",
            "new_inbound_shipping": "8.00", "new_sell_shipping": "0.00",
        })

        entry.refresh_from_db()
        self.assertEqual(entry.fees, Decimal("0"))
        self.assertEqual(entry.shipping, Decimal("0"))
        self.assertEqual(entry.buy_fees, Decimal("7.00"))
        self.assertEqual(entry.sell_fees, Decimal("5.00"))
        self.assertEqual(entry.inbound_shipping, Decimal("8.00"))
        self.assertFalse(entry.has_legacy_amounts)

    def test_mismatched_split_is_rejected(self):
        entry = _make_entry(self.user, fees="12.00")

        self.client.post(reverse("albright_reselling_app:ledger_split_legacy", args=[entry.pk]), {
            "new_buy_fees": "5.00", "new_sell_fees": "5.00",  # sums to 10, not 12
        })

        entry.refresh_from_db()
        self.assertEqual(entry.fees, Decimal("12.00"))  # unchanged - split rejected
        self.assertTrue(entry.has_legacy_amounts)


class MetricsTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="tester", password="pw-not-real-12345")

    def test_roi_pct(self):
        entry = _make_entry(self.user, cost="50.00", sold_for="75.00", status="sold_out")
        self.assertAlmostEqual(entry.roi_pct, 50.0, places=4)  # profit 25 / cost 50

    def test_roi_none_when_no_buy_cost(self):
        entry = _make_entry(self.user, cost="0.00")
        self.assertIsNone(entry.roi_pct)

    def test_days_to_sell_legacy_path(self):
        purchase_date = timezone.localdate() - timedelta(days=10)
        entry = _make_entry(
            self.user, cost="20.00", sold_for="30.00", status="sold_out",
            purchase_date=purchase_date, sold_date=timezone.localdate(),
        )
        self.assertEqual(entry.days_to_sell, 10)

    def test_days_to_sell_none_without_sold_date(self):
        entry = _make_entry(self.user, cost="20.00", sold_for="30.00", status="sold_out")
        self.assertIsNone(entry.days_to_sell)

    def test_days_to_sell_itemized_uses_last_sale_date_when_sold_out(self):
        purchase_date = timezone.localdate() - timedelta(days=20)
        entry = _make_entry(self.user, cost="20.00", status="sold_out", purchase_date=purchase_date)
        LedgerSale.objects.create(entry=entry, sale_date=purchase_date + timedelta(days=5), sale_price=Decimal("10"))
        LedgerSale.objects.create(entry=entry, sale_date=purchase_date + timedelta(days=12), sale_price=Decimal("15"))

        self.assertEqual(entry.days_to_sell, 12)

    def test_days_to_sell_none_while_only_partially_sold(self):
        entry = _make_entry(self.user, cost="20.00", status="partially_sold")
        LedgerSale.objects.create(entry=entry, sale_date=timezone.localdate(), sale_price=Decimal("10"))

        self.assertIsNone(entry.days_to_sell)

    def test_profit_per_hour(self):
        entry = _make_entry(
            self.user, cost="20.00", sold_for="50.00", status="sold_out", purchase_hours=Decimal("2.0"),
        )
        # profit = 50 - 20 = 30; total_hours = 2.0 -> 15/hr
        self.assertAlmostEqual(entry.profit_per_hour, 15.0, places=4)

    def test_profit_per_hour_none_when_zero_hours(self):
        entry = _make_entry(self.user, cost="20.00", sold_for="50.00", status="sold_out")
        self.assertIsNone(entry.profit_per_hour)

    def test_profit_per_hour_includes_sale_hours(self):
        entry = _make_entry(self.user, cost="20.00", status="sold_out", purchase_hours=Decimal("1.0"))
        LedgerSale.objects.create(
            entry=entry, sale_date=timezone.localdate(), sale_price=Decimal("50.00"), hours_spent=Decimal("1.0"),
        )
        # profit = 50 - 20 = 30; total_hours = 1 + 1 = 2 -> 15/hr
        self.assertAlmostEqual(entry.profit_per_hour, 15.0, places=4)

    def test_profit_per_hour_none_when_unsold(self):
        entry = _make_entry(self.user, cost="20.00", status="holding", purchase_hours=Decimal("2.0"))
        self.assertIsNone(entry.profit_per_hour)

    def test_prediction_error(self):
        entry = _make_entry(
            self.user, cost="20.00", sold_for="50.00", status="sold_out",
            predicted_sale_low=Decimal("40.00"), predicted_sale_high=Decimal("60.00"),
        )
        # mid = 50; actual = 50 -> error 0
        self.assertEqual(entry.prediction_error_dollars, Decimal("0"))
        self.assertEqual(entry.prediction_error_pct, 0.0)

    def test_prediction_error_none_without_prediction(self):
        entry = _make_entry(self.user, cost="20.00", sold_for="50.00", status="sold_out")
        self.assertIsNone(entry.prediction_error_dollars)

    def test_prediction_error_none_when_unsold(self):
        entry = _make_entry(
            self.user, cost="20.00", status="holding",
            predicted_sale_low=Decimal("40.00"), predicted_sale_high=Decimal("60.00"),
        )
        self.assertIsNone(entry.prediction_error_dollars)

    def test_sell_side_cost_prefers_itemized(self):
        entry = _make_entry(self.user, cost="20.00", sell_fees="99.00", sell_shipping="99.00")
        LedgerSale.objects.create(
            entry=entry, sale_date=timezone.localdate(), sale_price=Decimal("10"),
            selling_fees=Decimal("1.00"), outbound_shipping=Decimal("2.00"),
        )
        self.assertEqual(entry.sell_side_cost, Decimal("3.00"))


class AgingTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="tester", password="pw-not-real-12345")

    def test_no_suggestion_before_14_days(self):
        entry = _make_entry(self.user, purchase_date=timezone.localdate() - timedelta(days=10))
        self.assertIsNone(entry.aging_suggestion)

    def test_offer_to_watchers_at_14_days(self):
        entry = _make_entry(self.user, purchase_date=timezone.localdate() - timedelta(days=14))
        self.assertEqual(entry.aging_suggestion, "Offer to watchers")

    def test_drop_price_at_30_days(self):
        entry = _make_entry(self.user, purchase_date=timezone.localdate() - timedelta(days=30))
        self.assertEqual(entry.aging_suggestion, "Drop price (~10%)")

    def test_drop_price_again_at_60_days(self):
        entry = _make_entry(self.user, purchase_date=timezone.localdate() - timedelta(days=60))
        self.assertEqual(entry.aging_suggestion, "Drop price again (~10%)")

    def test_bundle_local_donate_at_90_days(self):
        entry = _make_entry(self.user, purchase_date=timezone.localdate() - timedelta(days=90))
        self.assertEqual(entry.aging_suggestion, "Bundle, list locally, or donate")

    def test_no_suggestion_for_sold_out_item(self):
        entry = _make_entry(
            self.user, purchase_date=timezone.localdate() - timedelta(days=90), status="sold_out",
            sold_for="10.00",
        )
        self.assertIsNone(entry.aging_suggestion)

    def test_falls_back_to_created_at_when_purchase_date_blank(self):
        entry = LedgerEntry.objects.create(owner=self.user, item="No Purchase Date", cost=Decimal("10.00"))
        self.assertEqual(entry.days_held, 0)  # created just now


class ScorecardMetricsTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="tester", password="pw-not-real-12345")

    def test_grouping_by_category_and_source(self):
        _make_entry(
            self.user, item="A", category="coins", source="shopgoodwill", cost="10.00", sold_for="20.00",
            status="sold_out",
        )
        _make_entry(
            self.user, item="B", category="coins", source="maxsold", cost="10.00", sold_for="5.00",
            status="sold_out",
        )
        _make_entry(self.user, item="C", cost="10.00", status="holding")  # unsold - excluded from grouping

        context = ledger_metrics.scorecard_context(self.user)

        by_category = {r["label"]: r for r in context["by_category"]}
        self.assertEqual(by_category["coins"]["count"], 2)
        self.assertEqual(by_category["coins"]["total_profit"], Decimal("5.00"))  # +10 and -5

        by_source = {r["label"]: r for r in context["by_source"]}
        self.assertEqual(by_source["shopgoodwill"]["count"], 1)
        self.assertEqual(by_source["maxsold"]["count"], 1)

    def test_blank_category_and_source_grouped_as_unassigned(self):
        _make_entry(self.user, item="No Category", cost="10.00", sold_for="20.00", status="sold_out")

        context = ledger_metrics.scorecard_context(self.user)

        labels = {r["label"] for r in context["by_category"]}
        self.assertIn("Unassigned", labels)

    def test_prediction_source_grouping_and_no_prediction_bucket(self):
        _make_entry(
            self.user, item="Scanner Predicted", cost="10.00", sold_for="20.00", status="sold_out",
            predicted_sale_source="scanner", predicted_sale_low=Decimal("15"), predicted_sale_high=Decimal("15"),
        )
        _make_entry(self.user, item="No Prediction", cost="10.00", sold_for="20.00", status="sold_out")

        context = ledger_metrics.scorecard_context(self.user)
        labels = {r["label"]: r for r in context["by_prediction_source"]}

        self.assertIn("Scanner", labels)
        self.assertIn("No Prediction", labels)

    def test_median_prediction_error_pct(self):
        _make_entry(
            self.user, item="E1", category="coins", cost="10.00", sold_for="20.00", status="sold_out",
            predicted_sale_low=Decimal("20.00"), predicted_sale_high=Decimal("20.00"),  # 0% error
        )
        _make_entry(
            self.user, item="E2", category="coins", cost="10.00", sold_for="30.00", status="sold_out",
            predicted_sale_low=Decimal("20.00"), predicted_sale_high=Decimal("20.00"),  # +50% error
        )

        context = ledger_metrics.scorecard_context(self.user)
        row = {r["label"]: r for r in context["by_category"]}["coins"]

        self.assertEqual(row["median_prediction_error_pct"], 25.0)

    def test_legacy_unsplit_count_per_group(self):
        _make_entry(
            self.user, item="Legacy", category="coins", cost="10.00", sold_for="20.00", status="sold_out",
            fees=Decimal("2.00"),
        )
        _make_entry(self.user, item="Clean", category="coins", cost="10.00", sold_for="20.00", status="sold_out")

        context = ledger_metrics.scorecard_context(self.user)
        row = {r["label"]: r for r in context["by_category"]}["coins"]

        self.assertEqual(row["legacy_unsplit_count"], 1)

    def test_conflicting_entries_surfaced(self):
        entry = _make_entry(self.user, cost="10.00", sold_for="20.00")
        LedgerSale.objects.create(entry=entry, sale_date=timezone.localdate(), sale_price=Decimal("5.00"))

        context = ledger_metrics.scorecard_context(self.user)

        self.assertIn(entry, context["conflicting_entries"])

    def test_aging_sorted_by_days_held_descending(self):
        _make_entry(self.user, item="Newer", purchase_date=timezone.localdate() - timedelta(days=5))
        _make_entry(self.user, item="Older", purchase_date=timezone.localdate() - timedelta(days=50))

        context = ledger_metrics.scorecard_context(self.user)

        self.assertEqual([row["entry"].item for row in context["aging"]], ["Older", "Newer"])

    def test_scorecard_view_requires_login_and_renders(self):
        _make_entry(self.user, cost="10.00", sold_for="20.00", status="sold_out")
        self.client.login(username="tester", password="pw-not-real-12345")

        response = self.client.get(reverse("albright_reselling_app:ledger_scorecard"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Scorecard")

    def test_scorecard_requires_login(self):
        response = self.client.get(reverse("albright_reselling_app:ledger_scorecard"))
        self.assertEqual(response.status_code, 302)


class RealizedSummaryTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="tester", password="pw-not-real-12345")

    def test_bought_and_sold_and_unsold_counts(self):
        now = timezone.now()
        _make_entry(self.user, item="Bought This Month", purchase_date=timezone.localdate())
        _make_entry(
            self.user, item="Sold This Month", cost="10.00", sold_for="30.00", status="sold_out",
            sold_date=timezone.localdate(), purchase_date=timezone.localdate() - timedelta(days=40),
        )
        _make_entry(
            self.user, item="Still Holding", cost="15.00", status="holding",
            purchase_date=timezone.localdate() - timedelta(days=40),
        )

        summary = ledger_metrics.realized_summary(self.user, now=now)

        self.assertEqual(summary["bought_this_month"], 1)
        self.assertEqual(summary["sold_this_month"], 1)
        self.assertEqual(summary["realized_profit_this_month"], Decimal("20.00"))
        # "Bought This Month" defaults to status=holding too, so it's
        # unsold inventory as well as being bought this month - these
        # aren't mutually exclusive buckets.
        self.assertEqual(summary["unsold_count"], 2)
        self.assertEqual(summary["unsold_value_at_cost"], Decimal("25.00"))  # 10.00 + 15.00

    def test_dashboard_shows_realized_block(self):
        _make_entry(self.user, item="Dash Item", cost="10.00", sold_for="30.00", status="sold_out",
                    sold_date=timezone.localdate())
        self.client.login(username="tester", password="pw-not-real-12345")

        response = self.client.get(reverse("albright_reselling_app:dashboard"))

        self.assertContains(response, "Realized")
        self.assertContains(response, "Open Scorecard")


def _make_watch(lot, my_max_bid="20.00", status="watching", resolved_at=None):
    return BidWatch.objects.create(
        lot=lot, my_max_bid=Decimal(my_max_bid), status=status, resolved_at=resolved_at,
    )


class DashboardTilesTests(TestCase):
    """ledger_metrics.dashboard_tiles() - the reseller dashboard's 5-tile
    summary row (profit/sales this month, inventory, needs action, my
    bids). Exercises a legacy sold_for entry, a written-off item, and a
    partially-sold item, per the feature's explicit test requirement."""

    def setUp(self):
        self.user = User.objects.create_user(username="tester", password="pw-not-real-12345")

    def test_empty_state(self):
        tiles = ledger_metrics.dashboard_tiles(self.user)

        self.assertEqual(tiles["profit_this_month"], Decimal("0"))
        self.assertEqual(tiles["all_time_profit"], Decimal("0"))
        self.assertEqual(tiles["sales_this_month_count"], 0)
        self.assertEqual(tiles["revenue_this_month"], Decimal("0"))
        self.assertIsNone(tiles["avg_roi_this_month"])
        self.assertEqual(tiles["inventory_count"], 0)
        self.assertEqual(tiles["inventory_cost"], Decimal("0"))
        self.assertEqual(tiles["needs_action_count"], 0)
        self.assertEqual(tiles["watching_count"], 0)
        self.assertEqual(tiles["likely_won_this_week"], 0)

    def test_profit_this_month_includes_legacy_sold_for_and_itemized_sales(self):
        now = timezone.now()
        today = timezone.localdate()
        # Legacy sold_for entry, sold today (this month).
        _make_entry(
            self.user, item="Legacy Sold", cost="10.00", sold_for="40.00", status="sold_out", sold_date=today,
        )
        # Itemized-sale entry, sold today.
        itemized = _make_entry(self.user, item="Itemized Sold", cost="10.00", status="sold_out")
        LedgerSale.objects.create(entry=itemized, sale_date=today, sale_price=Decimal("50.00"))
        # Sold last month - must not count toward this month's profit.
        last_month = (today.replace(day=1) - timedelta(days=1))
        _make_entry(
            self.user, item="Sold Last Month", cost="10.00", sold_for="999.00", status="sold_out",
            sold_date=last_month,
        )

        tiles = ledger_metrics.dashboard_tiles(self.user, now=now)

        # (40-10) + (50-10) = 70; last month's 999-10 excluded.
        self.assertEqual(tiles["profit_this_month"], Decimal("70.00"))
        self.assertEqual(tiles["sales_this_month_count"], 2)
        self.assertEqual(tiles["revenue_this_month"], Decimal("90.00"))  # 40 + 50

    def test_all_time_profit_includes_written_off_losses(self):
        now = timezone.now()
        today = timezone.localdate()
        _make_entry(self.user, item="Sold", cost="10.00", sold_for="40.00", status="sold_out", sold_date=today)
        _make_entry(self.user, item="Written Off", cost="15.00", status="written_off")

        tiles = ledger_metrics.dashboard_tiles(self.user, now=now)

        self.assertEqual(tiles["all_time_profit"], Decimal("15.00"))  # +30 - 15

    def test_profit_negative_this_month(self):
        today = timezone.localdate()
        _make_entry(self.user, item="Sold At Loss", cost="50.00", sold_for="20.00", status="sold_out",
                    sold_date=today)

        tiles = ledger_metrics.dashboard_tiles(self.user)

        self.assertEqual(tiles["profit_this_month"], Decimal("-30.00"))

    def test_avg_roi_this_month(self):
        today = timezone.localdate()
        _make_entry(self.user, item="A", cost="50.00", sold_for="75.00", status="sold_out", sold_date=today)
        _make_entry(self.user, item="B", cost="100.00", sold_for="125.00", status="sold_out", sold_date=today)

        tiles = ledger_metrics.dashboard_tiles(self.user)

        # ROI: 50% and 25% -> avg 37.5%
        self.assertAlmostEqual(float(tiles["avg_roi_this_month"]), 37.5, places=4)

    def test_inventory_counts_holding_and_partially_sold_not_sold_out(self):
        _make_entry(self.user, item="Holding", cost="10.00", status="holding")
        entry = _make_entry(self.user, item="Partial", cost="20.00", status="partially_sold")
        LedgerSale.objects.create(entry=entry, sale_date=timezone.localdate(), sale_price=Decimal("5.00"))
        _make_entry(self.user, item="Sold Out", cost="30.00", sold_for="50.00", status="sold_out")

        tiles = ledger_metrics.dashboard_tiles(self.user)

        self.assertEqual(tiles["inventory_count"], 2)
        self.assertEqual(tiles["inventory_cost"], Decimal("30.00"))  # 10 + 20

    def test_needs_action_counts_markdown_steps(self):
        _make_entry(self.user, item="Fresh", cost="10.00", status="holding",
                    purchase_date=timezone.localdate() - timedelta(days=5))
        _make_entry(self.user, item="At 14 Days", cost="10.00", status="holding",
                    purchase_date=timezone.localdate() - timedelta(days=16))
        _make_entry(self.user, item="Past 90", cost="10.00", status="holding",
                    purchase_date=timezone.localdate() - timedelta(days=200))

        tiles = ledger_metrics.dashboard_tiles(self.user)

        self.assertEqual(tiles["markdown_count"], 2)
        self.assertEqual(tiles["needs_action_count"], 2)

    def test_needs_action_includes_likely_won_wins_to_log(self):
        now = timezone.now()
        lot = SourcedLot.objects.create(
            source="shopgoodwill", external_id="needs-action-1", url="https://example.com/needs-action-1",
            title="Lot", current_price=Decimal("10"), end_time=now - timedelta(hours=1),
        )
        _make_watch(lot, status="likely_won", resolved_at=now)

        tiles = ledger_metrics.dashboard_tiles(self.user, now=now)

        self.assertEqual(tiles["wins_to_log"], 1)
        self.assertEqual(tiles["needs_action_count"], 1)

    def test_likely_won_lot_already_logged_not_counted_as_win_to_log(self):
        now = timezone.now()
        lot = SourcedLot.objects.create(
            source="shopgoodwill", external_id="needs-action-2", url="https://example.com/needs-action-2",
            title="Lot", current_price=Decimal("10"), end_time=now - timedelta(hours=1),
        )
        _make_watch(lot, status="likely_won", resolved_at=now)
        _make_entry(self.user, item="Already Logged", scanner_lot=lot)

        tiles = ledger_metrics.dashboard_tiles(self.user, now=now)

        self.assertEqual(tiles["wins_to_log"], 0)

    def test_my_bids_tile_counts_watching_and_recent_likely_won(self):
        now = timezone.now()
        watching_lot = SourcedLot.objects.create(
            source="shopgoodwill", external_id="mybids-1", url="https://example.com/mybids-1",
            title="Lot", current_price=Decimal("10"), end_time=now + timedelta(hours=3),
        )
        _make_watch(watching_lot, status="watching")

        won_lot = SourcedLot.objects.create(
            source="shopgoodwill", external_id="mybids-2", url="https://example.com/mybids-2",
            title="Lot", current_price=Decimal("10"), end_time=now - timedelta(hours=1),
        )
        _make_watch(won_lot, status="likely_won", resolved_at=now - timedelta(days=2))

        old_won_lot = SourcedLot.objects.create(
            source="shopgoodwill", external_id="mybids-3", url="https://example.com/mybids-3",
            title="Lot", current_price=Decimal("10"), end_time=now - timedelta(days=20),
        )
        _make_watch(old_won_lot, status="likely_won", resolved_at=now - timedelta(days=20))

        tiles = ledger_metrics.dashboard_tiles(self.user, now=now)

        self.assertEqual(tiles["watching_count"], 1)
        self.assertEqual(tiles["likely_won_this_week"], 1)  # only the 2-days-ago win


def _formset_post_data(entries, delete_pks=()):
    """Builds a full, valid LedgerEntryFormSet POST payload from a list of
    existing LedgerEntry rows - every required field filled from the row's
    own current values, so a save is a no-op except for any DELETE flags."""
    data = {
        "form-TOTAL_FORMS": str(len(entries)),
        "form-INITIAL_FORMS": str(len(entries)),
        "form-MIN_NUM_FORMS": "0",
        "form-MAX_NUM_FORMS": "1000",
    }
    for i, entry in enumerate(entries):
        prefix = f"form-{i}"
        data[f"{prefix}-id"] = str(entry.pk)
        data[f"{prefix}-item"] = entry.item
        data[f"{prefix}-cost"] = str(entry.cost)
        data[f"{prefix}-buyer_premium"] = str(entry.buyer_premium)
        data[f"{prefix}-sales_tax"] = str(entry.sales_tax)
        data[f"{prefix}-buy_fees"] = str(entry.buy_fees)
        data[f"{prefix}-inbound_shipping"] = str(entry.inbound_shipping)
        data[f"{prefix}-sold_for"] = "" if entry.sold_for is None else str(entry.sold_for)
        data[f"{prefix}-sell_fees"] = str(entry.sell_fees)
        data[f"{prefix}-sell_shipping"] = str(entry.sell_shipping)
        if entry.pk in delete_pks:
            data[f"{prefix}-DELETE"] = "on"
    return data


class LedgerPageRedesignTests(TestCase):
    """Covers the read-only vs. edit-mode Ledger page: full (untruncated)
    item names and $-formatted values by default, the Delete checkbox
    column only existing in edit mode, deleting via the edit-mode save
    still working, the read-only totals row, and the "Old unsplit" column
    for legacy (pre-split) rows."""

    def setUp(self):
        self.user = User.objects.create_user(username="tester", password="pw-not-real-12345")
        self.client.login(username="tester", password="pw-not-real-12345")

    def test_readonly_view_shows_full_name_and_formatted_values(self):
        long_name = "A very long descriptive item name that should wrap, never be truncated or ellipsized"
        _make_entry(self.user, item=long_name, cost="1234.50", sold_for="0")

        response = self.client.get(reverse("albright_reselling_app:ledger"))
        content = response.content.decode()

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, long_name)
        self.assertNotIn("…", content)  # no ellipsis character anywhere
        self.assertContains(response, "$1,234.50")
        # sold_for was explicitly 0 -> shown as "-", not "$0.00"
        self.assertNotContains(response, "$0.00")

    def test_delete_checkbox_only_present_in_edit_mode(self):
        _make_entry(self.user, item="Row One")

        readonly = self.client.get(reverse("albright_reselling_app:ledger"))
        edit = self.client.get(reverse("albright_reselling_app:ledger"), {"edit": "1"})

        self.assertNotIn("form-0-DELETE", readonly.content.decode())
        self.assertIn("form-0-DELETE", edit.content.decode())
        self.assertContains(edit, "Delete")

    def test_saving_with_deletion_removes_only_checked_entries(self):
        keep = _make_entry(self.user, item="Keep Me", cost="10.00")
        remove = _make_entry(self.user, item="Remove Me", cost="20.00")

        data = _formset_post_data([keep, remove], delete_pks={remove.pk})
        data["save_ledger"] = "Save Ledger"
        response = self.client.post(reverse("albright_reselling_app:ledger"), data)

        self.assertRedirects(response, reverse("albright_reselling_app:ledger"))
        remaining = LedgerEntry.objects.filter(owner=self.user)
        self.assertEqual(list(remaining.values_list("item", flat=True)), ["Keep Me"])

    def test_readonly_totals_row_sums_shown_rows(self):
        _make_entry(self.user, item="Entry A", cost="100.00", sold_for="150.00", status="sold_out")
        _make_entry(self.user, item="Entry B", cost="20.00", sold_for="10.00", status="written_off")

        response = self.client.get(reverse("albright_reselling_app:ledger"))
        content = response.content.decode()

        entry_a = LedgerEntry.objects.get(item="Entry A")
        entry_b = LedgerEntry.objects.get(item="Entry B")
        expected_buy_total = entry_a.buy_side_cost + entry_b.buy_side_cost
        expected_sold_for = (entry_a.sold_for or 0) + (entry_b.sold_for or 0)
        expected_profit = entry_a.profit + entry_b.profit

        self.assertIn('id="ledger-total-buytotal"', content)
        self.assertIn(f"${expected_buy_total:,.2f}", content)
        self.assertIn(f"${expected_sold_for:,.2f}", content)
        sign = "-" if expected_profit < 0 else ""
        self.assertIn(f"{sign}${abs(expected_profit):,.2f}", content)

    def test_legacy_amounts_shown_in_old_unsplit_column_and_buy_total_adds_up(self):
        entry = _make_entry(
            self.user, item="Legacy Row", cost="50.00", buyer_premium="5.00", sales_tax="2.00",
            buy_fees="1.00", inbound_shipping="3.00", fees="7.00", shipping="4.00",
        )
        clean_entry = _make_entry(self.user, item="Clean Row", cost="10.00")

        response = self.client.get(reverse("albright_reselling_app:ledger"))
        content = response.content.decode()

        self.assertContains(response, "Old unsplit")
        self.assertEqual(entry.legacy_total, Decimal("11.00"))
        self.assertIn("$11.00", content)
        # the visible buy-side columns (cost+premium+tax+buy_fees+inbound+old
        # unsplit) sum to exactly the buy total shown for that row
        visible_sum = (
            entry.cost + entry.buyer_premium + entry.sales_tax + entry.buy_fees
            + entry.inbound_shipping + entry.legacy_total
        )
        self.assertEqual(visible_sum, entry.buy_side_cost)
        self.assertIn(f"${entry.buy_side_cost:,.2f}", content)
        # a row with no legacy amounts still renders under the same (shared)
        # Old unsplit column without error, showing "-"
        self.assertEqual(clean_entry.legacy_total, 0)
