"""Tests for scanner/ai_review_service.py and the review views. All network
calls (OpenAI, Anthropic, eBay) are mocked - nothing here ever hits a real
API. scanner/ai_review.py's own grounding-rule tests live in
test_ai_review.py and aren't duplicated here.

RESELLING_SCANNER["AI_REVIEW"]["provider"] defaults to "openai" in real
settings, so tests that don't care which provider is used mock the OpenAI
client. Both provider packages are imported INSIDE
ai_review_service._make_client(), not at module load time, so they're
patched at their real location ("openai.OpenAI" / "anthropic.Anthropic"),
not on ai_review_service itself.
"""
import sys
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.conf import settings as django_settings
from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from albright_reselling_app.scanner.ai_review import ReviewResult
from albright_reselling_app.scanner.ai_review_service import ProviderUnavailable, ReviewLimitExceeded, review_lot
from albright_reselling_app.scanner_models import AIReview, LotEvaluation, SourcedLot

OPENAI_CLIENT = "openai.OpenAI"
ANTHROPIC_CLIENT = "anthropic.Anthropic"
RUN_REVIEW = "albright_reselling_app.scanner.ai_review_service.run_review"
EBAY_CONFIGURED = "albright_reselling_app.scanner.ai_review_service.ebay.is_configured"
EBAY_SEARCH = "albright_reselling_app.scanner.ai_review_service.ebay.search_active"


def _make_lot(external_id="lot-1", source="shopgoodwill", title="Morgan Silver Dollar", category="coins",
              max_bid="0", melt_value="0"):
    lot = SourcedLot.objects.create(
        source=source, external_id=external_id, url=f"https://example.com/{external_id}",
        title=title, current_price=Decimal("10.00"), end_time=timezone.now() + timedelta(hours=5),
    )
    LotEvaluation.objects.create(
        lot=lot, category=category, max_bid=Decimal(max_bid), melt_value=Decimal(melt_value),
    )
    return lot


def _result(resale_low=100.0, resale_high=140.0, confidence="high", cost_usd=0.05):
    return ReviewResult(
        identified_items=[{"name": "coin", "quantity": 1, "notes": ""}],
        comps=[{"title": "comp", "price": 120, "type": "sold", "date": "2026-09-01",
                "url": "https://ebay.com/x", "source": "eBay"}],
        resale_low=resale_low, resale_high=resale_high, confidence=confidence,
        red_flags=[], summary="ok", notes=[], cost_usd=cost_usd,
    )


def _with_provider(**overrides):
    """A full RESELLING_SCANNER dict with AI_REVIEW overridden - for
    override_settings() when a test needs a non-default provider/limit."""
    return {**django_settings.RESELLING_SCANNER,
            "AI_REVIEW": {**django_settings.RESELLING_SCANNER["AI_REVIEW"], **overrides}}


class ReviewLotTests(TestCase):
    @mock.patch(EBAY_CONFIGURED, return_value=False)
    @mock.patch(OPENAI_CLIENT)
    @mock.patch(RUN_REVIEW)
    def test_saves_done_review_with_suggested_max_bid(self, mock_run_review, mock_client_cls, mock_ebay_cfg):
        mock_run_review.return_value = _result(resale_low=100.0, resale_high=140.0, confidence="high")
        lot = _make_lot(category="coins")

        review = review_lot(lot)

        self.assertEqual(review.status, "done")
        self.assertEqual(review.resale_low, Decimal("100.00"))
        self.assertEqual(review.resale_high, Decimal("140.00"))
        self.assertEqual(review.confidence, "high")
        self.assertIsNotNone(review.suggested_max_bid)
        self.assertGreater(review.suggested_max_bid, 0)
        mock_client_cls.assert_called_once_with(timeout=120)

    @mock.patch(EBAY_CONFIGURED, return_value=False)
    @mock.patch(OPENAI_CLIENT)
    @mock.patch(RUN_REVIEW)
    def test_no_resale_low_means_no_suggested_max_bid(self, mock_run_review, mock_client_cls, mock_ebay_cfg):
        mock_run_review.return_value = _result(resale_low=None, resale_high=None, confidence="low")
        lot = _make_lot()

        review = review_lot(lot)

        self.assertEqual(review.status, "done")
        self.assertIsNone(review.resale_low)
        self.assertIsNone(review.suggested_max_bid)

    @mock.patch(EBAY_CONFIGURED, return_value=False)
    @mock.patch(OPENAI_CLIENT)
    @mock.patch(RUN_REVIEW)
    def test_saves_error_review_on_api_failure(self, mock_run_review, mock_client_cls, mock_ebay_cfg):
        mock_run_review.side_effect = RuntimeError("boom")
        lot = _make_lot()

        review = review_lot(lot)

        self.assertEqual(review.status, "error")
        self.assertIn("boom", review.error)
        self.assertEqual(review.cost_usd, Decimal("0"))
        self.assertIsNone(review.resale_low)

    def test_refuses_past_daily_limit(self):
        lot = _make_lot()
        AIReview.objects.create(lot=lot, status="done", cost_usd=Decimal("0.01"))

        with override_settings(RESELLING_SCANNER=_with_provider(daily_limit=1)):
            with self.assertRaises(ReviewLimitExceeded):
                review_lot(lot)

    def test_refuses_past_monthly_budget(self):
        lot = _make_lot()
        AIReview.objects.create(lot=lot, status="done", cost_usd=Decimal("0.05"))

        with override_settings(RESELLING_SCANNER=_with_provider(monthly_budget_usd=0.05)):
            with self.assertRaises(ReviewLimitExceeded):
                review_lot(lot)

    @mock.patch(EBAY_SEARCH)
    @mock.patch(EBAY_CONFIGURED, return_value=False)
    @mock.patch(OPENAI_CLIENT)
    @mock.patch(RUN_REVIEW)
    def test_skips_ebay_when_not_configured(self, mock_run_review, mock_client_cls, mock_ebay_cfg, mock_ebay_search):
        mock_run_review.return_value = _result()
        lot = _make_lot()

        review_lot(lot)

        mock_ebay_search.assert_not_called()

    @mock.patch(EBAY_SEARCH)
    @mock.patch(EBAY_CONFIGURED, return_value=True)
    @mock.patch(OPENAI_CLIENT)
    @mock.patch(RUN_REVIEW)
    def test_uses_ebay_when_configured(self, mock_run_review, mock_client_cls, mock_ebay_cfg, mock_ebay_search):
        mock_run_review.return_value = _result()
        mock_ebay_search.return_value = [{"title": "x", "price": 10.0, "url": "https://ebay.com/1"}]
        lot = _make_lot()

        review_lot(lot)

        mock_ebay_search.assert_called_once()
        self.assertEqual(mock_run_review.call_args.args[3], mock_ebay_search.return_value)


