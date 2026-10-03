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

from albright_reselling_app.scanner.adapters.base import RawLot, SourceBlocked
from albright_reselling_app.scanner.adapters.shopgoodwill import ShopGoodwillAdapter
from albright_reselling_app.scanner.alerts import (
    _ai_review_health_lines, _alerts_sent_lines, _in_quiet_hours, _my_bids_counts, _source_health_lines,
    _spot_fetch_health_lines, _spot_section, _track_closed_health_lines, build_digest_message, check_stale_source,
    check_zero_lots, run_alerts, send_critical_alert, send_daily_digest, send_telegram,
)
from albright_reselling_app.scanner_models import (
    AIReview, AlertSent, BidWatch, CriticalAlertSent, LotEvaluation, ScanRun, SourcedLot, SpotPrice,
)

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


def _make_zero_bid(external_id, end_time, max_bid="20.00", current_price="10.00", category="coins",
                    is_lead=False, lead_reason=""):
    lot = SourcedLot.objects.create(
        source="shopgoodwill", external_id=external_id, url=f"https://example.com/{external_id}",
        title=f"Zero Bid Lot {external_id}", current_price=Decimal(current_price), end_time=end_time,
        bid_count=0, raw={},
    )
    ev = LotEvaluation.objects.create(
        lot=lot, category=category, max_bid=Decimal(max_bid), is_lead=is_lead, lead_reason=lead_reason,
    )
    return lot, ev


def _make_relisted(external_id, end_time, relist_id=777, max_bid="20.00", current_price="10.00",
                    category="coins", is_lead=False, lead_reason="", source="shopgoodwill"):
    lot = SourcedLot.objects.create(
        source=source, external_id=external_id, url=f"https://example.com/{external_id}",
        title=f"Relisted Lot {external_id}", current_price=Decimal(current_price), end_time=end_time,
        features={"relist_id": relist_id, "is_relisted": True}, raw={"relistId": relist_id},
    )
    ev = LotEvaluation.objects.create(
        lot=lot, category=category, max_bid=Decimal(max_bid), is_lead=is_lead, lead_reason=lead_reason,
    )
    return lot, ev


def _at_local_hour(base, hour):
    """A datetime near `base` whose local time (settings.TIME_ZONE) falls at
    the given hour - quiet-hours tests need a concrete local hour, not just
    "now whatever that happens to be"."""
    local = timezone.localtime(base)
    return base + timedelta(hours=(hour - local.hour) % 24)


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


class ZeroBidAlertTests(TestCase):
    def test_sent_for_affordable_zero_bid_lot(self):
        now = timezone.now()
        lot, _ev = _make_zero_bid("zb-1", now + timedelta(minutes=30))

        sent, mock_post = _call_run_alerts(now, kinds=["zero_bid"])

        self.assertEqual(sent, [(lot.pk, "zero_bid")])
        self.assertTrue(AlertSent.objects.filter(lot=lot, kind="zero_bid").exists())
        text = mock_post.call_args.kwargs["json"]["text"]
        self.assertIn("No bids yet", text)

    def test_not_sent_when_bids_exist(self):
        now = timezone.now()
        lot, _ev = _make_zero_bid("zb-2", now + timedelta(minutes=30))
        lot.bid_count = 3
        lot.save()

        sent, _mock_post = _call_run_alerts(now, kinds=["zero_bid"])

        self.assertEqual(sent, [])

    def test_not_sent_when_bid_count_unknown(self):
        now = timezone.now()
        lot, _ev = _make_zero_bid("zb-3", now + timedelta(minutes=30))
        lot.bid_count = None
        lot.save()

        sent, _mock_post = _call_run_alerts(now, kinds=["zero_bid"])

        self.assertEqual(sent, [])

    def test_sent_for_lead_even_without_an_affordable_max_bid(self):
        now = timezone.now()
        lot, _ev = _make_zero_bid(
            "zb-4", now + timedelta(minutes=30), max_bid="0", is_lead=True, lead_reason="n64: bulk lot",
            category="games",
        )

        sent, mock_post = _call_run_alerts(now, kinds=["zero_bid"])

        self.assertEqual(sent, [(lot.pk, "zero_bid")])
        text = mock_post.call_args.kwargs["json"]["text"]
        self.assertIn("Lead: n64: bulk lot", text)

    def test_not_sent_when_not_affordable_and_not_a_lead(self):
        now = timezone.now()
        _make_zero_bid("zb-5", now + timedelta(minutes=30), max_bid="10.00", current_price="25.00")

        sent, _mock_post = _call_run_alerts(now, kinds=["zero_bid"])

        self.assertEqual(sent, [])

    def test_none_category_excluded(self):
        now = timezone.now()
        _make_zero_bid("zb-6", now + timedelta(minutes=30), category="none")

        sent, _mock_post = _call_run_alerts(now, kinds=["zero_bid"])

        self.assertEqual(sent, [])

    def test_excluded_unless_listed_in_kinds(self):
        now = timezone.now()
        _make_zero_bid("zb-7", now + timedelta(minutes=30))

        sent, _mock_post = _call_run_alerts(now, kinds=["candidate", "lead"])

        self.assertEqual(sent, [])

    def test_never_sent_twice(self):
        now = timezone.now()
        lot, _ev = _make_zero_bid("zb-8", now + timedelta(minutes=30))

        first_sent, first_post = _call_run_alerts(now, kinds=["zero_bid"])
        second_sent, second_post = _call_run_alerts(now + timedelta(minutes=5), kinds=["zero_bid"])

        self.assertEqual(first_sent, [(lot.pk, "zero_bid")])
        self.assertEqual(second_sent, [])
        second_post.assert_not_called()


