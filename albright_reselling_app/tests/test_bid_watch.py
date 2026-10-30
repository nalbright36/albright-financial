"""Tests for BidWatch: default_max_bid's prefill rule, the "set max bid"
view (watch_lot - creating or editing a watch), the one-click watchlist
toggle (toggle_watch), the Watchlist section's own AJAX refresh partial
(watchlist_section), and the watch-resolution sweep (scanner/
bid_watch.py) - including that a result alert is off by default and only
fires once enabled. The "closing soon" alert itself has moved to
scanner/alerts.send_watchlist_alerts() - see test_alerts.py and
test_watchlist_alerts_command.py. Dashboard exclusion (a watchlisted lot
never also appears in an Act Now/Watch table) is covered in
test_dashboard.py's WatchlistExclusionTests, not here. Every HTTP call
(Telegram, the adapters' own requests) is mocked - nothing here reaches a
real site."""
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.conf import settings
from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from albright_reselling_app.scanner.adapters.base import RawLot, SourceBlocked
from albright_reselling_app.scanner.bid_watch import RESOLVE_GIVE_UP_AFTER, default_max_bid, resolve_watches
from albright_reselling_app.scanner_models import AIReview, BidWatch, LotEvaluation, SourcedLot
from albright_labs.testing import with_apps

SEND_TELEGRAM = "albright_reselling_app.scanner.bid_watch.send_telegram"
SEARCH_CLOSED = "albright_reselling_app.scanner.adapters.shopgoodwill.ShopGoodwillAdapter.search_closed"
PAUSE = "albright_reselling_app.scanner.adapters.shopgoodwill.ShopGoodwillAdapter.pause"


def _cfg(**alerts_overrides):
    base = {"enabled": True, "window_minutes": 90, "kinds": ["candidate", "lead"], "min_headroom": 5.0,
            "candidate_confidence": ["high", "medium"], "max_per_run": 10}
    return {**settings.RESELLING_SCANNER, "ALERTS": {**base, **alerts_overrides}}


RESULT_ALERTS_ON = _cfg(watchlist_result_alerts=True)


def _make_lot(external_id, end_time, source="shopgoodwill", current_price="10.00", is_closed=False,
              final_price=None):
    return SourcedLot.objects.create(
        source=source, external_id=external_id, url=f"https://example.com/{external_id}",
        title=f"Morgan Silver Dollar {external_id}", current_price=Decimal(current_price), end_time=end_time,
        is_closed=is_closed, final_price=Decimal(str(final_price)) if final_price is not None else None,
    )


def _make_evaluation(lot, max_bid="20.00"):
    return LotEvaluation.objects.create(lot=lot, category="coins", max_bid=Decimal(max_bid))


class DefaultMaxBidTests(TestCase):
    def test_prefers_ai_review_suggested_max(self):
        lot = _make_lot("dmb-1", timezone.now() + timedelta(hours=3))
        evaluation = _make_evaluation(lot, max_bid="20.00")
        review = AIReview.objects.create(
            lot=lot, status="done", suggested_max_bid=Decimal("25.00"), cost_usd=Decimal("0.05"),
        )

        self.assertEqual(default_max_bid(evaluation, review), Decimal("25.00"))

    def test_falls_back_to_scanner_max_without_review(self):
        lot = _make_lot("dmb-2", timezone.now() + timedelta(hours=3))
        evaluation = _make_evaluation(lot, max_bid="20.00")

        self.assertEqual(default_max_bid(evaluation, None), Decimal("20.00"))

    def test_none_without_evaluation_or_review(self):
        self.assertIsNone(default_max_bid(None, None))


