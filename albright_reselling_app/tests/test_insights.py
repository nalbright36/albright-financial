"""Tests for scanner/insights.py (market history, calibration suggestions,
AI review accuracy), the calibrate management command, and the Insights
page's apply/dismiss/undo views - including the pipeline actually using an
applied override end to end. No HTTP involved - pure ORM fixtures."""
from datetime import timedelta
from decimal import Decimal
from io import StringIO

from django.conf import settings
from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from albright_reselling_app import insights_views  # noqa: F401 - imported for coverage clarity
from albright_reselling_app.models import LedgerEntry
from albright_reselling_app.scanner import insights, pipeline
from albright_reselling_app.scanner_models import AIReview, CalibrationOverride, CalibrationSuggestion, LotEvaluation, SourcedLot

SPOT = {"silver": 30.0, "gold": 2500.0}


def _make_closed_lot(external_id, source="shopgoodwill", category="coins", final_price="20.00",
                      max_bid="20.00", melt_value="20.00", expected_sale="20.00", end_time=None,
                      features=None, coin_keys="silver_eagle"):
    end_time = end_time or (timezone.now() - timedelta(days=5))
    lot = SourcedLot.objects.create(
        source=source, external_id=external_id, url=f"https://example.com/{external_id}",
        title=f"Lot {external_id}", current_price=Decimal("10.00"), end_time=end_time,
        is_closed=True, final_price=Decimal(final_price), features=features or {},
    )
    LotEvaluation.objects.create(
        lot=lot, category=category, max_bid=Decimal(max_bid), melt_value=Decimal(melt_value),
        expected_sale=Decimal(expected_sale), coin_keys=coin_keys,
    )
    return lot


class SourceCategoryStatsTests(TestCase):
    def test_counts_and_percent_at_or_under_max(self):
        _make_closed_lot("a", category="coins", final_price="18.00", max_bid="20.00")
        _make_closed_lot("b", category="coins", final_price="25.00", max_bid="20.00")

        rows = insights.source_category_stats()

        row = next(r for r in rows if r["source"] == "shopgoodwill" and r["category"] == "coins")
        self.assertEqual(row["closed_count"], 2)
        self.assertEqual(row["pct_at_or_under_max"], 50.0)

    def test_median_final_melt_and_expected_pct(self):
        _make_closed_lot("c", final_price="20.00", melt_value="20.00", expected_sale="25.00")
        _make_closed_lot("d", final_price="30.00", melt_value="20.00", expected_sale="25.00")

        rows = insights.source_category_stats()
        row = rows[0]

        self.assertEqual(row["median_final_melt_pct"], 125.0)  # (100+150)/2
        self.assertEqual(row["median_final_expected_pct"], 100.0)  # (80+120)/2

    def test_filters_by_source_category_and_date_range(self):
        in_window = _make_closed_lot("e", source="maxsold", category="jewelry",
                                      end_time=timezone.now() - timedelta(days=2))
        _make_closed_lot("f", source="shopgoodwill", category="coins")  # wrong source/category
        _make_closed_lot("g", source="maxsold", category="jewelry",
                         end_time=timezone.now() - timedelta(days=40))  # outside window

        rows = insights.source_category_stats(
            source="maxsold", category="jewelry", date_from=timezone.now() - timedelta(days=10),
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["closed_count"], 1)

    def test_trend_by_week_buckets_by_monday(self):
        monday = timezone.now() - timedelta(days=timezone.now().weekday())
        _make_closed_lot("h", end_time=monday)
        _make_closed_lot("i", end_time=monday + timedelta(days=2))

        rows = insights.source_category_stats()

        self.assertEqual(len(rows[0]["trend_by_week"]), 1)
        self.assertEqual(rows[0]["trend_by_week"][0][1], 2)