class RelistedAlertTests(TestCase):
    def test_sent_for_affordable_relisted_lot(self):
        now = timezone.now()
        lot, _ev = _make_relisted("rl-1", now + timedelta(minutes=30))

        sent, mock_post = _call_run_alerts(now, kinds=["relisted"])

        self.assertEqual(sent, [(lot.pk, "relisted")])
        self.assertTrue(AlertSent.objects.filter(lot=lot, kind="relisted").exists())
        text = mock_post.call_args.kwargs["json"]["text"]
        self.assertIn("Relisted", text)

    def test_not_sent_when_not_actually_relisted(self):
        now = timezone.now()
        lot = SourcedLot.objects.create(
            source="shopgoodwill", external_id="rl-2", url="https://example.com/rl-2", title="Not Relisted",
            current_price=Decimal("10.00"), end_time=now + timedelta(minutes=30), features={},
        )
        LotEvaluation.objects.create(lot=lot, category="coins", max_bid=Decimal("20.00"))

        sent, _mock_post = _call_run_alerts(now, kinds=["relisted"])

        self.assertEqual(sent, [])

    def test_not_sent_for_non_shopgoodwill_source(self):
        now = timezone.now()
        _make_relisted("rl-3", now + timedelta(minutes=30), source="maxsold")

        sent, _mock_post = _call_run_alerts(now, kinds=["relisted"])

        self.assertEqual(sent, [])

    def test_sent_for_lead_even_when_not_affordable(self):
        now = timezone.now()
        lot, _ev = _make_relisted(
            "rl-4", now + timedelta(minutes=30), max_bid="0", current_price="10.00", is_lead=True,
            lead_reason="n64: bulk lot", category="games",
        )

        sent, _mock_post = _call_run_alerts(now, kinds=["relisted"])

        self.assertEqual(sent, [(lot.pk, "relisted")])

    def test_not_sent_when_over_max_and_not_a_lead(self):
        now = timezone.now()
        _make_relisted("rl-5", now + timedelta(minutes=30), max_bid="10.00", current_price="25.00")

        sent, _mock_post = _call_run_alerts(now, kinds=["relisted"])

        self.assertEqual(sent, [])

    def test_none_category_excluded(self):
        now = timezone.now()
        _make_relisted("rl-6", now + timedelta(minutes=30), category="none")

        sent, _mock_post = _call_run_alerts(now, kinds=["relisted"])

        self.assertEqual(sent, [])

    def test_excluded_unless_listed_in_kinds(self):
        now = timezone.now()
        _make_relisted("rl-7", now + timedelta(minutes=30))

        sent, _mock_post = _call_run_alerts(now, kinds=["candidate", "lead"])

        self.assertEqual(sent, [])

    def test_never_sent_twice(self):
        now = timezone.now()
        lot, _ev = _make_relisted("rl-8", now + timedelta(minutes=30))

        first_sent, first_post = _call_run_alerts(now, kinds=["relisted"])
        second_sent, second_post = _call_run_alerts(now + timedelta(minutes=5), kinds=["relisted"])

        self.assertEqual(first_sent, [(lot.pk, "relisted")])
        self.assertEqual(second_sent, [])
        second_post.assert_not_called()


