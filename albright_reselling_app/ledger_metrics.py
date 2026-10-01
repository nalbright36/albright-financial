"""Cross-item ledger metrics: the Scorecard view's groupings/aging, and the
dashboard's small "Realized" block. Per-item math (profit, ROI, days to
sell, prediction error...) lives on LedgerEntry itself (models.py) - this
module only aggregates across items, so it stays testable without going
through a request/response cycle, the same pattern scanner/dashboard.py
and scanner/review_history.py already use.
"""
import statistics
from decimal import Decimal

from django.utils import timezone

from .models import LedgerEntry

UNSOLD_STATUSES = ("holding", "partially_sold")


def _mean(values):
    return statistics.mean(values) if values else None


def _median(values):
    return statistics.median(values) if values else None


def _group_summary(entries, key_func):
    """One row per distinct key_func(entry): count, total profit, average
    ROI, average days to sell, median prediction error, and how many of
    the group's entries still carry an unsplit legacy fees/shipping
    amount. `entries` should already be filtered to realized (sold or
    written-off) items - grouping unsold items here wouldn't mean much,
    since most of these figures are undefined for an item with no outcome
    yet."""
    groups = {}
    for entry in entries:
        groups.setdefault(key_func(entry), []).append(entry)

    rows = []
    for label, group in sorted(groups.items()):
        rois = [e.roi_pct for e in group if e.roi_pct is not None]
        days = [e.days_to_sell for e in group if e.days_to_sell is not None]
        errors = [e.prediction_error_pct for e in group if e.prediction_error_pct is not None]
        rows.append({
            "label": label,
            "count": len(group),
            "total_profit": sum((e.profit for e in group), Decimal("0")),
            "avg_roi_pct": _mean(rois),
            "avg_days_to_sell": _mean(days),
            "median_prediction_error_pct": _median(errors),
            "legacy_unsplit_count": sum(1 for e in group if e.has_legacy_amounts),
        })
    return rows


def _aging_rows(unsold_entries):
    rows = [
        {"entry": e, "days_held": e.days_held, "suggested_action": e.aging_suggestion,
         "value_at_cost": e.buy_side_cost}
        for e in unsold_entries
    ]
    rows.sort(key=lambda r: -r["days_held"])
    return rows


def _totals(realized_entries):
    rois = [e.roi_pct for e in realized_entries if e.roi_pct is not None]
    return {
        "count": len(realized_entries),
        "total_profit": sum((e.profit for e in realized_entries), Decimal("0")),
        "avg_roi_pct": _mean(rois),
    }


def scorecard_context(owner):
    entries = list(
        LedgerEntry.objects.filter(owner=owner)
        .select_related("scanner_lot", "ai_review").prefetch_related("sales")
    )
    realized = [e for e in entries if e.is_realized]
    unsold = [e for e in entries if e.status in UNSOLD_STATUSES]

    return {
        "entries": entries,
        "totals": _totals(realized),
        "by_category": _group_summary(realized, lambda e: e.category or "Unassigned"),
        "by_source": _group_summary(realized, lambda e: e.source or "Unassigned"),
        "by_prediction_source": _group_summary(
            realized,
            lambda e: e.get_predicted_sale_source_display() if e.predicted_sale_source else "No Prediction",
        ),
        "aging": _aging_rows(unsold),
        "conflicting_entries": [e for e in entries if e.has_conflicting_sale_data],
        "legacy_unsplit_entries": [e for e in entries if e.has_legacy_amounts],
        "unsold_count": len(unsold),
        "unsold_value_at_cost": sum((e.buy_side_cost for e in unsold), Decimal("0")),
    }


def realized_summary(owner, now=None):
    """The dashboard's "Realized" block: a few headline numbers, not the
    full Scorecard breakdown."""
    now = now or timezone.now()
    month_start = timezone.localdate(now).replace(day=1)

    entries = list(LedgerEntry.objects.filter(owner=owner).prefetch_related("sales"))
    bought_this_month = sum(
        1 for e in entries if (e.purchase_date or e.created_at.date()) >= month_start
    )

    sold_this_month_count = 0
    sold_this_month_profit = Decimal("0")
    for e in entries:
        if not e.is_realized:
            continue
        sold_this_month = (
            any(s.sale_date >= month_start for s in e.sales.all()) if e.has_itemized_sales
            else (e.sold_date and e.sold_date >= month_start) if e.sold_for is not None
            else (e.status == "written_off" and e.updated_at.date() >= month_start)
        )
        if sold_this_month:
            sold_this_month_count += 1
            sold_this_month_profit += e.profit

    unsold = [e for e in entries if e.status in UNSOLD_STATUSES]

    return {
        "bought_this_month": bought_this_month,
        "sold_this_month": sold_this_month_count,
        "realized_profit_this_month": sold_this_month_profit,
        "unsold_count": len(unsold),
        "unsold_value_at_cost": sum((e.buy_side_cost for e in unsold), Decimal("0")),
    }
