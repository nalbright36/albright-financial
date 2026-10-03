"""Cross-item ledger metrics: the Scorecard view's groupings/aging, and the
dashboard's small "Realized" block. Per-item math (profit, ROI, days to
sell, prediction error...) lives on LedgerEntry itself (models.py) - this
module only aggregates across items, so it stays testable without going
through a request/response cycle, the same pattern scanner/dashboard.py
and scanner/review_history.py already use.
"""
import statistics
from datetime import timedelta
from decimal import Decimal

from django.utils import timezone

from .models import LedgerEntry
from .scanner_models import BidWatch

UNSOLD_STATUSES = ("holding", "partially_sold")
MARKDOWN_STEPS = (14, 30, 60)  # 90+ is handled separately below - it never "expires" back out


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


def _is_sold_this_month(entry, month_start):
    """Was this entry's outcome dated within the current calendar month -
    by LedgerSale.sale_date for itemized sales, the legacy sold_date for a
    plain sold_for entry, or updated_at's date for a write-off (which has
    no sale date of its own). Factored out so the dashboard's Realized
    block and its top-of-page tiles can't drift apart on the definition."""
    if not entry.is_realized:
        return False
    if entry.has_itemized_sales:
        return any(s.sale_date >= month_start for s in entry.sales.all())
    if entry.sold_for is not None:
        return bool(entry.sold_date and entry.sold_date >= month_start)
    return entry.status == "written_off" and entry.updated_at.date() >= month_start


def _needs_markdown_action(entry):
    """Same step thresholds as LedgerEntry.aging_suggestion (14/30/60/90
    days). There's no "action taken" flag on LedgerEntry to check against,
    so this is the documented fallback: flagged for the 7 days after
    crossing a step, or continuously once past 90 (nothing "undoes" that
    one - you're always overdue until it sells or gets written off)."""
    days = entry.days_held
    if days >= 90:
        return True
    return any(step <= days < step + 7 for step in MARKDOWN_STEPS)


def realized_summary(owner, now=None):
    """The dashboard's "Realized" block: a few headline numbers, not the
    full Scorecard breakdown."""
    now = now or timezone.now()
    month_start = timezone.localdate(now).replace(day=1)

    entries = list(LedgerEntry.objects.filter(owner=owner).prefetch_related("sales"))
    bought_this_month = sum(
        1 for e in entries if (e.purchase_date or timezone.localtime(e.created_at).date()) >= month_start
    )

    sold_this_month = [e for e in entries if _is_sold_this_month(e, month_start)]
    sold_this_month_profit = sum((e.profit for e in sold_this_month), Decimal("0"))

    unsold = [e for e in entries if e.status in UNSOLD_STATUSES]

    return {
        "bought_this_month": bought_this_month,
        "sold_this_month": len(sold_this_month),
        "realized_profit_this_month": sold_this_month_profit,
        "unsold_count": len(unsold),
        "unsold_value_at_cost": sum((e.buy_side_cost for e in unsold), Decimal("0")),
    }


def dashboard_tiles(owner, now=None):
    """The reseller dashboard's top-of-page summary row: 5 ledger-focused
    tiles (profit/sales this month, inventory, needs-action, my bids),
    replacing the old pre-scanner-integration research-pipeline stats.
    Reuses the same entry properties and the same "sold this month"/aging
    rules the Scorecard and the Realized block already use, plus BidWatch
    for the two bid-tracking tiles, so none of these numbers can silently
    drift from what those other views already show."""
    now = now or timezone.now()
    month_start = timezone.localdate(now).replace(day=1)
    week_ago = now - timedelta(days=7)

    entries = list(LedgerEntry.objects.filter(owner=owner).prefetch_related("sales"))
    realized = [e for e in entries if e.is_realized]
    unsold = [e for e in entries if e.status in UNSOLD_STATUSES]
    sold_this_month = [e for e in entries if _is_sold_this_month(e, month_start)]

    revenue_this_month = sum(
        (e.total_realized for e in sold_this_month if e.total_realized is not None), Decimal("0"),
    )
    rois_this_month = [e.roi_pct for e in sold_this_month if e.roi_pct is not None]

    markdown_count = sum(1 for e in unsold if _needs_markdown_action(e))
    logged_lot_ids = {e.scanner_lot_id for e in entries if e.scanner_lot_id is not None}
    wins_to_log = BidWatch.objects.filter(status="likely_won").exclude(lot_id__in=logged_lot_ids).count()

    return {
        "profit_this_month": sum((e.profit for e in sold_this_month), Decimal("0")),
        "all_time_profit": sum((e.profit for e in realized), Decimal("0")),
        "sales_this_month_count": len(sold_this_month),
        "revenue_this_month": revenue_this_month,
        "avg_roi_this_month": _mean(rois_this_month),
        "inventory_count": len(unsold),
        "inventory_cost": sum((e.buy_side_cost for e in unsold), Decimal("0")),
        "markdown_count": markdown_count,
        "wins_to_log": wins_to_log,
        "needs_action_count": markdown_count + wins_to_log,
        "watching_count": BidWatch.objects.filter(status="watching").count(),
        "likely_won_this_week": BidWatch.objects.filter(status="likely_won", resolved_at__gte=week_ago).count(),
    }
