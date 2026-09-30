"""Tests for the AI Reviews history page: scanner/review_history.py's
query/filter/summary/CSV logic directly, and the review_history view end to
end via the test client. No real API calls happen anywhere here."""
import csv
import io
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from albright_reselling_app.scanner.review_history import CSV_HEADERS, filtered_reviews, filters_from_params, \
    summary_stats
from albright_reselling_app.scanner_models import AIReview, LotEvaluation, SourcedLot


def _make_lot(external_id, source="shopgoodwill", title=None, is_closed=False, final_price=None, end_time=None):
    return SourcedLot.objects.create(
        source=source, external_id=external_id, url=f"https://example.com/{external_id}",
        title=title or f"Lot {external_id}", current_price=Decimal("10.00"),
        end_time=end_time or (timezone.now() + timedelta(hours=5)),
        is_closed=is_closed, final_price=Decimal(str(final_price)) if final_price is not None else None,
    )


def _make_evaluation(lot, category="coins"):
    return LotEvaluation.objects.create(lot=lot, category=category)


def _make_review(lot, created_days_ago=0, confidence="high", status="done", cost_usd="0.05",
                  suggested_max_bid=None, resale_low=100, resale_high=140):
    review = AIReview.objects.create(
        lot=lot, status=status, confidence=confidence, cost_usd=Decimal(str(cost_usd)),
        resale_low=Decimal(str(resale_low)) if resale_low is not None else None,
        resale_high=Decimal(str(resale_high)) if resale_high is not None else None,
        suggested_max_bid=Decimal(str(suggested_max_bid)) if suggested_max_bid is not None else None,
        result={"comps": [{"url": "https://ebay.com/1"}], "red_flags": ["cleaned"], "summary": "solid lot"},
    )
    if created_days_ago:
        AIReview.objects.filter(pk=review.pk).update(created_at=timezone.now() - timedelta(days=created_days_ago))
        review.refresh_from_db()
    return review


def _filters(**overrides):
    base = filters_from_params({})
    base.update(overrides)
    return base


class FilteredReviewsTests(TestCase):
    def test_newest_first_by_default(self):
        lot_a = _make_lot("a")
        lot_b = _make_lot("b")
        older = _make_review(lot_a, created_days_ago=5)
        newer = _make_review(lot_b)

        results = list(filtered_reviews(_filters()))

        self.assertEqual(results, [newer, older])

    def test_filter_by_source(self):
        gw_lot = _make_lot("gw", source="shopgoodwill")
        ms_lot = _make_lot("ms", source="maxsold")
        _make_review(gw_lot)
        _make_review(ms_lot)

        results = filtered_reviews(_filters(source="maxsold"))

        self.assertEqual([r.lot_id for r in results], [ms_lot.pk])

    def test_filter_by_category(self):
        coin_lot = _make_lot("coin")
        _make_evaluation(coin_lot, category="coins")
        jewelry_lot = _make_lot("jewelry")
        _make_evaluation(jewelry_lot, category="jewelry")
        _make_review(coin_lot)
        _make_review(jewelry_lot)

        results = filtered_reviews(_filters(category="jewelry"))

        self.assertEqual([r.lot_id for r in results], [jewelry_lot.pk])

    def test_filter_by_confidence(self):
        lot_high = _make_lot("high")
        lot_low = _make_lot("low")
        _make_review(lot_high, confidence="high")
        _make_review(lot_low, confidence="low")

        results = filtered_reviews(_filters(confidence="low"))

        self.assertEqual([r.lot_id for r in results], [lot_low.pk])

    def test_filter_by_status(self):
        lot_done = _make_lot("done")
        lot_error = _make_lot("error")
        _make_review(lot_done, status="done")
        _make_review(lot_error, status="error")

        results = filtered_reviews(_filters(status="error"))

        self.assertEqual([r.lot_id for r in results], [lot_error.pk])

    def test_filter_by_search_text(self):
        _make_lot("m1", title="Morgan Silver Dollar")
        _make_lot("m2", title="Gold Ring Lot")
        _make_review(SourcedLot.objects.get(external_id="m1"))
        _make_review(SourcedLot.objects.get(external_id="m2"))

        results = filtered_reviews(_filters(q="morgan"))

        self.assertEqual([r.lot.title for r in results], ["Morgan Silver Dollar"])

    def test_filter_by_date_range(self):
        lot_recent = _make_lot("recent")
        lot_old = _make_lot("old")
        _make_review(lot_recent)
        _make_review(lot_old, created_days_ago=10)

        today = timezone.localdate()
        results = filtered_reviews(_filters(date_from=(today - timedelta(days=2)).isoformat()))

        self.assertEqual([r.lot_id for r in results], [lot_recent.pk])

    def test_filter_by_outcome_live(self):
        live_lot = _make_lot("live", is_closed=False)
        closed_lot = _make_lot("closed", is_closed=True, final_price="50.00")
        _make_review(live_lot)
        _make_review(closed_lot, suggested_max_bid="60.00")

        results = filtered_reviews(_filters(outcome="live"))

        self.assertEqual([r.lot_id for r in results], [live_lot.pk])

    def test_filter_by_outcome_under_and_over(self):
        under_lot = _make_lot("under", is_closed=True, final_price="40.00")
        over_lot = _make_lot("over", is_closed=True, final_price="80.00")
        _make_review(under_lot, suggested_max_bid="60.00")
        _make_review(over_lot, suggested_max_bid="60.00")

        under_results = filtered_reviews(_filters(outcome="under"))
        over_results = filtered_reviews(_filters(outcome="over"))

        self.assertEqual([r.lot_id for r in under_results], [under_lot.pk])
        self.assertEqual([r.lot_id for r in over_results], [over_lot.pk])