class TopSellersTests(TestCase):
    def test_minimum_sample_enforced(self):
        for i in range(4):
            lot = _make_closed_lot(f"low-{i}", melt_value="20.00", final_price="18.00")
            lot.features = {"seller_id": 555}
            lot.save()

        self.assertEqual(insights.top_sellers(), [])

    def test_ranks_by_lowest_median_final_melt_pct(self):
        for i in range(5):
            lot = _make_closed_lot(f"cheap-{i}", melt_value="20.00", final_price="15.00")
            lot.features = {"seller_id": 1}
            lot.save()
        for i in range(5):
            lot = _make_closed_lot(f"pricey-{i}", melt_value="20.00", final_price="25.00")
            lot.features = {"seller_id": 2}
            lot.save()

        rows = insights.top_sellers()

        self.assertEqual(rows[0]["seller"], 1)
        self.assertLess(rows[0]["median_final_melt_pct"], rows[1]["median_final_melt_pct"])

    def test_seller_key_per_source(self):
        for i in range(5):
            lot = _make_closed_lot(f"hibid-{i}", source="hibid", melt_value="20.00", final_price="18.00")
            lot.features = {"auctioneer": "Hessney Auction Co."}
            lot.save()
        for i in range(5):
            lot = _make_closed_lot(f"maxsold-{i}", source="maxsold", melt_value="20.00", final_price="18.00")
            lot.features = {"auction_id": 42}
            lot.save()

        rows = {r["source"]: r["seller"] for r in insights.top_sellers()}

        self.assertEqual(rows["hibid"], "Hessney Auction Co.")
        self.assertEqual(rows["maxsold"], 42)


class FinalMeltByHourWeekdayTests(TestCase):
    def test_grouped_by_hour_and_weekday(self):
        lot1 = _make_closed_lot("hr-1", melt_value="20.00", final_price="20.00")
        lot1.features = {"end_hour_local": 14, "end_weekday_local": 2}
        lot1.save()
        lot2 = _make_closed_lot("hr-2", melt_value="20.00", final_price="24.00")
        lot2.features = {"end_hour_local": 14, "end_weekday_local": 3}
        lot2.save()

        by_hour, by_weekday = insights.final_melt_by_hour_and_weekday()

        self.assertEqual(by_hour[14]["n"], 2)
        self.assertEqual(by_hour[14]["median_final_melt_pct"], 110.0)
        self.assertEqual(by_weekday[2]["n"], 1)
        self.assertEqual(by_weekday[3]["n"], 1)

    def test_lots_without_closing_features_excluded(self):
        _make_closed_lot("no-features", melt_value="20.00", final_price="20.00")

        by_hour, by_weekday = insights.final_melt_by_hour_and_weekday()

        self.assertEqual(by_hour, {})
        self.assertEqual(by_weekday, {})


class MarketHistoryDefaultsTests(TestCase):
    def test_defaults_to_last_30_days(self):
        history = insights.market_history()

        self.assertIsNotNone(history["date_from"])
        self.assertIsNone(history["date_to"])
        self.assertAlmostEqual(
            (timezone.now() - history["date_from"]).total_seconds(),
            timedelta(days=30).total_seconds(), delta=5,
        )


class CalibratedOverrideHelperTests(TestCase):
    def test_get_calibrated_falls_back_without_override(self):
        self.assertEqual(insights.get_calibrated("SOMETHING.not_set", 0.5), 0.5)

    def test_get_calibrated_uses_override(self):
        CalibrationOverride.objects.create(key="SOMETHING.set", value=Decimal("0.42"))
        self.assertEqual(insights.get_calibrated("SOMETHING.set", 0.5), 0.42)

    def test_calibrated_multipliers_substitutes_only_overridden_keys(self):
        base = {"default": 1.0, "silver_eagle": 1.08, "jewelry_gold": 0.80}
        CalibrationOverride.objects.create(key="RESALE_MULTIPLIERS.silver_eagle", value=Decimal("1.2"))

        result = insights.calibrated_multipliers(base)

        self.assertEqual(result["silver_eagle"], 1.2)
        self.assertEqual(result["jewelry_gold"], 0.80)
        self.assertEqual(result["default"], 1.0)

    def test_calibrated_category_fees_substitutes_only_overridden_keys(self):
        base = {"ebay_fee_pct": 0.1325, "min_profit_pct": 0.20}
        CalibrationOverride.objects.create(key="CATEGORY_FEES.jewelry.min_profit_pct", value=Decimal("0.15"))

        result = insights.calibrated_category_fees("jewelry", base)

        self.assertEqual(result["min_profit_pct"], 0.15)
        self.assertEqual(result["ebay_fee_pct"], 0.1325)


