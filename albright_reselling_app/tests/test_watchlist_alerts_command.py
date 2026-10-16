"""Tests for scanner.alerts.send_watchlist_alerts() and the watchlist_alerts
management command that calls it on a schedule. This is the ONLY place the
watchlist's "closing soon" Telegram alert is sent from - see scanner/bid_watch.py
(no longer sends it) and scan_lots (no longer calls it). Every HTTP call to
Telegram is mocked."""
import os
from datetime import timedelta
from decimal import Decimal
from io import StringIO
from unittest import mock

from django.conf import settings
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from albright_reselling_app.scanner.alerts import send_watchlist_alerts
from albright_reselling_app.scanner_models import AlertSent, BidWatch, SourcedLot

TELEGRAM_POST = "albright_reselling_app.scanner.alerts.requests.post"
CREDS = {"TELEGRAM_BOT_TOKEN": "test-token", "TELEGRAM_CHAT_ID": "12345"}


def _ok_response():
    resp = mock.Mock()
    resp.raise_for_status.return_value = None
    return resp


def _make_watch(external_id, end_time, my_max_bid="30.00", current_price="10.00"):
    lot = SourcedLot.objects.create(
        source="shopgoodwill", external_id=external_id, url=f"https://example.com/{external_id}",
        title=f"Morgan Silver Dollar {external_id}", current_price=Decimal(current_price), end_time=end_time,
        raw={},
    )
    watch = BidWatch.objects.create(
        lot=lot, my_max_bid=Decimal(my_max_bid) if my_max_bid is not None else None, status="watching",
    )
    return lot, watch