class ProviderSelectionTests(TestCase):
    """The provider setting picks the right client, and a missing provider
    package produces a clear error review instead of crashing."""

    @mock.patch(EBAY_CONFIGURED, return_value=False)
    @mock.patch(ANTHROPIC_CLIENT)
    @mock.patch(OPENAI_CLIENT)
    @mock.patch(RUN_REVIEW)
    def test_default_provider_uses_openai_client(self, mock_run_review, mock_openai_cls, mock_anthropic_cls,
                                                  mock_ebay_cfg):
        mock_run_review.return_value = _result()
        lot = _make_lot()

        review_lot(lot)

        mock_openai_cls.assert_called_once_with(timeout=120)
        mock_anthropic_cls.assert_not_called()

    @mock.patch(EBAY_CONFIGURED, return_value=False)
    @mock.patch(ANTHROPIC_CLIENT)
    @mock.patch(OPENAI_CLIENT)
    @mock.patch(RUN_REVIEW)
    def test_anthropic_provider_uses_anthropic_client(self, mock_run_review, mock_openai_cls, mock_anthropic_cls,
                                                        mock_ebay_cfg):
        mock_run_review.return_value = _result()
        lot = _make_lot()

        with override_settings(RESELLING_SCANNER=_with_provider(provider="anthropic")):
            review_lot(lot)

        mock_anthropic_cls.assert_called_once_with(timeout=120)
        mock_openai_cls.assert_not_called()

    def test_missing_openai_package_saves_error_review_and_raises(self):
        lot = _make_lot()

        with mock.patch.dict(sys.modules, {"openai": None}):
            with self.assertRaises(ProviderUnavailable):
                review_lot(lot)

        review = AIReview.objects.get()
        self.assertEqual(review.status, "error")
        self.assertIn("openai package is not installed", review.error)
        self.assertEqual(review.cost_usd, Decimal("0"))

    def test_missing_anthropic_package_saves_error_review_and_raises(self):
        lot = _make_lot()

        with override_settings(RESELLING_SCANNER=_with_provider(provider="anthropic")):
            with mock.patch.dict(sys.modules, {"anthropic": None}):
                with self.assertRaises(ProviderUnavailable):
                    review_lot(lot)

        review = AIReview.objects.get()
        self.assertEqual(review.status, "error")
        self.assertIn("anthropic package is not installed", review.error)


class RequestReviewViewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="tester", password="pw-not-real-12345")

    def test_requires_login(self):
        lot = _make_lot()
        response = self.client.post(reverse("albright_reselling_app:ai_review_request", args=[lot.pk]))

        self.assertEqual(response.status_code, 302)
        self.assertFalse(AIReview.objects.exists())

    def test_reuses_recent_review_unless_force(self):
        self.client.login(username="tester", password="pw-not-real-12345")
        lot = _make_lot()
        recent = AIReview.objects.create(lot=lot, status="done", resale_low=Decimal("100"),
                                          resale_high=Decimal("140"), cost_usd=Decimal("0.05"))

        response = self.client.post(reverse("albright_reselling_app:ai_review_request", args=[lot.pk]))

        self.assertRedirects(response, reverse("albright_reselling_app:ai_review_detail", args=[recent.pk]))
        self.assertEqual(AIReview.objects.count(), 1)  # no new review created

    @mock.patch(EBAY_CONFIGURED, return_value=False)
    @mock.patch(OPENAI_CLIENT)
    @mock.patch(RUN_REVIEW)
    def test_force_runs_new_review_even_if_recent_exists(self, mock_run_review, mock_client_cls, mock_ebay_cfg):
        self.client.login(username="tester", password="pw-not-real-12345")
        mock_run_review.return_value = _result()
        lot = _make_lot()
        recent = AIReview.objects.create(lot=lot, status="done", resale_low=Decimal("100"),
                                          resale_high=Decimal("140"), cost_usd=Decimal("0.05"))

        response = self.client.post(
            reverse("albright_reselling_app:ai_review_request", args=[lot.pk]), {"force": "1"}
        )

        self.assertEqual(AIReview.objects.count(), 2)
        new_review = AIReview.objects.exclude(pk=recent.pk).get()
        self.assertRedirects(response, reverse("albright_reselling_app:ai_review_detail", args=[new_review.pk]))

    def test_limit_refusal_redirects_to_dashboard_with_message(self):
        self.client.login(username="tester", password="pw-not-real-12345")
        # The existing review is for a DIFFERENT lot, so it counts toward
        # today's total without triggering the "reuse a recent review of
        # *this* lot" shortcut for the lot we're actually requesting.
        already_reviewed_lot = _make_lot(external_id="already-reviewed")
        AIReview.objects.create(lot=already_reviewed_lot, status="done", cost_usd=Decimal("0.01"))
        lot = _make_lot(external_id="needs-review")

        with override_settings(RESELLING_SCANNER=_with_provider(daily_limit=1)):
            response = self.client.post(
                reverse("albright_reselling_app:ai_review_request", args=[lot.pk]), follow=True
            )

        self.assertRedirects(response, reverse("albright_reselling_app:dashboard"))
        self.assertContains(response, "Daily AI review limit reached")

    def test_missing_package_redirects_to_dashboard_with_message(self):
        self.client.login(username="tester", password="pw-not-real-12345")
        lot = _make_lot()

        with mock.patch.dict(sys.modules, {"openai": None}):
            response = self.client.post(
                reverse("albright_reselling_app:ai_review_request", args=[lot.pk]), follow=True
            )

        self.assertRedirects(response, reverse("albright_reselling_app:dashboard"))
        self.assertContains(response, "openai package is not installed")
        # ...and it's still on record as an error review, not silently dropped.
        review = AIReview.objects.get()
        self.assertEqual(review.status, "error")


class ReviewDetailViewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="tester", password="pw-not-real-12345")
        self.client.login(username="tester", password="pw-not-real-12345")

    def test_renders_comps_and_notes(self):
        lot = _make_lot()
        review = AIReview.objects.create(
            lot=lot, status="done", model_name="test-model",
            result={
                "identified_items": [{"name": "Morgan Dollar", "quantity": 1, "notes": ""}],
                "comps": [{"title": "Morgan Dollar sold", "price": 45.0, "type": "sold", "date": "2026-09-01",
                           "url": "https://ebay.com/itm/1", "source": "eBay"}],
                "dropped_comps": 1,
                "notes": ["1 comp(s) dropped: link not found in search results"],
                "red_flags": ["possible cleaning"],
                "summary": "Solid Morgan dollar, typical wear.",
            },
            resale_low=Decimal("40.00"), resale_high=Decimal("55.00"), suggested_max_bid=Decimal("30.00"),
            confidence="high", cost_usd=Decimal("0.0523"),
        )

        response = self.client.get(reverse("albright_reselling_app:ai_review_detail", args=[review.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Morgan Dollar sold")
        self.assertContains(response, "possible cleaning")
        self.assertContains(response, "Solid Morgan dollar")
        self.assertContains(response, "link not found in search results")
        self.assertContains(response, lot.title)

    def test_error_review_shows_error_message(self):
        lot = _make_lot()
        review = AIReview.objects.create(lot=lot, status="error", error="OpenAI API timed out",
                                          cost_usd=Decimal("0"))

        response = self.client.get(reverse("albright_reselling_app:ai_review_detail", args=[review.pk]))

        self.assertContains(response, "OpenAI API timed out")

    def test_requires_login(self):
        self.client.logout()
        lot = _make_lot()
        review = AIReview.objects.create(lot=lot, status="done", cost_usd=Decimal("0"))

        response = self.client.get(reverse("albright_reselling_app:ai_review_detail", args=[review.pk]))

        self.assertEqual(response.status_code, 302)