class PipelineUsesOverrideEndToEndTests(TestCase):
    def test_applied_multiplier_override_changes_valuation(self):
        lot_default = SourcedLot.objects.create(
            source="shopgoodwill", external_id="override-default", url="https://example.com/override-default",
            title="2021 Silver Eagle 1 oz", current_price=Decimal("25.00"),
        )
        lot_overridden = SourcedLot.objects.create(
            source="shopgoodwill", external_id="override-applied", url="https://example.com/override-applied",
            title="2021 Silver Eagle 1 oz", current_price=Decimal("25.00"),
        )

        default_ev, _ = pipeline.evaluate(lot_default, SPOT, llm_budget=0, use_llm=False)

        CalibrationOverride.objects.create(key="RESALE_MULTIPLIERS.silver_eagle", value=Decimal("1.5"))
        overridden_ev, _ = pipeline.evaluate(lot_overridden, SPOT, llm_budget=0, use_llm=False)

        self.assertGreater(overridden_ev.expected_sale, default_ev.expected_sale)


class CalibrationSuggestionBuildingTests(TestCase):
    def setUp(self):
        self.cfg = settings.RESELLING_SCANNER

    def test_no_suggestion_below_minimum_sample(self):
        for i in range(4):  # below MIN_CLOSED_SAMPLE
            _make_closed_lot(f"small-{i}", final_price="30.00", expected_sale="20.00", coin_keys="silver_eagle")

        created = insights.build_calibration_suggestions(self.cfg)

        self.assertEqual([c.key for c in created if c.key == "RESALE_MULTIPLIERS.silver_eagle"], [])

    def test_no_suggestion_below_5_percent_threshold(self):
        # final/expected ratio ~1.03 -> suggested change is ~3%, below threshold
        for i in range(6):
            _make_closed_lot(f"tiny-gap-{i}", final_price="20.60", expected_sale="20.00",
                              coin_keys="silver_eagle")

        created = insights.build_calibration_suggestions(self.cfg)

        self.assertEqual([c.key for c in created if c.key == "RESALE_MULTIPLIERS.silver_eagle"], [])

    def test_suggestion_created_above_threshold_with_enough_sample(self):
        for i in range(6):
            _make_closed_lot(f"big-gap-{i}", final_price="24.00", expected_sale="20.00",
                              coin_keys="silver_eagle")

        created = insights.build_calibration_suggestions(self.cfg)

        matches = [c for c in created if c.key == "RESALE_MULTIPLIERS.silver_eagle"]
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0].sample_size, 6)

    def test_suggestion_capped_at_25_percent_move(self):
        # final/expected ratio of 2.0 would suggest doubling the multiplier -
        # capped to +25% of the current value instead.
        current = self.cfg["RESALE_MULTIPLIERS"]["silver_eagle"]
        for i in range(6):
            _make_closed_lot(f"huge-gap-{i}", final_price="40.00", expected_sale="20.00",
                              coin_keys="silver_eagle")

        created = insights.build_calibration_suggestions(self.cfg)
        match = next(c for c in created if c.key == "RESALE_MULTIPLIERS.silver_eagle")

        max_allowed = Decimal(str(current)) * Decimal("1.25")
        self.assertLessEqual(match.suggested_value, max_allowed)

    def test_no_duplicate_when_pending_suggestion_exists(self):
        CalibrationSuggestion.objects.create(
            key="RESALE_MULTIPLIERS.silver_eagle", current_value=Decimal("1.08"),
            suggested_value=Decimal("1.3"), sample_size=6, status="pending",
        )
        for i in range(6):
            _make_closed_lot(f"dup-{i}", final_price="24.00", expected_sale="20.00", coin_keys="silver_eagle")

        created = insights.build_calibration_suggestions(self.cfg)

        self.assertEqual([c.key for c in created if c.key == "RESALE_MULTIPLIERS.silver_eagle"], [])
        self.assertEqual(
            CalibrationSuggestion.objects.filter(key="RESALE_MULTIPLIERS.silver_eagle", status="pending").count(), 1,
        )

    def test_new_suggestion_allowed_once_prior_one_resolved(self):
        CalibrationSuggestion.objects.create(
            key="RESALE_MULTIPLIERS.silver_eagle", current_value=Decimal("1.08"),
            suggested_value=Decimal("1.3"), sample_size=6, status="dismissed",
        )
        for i in range(6):
            _make_closed_lot(f"resolved-{i}", final_price="24.00", expected_sale="20.00", coin_keys="silver_eagle")

        created = insights.build_calibration_suggestions(self.cfg)

        self.assertEqual(len([c for c in created if c.key == "RESALE_MULTIPLIERS.silver_eagle"]), 1)

    def test_ledger_sales_produce_suggestions(self):
        owner = User.objects.create_user(username="tester", password="pw-not-real-12345")
        for i in range(6):
            lot = SourcedLot.objects.create(
                source="shopgoodwill", external_id=f"ledger-se-{i}", url=f"https://example.com/ledger-se-{i}",
                title="Silver Eagle", current_price=Decimal("10.00"),
            )
            LotEvaluation.objects.create(lot=lot, category="coins", coin_keys="silver_eagle")
            LedgerEntry.objects.create(
                owner=owner, item=f"Silver Eagle {i}", cost=Decimal("10.00"), sold_for=Decimal("24.00"),
                status="sold_out", melt_value=Decimal("20.00"), scanner_lot=lot,
            )

        created = insights.build_calibration_suggestions(self.cfg)

        matches = [c for c in created if c.key == "RESALE_MULTIPLIERS.silver_eagle"]
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0].sample_size, 6)

    def test_manual_ledger_entries_without_scanner_lot_are_skipped(self):
        owner = User.objects.create_user(username="tester2", password="pw-not-real-12345")
        for i in range(6):
            LedgerEntry.objects.create(
                owner=owner, item=f"Hand entry {i}", cost=Decimal("10.00"), sold_for=Decimal("24.00"),
                status="sold_out", melt_value=Decimal("20.00"),
            )

        created = insights.build_calibration_suggestions(self.cfg)
        matches = [c for c in created if c.key.startswith("RESALE_MULTIPLIERS.")]

        self.assertEqual(matches, [])

    def test_category_fee_suggestion_created(self):
        for i in range(6):
            _make_closed_lot(f"fee-gap-{i}", category="jewelry", final_price="16.00", expected_sale="20.00",
                              coin_keys="jewelry_gold")

        created = insights.build_calibration_suggestions(self.cfg)

        self.assertTrue(any(c.key == "CATEGORY_FEES.jewelry.min_profit_pct" for c in created))