class SendWatchlistAlertsTests(TestCase):
    @mock.patch(TELEGRAM_POST)
    @mock.patch.dict(os.environ, CREDS)
    def test_fires_when_at_or_inside_the_cutoff(self, mock_post):
        mock_post.return_value = _ok_response()
        now = timezone.now()
        lot, _watch = _make_watch("w1", now + timedelta(minutes=60))

        sent = send_watchlist_alerts(now=now)

        self.assertEqual(sent, [lot.pk])
        mock_post.assert_called_once()

    @mock.patch(TELEGRAM_POST)
    @mock.patch.dict(os.environ, CREDS)
    def test_does_not_fire_before_the_cutoff(self, mock_post):
        now = timezone.now()
        _make_watch("w2", now + timedelta(minutes=61))

        sent = send_watchlist_alerts(now=now)

        self.assertEqual(sent, [])
        mock_post.assert_not_called()

    @mock.patch(TELEGRAM_POST)
    @mock.patch.dict(os.environ, CREDS)
    def test_does_not_fire_after_the_lot_has_already_closed(self, mock_post):
        now = timezone.now()
        _make_watch("w3", now - timedelta(minutes=1))

        sent = send_watchlist_alerts(now=now)

        self.assertEqual(sent, [])
        mock_post.assert_not_called()

    @mock.patch(TELEGRAM_POST)
    @mock.patch.dict(os.environ, CREDS)
    def test_never_fires_twice_for_the_same_lot(self, mock_post):
        mock_post.return_value = _ok_response()
        now = timezone.now()
        lot, _watch = _make_watch("w4", now + timedelta(minutes=30))

        first = send_watchlist_alerts(now=now)
        second = send_watchlist_alerts(now=now + timedelta(minutes=1))

        self.assertEqual(first, [lot.pk])
        self.assertEqual(second, [])
        mock_post.assert_called_once()

    @mock.patch(TELEGRAM_POST)
    @mock.patch.dict(os.environ, CREDS)
    def test_overlapping_calls_never_double_send(self, mock_post):
        """Simulates scan_lots and the management command both landing at
        the same moment - AlertSent's get_or_create is what actually
        prevents the double-send, not any locking in send_watchlist_alerts
        itself."""
        mock_post.return_value = _ok_response()
        now = timezone.now()
        lot, _watch = _make_watch("w5", now + timedelta(minutes=10))

        first = send_watchlist_alerts(now=now)
        second = send_watchlist_alerts(now=now)

        self.assertEqual(first, [lot.pk])
        self.assertEqual(second, [])
        mock_post.assert_called_once()
        self.assertEqual(AlertSent.objects.filter(lot=lot, kind="watchlist").count(), 1)

    @mock.patch(TELEGRAM_POST)
    @mock.patch.dict(os.environ, CREDS)
    def test_only_watching_status_is_considered_not_resolved_watches(self, mock_post):
        now = timezone.now()
        lot = SourcedLot.objects.create(
            source="shopgoodwill", external_id="w6", url="https://example.com/w6",
            title="Already resolved", current_price=Decimal("10.00"), end_time=now - timedelta(minutes=5),
            raw={},
        )
        BidWatch.objects.create(lot=lot, my_max_bid=Decimal("30.00"), status="likely_won")

        sent = send_watchlist_alerts(now=now)

        self.assertEqual(sent, [])
        mock_post.assert_not_called()

    @mock.patch(TELEGRAM_POST)
    @mock.patch.dict(os.environ, CREDS)
    def test_respects_configured_watchlist_minutes(self, mock_post):
        mock_post.return_value = _ok_response()
        now = timezone.now()
        lot, _watch = _make_watch("w7", now + timedelta(minutes=20))

        cfg = {**settings.RESELLING_SCANNER, "ALERTS": {**settings.RESELLING_SCANNER["ALERTS"], "watchlist_minutes": 15}}
        with override_settings(RESELLING_SCANNER=cfg):
            sent = send_watchlist_alerts(now=now)
        self.assertEqual(sent, [])

        cfg["ALERTS"]["watchlist_minutes"] = 30
        with override_settings(RESELLING_SCANNER=cfg):
            sent = send_watchlist_alerts(now=now)
        self.assertEqual(sent, [lot.pk])

    @mock.patch(TELEGRAM_POST)
    @mock.patch.dict(os.environ, CREDS)
    def test_ignores_quiet_hours(self, mock_post):
        mock_post.return_value = _ok_response()
        now = timezone.now().replace(hour=2, minute=0, second=0, microsecond=0)
        lot, _watch = _make_watch("w8", now + timedelta(minutes=10))

        cfg = {**settings.RESELLING_SCANNER, "ALERTS": {**settings.RESELLING_SCANNER["ALERTS"], "quiet_hours": [0, 23]}}
        with override_settings(RESELLING_SCANNER=cfg):
            sent = send_watchlist_alerts(now=now)

        self.assertEqual(sent, [lot.pk])
        mock_post.assert_called_once()

    @mock.patch(TELEGRAM_POST)
    @mock.patch.dict(os.environ, CREDS)
    def test_master_enabled_flag_still_gates_it(self, mock_post):
        now = timezone.now()
        _make_watch("w9", now + timedelta(minutes=10))

        cfg = {**settings.RESELLING_SCANNER, "ALERTS": {**settings.RESELLING_SCANNER["ALERTS"], "enabled": False}}
        with override_settings(RESELLING_SCANNER=cfg):
            sent = send_watchlist_alerts(now=now)

        self.assertEqual(sent, [])
        mock_post.assert_not_called()

    @mock.patch(TELEGRAM_POST)
    @mock.patch.dict(os.environ, CREDS)
    def test_message_includes_max_bid_and_headroom_when_set(self, mock_post):
        mock_post.return_value = _ok_response()
        now = timezone.now()
        _make_watch("w10", now + timedelta(minutes=10), my_max_bid="25.00", current_price="18.00")

        send_watchlist_alerts(now=now)

        message = mock_post.call_args.kwargs["json"]["text"]
        self.assertIn("Your max: $25.00", message)
        self.assertIn("headroom: $7.00", message)
        self.assertIn("Current bid: $18.00", message)
        self.assertIn("as of", message)
        self.assertIn("m left)", message)
        self.assertIn("example.com/w10", message)

    @mock.patch(TELEGRAM_POST)
    @mock.patch.dict(os.environ, CREDS)
    def test_message_omits_max_bid_line_when_not_set(self, mock_post):
        mock_post.return_value = _ok_response()
        now = timezone.now()
        _make_watch("w11", now + timedelta(minutes=10), my_max_bid=None, current_price="18.00")

        send_watchlist_alerts(now=now)

        message = mock_post.call_args.kwargs["json"]["text"]
        self.assertNotIn("Your max", message)
        self.assertNotIn("headroom", message)