class ToggleWatchViewTests(TestCase):
    def setUp(self):
        self.user = with_apps(User.objects.create_user(username="tester", password="pw-not-real-12345"))
        self.client.login(username="tester", password="pw-not-real-12345")

    def test_adds_lot_with_no_max_bid(self):
        lot = _make_lot("toggle-1", timezone.now() + timedelta(hours=3))

        response = self.client.post(reverse("albright_reselling_app:toggle_watch", args=[lot.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"watching": True})
        watch = BidWatch.objects.get(lot=lot)
        self.assertIsNone(watch.my_max_bid)
        self.assertEqual(watch.status, "watching")

    def test_second_click_removes_it(self):
        lot = _make_lot("toggle-2", timezone.now() + timedelta(hours=3))
        BidWatch.objects.create(lot=lot, status="watching")

        response = self.client.post(reverse("albright_reselling_app:toggle_watch", args=[lot.pk]))

        self.assertEqual(response.json(), {"watching": False})
        self.assertFalse(BidWatch.objects.filter(lot=lot).exists())

    def test_removes_a_watch_with_a_max_bid_set_too(self):
        lot = _make_lot("toggle-3", timezone.now() + timedelta(hours=3))
        BidWatch.objects.create(lot=lot, status="watching", my_max_bid=Decimal("25.00"))

        self.client.post(reverse("albright_reselling_app:toggle_watch", args=[lot.pk]))

        self.assertFalse(BidWatch.objects.filter(lot=lot).exists())

    def test_rejects_a_closed_lot(self):
        lot = _make_lot("toggle-4", timezone.now() - timedelta(hours=1))

        response = self.client.post(reverse("albright_reselling_app:toggle_watch", args=[lot.pk]))

        self.assertEqual(response.status_code, 400)
        self.assertFalse(BidWatch.objects.filter(lot=lot).exists())

    def test_requires_login(self):
        self.client.logout()
        lot = _make_lot("toggle-5", timezone.now() + timedelta(hours=3))

        response = self.client.post(reverse("albright_reselling_app:toggle_watch", args=[lot.pk]))

        self.assertEqual(response.status_code, 302)

    def test_requires_post(self):
        lot = _make_lot("toggle-6", timezone.now() + timedelta(hours=3))

        response = self.client.get(reverse("albright_reselling_app:toggle_watch", args=[lot.pk]))

        self.assertEqual(response.status_code, 405)


class WatchlistSectionViewTests(TestCase):
    """The #watchlist-body AJAX partial (scanner_views.watchlist_section) -
    static/js/auction_scanner.js fetches this after a lot is added to the
    watchlist via any table's star, so the Watchlist section updates
    without a full page reload. Just the table/empty-state fragment, not
    a full page - see _watchlist_section.html."""

    def setUp(self):
        self.user = with_apps(User.objects.create_user(username="tester", password="pw-not-real-12345"))
        self.client.login(username="tester", password="pw-not-real-12345")

    def test_renders_watched_lot(self):
        lot = _make_lot("wl-section-1", timezone.now() + timedelta(hours=3))
        BidWatch.objects.create(lot=lot, status="watching", my_max_bid=Decimal("25.00"))

        response = self.client.get(reverse("albright_reselling_app:watchlist_section"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, lot.title)
        self.assertContains(response, "25.00")

    def test_renders_empty_state(self):
        response = self.client.get(reverse("albright_reselling_app:watchlist_section"))

        self.assertContains(response, "Nothing on your watchlist.")

    def test_is_just_the_fragment_not_a_full_page(self):
        response = self.client.get(reverse("albright_reselling_app:watchlist_section"))

        content = response.content.decode()
        self.assertNotIn("<html", content)
        self.assertNotIn('id="scanner-tabs"', content)

    def test_requires_login(self):
        self.client.logout()

        response = self.client.get(reverse("albright_reselling_app:watchlist_section"))

        self.assertEqual(response.status_code, 302)


class WatchLotViewTests(TestCase):
    def setUp(self):
        self.user = with_apps(User.objects.create_user(username="tester", password="pw-not-real-12345"))
        self.client.login(username="tester", password="pw-not-real-12345")

    def test_get_prefills_from_scanner_max_bid(self):
        lot = _make_lot("watch-get-1", timezone.now() + timedelta(hours=3))
        _make_evaluation(lot, max_bid="20.00")

        response = self.client.get(reverse("albright_reselling_app:watch_lot", args=[lot.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["form"].initial["my_max_bid"], Decimal("20.00"))

    def test_get_prefills_from_ai_review_when_present(self):
        lot = _make_lot("watch-get-2", timezone.now() + timedelta(hours=3))
        _make_evaluation(lot, max_bid="20.00")
        review = AIReview.objects.create(
            lot=lot, status="done", suggested_max_bid=Decimal("27.50"), cost_usd=Decimal("0.05"),
        )

        response = self.client.get(
            reverse("albright_reselling_app:watch_lot", args=[lot.pk]), {"review": review.pk}
        )

        self.assertEqual(response.context["form"].initial["my_max_bid"], Decimal("27.50"))

    def test_post_creates_bid_watch_when_none_exists(self):
        lot = _make_lot("watch-post-1", timezone.now() + timedelta(hours=3))
        _make_evaluation(lot, max_bid="20.00")

        response = self.client.post(
            reverse("albright_reselling_app:watch_lot", args=[lot.pk]), {"my_max_bid": "22.50"}
        )

        self.assertRedirects(response, reverse("albright_reselling_app:watchlist"))  # no "next": the Watchlist page
        watch = BidWatch.objects.get(lot=lot)
        self.assertEqual(watch.my_max_bid, Decimal("22.50"))
        self.assertEqual(watch.status, "watching")

    def test_get_on_an_already_watched_lot_shows_the_existing_max_bid(self):
        """Unlike the old "already watching" redirect, this now shows the
        form pre-filled with whatever max bid is already on record, so it
        doubles as an edit page."""
        lot = _make_lot("watch-edit-1", timezone.now() + timedelta(hours=3))
        BidWatch.objects.create(lot=lot, status="watching", my_max_bid=Decimal("18.00"))

        response = self.client.get(reverse("albright_reselling_app:watch_lot", args=[lot.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["form"].initial["my_max_bid"], Decimal("18.00"))

    def test_post_edits_an_existing_watch_instead_of_duplicating(self):
        lot = _make_lot("watch-edit-2", timezone.now() + timedelta(hours=3))
        watch = BidWatch.objects.create(lot=lot, status="watching")

        response = self.client.post(
            reverse("albright_reselling_app:watch_lot", args=[lot.pk]), {"my_max_bid": "30.00"}
        )

        self.assertRedirects(response, reverse("albright_reselling_app:watchlist"))
        self.assertEqual(BidWatch.objects.filter(lot=lot).count(), 1)
        watch.refresh_from_db()
        self.assertEqual(watch.my_max_bid, Decimal("30.00"))
        self.assertEqual(watch.status, "watching")  # editing the max bid never resets status

    def test_requires_login(self):
        self.client.logout()
        lot = _make_lot("watch-nologin", timezone.now() + timedelta(hours=3))

        response = self.client.get(reverse("albright_reselling_app:watch_lot", args=[lot.pk]))

        self.assertEqual(response.status_code, 302)


class ResolveWatchesTests(TestCase):
    def setUp(self):
        self.now = timezone.now()

    @mock.patch(SEND_TELEGRAM)
    @override_settings(RESELLING_SCANNER=RESULT_ALERTS_ON)
    def test_already_closed_lot_skips_http_lookup_and_resolves_likely_won(self, mock_send):
        lot = _make_lot(
            "resolve-1", self.now - timedelta(hours=1), is_closed=True, final_price="15.00",
        )
        watch = BidWatch.objects.create(lot=lot, my_max_bid=Decimal("20.00"))

        with mock.patch(SEARCH_CLOSED) as mock_search:
            resolved = resolve_watches(now=self.now)

        mock_search.assert_not_called()
        self.assertEqual(len(resolved), 1)
        watch.refresh_from_db()
        self.assertEqual(watch.status, "likely_won")
        self.assertIsNotNone(watch.resolved_at)
        mock_send.assert_called_once()
        self.assertTrue(watch.result_alert_sent)

    @mock.patch(SEND_TELEGRAM)
    def test_result_alert_not_sent_by_default(self, mock_send):
        """ALERTS["watchlist_result_alerts"] defaults to False - likely_won/
        lost is still settled (for the dashboard/Ledger), just quietly."""
        lot = _make_lot("resolve-default-1", self.now - timedelta(hours=1), is_closed=True, final_price="15.00")
        watch = BidWatch.objects.create(lot=lot, my_max_bid=Decimal("20.00"))

        resolved = resolve_watches(now=self.now)

        self.assertEqual(len(resolved), 1)
        watch.refresh_from_db()
        self.assertEqual(watch.status, "likely_won")
        mock_send.assert_not_called()
        self.assertFalse(watch.result_alert_sent)

    @mock.patch(SEND_TELEGRAM)
    @override_settings(RESELLING_SCANNER=RESULT_ALERTS_ON)
    def test_already_closed_lot_resolves_lost_when_over_max(self, mock_send):
        lot = _make_lot(
            "resolve-2", self.now - timedelta(hours=1), is_closed=True, final_price="25.00",
        )
        watch = BidWatch.objects.create(lot=lot, my_max_bid=Decimal("20.00"))

        resolve_watches(now=self.now)

        watch.refresh_from_db()
        self.assertEqual(watch.status, "lost")

    @mock.patch(SEND_TELEGRAM)
    def test_resolves_unknown_when_no_max_bid_was_ever_set(self, mock_send):
        """A watch added via the one-click star (no max bid) can't be told
        won from lost once it closes - there's nothing to compare the
        final price against, so it settles as "unknown" instead."""
        lot = _make_lot("resolve-nomax-1", self.now - timedelta(hours=1), is_closed=True, final_price="15.00")
        watch = BidWatch.objects.create(lot=lot, my_max_bid=None)

        resolved = resolve_watches(now=self.now)

        self.assertEqual(len(resolved), 1)
        watch.refresh_from_db()
        self.assertEqual(watch.status, "unknown")

    @mock.patch(SEND_TELEGRAM)
    @mock.patch(PAUSE)
    @override_settings(RESELLING_SCANNER=RESULT_ALERTS_ON)
    def test_search_closed_lookup_resolves_likely_won(self, mock_pause, mock_send):
        lot = _make_lot("resolve-3", self.now - timedelta(hours=2))
        watch = BidWatch.objects.create(lot=lot, my_max_bid=Decimal("20.00"))
        found = RawLot(
            source="shopgoodwill", external_id="resolve-3", url=lot.url, title=lot.title, current_price=18.00,
        )

        with mock.patch(SEARCH_CLOSED, return_value=iter([found])):
            resolved = resolve_watches(now=self.now)

        self.assertEqual(len(resolved), 1)
        watch.refresh_from_db()
        self.assertEqual(watch.status, "likely_won")
        lot.refresh_from_db()
        self.assertEqual(lot.final_price, Decimal("18.00"))
        self.assertTrue(lot.is_closed)
        mock_pause.assert_called_once()

    @mock.patch(SEND_TELEGRAM)
    @mock.patch(PAUSE)
    def test_no_match_before_give_up_window_stays_watching(self, mock_pause, mock_send):
        lot = _make_lot("resolve-4", self.now - timedelta(hours=2))
        BidWatch.objects.create(lot=lot, my_max_bid=Decimal("20.00"))

        with mock.patch(SEARCH_CLOSED, return_value=iter([])):
            resolved = resolve_watches(now=self.now)

        self.assertEqual(resolved, [])
        mock_send.assert_not_called()
        self.assertEqual(BidWatch.objects.get(lot=lot).status, "watching")

    @mock.patch(SEND_TELEGRAM)
    @mock.patch(PAUSE)
    @override_settings(RESELLING_SCANNER=RESULT_ALERTS_ON)
    def test_unknown_after_give_up_window(self, mock_pause, mock_send):
        lot = _make_lot("resolve-5", self.now - RESOLVE_GIVE_UP_AFTER - timedelta(hours=1))
        watch = BidWatch.objects.create(lot=lot, my_max_bid=Decimal("20.00"))

        with mock.patch(SEARCH_CLOSED, return_value=iter([])):
            resolved = resolve_watches(now=self.now)

        self.assertEqual(len(resolved), 1)
        watch.refresh_from_db()
        self.assertEqual(watch.status, "unknown")
        mock_send.assert_called_once()

    @mock.patch(SEND_TELEGRAM)
    @override_settings(RESELLING_SCANNER=RESULT_ALERTS_ON)
    def test_result_alert_sent_exactly_once_across_calls(self, mock_send):
        lot = _make_lot("resolve-6", self.now - timedelta(hours=1), is_closed=True, final_price="15.00")
        BidWatch.objects.create(lot=lot, my_max_bid=Decimal("20.00"))

        resolve_watches(now=self.now)  # first call resolves + alerts
        # second sweep would only pick up status="watching" rows, but even
        # a direct re-resolve attempt must not double-send.
        watch = BidWatch.objects.get(lot=lot)
        watch.status = "watching"
        watch.save(update_fields=["status"])
        resolve_watches(now=self.now)

        mock_send.assert_called_once()

    @mock.patch(SEND_TELEGRAM)
    @mock.patch(PAUSE)
    def test_source_blocked_during_lookup_leaves_watch_pending(self, mock_pause, mock_send):
        lot = _make_lot("resolve-7", self.now - timedelta(hours=2))
        BidWatch.objects.create(lot=lot, my_max_bid=Decimal("20.00"))

        def _raise(*args, **kwargs):
            raise SourceBlocked("blocked")

        with mock.patch(SEARCH_CLOSED, side_effect=_raise):
            resolved = resolve_watches(now=self.now)

        self.assertEqual(resolved, [])
        mock_send.assert_not_called()
        self.assertEqual(BidWatch.objects.get(lot=lot).status, "watching")

    @mock.patch(SEND_TELEGRAM)
    def test_still_live_watch_not_touched(self, mock_send):
        lot = _make_lot("resolve-8", self.now + timedelta(hours=2))
        BidWatch.objects.create(lot=lot, my_max_bid=Decimal("20.00"))

        resolved = resolve_watches(now=self.now)

        self.assertEqual(resolved, [])
        mock_send.assert_not_called()
