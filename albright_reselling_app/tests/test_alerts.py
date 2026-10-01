"""Tests for scanner/alerts.py: send_telegram (every HTTP call mocked -
nothing here ever reaches Telegram, even though real credentials are
loaded into os.environ from .env at Django startup), the alert rules
(run_alerts), the daily digest content, and the test_alert/alert_digest
management commands."""
import os
from datetime import timedelta
from decimal import Decimal
from io import StringIO
from unittest import mock

from django.conf import settings
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from albright_reselling_app.scanner.alerts import build_digest_message, run_alerts, send_daily_digest, send_telegram
from albright_reselling_app.scanner_models import AlertSent, LotEvaluation, SourcedLot

TELEGRAM_POST = "albright_reselling_app.scanner.alerts.requests.post"
CREDS = {"TELEGRAM_BOT_TOKEN": "test-token", "TELEGRAM_CHAT_ID": "12345"}
NO_CREDS = {"TELEGRAM_BOT_TOKEN": "", "TELEGRAM_CHAT_ID": ""}

ALERTS_CFG = {"enabled": True, "window_minutes": 90, "kinds": ["candidate", "lead"], "min_headroom": 5.0,
              "candidate_confidence": ["high", "medium"], "max_per_run": 10}


def _cfg(**overrides):
    return {**settings.RESELLING_SCANNER, "ALERTS": {**ALERTS_CFG, **overrides}}


def _ok_response():
    resp = mock.Mock()
    resp.raise_for_status.return_value = None
    return resp


def _make_lot(external_id, end_time, source="shopgoodwill", current_price="10.00", raw=None):
    return SourcedLot.objects.create(
        source=source, external_id=external_id, url=f"https://example.com/{external_id}",
        title=f"Morgan Silver Dollar {external_id}", current_price=Decimal(current_price), end_time=end_time,
        raw=raw or {},
    )


def _make_candidate(external_id, end_time, confidence="high", headroom="10.00", max_bid="30.00",
                     category="coins", source="shopgoodwill", raw=None):
    lot = _make_lot(external_id, end_time, source=source, raw=raw)
    ev = LotEvaluation.objects.create(
        lot=lot, category=category, is_candidate=True, confidence=confidence,
        headroom=Decimal(headroom), max_bid=Decimal(max_bid),
    )
    return lot, ev


def _make_lead(external_id, end_time, reason="n64: bulk lot", category="games"):
    lot = _make_lot(external_id, end_time)
    ev = LotEvaluation.objects.create(lot=lot, category=category, is_lead=True, lead_reason=reason)
    return lot, ev


def _make_check_by_hand(external_id, end_time, headroom="2.00", max_bid="20.00", category="coins"):
    lot = _make_lot(external_id, end_time)
    ev = LotEvaluation.objects.create(
        lot=lot, category=category, confidence="low", headroom=Decimal(headroom), max_bid=Decimal(max_bid),
    )
    return lot, ev


def _call_run_alerts(now, **cfg_overrides):
    """Runs run_alerts() under the given ALERTS config, with Telegram fully
    mocked - returns (sent, mock_post) so tests can assert on both the
    returned (lot_id, kind) pairs and what was actually "sent"."""
    with override_settings(RESELLING_SCANNER=_cfg(**cfg_overrides)):
        with mock.patch(TELEGRAM_POST) as mock_post, mock.patch.dict(os.environ, CREDS):
            mock_post.return_value = _ok_response()
            sent = run_alerts(now=now)
    return sent, mock_post


class SendTelegramTests(TestCase):
    @mock.patch(TELEGRAM_POST)
    @mock.patch.dict(os.environ, CREDS)
    def test_posts_with_correct_payload(self, mock_post):
        mock_post.return_value = _ok_response()

        result = send_telegram("hello world")

        self.assertTrue(result)
        mock_post.assert_called_once()
        url = mock_post.call_args.args[0]
        self.assertIn("test-token", url)
        self.assertIn("/sendMessage", url)
        payload = mock_post.call_args.kwargs["json"]
        self.assertEqual(payload["chat_id"], "12345")
        self.assertEqual(payload["text"], "hello world")
        self.assertEqual(payload["parse_mode"], "HTML")
        self.assertFalse(payload["disable_web_page_preview"])
        self.assertEqual(mock_post.call_args.kwargs["timeout"], 15)

    @mock.patch(TELEGRAM_POST)
    @mock.patch.dict(os.environ, NO_CREDS)
    def test_missing_env_vars_does_not_crash_or_call_telegram(self, mock_post):
        result = send_telegram("hello")

        self.assertFalse(result)
        mock_post.assert_not_called()

    @mock.patch(TELEGRAM_POST)
    @mock.patch.dict(os.environ, CREDS)
    def test_request_failure_does_not_raise(self, mock_post):
        mock_post.side_effect = Exception("network boom")

        result = send_telegram("hello")  # must not raise

        self.assertFalse(result)