class InQuietHoursTests(TestCase):
    """Direct boundary tests for the pure _in_quiet_hours helper - 23 -> 7
    wraps past midnight and is inclusive of 23:00, exclusive of 07:00."""

    def test_no_quiet_hours_configured(self):
        self.assertFalse(_in_quiet_hours(timezone.now(), {}))

    def test_inside_wrapped_window_late_night(self):
        now = _at_local_hour(timezone.now(), 23)
        self.assertTrue(_in_quiet_hours(now, {"quiet_hours": [23, 7]}))

    def test_inside_wrapped_window_early_morning(self):
        now = _at_local_hour(timezone.now(), 6)
        self.assertTrue(_in_quiet_hours(now, {"quiet_hours": [23, 7]}))

    def test_boundary_hour_7_is_not_quiet(self):
        now = _at_local_hour(timezone.now(), 7)
        self.assertFalse(_in_quiet_hours(now, {"quiet_hours": [23, 7]}))

    def test_boundary_hour_22_is_not_quiet(self):
        now = _at_local_hour(timezone.now(), 22)
        self.assertFalse(_in_quiet_hours(now, {"quiet_hours": [23, 7]}))

    def test_midday_not_quiet(self):
        now = _at_local_hour(timezone.now(), 14)
        self.assertFalse(_in_quiet_hours(now, {"quiet_hours": [23, 7]}))

    def test_non_wrapping_window(self):
        # A same-day window (start < end) - not what quiet_hours is
        # configured as, but the helper should handle it correctly too.
        self.assertTrue(_in_quiet_hours(_at_local_hour(timezone.now(), 10), {"quiet_hours": [9, 17]}))
        self.assertFalse(_in_quiet_hours(_at_local_hour(timezone.now(), 18), {"quiet_hours": [9, 17]}))


class QuietHoursGatingTests(TestCase):
    def test_candidate_held_during_quiet_hours_and_not_marked_sent(self):
        quiet_now = _at_local_hour(timezone.now(), 2)
        lot, _ev = _make_candidate("qh-cand-1", quiet_now + timedelta(minutes=30))

        sent, mock_post = _call_run_alerts(quiet_now, quiet_hours=[23, 7])

        self.assertEqual(sent, [])
        mock_post.assert_not_called()
        self.assertFalse(AlertSent.objects.filter(lot=lot).exists())

    def test_held_candidate_sends_once_quiet_hours_end(self):
        # window_minutes is widened so the same lot is still "ending soon"
        # at both check times, which are hours apart (crossing the quiet
        # hours boundary) - window_minutes=90 (the default) would make the
        # lot stale by the second check for reasons having nothing to do
        # with quiet hours.
        quiet_now = _at_local_hour(timezone.now(), 2)
        lot, _ev = _make_candidate("qh-cand-2", quiet_now + timedelta(hours=20))

        held_sent, _ = _call_run_alerts(quiet_now, quiet_hours=[23, 7], window_minutes=1440)
        awake_now = quiet_now + timedelta(hours=6)  # local hour 8 - past quiet hours
        later_sent, mock_post = _call_run_alerts(awake_now, quiet_hours=[23, 7], window_minutes=1440)

        self.assertEqual(held_sent, [])
        self.assertEqual(later_sent, [(lot.pk, "candidate")])
        mock_post.assert_called_once()

    def test_lead_zero_bid_and_relisted_all_held_during_quiet_hours(self):
        quiet_now = _at_local_hour(timezone.now(), 3)
        _make_lead("qh-lead", quiet_now + timedelta(minutes=30))
        _make_zero_bid("qh-zb", quiet_now + timedelta(minutes=30))
        _make_relisted("qh-rl", quiet_now + timedelta(minutes=30))

        sent, mock_post = _call_run_alerts(
            quiet_now, kinds=["candidate", "lead", "zero_bid", "relisted"], quiet_hours=[23, 7],
        )

        self.assertEqual(sent, [])
        mock_post.assert_not_called()

    def test_check_by_hand_not_held_during_quiet_hours(self):
        """Quiet hours only gates candidate/lead/zero_bid/relisted per the
        spec - check_by_hand is deliberately left out."""
        quiet_now = _at_local_hour(timezone.now(), 2)
        lot, _ev = _make_check_by_hand("qh-cbh", quiet_now + timedelta(minutes=30))

        sent, mock_post = _call_run_alerts(
            quiet_now, kinds=["candidate", "lead", "check_by_hand"], quiet_hours=[23, 7],
        )

        self.assertEqual(sent, [(lot.pk, "check_by_hand")])
        mock_post.assert_called_once()

    def test_outside_quiet_hours_sends_normally(self):
        awake_now = _at_local_hour(timezone.now(), 14)
        lot, _ev = _make_candidate("qh-awake", awake_now + timedelta(minutes=30))

        sent, mock_post = _call_run_alerts(awake_now, quiet_hours=[23, 7])

        self.assertEqual(sent, [(lot.pk, "candidate")])
        mock_post.assert_called_once()


