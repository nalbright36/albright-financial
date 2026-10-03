"""Tests for the prune_lots management command: deletes stale scanner lots
past retention, while never touching anything with real history (an
AIReview, a BidWatch, a linked LedgerEntry, or a closed lot that had a real
max bid). No HTTP involved - pure ORM fixtures."""
from datetime import timedelta
from decimal import Decimal
from io import StringIO

from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from albright_reselling_app.models import LedgerEntry
from albright_reselling_app.scanner_models import AIReview, BidWatch, LotEvaluation, SourcedLot


def _make_lot(external_id, source="shopgoodwill", last_seen_days_ago=0, end_time_days_ago=None, is_closed=False):
    lot = SourcedLot.objects.create(
        source=source, external_id=external_id, url=f"https://example.com/{external_id}",
        title=f"Lot {external_id}", current_price=Decimal("10.00"), is_closed=is_closed,
        end_time=timezone.now() - timedelta(days=end_time_days_ago) if end_time_days_ago is not None else None,
    )
    if last_seen_days_ago:
        SourcedLot.objects.filter(pk=lot.pk).update(last_seen=timezone.now() - timedelta(days=last_seen_days_ago))
        lot.refresh_from_db()
    return lot


def _evaluate(lot, category="coins", max_bid="0", is_lead=False):
    return LotEvaluation.objects.create(lot=lot, category=category, max_bid=Decimal(max_bid), is_lead=is_lead)


def _run(dry_run=False):
    out = StringIO()
    args = ["--dry-run"] if dry_run else []
    call_command("prune_lots", *args, stdout=out)
    return out.getvalue()


class StaleNoneCategoryTests(TestCase):
    def test_stale_none_category_deleted(self):
        lot = _make_lot("none-stale", last_seen_days_ago=31)
        _evaluate(lot, category="none")

        _run()

        self.assertFalse(SourcedLot.objects.filter(pk=lot.pk).exists())

    def test_recent_none_category_kept(self):
        lot = _make_lot("none-recent", last_seen_days_ago=5)
        _evaluate(lot, category="none")

        _run()

        self.assertTrue(SourcedLot.objects.filter(pk=lot.pk).exists())

    def test_stale_but_real_category_kept(self):
        lot = _make_lot("coins-stale", last_seen_days_ago=31)
        _evaluate(lot, category="coins")

        _run()

        self.assertTrue(SourcedLot.objects.filter(pk=lot.pk).exists())


class StaleClosedNoSignalTests(TestCase):
    def test_old_closed_no_max_bid_no_lead_deleted(self):
        lot = _make_lot("closed-stale", end_time_days_ago=121, is_closed=True)
        _evaluate(lot, category="coins", max_bid="0", is_lead=False)

        _run()

        self.assertFalse(SourcedLot.objects.filter(pk=lot.pk).exists())

    def test_old_closed_no_evaluation_at_all_deleted(self):
        lot = _make_lot("closed-no-eval", end_time_days_ago=121, is_closed=True)

        _run()

        self.assertFalse(SourcedLot.objects.filter(pk=lot.pk).exists())

    def test_recent_closed_no_signal_kept(self):
        lot = _make_lot("closed-recent", end_time_days_ago=5, is_closed=True)
        _evaluate(lot, category="coins", max_bid="0", is_lead=False)

        _run()

        self.assertTrue(SourcedLot.objects.filter(pk=lot.pk).exists())

    def test_old_closed_with_lead_kept(self):
        lot = _make_lot("closed-lead", end_time_days_ago=121, is_closed=True)
        _evaluate(lot, category="games", max_bid="0", is_lead=True)

        _run()

        self.assertTrue(SourcedLot.objects.filter(pk=lot.pk).exists())

    def test_old_closed_with_max_bid_kept(self):
        lot = _make_lot("closed-maxbid", end_time_days_ago=121, is_closed=True)
        _evaluate(lot, category="coins", max_bid="25.00", is_lead=False)

        _run()

        self.assertTrue(SourcedLot.objects.filter(pk=lot.pk).exists())

    def test_still_open_lot_never_deleted_by_this_rule(self):
        lot = _make_lot("open-old", end_time_days_ago=200, is_closed=False)
        _evaluate(lot, category="coins", max_bid="0", is_lead=False)

        _run()

        self.assertTrue(SourcedLot.objects.filter(pk=lot.pk).exists())


class ProtectionRuleTests(TestCase):
    """Every protection applies regardless of which deletion rule would
    otherwise have caught the lot - exercised here via the stale
    none-category rule, the most permissive (easiest to trigger) one."""

    def test_lot_with_ai_review_never_deleted(self):
        lot = _make_lot("protected-ai", last_seen_days_ago=31)
        _evaluate(lot, category="none")
        AIReview.objects.create(lot=lot, status="done", cost_usd=Decimal("0.05"))

        _run()

        self.assertTrue(SourcedLot.objects.filter(pk=lot.pk).exists())

    def test_lot_with_bid_watch_never_deleted(self):
        lot = _make_lot("protected-watch", last_seen_days_ago=31)
        _evaluate(lot, category="none")
        BidWatch.objects.create(lot=lot, my_max_bid=Decimal("20.00"))

        _run()

        self.assertTrue(SourcedLot.objects.filter(pk=lot.pk).exists())

    def test_lot_with_ledger_entry_never_deleted(self):
        lot = _make_lot("protected-ledger", last_seen_days_ago=31)
        _evaluate(lot, category="none")
        user = User.objects.create_user(username="tester", password="pw-not-real-12345")
        LedgerEntry.objects.create(owner=user, item="Won It", cost=Decimal("10.00"), scanner_lot=lot)

        _run()

        self.assertTrue(SourcedLot.objects.filter(pk=lot.pk).exists())

    def test_lot_with_evaluation_cascade_deleted_with_lot(self):
        lot = _make_lot("cascade-eval", last_seen_days_ago=31)
        ev = _evaluate(lot, category="none")

        _run()

        self.assertFalse(LotEvaluation.objects.filter(pk=ev.pk).exists())


class DryRunTests(TestCase):
    def test_dry_run_deletes_nothing(self):
        lot = _make_lot("dry-run-none", last_seen_days_ago=31)
        _evaluate(lot, category="none")

        output = _run(dry_run=True)

        self.assertTrue(SourcedLot.objects.filter(pk=lot.pk).exists())
        self.assertIn("Would delete", output)

    def test_real_run_says_deleting(self):
        lot = _make_lot("real-run-none", last_seen_days_ago=31)
        _evaluate(lot, category="none")

        output = _run(dry_run=False)

        self.assertIn("Deleting", output)
        self.assertFalse(SourcedLot.objects.filter(pk=lot.pk).exists())


class OutputTests(TestCase):
    def test_counts_printed_per_source(self):
        shopgoodwill_lot = _make_lot("count-sgw", source="shopgoodwill", last_seen_days_ago=31)
        _evaluate(shopgoodwill_lot, category="none")
        hibid_lot = _make_lot("count-hibid", source="hibid", last_seen_days_ago=31)
        _evaluate(hibid_lot, category="none")

        output = _run()

        self.assertIn("shopgoodwill: 1", output)
        self.assertIn("hibid: 1", output)

    def test_nothing_to_prune_message(self):
        output = _run()

        self.assertIn("Nothing to prune.", output)

    def test_row_counts_printed(self):
        output = _run()

        self.assertIn("Approximate row counts:", output)
        self.assertIn("sourcedlot", output.lower())
