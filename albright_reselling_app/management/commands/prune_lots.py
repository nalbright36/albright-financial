"""Usage:
    python manage.py prune_lots                 # delete what's eligible
    python manage.py prune_lots --dry-run         # report only, delete nothing

Data retention for the scanner tables - HiBid alone can add thousands of
rows per day, so SourcedLot (and its LotEvaluation) needs a prune pass, not
unbounded growth. Two deletion rules, run independently:

  1. category "none" (never matched a tracked category) and not re-seen in
     the last 30 days - dead weight from day one, no reason to keep it once
     stale.
  2. closed more than 120 days ago with no max bid ever computed and never
     flagged as a lead - closed out, uninteresting, and old enough that
     it's not informing any recent decision.

Never deletes a lot that has an AIReview, a BidWatch, a linked LedgerEntry,
or that closed with a real max bid on record (calibration data for the
max-bid math - see track_closed's under/over-max summary) - that
protection is checked independently of which rule above would otherwise
catch the lot, since any of those four is reason enough to keep it.

Deleting a SourcedLot cascades to its LotEvaluation (OneToOneField,
on_delete=CASCADE) and any AlertSent rows automatically - no separate
cleanup needed for those.
"""
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db import connection
from django.db.models import Count, Q
from django.utils import timezone

from albright_reselling_app.scanner_models import AIReview, AlertSent, BidWatch, CriticalAlertSent, \
    LotEvaluation, ScanRun, SourcedLot, SpotPrice

NONE_CATEGORY_STALE_AFTER = timedelta(days=30)
CLOSED_STALE_AFTER = timedelta(days=120)

ROW_COUNT_MODELS = [SourcedLot, LotEvaluation, AIReview, BidWatch, ScanRun, AlertSent, CriticalAlertSent, SpotPrice]


def _approx_row_count(model):
    """Postgres' own planner estimate (pg_class.reltuples) instead of a
    real COUNT(*) - exact isn't the point here, and a full scan of a
    SourcedLot table with hundreds of thousands of HiBid rows is exactly
    the kind of cost this command shouldn't add every time someone runs
    it. Falls back to a real count on any other backend (e.g. SQLite in
    tests) or if the catalog lookup comes back empty."""
    if connection.vendor == "postgresql":
        with connection.cursor() as cursor:
            cursor.execute("SELECT reltuples::bigint FROM pg_class WHERE relname = %s", [model._meta.db_table])
            row = cursor.fetchone()
            if row and row[0] is not None and row[0] >= 0:
                return int(row[0])
    return model.objects.count()


def _protected_ids():
    """Lot ids that must never be pruned, regardless of which rule below
    would otherwise catch them."""
    return set(
        SourcedLot.objects.filter(
            Q(ai_reviews__isnull=False) | Q(bid_watch__isnull=False) | Q(ledger_entries__isnull=False)
            | Q(is_closed=True, evaluation__max_bid__gt=0)
        ).values_list("pk", flat=True)
    )


def _stale_none_category(now):
    cutoff = now - NONE_CATEGORY_STALE_AFTER
    return SourcedLot.objects.filter(evaluation__category="none", last_seen__lt=cutoff)


def _stale_closed_no_signal(now):
    cutoff = now - CLOSED_STALE_AFTER
    return (
        SourcedLot.objects.filter(is_closed=True, end_time__lt=cutoff)
        .filter(Q(evaluation__isnull=True) | Q(evaluation__max_bid=0, evaluation__is_lead=False))
    )


class Command(BaseCommand):
    help = "Delete stale scanner lots (and their evaluations) past retention, protecting anything with real history."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Report what would be deleted; delete nothing.")

    def handle(self, *args, **opts):
        now = timezone.now()
        protected_ids = _protected_ids()

        none_category_ids = set(_stale_none_category(now).exclude(pk__in=protected_ids).values_list("pk", flat=True))
        stale_closed_ids = set(_stale_closed_no_signal(now).exclude(pk__in=protected_ids).values_list("pk", flat=True))
        prunable_ids = none_category_ids | stale_closed_ids

        verb = "Would delete" if opts["dry_run"] else "Deleting"
        if not prunable_ids:
            self.stdout.write("Nothing to prune.")
        else:
            self.stdout.write(
                f"{verb} {len(prunable_ids)} lot(s): {len(none_category_ids)} stale none-category, "
                f"{len(stale_closed_ids)} stale closed with no signal "
                f"({len(none_category_ids & stale_closed_ids)} matched both rules)."
            )
            by_source = (
                SourcedLot.objects.filter(pk__in=prunable_ids)
                .values("source").annotate(n=Count("id")).order_by("source")
            )
            for row in by_source:
                self.stdout.write(f"  {row['source']}: {row['n']}")

            if not opts["dry_run"]:
                SourcedLot.objects.filter(pk__in=prunable_ids).delete()

        self.stdout.write("")
        self.stdout.write("Approximate row counts:")
        for model in ROW_COUNT_MODELS:
            self.stdout.write(f"  {model._meta.db_table}: {_approx_row_count(model):,}")