class RunAlertsTests(TestCase):
    def test_candidate_alert_sent_for_qualifying_lot(self):
        now = timezone.now()
        lot, _ev = _make_candidate("cand-1", now + timedelta(minutes=30))

        sent, mock_post = _call_run_alerts(now)

        self.assertEqual(sent, [(lot.pk, "candidate")])
        self.assertTrue(AlertSent.objects.filter(lot=lot, kind="candidate").exists())
        mock_post.assert_called_once()

    def test_candidate_below_min_headroom_not_alerted(self):
        now = timezone.now()
        _make_candidate("cand-low-hr", now + timedelta(minutes=30), headroom="1.00")

        sent, mock_post = _call_run_alerts(now, min_headroom=5.0)

        self.assertEqual(sent, [])
        mock_post.assert_not_called()

    def test_candidate_confidence_not_in_list_not_alerted(self):
        now = timezone.now()
        _make_candidate("cand-low-conf", now + timedelta(minutes=30), confidence="low")

        sent, _mock_post = _call_run_alerts(now, candidate_confidence=["high", "medium"])

        self.assertEqual(sent, [])

    def test_lead_alert_sent(self):
        now = timezone.now()
        lot, _ev = _make_lead("lead-1", now + timedelta(minutes=45))

        sent, mock_post = _call_run_alerts(now)

        self.assertEqual(sent, [(lot.pk, "lead")])
        mock_post.assert_called_once()

    def test_check_by_hand_excluded_unless_listed_in_kinds(self):
        now = timezone.now()
        _make_check_by_hand("cbh-1", now + timedelta(minutes=30))

        sent_without, _ = _call_run_alerts(now, kinds=["candidate", "lead"])
        self.assertEqual(sent_without, [])

        sent_with, mock_post = _call_run_alerts(now, kinds=["candidate", "lead", "check_by_hand"])
        self.assertEqual(len(sent_with), 1)
        self.assertEqual(sent_with[0][1], "check_by_hand")
        mock_post.assert_called_once()

    def test_lot_outside_window_not_alerted(self):
        now = timezone.now()
        _make_candidate("cand-far", now + timedelta(hours=5))  # window is 90 min

        sent, mock_post = _call_run_alerts(now, window_minutes=90)

        self.assertEqual(sent, [])
        mock_post.assert_not_called()

    def test_already_ended_lot_not_alerted(self):
        now = timezone.now()
        _make_candidate("cand-ended", now - timedelta(minutes=5))

        sent, _mock_post = _call_run_alerts(now)

        self.assertEqual(sent, [])

    def test_no_duplicate_alert_across_runs(self):
        now = timezone.now()
        lot, _ev = _make_candidate("cand-dup", now + timedelta(minutes=30))

        first_sent, first_post = _call_run_alerts(now)
        second_sent, second_post = _call_run_alerts(now + timedelta(minutes=5))

        self.assertEqual(first_sent, [(lot.pk, "candidate")])
        self.assertEqual(second_sent, [])
        first_post.assert_called_once()
        second_post.assert_not_called()
        self.assertEqual(AlertSent.objects.filter(lot=lot, kind="candidate").count(), 1)

    def test_max_per_run_sends_soonest_ending_first(self):
        now = timezone.now()
        lot_soon, _ = _make_candidate("cand-soon", now + timedelta(minutes=10))
        lot_mid, _ = _make_candidate("cand-mid", now + timedelta(minutes=20))
        lot_late, _ = _make_candidate("cand-late", now + timedelta(minutes=30))

        sent, mock_post = _call_run_alerts(now, max_per_run=2)

        self.assertEqual([lot_id for lot_id, _kind in sent], [lot_soon.pk, lot_mid.pk])
        self.assertEqual(mock_post.call_count, 2)
        # The lot that didn't make the cut is still eligible for the next run.
        self.assertFalse(AlertSent.objects.filter(lot=lot_late).exists())

    def test_disabled_alerts_sends_nothing(self):
        now = timezone.now()
        _make_candidate("cand-disabled", now + timedelta(minutes=30))

        sent, mock_post = _call_run_alerts(now, enabled=False)

        self.assertEqual(sent, [])
        mock_post.assert_not_called()

    def test_message_contains_link_and_numbers(self):
        now = timezone.now()
        lot, _ev = _make_candidate("cand-msg", now + timedelta(minutes=30), headroom="12.50", max_bid="45.00")
        lot.current_price = Decimal("32.50")
        lot.save()

        _sent, mock_post = _call_run_alerts(now)

        text = mock_post.call_args.kwargs["json"]["text"]
        self.assertIn(lot.url, text)
        self.assertIn("32.50", text)
        self.assertIn("45.00", text)
        self.assertIn("12.50", text)
        self.assertIn("Run AI review", text)

    def test_maxsold_city_and_distance_in_message(self):
        now = timezone.now()
        lot, _ev = _make_candidate(
            "cand-ms", now + timedelta(minutes=30), source="maxsold",
            raw={"_pickup": {"city": "Tampa", "distance_miles": 9.3, "auction_id": 1, "auction_title": "x",
                              "has_shipping": False}},
        )

        _sent, mock_post = _call_run_alerts(now)

        text = mock_post.call_args.kwargs["json"]["text"]
        self.assertIn("Tampa", text)
        self.assertIn("9.3", text)