class WatchlistAlertsCommandTests(TestCase):
    @mock.patch(TELEGRAM_POST)
    @mock.patch.dict(os.environ, CREDS)
    def test_single_check_sends_a_due_alert(self, mock_post):
        mock_post.return_value = _ok_response()
        now = timezone.now()
        lot, _watch = _make_watch("cmd1", now + timedelta(minutes=10))

        out = StringIO()
        call_command("watchlist_alerts", stdout=out)

        mock_post.assert_called_once()
        self.assertIn("Watchlist alerts sent: 1", out.getvalue())
        self.assertEqual(AlertSent.objects.filter(lot=lot, kind="watchlist").count(), 1)

    @mock.patch(TELEGRAM_POST)
    @mock.patch.dict(os.environ, CREDS)
    def test_single_check_reports_nothing_due(self, mock_post):
        now = timezone.now()
        _make_watch("cmd2", now + timedelta(minutes=90))

        out = StringIO()
        call_command("watchlist_alerts", stdout=out)

        mock_post.assert_not_called()
        self.assertIn("No watchlist alerts due.", out.getvalue())

    @mock.patch("albright_reselling_app.management.commands.watchlist_alerts.time.sleep")
    @mock.patch("albright_reselling_app.management.commands.watchlist_alerts.send_watchlist_alerts")
    def test_loop_checks_repeatedly_then_exits(self, mock_send, mock_sleep):
        """--loop-minutes 12 --every 5: the command calls timezone.now()
        once to set the deadline, then once more after each check to decide
        whether to keep looping. Three "elapsed" readings under the
        deadline followed by one past it means three checks and two
        (mocked, never-really-sleeping) sleeps in between. send_watchlist_alerts
        itself is mocked here so this test is purely about the command's
        own loop/deadline bookkeeping, not the DB-backed alert logic
        (covered by SendWatchlistAlertsTests above) - note timezone.now is
        one shared module attribute, so patching it here would otherwise
        also control (and get consumed by) any real call inside
        send_watchlist_alerts()."""
        mock_send.return_value = []
        base = timezone.now()

        times = iter([
            base,                          # deadline = base + 12min
            base + timedelta(minutes=2),   # after check 1: still under deadline
            base + timedelta(minutes=7),   # after check 2: still under deadline
            base + timedelta(minutes=13),  # after check 3: past deadline, breaks
        ])

        with mock.patch(
            "albright_reselling_app.management.commands.watchlist_alerts.timezone.now",
            side_effect=lambda: next(times),
        ):
            out = StringIO()
            call_command("watchlist_alerts", "--loop-minutes", "12", "--every", "5", stdout=out)

        self.assertEqual(mock_send.call_count, 3)
        self.assertEqual(out.getvalue().count("No watchlist alerts due."), 3)
        self.assertEqual(mock_sleep.call_count, 2)
        mock_sleep.assert_called_with(5 * 60)

    @mock.patch(TELEGRAM_POST)
    @mock.patch.dict(os.environ, CREDS)
    def test_overlapping_runs_of_the_command_never_double_send(self, mock_post):
        mock_post.return_value = _ok_response()
        now = timezone.now()
        lot, _watch = _make_watch("cmd4", now + timedelta(minutes=10))

        call_command("watchlist_alerts", stdout=StringIO())
        call_command("watchlist_alerts", stdout=StringIO())

        mock_post.assert_called_once()
        self.assertEqual(AlertSent.objects.filter(lot=lot, kind="watchlist").count(), 1)