class DigestTests(TestCase):
    def test_hibid_included_in_health_section(self):
        """build_digest_message() loops every configured source
        (RESELLING_SCANNER["SOURCES"]), so adding hibid there is enough -
        no separate digest-side wiring needed."""
        message = build_digest_message(now=timezone.now())

        self.assertIn("HiBid:", message)

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


def _make_spot(metal, price, source="goldapi", fetched_at=None):
    row = SpotPrice.objects.create(metal=metal, price_usd=Decimal(str(price)), source=source)
    if fetched_at is not None:
        SpotPrice.objects.filter(pk=row.pk).update(fetched_at=fetched_at)
        row.refresh_from_db()
    return row


class SpotSectionTests(TestCase):
    def test_price_and_change_reported(self):
        now = timezone.now()
        _make_spot("silver", "30.00", fetched_at=now - timedelta(days=1))
        _make_spot("silver", "30.60", fetched_at=now)  # +2%
        _make_spot("gold", "2500.00", fetched_at=now - timedelta(days=1))
        _make_spot("gold", "2500.00", fetched_at=now)  # 0%

        lines, warnings = _spot_section(now)

        self.assertTrue(any("30.60" in line and "+2.0%" in line for line in lines))
        self.assertEqual(warnings, [])

    def test_warns_when_move_exceeds_3_percent(self):
        now = timezone.now()
        _make_spot("silver", "30.00", fetched_at=now - timedelta(days=1))
        _make_spot("silver", "31.50", fetched_at=now)  # +5%
        _make_spot("gold", "2500.00", fetched_at=now - timedelta(days=1))
        _make_spot("gold", "2500.00", fetched_at=now)

        lines, warnings = _spot_section(now)

        self.assertTrue(any("shifted" in w.lower() for w in warnings))
        self.assertTrue(any("Silver" in w for w in warnings))

    def test_no_warning_at_exactly_3_percent_or_below(self):
        now = timezone.now()
        _make_spot("silver", "100.00", fetched_at=now - timedelta(days=1))
        _make_spot("silver", "103.00", fetched_at=now)  # exactly +3%
        _make_spot("gold", "2500.00", fetched_at=now - timedelta(days=1))
        _make_spot("gold", "2500.00", fetched_at=now)

        _lines, warnings = _spot_section(now)

        self.assertEqual(warnings, [])

    def test_failed_fetch_today_reported_clearly(self):
        now = timezone.now()
        _make_spot("silver", "0", source="goldapi_failed", fetched_at=now)
        _make_spot("gold", "2500.00", fetched_at=now - timedelta(days=1))
        _make_spot("gold", "2500.00", fetched_at=now)

        lines, warnings = _spot_section(now)

        self.assertTrue(any("FAILED" in line for line in lines))
        self.assertTrue(any("fetch failed" in w.lower() for w in warnings))

    def test_no_price_today_reported(self):
        now = timezone.now()
        _make_spot("silver", "30.00", fetched_at=now - timedelta(days=2))
        _make_spot("gold", "2500.00", fetched_at=now - timedelta(days=2))

        lines, warnings = _spot_section(now)

        self.assertTrue(any("no price fetched today" in line for line in lines))
        self.assertEqual(len(warnings), 2)

    def test_no_prior_price_does_not_warn(self):
        now = timezone.now()
        _make_spot("silver", "30.00", fetched_at=now)
        _make_spot("gold", "2500.00", fetched_at=now)

        lines, warnings = _spot_section(now)

        self.assertTrue(any("no prior price" in line for line in lines))
        self.assertEqual(warnings, [])