class DigestTests(TestCase):
    def test_includes_yesterday_results_and_todays_live_counts(self):
        now = timezone.now()
        yesterday = now - timedelta(days=1)

        under_lot = _make_lot("digest-under", yesterday)
        under_lot.is_closed = True
        under_lot.final_price = Decimal("15.00")
        under_lot.save()
        LotEvaluation.objects.create(lot=under_lot, category="coins", max_bid=Decimal("20.00"))

        over_lot = _make_lot("digest-over", yesterday)
        over_lot.is_closed = True
        over_lot.final_price = Decimal("25.00")
        over_lot.save()
        LotEvaluation.objects.create(lot=over_lot, category="coins", max_bid=Decimal("20.00"))

        _make_candidate("digest-live-cand", now + timedelta(hours=3))
        _make_lead("digest-live-lead", now + timedelta(hours=3))

        message = build_digest_message(now=now)

        self.assertIn("ShopGoodwill coins: 1 under max, 1 over max", message)
        self.assertIn("1 candidate(s)", message)
        self.assertIn("1 lead(s)", message)

    def test_no_results_yesterday_message(self):
        message = build_digest_message(now=timezone.now())

        self.assertIn("No lots closed with a max bid yesterday", message)

    @mock.patch(TELEGRAM_POST)
    @mock.patch.dict(os.environ, CREDS)
    def test_send_daily_digest_sends_via_telegram(self, mock_post):
        mock_post.return_value = _ok_response()

        message = send_daily_digest(now=timezone.now())

        mock_post.assert_called_once()
        self.assertEqual(mock_post.call_args.kwargs["json"]["text"], message)


class ManagementCommandTests(TestCase):
    @mock.patch(TELEGRAM_POST)
    @mock.patch.dict(os.environ, CREDS)
    def test_test_alert_command_sends_message(self, mock_post):
        mock_post.return_value = _ok_response()
        out = StringIO()

        call_command("test_alert", stdout=out)

        mock_post.assert_called_once()
        self.assertEqual(mock_post.call_args.kwargs["json"]["text"], "Scanner alerts are working")
        self.assertIn("Test alert sent", out.getvalue())

    @mock.patch(TELEGRAM_POST)
    @mock.patch.dict(os.environ, NO_CREDS)
    def test_test_alert_command_reports_failure_without_crashing(self, mock_post):
        out = StringIO()

        call_command("test_alert", stdout=out)

        mock_post.assert_not_called()
        self.assertIn("NOT sent", out.getvalue())

    @mock.patch(TELEGRAM_POST)
    @mock.patch.dict(os.environ, CREDS)
    def test_alert_digest_command_sends_and_prints_message(self, mock_post):
        mock_post.return_value = _ok_response()
        out = StringIO()

        call_command("alert_digest", stdout=out)

        mock_post.assert_called_once()
        self.assertIn("Scanner Daily Digest", out.getvalue())
