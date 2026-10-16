"""Tests for lot dismissal: the dashboard's Act Now "Dismiss" button
(LotDismissal, scanner/dashboard.py's hide/reopen rule, dismiss_views.py),
its effect on the Today page's candidates section and Telegram's per-lot
alerts, and confirmation that the Watchlist section/the Watch and
Performance tabs/AI review history are all untouched by it."""
import os
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.conf import settings
from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from albright_reselling_app import selling
from albright_reselling_app.scanner.alerts import run_alerts
from albright_reselling_app.scanner.dashboard import get_scanner_dashboard_context
from albright_reselling_app.scanner_models import BidWatch, LotDismissal, LotEvaluation, SourcedLot

TELEGRAM_POST = "albright_reselling_app.scanner.alerts.requests.post"
CREDS = {"TELEGRAM_BOT_TOKEN": "test-token", "TELEGRAM_CHAT_ID": "12345"}


def _make_lot(external_id, end_time, current_price="10.00", source="shopgoodwill", bid_count=None):
    return SourcedLot.objects.create(
        source=source, external_id=external_id, url=f"https://example.com/{external_id}",
        title=f"Morgan Silver Dollar {external_id}", current_price=Decimal(current_price), end_time=end_time,
        bid_count=bid_count,
    )


def _make_evaluation(lot, is_candidate=True, max_bid="30.00", headroom="20.00", confidence="high",
                      category="coins"):
    return LotEvaluation.objects.create(
        lot=lot, category=category, is_candidate=is_candidate, max_bid=Decimal(max_bid),
        headroom=Decimal(headroom), confidence=confidence,
    )


def _make_lead(lot, category="games", lead_reason="n64: bulk lot"):
    return LotEvaluation.objects.create(lot=lot, category=category, is_lead=True, lead_reason=lead_reason)


def _dismiss(lot, headroom_at_dismissal="20.00", reason="", dismissed_at=None):
    return LotDismissal.objects.create(
        lot=lot, dismissed_at=dismissed_at or timezone.now(),
        headroom_at_dismissal=Decimal(headroom_at_dismissal) if headroom_at_dismissal is not None else None,
        reason=reason,
    )


class LotDismissalModelTests(TestCase):
    def test_reason_optional(self):
        lot = _make_lot("model-1", timezone.now() + timedelta(hours=2))
        dismissal = LotDismissal.objects.create(lot=lot, dismissed_at=timezone.now())
        self.assertEqual(dismissal.reason, "")

    def test_headroom_at_dismissal_nullable(self):
        lot = _make_lot("model-2", timezone.now() + timedelta(hours=2))
        dismissal = LotDismissal.objects.create(lot=lot, dismissed_at=timezone.now(), headroom_at_dismissal=None)
        self.assertIsNone(dismissal.headroom_at_dismissal)