class SourceHealthTests(TestCase):
    def test_ok_runs_produce_no_warnings(self):
        now = timezone.now()
        for _ in range(3):
            ScanRun.objects.create(source="shopgoodwill", lots_seen=10, candidates=1)

        lines, warnings = _source_health_lines("shopgoodwill", now)

        self.assertIn("ShopGoodwill: 3 run(s) in last 24h (expect ~24)", lines)
        self.assertEqual(warnings, [])

    def test_expected_runs_per_day_uses_hibid_longer_interval(self):
        now = timezone.now()
        ScanRun.objects.create(source="hibid", lots_seen=10, candidates=1)

        lines, _warnings = _source_health_lines("hibid", now)

        self.assertIn("HiBid: 1 run(s) in last 24h (expect ~4)", lines)

    def test_error_run_flagged(self):
        now = timezone.now()
        ScanRun.objects.create(source="shopgoodwill", lots_seen=5, error="boom")

        _lines, warnings = _source_health_lines("shopgoodwill", now)

        self.assertTrue(any("error" in w.lower() for w in warnings))

    def test_failed_keywords_flagged(self):
        now = timezone.now()
        ScanRun.objects.create(source="shopgoodwill", lots_seen=5, failed_keywords=["peace dollar"])

        lines, warnings = _source_health_lines("shopgoodwill", now)

        self.assertTrue(any("peace dollar" in line for line in lines))
        self.assertTrue(any("peace dollar" in w for w in warnings))

    def test_zero_lot_run_flagged(self):
        now = timezone.now()
        ScanRun.objects.create(source="shopgoodwill", lots_seen=0)

        _lines, warnings = _source_health_lines("shopgoodwill", now)

        self.assertTrue(any("data format" in w for w in warnings))

    def test_missing_source_never_run_flagged(self):
        now = timezone.now()

        lines, warnings = _source_health_lines("shopgoodwill", now)

        self.assertIn("  Last run: never", lines)
        self.assertTrue(any("never run" in w for w in warnings))

    def test_runs_older_than_24h_excluded_from_count(self):
        now = timezone.now()
        old = ScanRun.objects.create(source="shopgoodwill", lots_seen=5)
        ScanRun.objects.filter(pk=old.pk).update(started_at=now - timedelta(hours=30))

        lines, _warnings = _source_health_lines("shopgoodwill", now)

        self.assertIn("ShopGoodwill: 0 run(s) in last 24h (expect ~24)", lines)
        # still shown as the (stale) last run, just not counted in the 24h window
        self.assertFalse(any(line.startswith("  Last run: never") for line in lines))


class OtherHealthSectionTests(TestCase):
    def test_spot_fetch_health_counts_failures_and_monthly_attempts(self):
        now = timezone.now()
        SpotPrice.objects.create(metal="silver", price_usd=Decimal("0"), source="goldapi_failed")
        SpotPrice.objects.create(metal="gold", price_usd=Decimal("2500"), source="goldapi")

        lines, warnings = _spot_fetch_health_lines(now)

        self.assertTrue(any("2/" in line for line in lines))  # 2 attempts this month
        self.assertTrue(any("failed" in w.lower() for w in warnings))

    def test_spot_fetch_monthly_limit_reached(self):
        now = timezone.now()
        limit = settings.RESELLING_SCANNER["SPOT"]["monthly_api_limit"]
        for i in range(limit):
            SpotPrice.objects.create(metal="silver", price_usd=Decimal("30"), source="goldapi")

        _lines, warnings = _spot_fetch_health_lines(now)

        self.assertTrue(any("monthly limit reached" in w.lower() for w in warnings))

    def test_track_closed_no_activity_warns(self):
        now = timezone.now()

        _lines, warnings = _track_closed_health_lines(now)

        self.assertTrue(any("may not have run" in w for w in warnings))

    def test_track_closed_activity_no_warning(self):
        now = timezone.now()
        lot = SourcedLot.objects.create(
            source="shopgoodwill", external_id="tc-1", url="https://example.com/tc-1", title="Lot",
            current_price=Decimal("10"), final_checked_at=now,
        )

        lines, warnings = _track_closed_health_lines(now)

        self.assertIn("track_closed: 1 lot(s) updated in last 24h", lines)
        self.assertEqual(warnings, [])
        self.assertTrue(lot.pk)

    def test_ai_review_budget_warning_at_80_percent(self):
        now = timezone.now()
        budget = settings.RESELLING_SCANNER["AI_REVIEW"]["monthly_budget_usd"]
        lot = SourcedLot.objects.create(
            source="shopgoodwill", external_id="ai-budget-1", url="https://example.com/ai-budget-1", title="Lot",
            current_price=Decimal("10"),
        )
        AIReview.objects.create(lot=lot, status="done", cost_usd=Decimal(str(budget)) * Decimal("0.85"))

        _lines, warnings = _ai_review_health_lines(now)

        self.assertTrue(any("budget" in w.lower() for w in warnings))

    def test_ai_review_no_warning_below_80_percent(self):
        now = timezone.now()
        lot = SourcedLot.objects.create(
            source="shopgoodwill", external_id="ai-budget-2", url="https://example.com/ai-budget-2", title="Lot",
            current_price=Decimal("10"),
        )
        AIReview.objects.create(lot=lot, status="done", cost_usd=Decimal("0.01"))

        _lines, warnings = _ai_review_health_lines(now)

        self.assertEqual(warnings, [])

    def test_alerts_sent_grouped_by_kind(self):
        now = timezone.now()
        lot = SourcedLot.objects.create(
            source="shopgoodwill", external_id="alert-kind-1", url="https://example.com/alert-kind-1", title="Lot",
            current_price=Decimal("10"),
        )
        AlertSent.objects.create(lot=lot, kind="candidate")

        lines = _alerts_sent_lines(now)

        self.assertTrue(any("candidate: 1" in line for line in lines))

    def test_alerts_sent_none(self):
        now = timezone.now()

        lines = _alerts_sent_lines(now)

        self.assertEqual(lines, ["Alerts sent (24h): none"])


