"""Usage:
    python manage.py export_features                        # all closed lots -> features_export.csv
    python manage.py export_features --since 2026-01-01      # only lots that ended on/after this date
    python manage.py export_features --out /tmp/features.csv # custom output path

Writes one CSV row per closed lot: core outcome columns (source, category,
title, final price vs. melt/expected/max bid, bid count at close) plus
every key ever seen in any lot's `features` JSON as its own column - the
features schema has grown over time (closing-time keys only exist once a
lot has closed), so the header is the union of every key actually present
in the rows being exported, not a fixed list.

This is the dataset for the offline "what listing characteristics predict
a cheap close" analysis - nothing in the app reads this file back.
"""
import csv
from datetime import datetime

from django.core.management.base import BaseCommand, CommandError

from albright_reselling_app.scanner_models import SourcedLot

CORE_FIELDS = [
    "source", "category", "title", "final_price", "melt_value", "expected_sale", "max_bid",
    "final_to_melt_ratio", "final_to_expected_ratio", "closed_under_max", "bid_count_at_close",
]


def _ratio(numerator, denominator):
    if numerator is None or not denominator:
        return None
    return round(float(numerator) / float(denominator), 4)


def _row(lot, feature_keys):
    evaluation = getattr(lot, "evaluation", None)
    melt_value = evaluation.melt_value if evaluation else None
    expected_sale = evaluation.expected_sale if evaluation else None
    max_bid = evaluation.max_bid if evaluation else None
    category = evaluation.category if evaluation else ""

    row = [
        lot.source, category, lot.title, lot.final_price, melt_value, expected_sale, max_bid,
        _ratio(lot.final_price, melt_value), _ratio(lot.final_price, expected_sale),
        bool(max_bid and max_bid > 0 and lot.final_price <= max_bid), lot.bid_count_at_close,
    ]
    features = lot.features or {}
    row += [features.get(key, "") for key in feature_keys]
    return row


class Command(BaseCommand):
    help = "Export a CSV of closed lots plus their listing features, for offline analysis."

    def add_arguments(self, parser):
        parser.add_argument("--since", default=None,
                             help="YYYY-MM-DD - only lots whose end_time is on/after this date.")
        parser.add_argument("--out", default="features_export.csv", help="Output CSV path.")

    def handle(self, *args, **opts):
        qs = SourcedLot.objects.filter(is_closed=True, final_price__isnull=False).select_related("evaluation")

        if opts["since"]:
            try:
                since = datetime.strptime(opts["since"], "%Y-%m-%d").date()
            except ValueError as exc:
                raise CommandError("--since must be YYYY-MM-DD") from exc
            qs = qs.filter(end_time__date__gte=since)

        lots = list(qs.order_by("end_time"))
        feature_keys = sorted({key for lot in lots for key in (lot.features or {})})
        header = CORE_FIELDS + feature_keys

        with open(opts["out"], "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(header)
            for lot in lots:
                writer.writerow(_row(lot, feature_keys))

        self.stdout.write(f"Wrote {len(lots)} closed lot(s) to {opts['out']}")
