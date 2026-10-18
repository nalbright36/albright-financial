"""The Watchlist page's Archive tab: every archived BidWatch (soft-removed,
or closed while still watched - see scanner/bid_watch.py) as table rows,
plus how its final prices landed vs. the max snapshotted at archive time
(compare_max) - split by whose max that was (mine vs. the scanner's), by
source, by category, and by archive reason. Read-only, no network; hard-
deleted watches don't exist any more, so they're never counted.
"""
import statistics

from ..scanner_models import BidWatch
from .dashboard import _source_display

RESOLVED_OUTCOMES = ("below", "at", "above")
OUTCOME_DISPLAY = {"below": "Below", "at": "At", "above": "Above", "unknown": "Unknown", "pending": "Pending"}


def _pct(part, whole):
    return round(part / whole * 100, 1) if whole else None


def summarize(watches):
    """Over the watches with a real outcome (below/at/above - "unknown"
    and still-pending ones are counted separately, never in the %s):
    how many, % closed at or below the max, % below / at / above, and
    the median of (final price - max)."""
    resolved = [w for w in watches if w.outcome in RESOLVED_OUTCOMES]
    counts = {outcome: sum(1 for w in resolved if w.outcome == outcome) for outcome in RESOLVED_OUTCOMES}
    diffs = [w.final_price - w.compare_max for w in resolved]
    return {
        "resolved": len(resolved),
        "pct_at_or_below": _pct(counts["below"] + counts["at"], len(resolved)),
        "pct_below": _pct(counts["below"], len(resolved)),
        "pct_at": _pct(counts["at"], len(resolved)),
        "pct_above": _pct(counts["above"], len(resolved)),
        "median_diff": statistics.median(diffs) if diffs else None,
        "unknown": sum(1 for w in watches if w.outcome == "unknown"),
        "pending": sum(1 for w in watches if w.outcome is None),
    }


def _grouped(watches, key_func, label_func=lambda key: key):
    groups = {}
    for watch in watches:
        groups.setdefault(key_func(watch), []).append(watch)
    return [{"key": key, "label": label_func(key), **summarize(group)} for key, group in sorted(groups.items())]


def _category(watch):
    evaluation = getattr(watch.lot, "evaluation", None)
    return evaluation.category if evaluation else "unknown"


def archived_watches():
    return list(
        BidWatch.objects.archived().select_related("lot", "lot__evaluation").order_by("-archived_at")
    )


def archive_summary(watches):
    """The Archive tab's top summary (All / My max / Scanner max) and its
    breakdowns by source, category, and archive reason."""
    reasons = dict(BidWatch.ARCHIVE_REASON_CHOICES)
    return {
        "all": summarize(watches),
        "manual": summarize([w for w in watches if w.compare_source == "manual"]),
        "scanner": summarize([w for w in watches if w.compare_source == "scanner"]),
        "by_source": _grouped(watches, lambda w: w.lot.source, _source_display),
        "by_category": _grouped(watches, _category, lambda key: key.title()),
        "by_reason": _grouped(watches, lambda w: w.archive_reason, lambda key: reasons.get(key, key or "Unknown")),
    }


def archive_row(watch):
    lot = watch.lot
    outcome = watch.outcome or "pending"
    difference = (
        watch.final_price - watch.compare_max
        if watch.final_price is not None and watch.compare_max is not None else None
    )
    return {
        "watch": watch,
        "lot_id": lot.pk,
        "title": lot.title,
        "lot_url": lot.url,
        "image_url": lot.image_url,
        "source": lot.source,
        "source_display": _source_display(lot.source),
        "category": _category(watch),
        "archived_at": watch.archived_at,
        "archive_reason": watch.archive_reason,
        "archive_reason_display": watch.get_archive_reason_display(),
        "compare_max": watch.compare_max,
        "compare_source": watch.compare_source,
        "final_price": watch.final_price,
        "difference": difference,
        "outcome": outcome,
        "outcome_display": OUTCOME_DISPLAY.get(outcome, outcome),
        "can_log_win": outcome in ("below", "at"),
    }
