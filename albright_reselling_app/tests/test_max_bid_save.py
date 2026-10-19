"""Saving a max bid never strands you on another page: the save endpoint
(scanner_views.watch_lot) answers the Watchlist page's fetch() with the
row's new numbers as JSON, and a plain (no-JS) form POST redirects back to
its "next" page - this site's URLs only, defaulting to the Watchlist page.
Same for "Use as my max bid" and the star's "Set max bid" link."""
import re
from datetime import timedelta
from urllib.parse import quote
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from albright_reselling_app.scanner_models import AIReview, BidWatch, LotEvaluation, LotUserEstimate, SourcedLot

AJAX = {"HTTP_X_REQUESTED_WITH": "XMLHttpRequest"}


def _lot(external_id, current_price="10.00", scanner_max="30.00"):
    lot = SourcedLot.objects.create(
        source="shopgoodwill", external_id=external_id, url=f"https://example.com/{external_id}",
        title=f"Max Bid Lot {external_id}", current_price=Decimal(current_price),
        end_time=timezone.now() + timedelta(hours=5), raw={},
    )
    LotEvaluation.objects.create(lot=lot, category="coins", max_bid=Decimal(scanner_max))
    return lot


class MaxBidSaveTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="tester", password="pw-not-real-12345")
        self.client.login(username="tester", password="pw-not-real-12345")

    def save(self, lot, value, **kwargs):
        data = {"my_max_bid": value, **kwargs.pop("data", {})}
        return self.client.post(reverse("albright_reselling_app:watch_lot", args=[lot.pk]), data, **kwargs)

    # ---------------- AJAX ----------------
    def test_ajax_save_returns_updated_row_values(self):
        lot = _lot("ajax-1", current_price="10.00")
        BidWatch.objects.create(lot=lot, status="watching")

        response = self.save(lot, "25.00", **AJAX)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {
            "ok": True, "lot_id": lot.pk, "my_max_bid": 25.0, "my_bid": 25.0, "my_bid_source": "mine",
            "current_bid": 10.0, "headroom": 15.0, "is_over_my_bid": False,
        })
        self.assertEqual(BidWatch.objects.get(lot=lot).my_max_bid, Decimal("25.00"))

    def test_ajax_save_below_current_bid_turns_red(self):
        lot = _lot("ajax-2", current_price="30.00")
        BidWatch.objects.create(lot=lot, status="watching")

        data = self.save(lot, "25.00", **AJAX).json()

        self.assertEqual(data["headroom"], -5.0)
        self.assertTrue(data["is_over_my_bid"])

    def test_ajax_clearing_falls_back_to_scanner_max(self):
        lot = _lot("ajax-3", current_price="10.00", scanner_max="30.00")
        BidWatch.objects.create(lot=lot, status="watching", my_max_bid=Decimal("25.00"))

        data = self.save(lot, "", **AJAX).json()

        self.assertIsNone(data["my_max_bid"])
        self.assertEqual((data["my_bid"], data["my_bid_source"], data["headroom"]), (30.0, "scanner", 20.0))
        self.assertIsNone(BidWatch.objects.get(lot=lot).my_max_bid)

    def test_ajax_invalid_value_is_a_400_with_message(self):
        lot = _lot("ajax-4")
        watch = BidWatch.objects.create(lot=lot, status="watching", my_max_bid=Decimal("25.00"))

        response = self.save(lot, "abc", **AJAX)

        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.json()["ok"])
        self.assertTrue(response.json()["error"])
        watch.refresh_from_db()
        self.assertEqual(watch.my_max_bid, Decimal("25.00"))

    # ---------------- No-JS form ----------------
    def test_form_post_from_watchlist_redirects_back_to_watchlist(self):
        lot = _lot("form-1")
        BidWatch.objects.create(lot=lot, status="watching")
        watchlist = reverse("albright_reselling_app:watchlist")

        response = self.save(lot, "25.00", data={"next": watchlist})

        self.assertRedirects(response, watchlist)
        self.assertEqual(BidWatch.objects.get(lot=lot).my_max_bid, Decimal("25.00"))

    def test_form_post_keeps_query_and_hash_free_next_paths(self):
        lot = _lot("form-2")
        BidWatch.objects.create(lot=lot, status="watching")
        next_url = reverse("albright_reselling_app:dashboard") + "?source=hibid"

        response = self.save(lot, "25.00", data={"next": next_url})

        self.assertRedirects(response, next_url)

    def test_unsafe_next_is_ignored(self):
        lot = _lot("form-3")
        BidWatch.objects.create(lot=lot, status="watching")
        watchlist = reverse("albright_reselling_app:watchlist")

        for unsafe in ("https://evil.example.com/steal", "//evil.example.com/", "javascript:alert(1)",
                       "http://testserver.evil.com/"):
            with self.subTest(next=unsafe):
                response = self.save(lot, "25.00", data={"next": unsafe})
                self.assertRedirects(response, watchlist)

    def test_no_next_defaults_to_watchlist(self):
        lot = _lot("form-4")

        response = self.save(lot, "25.00")

        self.assertRedirects(response, reverse("albright_reselling_app:watchlist"))

    def test_full_page_form_carries_next_through(self):
        lot = _lot("form-5")
        BidWatch.objects.create(lot=lot, status="watching")
        review = AIReview.objects.create(lot=lot, status="done", cost_usd=Decimal("0"))
        back = reverse("albright_reselling_app:ai_review_detail", args=[review.pk])

        content = self.client.get(
            reverse("albright_reselling_app:watch_lot", args=[lot.pk]), {"next": back},
        ).content.decode()

        self.assertIn(f'<input type="hidden" name="next" value="{back}">', content)
        self.assertIn(f'<a href="{back}"', content)
        self.assertNotIn("Back to dashboard", content)

    # ---------------- Other entry points ----------------
    def test_watchlist_row_has_inline_form_pointing_back_here(self):
        lot = _lot("inline-1")
        BidWatch.objects.create(lot=lot, status="watching", my_max_bid=Decimal("25.00"))

        content = self.client.get(reverse("albright_reselling_app:watchlist")).content.decode()
        row = re.search(rf'<tr class="scanner-row" data-lot-id="{lot.pk}".*?</tr>', content, re.S).group(0)

        self.assertIn(f'action="{reverse("albright_reselling_app:watch_lot", args=[lot.pk])}"', row)
        self.assertIn("js-max-bid-form", row)
        self.assertIn(f'name="next" value="{reverse("albright_reselling_app:watchlist")}"', row)
        self.assertIn('name="my_max_bid"', row)
        self.assertIn('value="25.00"', row)
        self.assertIn("js-max-bid-status", row)

    def test_use_as_my_max_form_returns_to_watchlist(self):
        lot = _lot("use-1")
        BidWatch.objects.create(lot=lot, status="watching")
        LotUserEstimate.objects.create(lot=lot, resale_estimate=Decimal("100"), my_max_bid=Decimal("42.29"))

        content = self.client.get(reverse("albright_reselling_app:watchlist")).content.decode()
        response = self.client.post(reverse("albright_reselling_app:use_my_max_bid", args=[lot.pk]),
                                    {"next": "https://evil.example.com/"})

        self.assertIn("js-use-my-max", content)
        self.assertRedirects(response, reverse("albright_reselling_app:watchlist"))

    def test_star_set_max_bid_link_returns_to_current_page(self):
        lot = _lot("star-1")
        BidWatch.objects.create(lot=lot, status="watching")
        review = AIReview.objects.create(lot=lot, status="done", cost_usd=Decimal("0"))
        page = reverse("albright_reselling_app:ai_review_detail", args=[review.pk])

        content = self.client.get(page).content.decode()

        expected = f'{reverse("albright_reselling_app:watch_lot", args=[lot.pk])}?next={quote(page, safe="/")}'  # |urlencode keeps "/"
        self.assertIn(f'href="{expected}"', content)

    def test_ai_review_page_does_not_treat_archived_watch_as_watching(self):
        lot = _lot("star-2")
        BidWatch.objects.create(lot=lot, status="watching", archived_at=timezone.now(), archive_reason="soft_removed")
        review = AIReview.objects.create(lot=lot, status="done", cost_usd=Decimal("0"))

        content = self.client.get(reverse("albright_reselling_app:ai_review_detail", args=[review.pk])).content.decode()

        self.assertIn('<span class="js-watch-label">Add to Watchlist</span>', content)