class SummaryStatsTests(TestCase):
    def test_totals_this_month_and_by_confidence_status(self):
        _make_review(_make_lot("r1"), confidence="high", status="done", cost_usd="0.05")
        _make_review(_make_lot("r2"), confidence="high", status="done", cost_usd="0.05")
        _make_review(_make_lot("r3"), confidence="low", status="error", cost_usd="0.10", created_days_ago=40)

        summary = summary_stats()

        self.assertEqual(summary["total_reviews"], 3)
        self.assertEqual(summary["reviews_this_month"], 2)
        self.assertEqual(summary["total_cost"], Decimal("0.20"))
        self.assertEqual(summary["month_cost"], Decimal("0.10"))
        self.assertAlmostEqual(float(summary["avg_cost"]), 0.20 / 3, places=6)
        self.assertEqual(summary["by_confidence"], {"high": 2, "medium": 0, "low": 1})
        self.assertEqual(summary["by_status"]["done"], 2)
        self.assertEqual(summary["by_status"]["error"], 1)


class ReviewHistoryViewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="tester", password="pw-not-real-12345")

    def test_requires_login(self):
        response = self.client.get(reverse("albright_reselling_app:ai_review_history"))
        self.assertEqual(response.status_code, 302)

    def test_lists_reviews_newest_first(self):
        self.client.login(username="tester", password="pw-not-real-12345")
        older = _make_review(_make_lot("older", title="Older Lot"), created_days_ago=3)
        newer = _make_review(_make_lot("newer", title="Newer Lot"))

        response = self.client.get(reverse("albright_reselling_app:ai_review_history"))

        rows = response.context["rows"]
        self.assertEqual([r["review"].pk for r in rows], [newer.pk, older.pk])
        self.assertContains(response, "Newer Lot")
        self.assertContains(response, "Older Lot")

    def test_paginates_at_25_per_page(self):
        self.client.login(username="tester", password="pw-not-real-12345")
        for i in range(30):
            _make_review(_make_lot(f"lot-{i}", title=f"Lot {i}"))

        page1 = self.client.get(reverse("albright_reselling_app:ai_review_history"))
        page2 = self.client.get(reverse("albright_reselling_app:ai_review_history"), {"page": 2})

        self.assertEqual(len(page1.context["rows"]), 25)
        self.assertEqual(len(page2.context["rows"]), 5)
        self.assertEqual(page1.context["page"].paginator.count, 30)

    def test_outcome_column_shows_under_and_over(self):
        self.client.login(username="tester", password="pw-not-real-12345")
        under_lot = _make_lot("under-lot", title="Under Lot", is_closed=True, final_price="40.00")
        over_lot = _make_lot("over-lot", title="Over Lot", is_closed=True, final_price="80.00")
        _make_review(under_lot, suggested_max_bid="60.00")
        _make_review(over_lot, suggested_max_bid="60.00")

        response = self.client.get(reverse("albright_reselling_app:ai_review_history"))

        by_title = {r["lot_title"]: r["outcome"] for r in response.context["rows"]}
        self.assertEqual(by_title["Under Lot"]["verdict"], "under")
        self.assertEqual(by_title["Over Lot"]["verdict"], "over")
        self.assertContains(response, "Under AI max")
        self.assertContains(response, "Over AI max")

    def test_csv_export_returns_filtered_rows_with_headers(self):
        self.client.login(username="tester", password="pw-not-real-12345")
        _make_review(_make_lot("keep", title="Keep Me"), status="done")
        _make_review(_make_lot("drop", title="Drop Me"), status="error")

        response = self.client.get(
            reverse("albright_reselling_app:ai_review_history"), {"status": "done", "export": "csv"}
        )

        self.assertEqual(response["Content-Type"], "text/csv")
        reader = csv.reader(io.StringIO(response.content.decode("utf-8")))
        rows = list(reader)
        self.assertEqual(rows[0], CSV_HEADERS)
        self.assertEqual(len(rows), 2)  # header + one matching review
        self.assertIn("Keep Me", rows[1])
        self.assertNotIn("Drop Me", [cell for row in rows for cell in row])