class DashboardDismissalFilteringTests(TestCase):
    """get_scanner_dashboard_context()'s hide/reopen rule, applied to all
    3 Act Now tables (candidates ending soon, check by hand, leads)."""

    def test_dismissed_candidate_hidden_from_active_list(self):
        lot = _make_lot("dsm-cand-1", timezone.now() + timedelta(hours=3))
        _make_evaluation(lot, is_candidate=True, headroom="20.00")
        _dismiss(lot, headroom_at_dismissal="20.00")

        context = get_scanner_dashboard_context()

        self.assertEqual(context["live_candidates_ending_soon"], [])
        self.assertEqual([r["title"] for r in context["dismissed_rows"]], [lot.title])

    def test_dismissed_check_by_hand_hidden_from_active_list(self):
        lot = _make_lot("dsm-cbh-1", timezone.now() + timedelta(hours=3))
        _make_evaluation(lot, is_candidate=False, confidence="low", max_bid="20.00", headroom="5.00")
        _dismiss(lot, headroom_at_dismissal="5.00")

        context = get_scanner_dashboard_context()

        self.assertEqual(context["check_by_hand"], [])
        self.assertEqual([r["title"] for r in context["dismissed_rows"]], [lot.title])

    def test_dismissed_lead_hidden_from_active_list(self):
        lot = _make_lot("dsm-lead-1", timezone.now() + timedelta(hours=3))
        _make_lead(lot)
        _dismiss(lot, headroom_at_dismissal="0")

        context = get_scanner_dashboard_context()

        self.assertEqual(context["leads_to_review"], [])
        self.assertEqual([r["title"] for r in context["dismissed_rows"]], [lot.title])

    def test_dismissed_lead_never_reopens_on_its_own(self):
        """A lead's headroom is always 0 (see pipeline._apply_lead) - there's
        nothing for it to grow past, so it only comes back via Undo."""
        lot = _make_lot("dsm-lead-2", timezone.now() + timedelta(hours=3))
        _make_lead(lot)
        _dismiss(lot, headroom_at_dismissal="0", dismissed_at=timezone.now() - timedelta(days=365))

        context = get_scanner_dashboard_context()

        self.assertEqual(context["leads_to_review"], [])

    def test_reopens_once_headroom_grows_past_threshold(self):
        lot = _make_lot("dsm-reopen-1", timezone.now() + timedelta(hours=3))
        _make_evaluation(lot, is_candidate=True, headroom="50.00")  # grew by 30 since dismissal
        _dismiss(lot, headroom_at_dismissal="20.00")

        with override_settings(RESELLING_SCANNER={**settings.RESELLING_SCANNER, "DISMISS_REOPEN_HEADROOM": 25}):
            context = get_scanner_dashboard_context()

        self.assertEqual([r["title"] for r in context["live_candidates_ending_soon"]], [lot.title])
        self.assertEqual(context["dismissed_rows"], [])
        row = context["live_candidates_ending_soon"][0]
        self.assertEqual(row["reopened_headroom_increase"], Decimal("30.00"))

    def test_stays_dismissed_below_reopen_threshold(self):
        lot = _make_lot("dsm-reopen-2", timezone.now() + timedelta(hours=3))
        _make_evaluation(lot, is_candidate=True, headroom="40.00")  # only grew by 20
        _dismiss(lot, headroom_at_dismissal="20.00")

        with override_settings(RESELLING_SCANNER={**settings.RESELLING_SCANNER, "DISMISS_REOPEN_HEADROOM": 25}):
            context = get_scanner_dashboard_context()

        self.assertEqual(context["live_candidates_ending_soon"], [])
        self.assertEqual(len(context["dismissed_rows"]), 1)

    def test_reopen_threshold_defaults_to_25(self):
        lot = _make_lot("dsm-reopen-3", timezone.now() + timedelta(hours=3))
        _make_evaluation(lot, is_candidate=True, headroom="46.00")  # grew by exactly 25 (>= threshold)
        _dismiss(lot, headroom_at_dismissal="21.00")

        cfg = {k: v for k, v in settings.RESELLING_SCANNER.items() if k != "DISMISS_REOPEN_HEADROOM"}
        with override_settings(RESELLING_SCANNER=cfg):
            context = get_scanner_dashboard_context()

        self.assertEqual([r["title"] for r in context["live_candidates_ending_soon"]], [lot.title])

    def test_undo_restores_a_dismissed_lot(self):
        lot = _make_lot("dsm-undo-1", timezone.now() + timedelta(hours=3))
        _make_evaluation(lot, is_candidate=True, headroom="20.00")
        dismissal = _dismiss(lot, headroom_at_dismissal="20.00")

        self.assertEqual(get_scanner_dashboard_context()["live_candidates_ending_soon"], [])

        dismissal.delete()

        context = get_scanner_dashboard_context()
        self.assertEqual([r["title"] for r in context["live_candidates_ending_soon"]], [lot.title])
        self.assertEqual(context["dismissed_rows"], [])

    def test_undismissed_candidates_unaffected(self):
        lot = _make_lot("dsm-other-1", timezone.now() + timedelta(hours=3))
        _make_evaluation(lot, is_candidate=True)

        context = get_scanner_dashboard_context()

        self.assertEqual([r["title"] for r in context["live_candidates_ending_soon"]], [lot.title])
        self.assertEqual(context["dismissed_rows"], [])
        self.assertEqual(context["dismissed_count"], 0)

    def test_tab_counts_exclude_dismissed(self):
        lot1 = _make_lot("dsm-count-1", timezone.now() + timedelta(hours=3))
        _make_evaluation(lot1, is_candidate=True)
        lot2 = _make_lot("dsm-count-2", timezone.now() + timedelta(hours=3))
        _make_evaluation(lot2, is_candidate=True, headroom="20.00")
        _dismiss(lot2, headroom_at_dismissal="20.00")

        context = get_scanner_dashboard_context()

        self.assertEqual(context["tab_counts"]["act_now"], 1)
        self.assertEqual(context["dismissed_count"], 1)

    def test_watched_lot_unaffected_by_dismissal(self):
        """Watchlisted lots are never hidden by Dismiss - a watched lot
        that also happens to carry a dismissal (shouldn't normally happen
        through the UI, but nothing stops it at the DB level) still shows
        up in the Watchlist section. It's also excluded from its own Act
        Now table entirely (a watchlisted lot appears only in the
        Watchlist section - see get_scanner_dashboard_context()), so the
        stale dismissal on it is moot - it doesn't show up as dismissed
        either."""
        lot = _make_lot("dsm-watch-1", timezone.now() + timedelta(hours=3))
        _make_evaluation(lot, is_candidate=True, headroom="20.00")
        BidWatch.objects.create(lot=lot, my_max_bid=Decimal("25.00"), status="watching")
        _dismiss(lot, headroom_at_dismissal="20.00")

        context = get_scanner_dashboard_context()

        self.assertEqual([r["title"] for r in context["watched_lots"]], [lot.title])
        self.assertEqual(context["live_candidates_ending_soon"], [])
        self.assertEqual(context["dismissed_rows"], [])


class TodayPageDismissalTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="tester", password="pw-not-real-12345")

    def test_dismissed_candidate_excluded_from_today_candidates(self):
        lot = _make_lot("dsm-today-1", timezone.now() + timedelta(hours=3))
        _make_evaluation(lot, is_candidate=True, headroom="20.00")
        _dismiss(lot, headroom_at_dismissal="20.00")

        sections = selling.today_sections(self.user)

        self.assertEqual([c["title"] for c in sections["candidates"]], [])

    def test_undismissed_candidate_shown_on_today(self):
        lot = _make_lot("dsm-today-2", timezone.now() + timedelta(hours=3))
        _make_evaluation(lot, is_candidate=True)

        sections = selling.today_sections(self.user)

        self.assertEqual([c["title"] for c in sections["candidates"]], [lot.title])

    def test_reopened_candidate_shown_on_today(self):
        lot = _make_lot("dsm-today-3", timezone.now() + timedelta(hours=3))
        _make_evaluation(lot, is_candidate=True, headroom="50.00")
        _dismiss(lot, headroom_at_dismissal="20.00")

        with override_settings(RESELLING_SCANNER={**settings.RESELLING_SCANNER, "DISMISS_REOPEN_HEADROOM": 25}):
            sections = selling.today_sections(self.user)

        self.assertEqual([c["title"] for c in sections["candidates"]], [lot.title])


class AlertDismissalTests(TestCase):
    """scanner/alerts.run_alerts() - candidate/lead/zero_bid/relisted (and
    check_by_hand, if enabled) all funnel through the same filter."""

    def _call_run_alerts(self, now, **cfg_overrides):
        base = {"enabled": True, "window_minutes": 180, "kinds": ["candidate", "lead", "zero_bid"],
                "min_headroom": -1000.0, "candidate_confidence": ["high", "medium", "low", ""], "max_per_run": 10,
                # These tests predate the watchlist and exercise run_alerts()'s
                # per-lot logic directly, so opt out of the real "watchlist_only"
                # default - see test_alerts.ModeTests for that default itself.
                "mode": "all"}
        cfg = {**settings.RESELLING_SCANNER, "ALERTS": {**base, **cfg_overrides}}
        with override_settings(RESELLING_SCANNER=cfg):
            with mock.patch(TELEGRAM_POST) as mock_post, mock.patch.dict(os.environ, CREDS):
                resp = mock.Mock()
                resp.raise_for_status.return_value = None
                mock_post.return_value = resp
                sent = run_alerts(now=now)
        return sent, mock_post

    def test_dismissed_candidate_not_alerted(self):
        now = timezone.now()
        lot = _make_lot("dsm-alert-1", now + timedelta(minutes=30))
        _make_evaluation(lot, is_candidate=True, headroom="20.00")
        _dismiss(lot, headroom_at_dismissal="20.00")

        sent, mock_post = self._call_run_alerts(now)

        self.assertEqual(sent, [])
        mock_post.assert_not_called()

    def test_dismissed_lead_not_alerted(self):
        now = timezone.now()
        lot = _make_lot("dsm-alert-2", now + timedelta(minutes=30))
        _make_lead(lot)
        _dismiss(lot, headroom_at_dismissal="0")

        sent, _mock_post = self._call_run_alerts(now)

        self.assertEqual(sent, [])

    def test_dismissed_zero_bid_not_alerted(self):
        now = timezone.now()
        lot = _make_lot("dsm-alert-3", now + timedelta(minutes=30), bid_count=0)
        _make_evaluation(lot, is_candidate=False, max_bid="20.00", headroom="10.00", confidence="high")
        _dismiss(lot, headroom_at_dismissal="10.00")

        sent, _mock_post = self._call_run_alerts(now)

        self.assertEqual(sent, [])

    def test_undismissed_candidate_still_alerted(self):
        now = timezone.now()
        lot = _make_lot("dsm-alert-4", now + timedelta(minutes=30))
        _make_evaluation(lot, is_candidate=True)

        sent, mock_post = self._call_run_alerts(now)

        self.assertEqual(sent, [(lot.pk, "candidate")])
        mock_post.assert_called_once()

    def test_reopened_candidate_alerted_again(self):
        now = timezone.now()
        lot = _make_lot("dsm-alert-5", now + timedelta(minutes=30))
        _make_evaluation(lot, is_candidate=True, headroom="50.00")
        _dismiss(lot, headroom_at_dismissal="20.00")

        with override_settings(RESELLING_SCANNER={**settings.RESELLING_SCANNER, "DISMISS_REOPEN_HEADROOM": 25}):
            sent, mock_post = self._call_run_alerts(now)

        self.assertEqual(sent, [(lot.pk, "candidate")])
        mock_post.assert_called_once()


class DismissViewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="tester", password="pw-not-real-12345")
        self.client.login(username="tester", password="pw-not-real-12345")

    def test_dismiss_creates_record_with_current_headroom(self):
        lot = _make_lot("view-dsm-1", timezone.now() + timedelta(hours=3))
        _make_evaluation(lot, headroom="20.00")

        response = self.client.post(reverse("albright_reselling_app:dismiss_lot", args=[lot.pk]))

        self.assertEqual(response.status_code, 200)
        dismissal = LotDismissal.objects.get(lot=lot)
        self.assertEqual(dismissal.headroom_at_dismissal, Decimal("20.00"))
        self.assertEqual(dismissal.reason, "")

    def test_dismiss_with_valid_reason(self):
        lot = _make_lot("view-dsm-2", timezone.now() + timedelta(hours=3))
        _make_evaluation(lot, headroom="20.00")

        self.client.post(reverse("albright_reselling_app:dismiss_lot", args=[lot.pk]), {"reason": "too_far"})

        self.assertEqual(LotDismissal.objects.get(lot=lot).reason, "too_far")

    def test_dismiss_with_invalid_reason_falls_back_to_blank(self):
        lot = _make_lot("view-dsm-3", timezone.now() + timedelta(hours=3))
        _make_evaluation(lot, headroom="20.00")

        self.client.post(reverse("albright_reselling_app:dismiss_lot", args=[lot.pk]), {"reason": "bogus"})

        self.assertEqual(LotDismissal.objects.get(lot=lot).reason, "")

    def test_dismiss_twice_updates_rather_than_duplicating(self):
        lot = _make_lot("view-dsm-4", timezone.now() + timedelta(hours=3))
        _make_evaluation(lot, headroom="20.00")

        self.client.post(reverse("albright_reselling_app:dismiss_lot", args=[lot.pk]), {"reason": "too_far"})
        self.client.post(reverse("albright_reselling_app:dismiss_lot", args=[lot.pk]), {"reason": "reviewed"})

        self.assertEqual(LotDismissal.objects.filter(lot=lot).count(), 1)
        self.assertEqual(LotDismissal.objects.get(lot=lot).reason, "reviewed")

    def test_undo_deletes_the_record(self):
        lot = _make_lot("view-dsm-5", timezone.now() + timedelta(hours=3))
        _dismiss(lot)

        response = self.client.post(reverse("albright_reselling_app:undo_dismiss", args=[lot.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertFalse(LotDismissal.objects.filter(lot=lot).exists())

    def test_undo_when_not_dismissed_is_a_harmless_no_op(self):
        lot = _make_lot("view-dsm-6", timezone.now() + timedelta(hours=3))

        response = self.client.post(reverse("albright_reselling_app:undo_dismiss", args=[lot.pk]))

        self.assertEqual(response.status_code, 200)

    def test_dismiss_requires_login(self):
        self.client.logout()
        lot = _make_lot("view-dsm-7", timezone.now() + timedelta(hours=3))

        response = self.client.post(reverse("albright_reselling_app:dismiss_lot", args=[lot.pk]))

        self.assertEqual(response.status_code, 302)

    def test_dismiss_requires_post(self):
        lot = _make_lot("view-dsm-8", timezone.now() + timedelta(hours=3))

        response = self.client.get(reverse("albright_reselling_app:dismiss_lot", args=[lot.pk]))

        self.assertEqual(response.status_code, 405)

    def test_dismiss_missing_lot_404s(self):
        response = self.client.post(reverse("albright_reselling_app:dismiss_lot", args=[999999]))

        self.assertEqual(response.status_code, 404)


class DashboardTemplateDismissalTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="tester", password="pw-not-real-12345")
        self.client.login(username="tester", password="pw-not-real-12345")

    def test_dismiss_button_present_on_candidates_check_by_hand_and_leads(self):
        soon_lot = _make_lot("tmpl-dsm-1", timezone.now() + timedelta(hours=3))
        _make_evaluation(soon_lot, is_candidate=True)
        cbh_lot = _make_lot("tmpl-dsm-2", timezone.now() + timedelta(hours=3))
        _make_evaluation(cbh_lot, is_candidate=False, confidence="low", max_bid="20.00", headroom="5.00")
        lead_lot = _make_lot("tmpl-dsm-3", timezone.now() + timedelta(hours=3))
        _make_lead(lead_lot)

        response = self.client.get(reverse("albright_reselling_app:dashboard"))
        content = response.content.decode()

        self.assertEqual(content.count("js-dismiss-btn"), 3)
        self.assertEqual(content.count("js-dismiss-reason"), 3)

    def test_dismiss_button_absent_from_closest_calls_and_results(self):
        closest_lot = _make_lot("tmpl-dsm-4", timezone.now() + timedelta(hours=3))
        _make_evaluation(closest_lot, is_candidate=False, max_bid="20.00", headroom="-5.00", confidence="high")

        response = self.client.get(reverse("albright_reselling_app:dashboard"))
        content = response.content.decode()

        watch_section = content.split('id="watch"')[1].split('id="performance"')[0]
        self.assertIn(closest_lot.title, watch_section)
        self.assertNotIn("js-dismiss-btn", watch_section)

    def test_dismissed_lot_hidden_from_main_table_shown_in_dismissed_panel(self):
        lot = _make_lot("tmpl-dsm-5", timezone.now() + timedelta(hours=3))
        _make_evaluation(lot, is_candidate=True, headroom="20.00")
        _dismiss(lot, headroom_at_dismissal="20.00", reason="too_far")

        response = self.client.get(reverse("albright_reselling_app:dashboard"))
        content = response.content.decode()

        before_panel, _sep, after_panel = content.partition("Dismissed Lots")
        self.assertNotIn(lot.title, before_panel)
        self.assertIn(lot.title, after_panel)
        self.assertIn('id="dismissed-count">1<', content)

    def test_reopened_badge_shown(self):
        lot = _make_lot("tmpl-dsm-6", timezone.now() + timedelta(hours=3))
        _make_evaluation(lot, is_candidate=True, headroom="50.00")
        _dismiss(lot, headroom_at_dismissal="20.00")

        with override_settings(RESELLING_SCANNER={**settings.RESELLING_SCANNER, "DISMISS_REOPEN_HEADROOM": 25}):
            response = self.client.get(reverse("albright_reselling_app:dashboard"))

        self.assertContains(response, "Back: headroom up $30.00")

    def test_show_dismissed_toggle_count_matches_dismissed_only(self):
        active_lot = _make_lot("tmpl-dsm-7", timezone.now() + timedelta(hours=3))
        _make_evaluation(active_lot, is_candidate=True)
        dismissed_lot = _make_lot("tmpl-dsm-8", timezone.now() + timedelta(hours=3))
        _make_evaluation(dismissed_lot, is_candidate=True, headroom="20.00")
        _dismiss(dismissed_lot, headroom_at_dismissal="20.00")

        response = self.client.get(reverse("albright_reselling_app:dashboard"))

        self.assertContains(response, 'id="tab-badge-act-now">1<')
        self.assertContains(response, 'id="dismissed-count">1<')