class MyBidsTests(TestCase):
    """_my_bids_counts now reads real BidWatch rows (created via the "I bid
    on this" button), not a scanner-candidate heuristic - see
    scanner/bid_watch.py and ledger_metrics.dashboard_tiles()."""

    def test_watching_counts_live_watches(self):
        now = timezone.now()
        lot = _make_lot("bid-watch-1", now + timedelta(hours=3))
        BidWatch.objects.create(lot=lot, my_max_bid=Decimal("30.00"), status="watching")

        watching, likely_won, lost = _my_bids_counts(now)

        self.assertEqual(watching, 1)
        self.assertEqual((likely_won, lost), (0, 0))

    def test_likely_won_within_last_24h(self):
        now = timezone.now()
        lot = _make_lot("bid-won-1", now - timedelta(hours=1))
        BidWatch.objects.create(
            lot=lot, my_max_bid=Decimal("20.00"), status="likely_won",
            resolved_at=now - timedelta(hours=2),
        )

        watching, likely_won, lost = _my_bids_counts(now)

        self.assertEqual(watching, 0)
        self.assertEqual(likely_won, 1)
        self.assertEqual(lost, 0)

    def test_lost_within_last_24h(self):
        now = timezone.now()
        lot = _make_lot("bid-lost-1", now - timedelta(hours=1))
        BidWatch.objects.create(
            lot=lot, my_max_bid=Decimal("20.00"), status="lost",
            resolved_at=now - timedelta(hours=2),
        )

        _watching, likely_won, lost = _my_bids_counts(now)

        self.assertEqual(likely_won, 0)
        self.assertEqual(lost, 1)

    def test_resolved_outside_24h_window_not_counted(self):
        now = timezone.now()
        lot = _make_lot("bid-old-1", now - timedelta(days=3))
        BidWatch.objects.create(
            lot=lot, my_max_bid=Decimal("20.00"), status="likely_won",
            resolved_at=now - timedelta(hours=25),
        )

        _watching, likely_won, _lost = _my_bids_counts(now)

        self.assertEqual(likely_won, 0)

    def test_lots_without_a_watch_not_counted(self):
        now = timezone.now()
        _make_candidate("bid-none-1", now + timedelta(hours=3))  # scanner candidate, never watched

        watching, likely_won, lost = _my_bids_counts(now)

        self.assertEqual((watching, likely_won, lost), (0, 0, 0))