class CalibrateCommandTests(TestCase):
    def test_command_creates_and_prints_suggestions(self):
        for i in range(6):
            _make_closed_lot(f"cmd-{i}", final_price="24.00", expected_sale="20.00", coin_keys="silver_eagle")

        out = StringIO()
        call_command("calibrate", stdout=out)

        self.assertIn("RESALE_MULTIPLIERS.silver_eagle", out.getvalue())
        self.assertTrue(CalibrationSuggestion.objects.filter(key="RESALE_MULTIPLIERS.silver_eagle").exists())

    def test_command_reports_nothing_when_no_suggestions(self):
        out = StringIO()
        call_command("calibrate", stdout=out)

        self.assertIn("No new suggestions", out.getvalue())


class ApplyDismissUndoViewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="tester", password="pw-not-real-12345")
        self.client.login(username="tester", password="pw-not-real-12345")

    def test_apply_creates_override_and_marks_applied(self):
        suggestion = CalibrationSuggestion.objects.create(
            key="RESALE_MULTIPLIERS.silver_eagle", current_value=Decimal("1.08"),
            suggested_value=Decimal("1.2"), sample_size=6, evidence="test evidence",
        )

        response = self.client.post(reverse("albright_reselling_app:apply_suggestion", args=[suggestion.pk]))

        self.assertRedirects(response, reverse("albright_reselling_app:insights"))
        suggestion.refresh_from_db()
        self.assertEqual(suggestion.status, "applied")
        override = CalibrationOverride.objects.get(key="RESALE_MULTIPLIERS.silver_eagle")
        self.assertEqual(override.value, Decimal("1.2"))
        self.assertEqual(override.applied_by, self.user)

    def test_dismiss_marks_dismissed_without_creating_override(self):
        suggestion = CalibrationSuggestion.objects.create(
            key="RESALE_MULTIPLIERS.silver_eagle", current_value=Decimal("1.08"),
            suggested_value=Decimal("1.2"), sample_size=6,
        )

        self.client.post(reverse("albright_reselling_app:dismiss_suggestion", args=[suggestion.pk]))

        suggestion.refresh_from_db()
        self.assertEqual(suggestion.status, "dismissed")
        self.assertFalse(CalibrationOverride.objects.filter(key="RESALE_MULTIPLIERS.silver_eagle").exists())

    def test_undo_deletes_override(self):
        override = CalibrationOverride.objects.create(key="RESALE_MULTIPLIERS.silver_eagle", value=Decimal("1.2"))

        response = self.client.post(reverse("albright_reselling_app:undo_override", args=[override.pk]))

        self.assertRedirects(response, reverse("albright_reselling_app:insights"))
        self.assertFalse(CalibrationOverride.objects.filter(pk=override.pk).exists())

    def test_insights_page_shows_pending_suggestion_and_applied_override(self):
        CalibrationSuggestion.objects.create(
            key="RESALE_MULTIPLIERS.silver_eagle", current_value=Decimal("1.08"),
            suggested_value=Decimal("1.2"), sample_size=6, evidence="evidence text",
        )
        CalibrationOverride.objects.create(key="CATEGORY_FEES.jewelry.min_profit_pct", value=Decimal("0.15"))

        response = self.client.get(reverse("albright_reselling_app:insights"))

        self.assertContains(response, "RESALE_MULTIPLIERS.silver_eagle")
        self.assertContains(response, "CATEGORY_FEES.jewelry.min_profit_pct")


