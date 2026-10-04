"""Tests for LotFeedback ("Wrong?" reports): creation from the dashboard's
expandable lot rows and the AI review page, resolving, and exporting open
reports as text test cases. No HTTP involved beyond the Django test
client - nothing ever reaches a real auction site."""
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from albright_reselling_app.scanner_models import AIReview, LotEvaluation, LotFeedback, SourcedLot


def _make_lot(external_id, end_time=None, title="Lot of 8 Nintendo 64 Games"):
    return SourcedLot.objects.create(
        source="shopgoodwill", external_id=external_id, url=f"https://example.com/{external_id}",
        title=title, description="Bulk lot, untested", current_price=Decimal("10.00"),
        end_time=end_time or (timezone.now() + timedelta(hours=3)),
    )


class LotFeedbackFormViewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="tester", password="pw-not-real-12345")
        self.client.login(username="tester", password="pw-not-real-12345")

    def test_get_shows_form(self):
        lot = _make_lot("fb-get-1")

        response = self.client.get(reverse("albright_reselling_app:lot_feedback", args=[lot.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, lot.title)
        self.assertContains(response, "Wrong?")

    def test_post_creates_feedback_and_redirects_to_next(self):
        lot = _make_lot("fb-post-1")

        response = self.client.post(
            f"{reverse('albright_reselling_app:lot_feedback', args=[lot.pk])}?next=/albright_reselling_app/",
            {"kind": "misread", "note": "Not actually silver", "next": "/albright_reselling_app/"},
        )

        self.assertRedirects(response, "/albright_reselling_app/")
        feedback = LotFeedback.objects.get(lot=lot)
        self.assertEqual(feedback.kind, "misread")
        self.assertEqual(feedback.note, "Not actually silver")
        self.assertFalse(feedback.resolved)

    def test_post_defaults_next_to_dashboard_when_absent(self):
        lot = _make_lot("fb-post-2")

        response = self.client.post(
            reverse("albright_reselling_app:lot_feedback", args=[lot.pk]), {"kind": "other", "note": ""},
        )

        self.assertRedirects(response, reverse("albright_reselling_app:dashboard"))

    def test_off_site_next_is_ignored(self):
        """_safe_next only accepts a same-site path - a crafted ?next=
        can't redirect the user off-site."""
        lot = _make_lot("fb-post-3")

        response = self.client.post(
            f"{reverse('albright_reselling_app:lot_feedback', args=[lot.pk])}?next=https://evil.example.com",
            {"kind": "other", "note": "", "next": "https://evil.example.com"},
        )

        self.assertRedirects(response, reverse("albright_reselling_app:dashboard"))

    def test_requires_login(self):
        self.client.logout()
        lot = _make_lot("fb-nologin")

        response = self.client.get(reverse("albright_reselling_app:lot_feedback", args=[lot.pk]))

        self.assertEqual(response.status_code, 302)

    def test_wrong_button_shown_on_dashboard_row(self):
        lot = _make_lot("fb-dash-1")
        LotEvaluation.objects.create(lot=lot, category="games", is_lead=True, lead_reason="n64: bulk lot")

        response = self.client.get(reverse("albright_reselling_app:dashboard"))

        self.assertContains(response, reverse("albright_reselling_app:lot_feedback", args=[lot.pk]))

    def test_wrong_button_shown_on_ai_review_page(self):
        lot = _make_lot("fb-review-1")
        LotEvaluation.objects.create(lot=lot, category="coins", max_bid=Decimal("20.00"))
        review = AIReview.objects.create(
            lot=lot, status="done", resale_low=Decimal("25.00"), resale_high=Decimal("35.00"),
            cost_usd=Decimal("0.05"),
        )

        response = self.client.get(reverse("albright_reselling_app:ai_review_detail", args=[review.pk]))

        self.assertContains(response, reverse("albright_reselling_app:lot_feedback", args=[lot.pk]))


class ResolveLotFeedbackTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="tester", password="pw-not-real-12345")
        self.client.login(username="tester", password="pw-not-real-12345")

    def test_resolve_marks_resolved(self):
        lot = _make_lot("fb-resolve-1")
        feedback = LotFeedback.objects.create(lot=lot, kind="not_a_deal", note="Already over my max")

        response = self.client.post(reverse("albright_reselling_app:resolve_lot_feedback", args=[feedback.pk]))

        self.assertRedirects(response, reverse("albright_reselling_app:insights"))
        feedback.refresh_from_db()
        self.assertTrue(feedback.resolved)

    def test_resolved_report_not_shown_on_insights_page(self):
        lot = _make_lot("fb-resolve-2")
        LotFeedback.objects.create(lot=lot, kind="other", note="", resolved=True)

        response = self.client.get(reverse("albright_reselling_app:insights"))

        self.assertNotContains(response, lot.title)

    def test_open_report_shown_on_insights_page_with_scanner_notes(self):
        lot = _make_lot("fb-open-1")
        LotEvaluation.objects.create(
            lot=lot, category="games", is_lead=True, lead_reason="n64: bulk lot", flags=["bulk lot"],
        )
        LotFeedback.objects.create(lot=lot, kind="wrong_category", note="This is actually jewelry")

        response = self.client.get(reverse("albright_reselling_app:insights"))

        self.assertContains(response, lot.title)
        self.assertContains(response, "n64: bulk lot")
        self.assertContains(response, "This is actually jewelry")


class ExportLotFeedbackTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="tester", password="pw-not-real-12345")
        self.client.login(username="tester", password="pw-not-real-12345")

    def test_export_includes_title_description_and_note_per_open_report(self):
        lot = _make_lot("fb-export-1", title="14k Gold Ring No Weight")
        LotFeedback.objects.create(lot=lot, kind="misread", note="Actually 10k, not 14k")

        response = self.client.get(reverse("albright_reselling_app:export_lot_feedback"))
        content = response.content.decode()

        self.assertEqual(response["Content-Type"], "text/plain")
        self.assertIn("attachment", response["Content-Disposition"])
        self.assertIn("Title: 14k Gold Ring No Weight", content)
        self.assertIn("Description: Bulk lot, untested", content)
        self.assertIn("Note: Actually 10k, not 14k", content)

    def test_export_excludes_resolved_reports(self):
        lot = _make_lot("fb-export-2", title="Resolved Lot")
        LotFeedback.objects.create(lot=lot, kind="other", note="already handled", resolved=True)

        response = self.client.get(reverse("albright_reselling_app:export_lot_feedback"))

        self.assertNotIn("Resolved Lot", response.content.decode())

    def test_export_empty_when_no_open_reports(self):
        response = self.client.get(reverse("albright_reselling_app:export_lot_feedback"))

        self.assertEqual(response.content.decode(), "")