class CriticalAlertTests(TestCase):
    @mock.patch(TELEGRAM_POST)
    @mock.patch.dict(os.environ, CREDS)
    def test_sends_and_records(self, mock_post):
        mock_post.return_value = _ok_response()

        result = send_critical_alert("shopgoodwill", "blocked", "test message")

        self.assertTrue(result)
        mock_post.assert_called_once()
        self.assertTrue(CriticalAlertSent.objects.filter(source="shopgoodwill", alert_type="blocked").exists())

    @mock.patch(TELEGRAM_POST)
    @mock.patch.dict(os.environ, CREDS)
    def test_sends_during_quiet_hours(self, mock_post):
        """Critical system alerts always send - quiet_hours only gates
        run_alerts()'s per-lot kinds, send_critical_alert never checks it.
        Uses the real ALERTS config (which has critical_alerts=True) with
        quiet_hours added, not the test-local _cfg() fixture - that fixture
        doesn't set critical_alerts at all, which would make this a no-op
        regardless of quiet hours and prove nothing."""
        mock_post.return_value = _ok_response()
        quiet_now = _at_local_hour(timezone.now(), 2)
        real_alerts_cfg = {**settings.RESELLING_SCANNER["ALERTS"], "quiet_hours": [23, 7]}

        with override_settings(RESELLING_SCANNER={**settings.RESELLING_SCANNER, "ALERTS": real_alerts_cfg}):
            result = send_critical_alert("shopgoodwill", "blocked", "test message", now=quiet_now)

        self.assertTrue(result)
        mock_post.assert_called_once()

    @mock.patch(TELEGRAM_POST)
    @mock.patch.dict(os.environ, CREDS)
    def test_rate_limited_within_6_hours(self, mock_post):
        mock_post.return_value = _ok_response()
        now = timezone.now()

        send_critical_alert("shopgoodwill", "blocked", "first", now=now)
        second = send_critical_alert("shopgoodwill", "blocked", "second", now=now + timedelta(hours=5))

        self.assertFalse(second)
        mock_post.assert_called_once()
        self.assertEqual(CriticalAlertSent.objects.filter(source="shopgoodwill", alert_type="blocked").count(), 1)

    @mock.patch(TELEGRAM_POST)
    @mock.patch.dict(os.environ, CREDS)
    def test_allowed_again_after_6_hours(self, mock_post):
        mock_post.return_value = _ok_response()
        now = timezone.now()

        send_critical_alert("shopgoodwill", "blocked", "first", now=now)
        second = send_critical_alert("shopgoodwill", "blocked", "second", now=now + timedelta(hours=7))

        self.assertTrue(second)
        self.assertEqual(mock_post.call_count, 2)

    @mock.patch(TELEGRAM_POST)
    @mock.patch.dict(os.environ, CREDS)
    def test_different_alert_types_not_rate_limited_against_each_other(self, mock_post):
        mock_post.return_value = _ok_response()
        now = timezone.now()

        send_critical_alert("shopgoodwill", "blocked", "a", now=now)
        second = send_critical_alert("shopgoodwill", "zero_lots", "b", now=now)

        self.assertTrue(second)
        self.assertEqual(mock_post.call_count, 2)

    @mock.patch(TELEGRAM_POST)
    @mock.patch.dict(os.environ, CREDS)
    def test_different_sources_not_rate_limited_against_each_other(self, mock_post):
        mock_post.return_value = _ok_response()
        now = timezone.now()

        send_critical_alert("shopgoodwill", "blocked", "a", now=now)
        second = send_critical_alert("maxsold", "blocked", "b", now=now)

        self.assertTrue(second)
        self.assertEqual(mock_post.call_count, 2)

    @mock.patch(TELEGRAM_POST)
    @mock.patch.dict(os.environ, CREDS)
    def test_disabled_via_setting(self, mock_post):
        with override_settings(RESELLING_SCANNER=_cfg(critical_alerts=False)):
            result = send_critical_alert("shopgoodwill", "blocked", "test")

        self.assertFalse(result)
        mock_post.assert_not_called()
        self.assertFalse(CriticalAlertSent.objects.exists())

    @mock.patch(TELEGRAM_POST)
    @mock.patch.dict(os.environ, CREDS)
    def test_check_zero_lots_triggers_alert(self, mock_post):
        mock_post.return_value = _ok_response()

        result = check_zero_lots("shopgoodwill", 0)

        self.assertTrue(result)
        mock_post.assert_called_once()

    @mock.patch(TELEGRAM_POST)
    @mock.patch.dict(os.environ, CREDS)
    def test_check_zero_lots_no_alert_when_lots_seen(self, mock_post):
        result = check_zero_lots("shopgoodwill", 5)

        self.assertFalse(result)
        mock_post.assert_not_called()

    @mock.patch(TELEGRAM_POST)
    @mock.patch.dict(os.environ, CREDS)
    def test_check_stale_source_triggers_after_3_hours(self, mock_post):
        mock_post.return_value = _ok_response()
        now = timezone.now()
        run = ScanRun.objects.create(source="shopgoodwill", lots_seen=5)
        ScanRun.objects.filter(pk=run.pk).update(
            started_at=now - timedelta(hours=4), finished_at=now - timedelta(hours=4),
        )

        result = check_stale_source("shopgoodwill", now=now)

        self.assertTrue(result)
        mock_post.assert_called_once()

    @mock.patch(TELEGRAM_POST)
    @mock.patch.dict(os.environ, CREDS)
    def test_check_stale_source_no_alert_within_3_hours(self, mock_post):
        now = timezone.now()
        run = ScanRun.objects.create(source="shopgoodwill", lots_seen=5)
        ScanRun.objects.filter(pk=run.pk).update(
            started_at=now - timedelta(hours=1), finished_at=now - timedelta(hours=1),
        )

        result = check_stale_source("shopgoodwill", now=now)

        self.assertFalse(result)
        mock_post.assert_not_called()

    @mock.patch(TELEGRAM_POST)
    @mock.patch.dict(os.environ, CREDS)
    def test_check_stale_source_no_alert_when_never_run(self, mock_post):
        result = check_stale_source("shopgoodwill")

        self.assertFalse(result)
        mock_post.assert_not_called()

    @mock.patch(TELEGRAM_POST)
    @mock.patch.dict(os.environ, CREDS)
    def test_check_stale_source_uses_hibid_longer_threshold(self, mock_post):
        """HiBid's expected_interval_hours=6 gives an 8h threshold (6+2) -
        a 7h-old finish, already stale for ShopGoodwill's default 3h
        threshold, is still fine for HiBid."""
        mock_post.return_value = _ok_response()
        now = timezone.now()
        run = ScanRun.objects.create(source="hibid", lots_seen=5)
        ScanRun.objects.filter(pk=run.pk).update(
            started_at=now - timedelta(hours=7), finished_at=now - timedelta(hours=7),
        )

        result = check_stale_source("hibid", now=now)

        self.assertFalse(result)
        mock_post.assert_not_called()

    @mock.patch(TELEGRAM_POST)
    @mock.patch.dict(os.environ, CREDS)
    def test_check_stale_source_triggers_for_hibid_past_its_longer_threshold(self, mock_post):
        mock_post.return_value = _ok_response()
        now = timezone.now()
        run = ScanRun.objects.create(source="hibid", lots_seen=5)
        ScanRun.objects.filter(pk=run.pk).update(
            started_at=now - timedelta(hours=9), finished_at=now - timedelta(hours=9),
        )

        result = check_stale_source("hibid", now=now)

        self.assertTrue(result)
        mock_post.assert_called_once()
        self.assertIn("8h", mock_post.call_args.kwargs["json"]["text"])