class AIReviewAccuracyTests(TestCase):
    def _make_review(self, external_id, category="coins", resale_low="20.00", resale_high="30.00",
                      final_price=None, is_closed=True):
        lot = SourcedLot.objects.create(
            source="shopgoodwill", external_id=external_id, url=f"https://example.com/{external_id}",
            title="Lot", current_price=Decimal("10.00"), is_closed=is_closed,
            final_price=Decimal(final_price) if final_price is not None else None,
        )
        LotEvaluation.objects.create(lot=lot, category=category)
        return AIReview.objects.create(
            lot=lot, status="done", resale_low=Decimal(resale_low), resale_high=Decimal(resale_high),
            cost_usd=Decimal("0.05"),
        )

    def test_counts_and_in_range_pct(self):
        self._make_review("acc-1", resale_low="20.00", resale_high="30.00", final_price="25.00")  # in range
        self._make_review("acc-2", resale_low="20.00", resale_high="30.00", final_price="40.00")  # out of range

        stats = insights.ai_review_accuracy()

        self.assertEqual(stats["coins"]["count"], 2)
        self.assertEqual(stats["coins"]["pct_in_range"], 50.0)

    def test_median_error_pct(self):
        # mid = 25; final 25 -> 0% error; final 37.5 -> +50% error
        self._make_review("acc-3", resale_low="20.00", resale_high="30.00", final_price="25.00")
        self._make_review("acc-4", resale_low="20.00", resale_high="30.00", final_price="37.50")

        stats = insights.ai_review_accuracy()

        self.assertEqual(stats["coins"]["median_error_pct"], 25.0)

    def test_live_lot_with_no_outcome_excluded(self):
        self._make_review("acc-5", is_closed=False, final_price=None)

        stats = insights.ai_review_accuracy()

        self.assertEqual(stats, {})

    def test_ledger_sale_outcome_preferred_over_lot_final_price(self):
        """Ledger win is more authoritative than the auction's own close -
        used instead of lot.final_price when both exist."""
        owner = User.objects.create_user(username="tester3", password="pw-not-real-12345")
        review = self._make_review("acc-6", resale_low="20.00", resale_high="30.00", final_price="40.00")
        LedgerEntry.objects.create(
            owner=owner, item="Won It", cost=Decimal("10.00"), sold_for=Decimal("25.00"),
            status="sold_out", scanner_lot=review.lot, ai_review=review,
        )

        stats = insights.ai_review_accuracy()

        # 25 is in range [20,30]; 40 (the lot's own final_price) would not be.
        self.assertEqual(stats["coins"]["pct_in_range"], 100.0)

    def test_written_off_ledger_entry_ignored_falls_back_to_lot_final_price(self):
        owner = User.objects.create_user(username="tester4", password="pw-not-real-12345")
        review = self._make_review("acc-7", resale_low="20.00", resale_high="30.00", final_price="25.00")
        LedgerEntry.objects.create(
            owner=owner, item="Written Off", cost=Decimal("10.00"), status="written_off",
            scanner_lot=review.lot, ai_review=review,
        )

        stats = insights.ai_review_accuracy()

        self.assertEqual(stats["coins"]["count"], 1)
        self.assertEqual(stats["coins"]["pct_in_range"], 100.0)  # used lot.final_price (25, in range)

    def test_filter_by_category(self):
        self._make_review("acc-8", category="coins", final_price="25.00")
        self._make_review("acc-9", category="jewelry", final_price="25.00")

        stats = insights.ai_review_accuracy(category="jewelry")

        self.assertEqual(list(stats.keys()), ["jewelry"])
