"""Tests for BidWatch: the "I bid on this" button/view, default_max_bid's
prefill rule, the closing-soon alert sweep, and the watch-resolution sweep
(scanner/bid_watch.py). Every HTTP call (Telegram, the adapters' own
requests) is mocked - nothing here reaches a real site."""
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from albright_reselling_app.scanner.adapters.base import RawLot, SourceBlocked
from albright_reselling_app.scanner.bid_watch import (
    RESOLVE_GIVE_UP_AFTER, default_max_bid, resolve_watches, send_closing_soon_alerts,
)
from albright_reselling_app.scanner_models import AIReview, BidWatch, LotEvaluation, SourcedLot

SEND_TELEGRAM = "albright_reselling_app.scanner.bid_watch.send_telegram"
SEARCH_CLOSED = "albright_reselling_app.scanner.adapters.shopgoodwill.ShopGoodwillAdapter.search_closed"
PAUSE = "albright_reselling_app.scanner.adapters.shopgoodwill.ShopGoodwillAdapter.pause"

ALERTS_CFG = {"enabled": True, "window_minutes": 90, "kinds": ["candidate", "lead"], "min_headroom": 5.0,
              "candidate_confidence": ["high", "medium"], "max_per_run": 10}


def _cfg(**overrides):
    from django.conf import settings
    return {**settings.RESELLING_SCANNER, "ALERTS": {**ALERTS_CFG, **overrides}}


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


class WatchLotViewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="tester", password="pw-not-real-12345")
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

    def test_post_creates_bid_watch(self):
        lot = _make_lot("watch-post-1", timezone.now() + timedelta(hours=3))
        _make_evaluation(lot, max_bid="20.00")

        response = self.client.post(
            reverse("albright_reselling_app:watch_lot", args=[lot.pk]), {"my_max_bid": "22.50"}
        )

        self.assertRedirects(response, reverse("albright_reselling_app:dashboard"))
        watch = BidWatch.objects.get(lot=lot)
        self.assertEqual(watch.my_max_bid, Decimal("22.50"))
        self.assertEqual(watch.status, "watching")

    def test_already_watching_redirects_without_a_second_watch(self):
        lot = _make_lot("watch-dup-1", timezone.now() + timedelta(hours=3))
        BidWatch.objects.create(lot=lot, my_max_bid=Decimal("20.00"))

        response = self.client.get(reverse("albright_reselling_app:watch_lot", args=[lot.pk]))

        self.assertRedirects(response, reverse("albright_reselling_app:dashboard"))
        self.assertEqual(BidWatch.objects.filter(lot=lot).count(), 1)

    def test_requires_login(self):
        self.client.logout()
        lot = _make_lot("watch-nologin", timezone.now() + timedelta(hours=3))

        response = self.client.get(reverse("albright_reselling_app:watch_lot", args=[lot.pk]))

        self.assertEqual(response.status_code, 302)


class ClosingSoonAlertTests(TestCase):
    @mock.patch(SEND_TELEGRAM)
    @override_settings(RESELLING_SCANNER=_cfg())
    def test_sends_once_inside_the_alert_window(self, mock_send):
        now = timezone.now()
        lot = _make_lot("closing-1", now + timedelta(minutes=30))
        watch = BidWatch.objects.create(lot=lot, my_max_bid=Decimal("20.00"))

        sent = send_closing_soon_alerts(now=now)

        self.assertEqual(sent, [lot.pk])
        mock_send.assert_called_once()
        watch.refresh_from_db()
        self.assertTrue(watch.closing_alert_sent)

    @mock.patch(SEND_TELEGRAM)
    @override_settings(RESELLING_SCANNER=_cfg())
    def test_not_sent_again_once_flagged(self, mock_send):
        now = timezone.now()
        lot = _make_lot("closing-2", now + timedelta(minutes=30))
        BidWatch.objects.create(lot=lot, my_max_bid=Decimal("20.00"), closing_alert_sent=True)

        sent = send_closing_soon_alerts(now=now)

        self.assertEqual(sent, [])
        mock_send.assert_not_called()

    @mock.patch(SEND_TELEGRAM)
    @override_settings(RESELLING_SCANNER=_cfg())
    def test_not_sent_outside_the_window(self, mock_send):
        now = timezone.now()
        lot = _make_lot("closing-3", now + timedelta(days=3))
        BidWatch.objects.create(lot=lot, my_max_bid=Decimal("20.00"))

        sent = send_closing_soon_alerts(now=now)

        self.assertEqual(sent, [])
        mock_send.assert_not_called()

    @mock.patch(SEND_TELEGRAM)
    @override_settings(RESELLING_SCANNER=_cfg(enabled=False))
    def test_disabled_when_alerts_off(self, mock_send):
        now = timezone.now()
        lot = _make_lot("closing-4", now + timedelta(minutes=30))
        BidWatch.objects.create(lot=lot, my_max_bid=Decimal("20.00"))

        sent = send_closing_soon_alerts(now=now)

        self.assertEqual(sent, [])
        mock_send.assert_not_called()

    @mock.patch(SEND_TELEGRAM)
    @override_settings(RESELLING_SCANNER=_cfg(quiet_hours=[23, 7]))
    def test_sends_during_quiet_hours(self, mock_send):
        """BidWatch alerts always send - quiet_hours only gates
        scanner.alerts.run_alerts()'s per-lot kinds, never this sweep."""
        now = timezone.now()
        local = timezone.localtime(now)
        quiet_now = now + timedelta(hours=(2 - local.hour) % 24)  # ~2am local
        lot = _make_lot("closing-5", quiet_now + timedelta(minutes=30))
        BidWatch.objects.create(lot=lot, my_max_bid=Decimal("20.00"))

        sent = send_closing_soon_alerts(now=quiet_now)

        self.assertEqual(sent, [lot.pk])
        mock_send.assert_called_once()


class ResolveWatchesTests(TestCase):
    def setUp(self):
        self.now = timezone.now()

    @mock.patch(SEND_TELEGRAM)
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
    def test_already_closed_lot_resolves_lost_when_over_max(self, mock_send):
        lot = _make_lot(
            "resolve-2", self.now - timedelta(hours=1), is_closed=True, final_price="25.00",
        )
        watch = BidWatch.objects.create(lot=lot, my_max_bid=Decimal("20.00"))

        resolve_watches(now=self.now)

        watch.refresh_from_db()
        self.assertEqual(watch.status, "lost")

    @mock.patch(SEND_TELEGRAM)
    @mock.patch(PAUSE)
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