class ScanLotsCriticalAlertIntegrationTests(TestCase):
    """scan_lots itself wires SourceBlocked/zero-lots into the critical
    alert path - exercised end to end (with the adapter mocked, never
    hitting shopgoodwill.com, and Telegram mocked too)."""

    @mock.patch(TELEGRAM_POST)
    @mock.patch.dict(os.environ, CREDS)
    @mock.patch("albright_reselling_app.scanner.adapters.base.time.sleep")
    @mock.patch("albright_reselling_app.scanner.pipeline.get_all_spot", return_value={"silver": 30.0, "gold": 2500.0})
    def test_source_blocked_triggers_critical_alert(self, mock_spot, mock_sleep, mock_post):
        mock_post.return_value = _ok_response()

        def fake_search(self, keyword):
            raise SourceBlocked("403")
            yield  # pragma: no cover - makes this a generator

        out = StringIO()
        with mock.patch.object(ShopGoodwillAdapter, "search", fake_search):
            call_command("scan_lots", "--keyword", "morgan dollar", stdout=out, stderr=out)

        self.assertTrue(CriticalAlertSent.objects.filter(source="shopgoodwill", alert_type="blocked").exists())
        self.assertTrue(
            any("BLOCKED" in c.kwargs["json"]["text"] for c in mock_post.call_args_list)
        )

    @mock.patch(TELEGRAM_POST)
    @mock.patch.dict(os.environ, CREDS)
    @mock.patch("albright_reselling_app.scanner.adapters.base.time.sleep")
    @mock.patch("albright_reselling_app.scanner.pipeline.get_all_spot", return_value={"silver": 30.0, "gold": 2500.0})
    def test_zero_lots_triggers_critical_alert(self, mock_spot, mock_sleep, mock_post):
        mock_post.return_value = _ok_response()

        def fake_search(self, keyword):
            return iter([])

        out = StringIO()
        with mock.patch.object(ShopGoodwillAdapter, "search", fake_search):
            call_command("scan_lots", "--keyword", "morgan dollar", stdout=out, stderr=out)

        self.assertTrue(CriticalAlertSent.objects.filter(source="shopgoodwill", alert_type="zero_lots").exists())

    @mock.patch(TELEGRAM_POST)
    @mock.patch.dict(os.environ, CREDS)
    @mock.patch("albright_reselling_app.scanner.adapters.base.time.sleep")
    @mock.patch("albright_reselling_app.scanner.pipeline.get_all_spot", return_value={"silver": 30.0, "gold": 2500.0})
    def test_stale_source_checked_at_start_of_run(self, mock_spot, mock_sleep, mock_post):
        mock_post.return_value = _ok_response()
        now = timezone.now()
        run = ScanRun.objects.create(source="shopgoodwill", lots_seen=5)
        ScanRun.objects.filter(pk=run.pk).update(
            started_at=now - timedelta(hours=5), finished_at=now - timedelta(hours=5),
        )

        def fake_search(self, keyword):
            yield RawLot(source="shopgoodwill", external_id="x", url="https://example.com/x", title="t",
                         current_price=10.0)

        out = StringIO()
        with mock.patch.object(ShopGoodwillAdapter, "search", fake_search):
            call_command("scan_lots", "--keyword", "morgan dollar", stdout=out, stderr=out)

        self.assertTrue(CriticalAlertSent.objects.filter(source="shopgoodwill", alert_type="stale").exists())
