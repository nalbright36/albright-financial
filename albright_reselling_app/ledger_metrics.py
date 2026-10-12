"""Cross-item ledger metrics: the Scorecard view's groupings/aging, and the
dashboard's small "Realized" block. Per-item math (profit, ROI, days to
sell, prediction error...) lives on LedgerEntry itself (models.py) - this
module only aggregates across items, so it stays testable without going
through a request/response cycle, the same pattern scanner/dashboard.py
and scanner/review_history.py already use.
"""
import statistics
from datetime import date, timedelta
from decimal import Decimal

from django.utils import timezone

from .models import LedgerEntry
from .scanner_models import BidWatch

UNSOLD_STATUSES = ("holding", "partially_sold")


def exclude_personal(queryset):
    """Filters out Personal Items entries from any LedgerEntry queryset -
    the one place this exclusion lives. Every reselling calculation
    (dashboard tiles, Scorecard, aging/markdown, calibration, Insights,
    the scanner's "I won this" flow) must go through this, so personal
    items can never leak into resale numbers. Takes/returns a queryset
    (not an owner) so it composes with whatever filtering the caller
    already has in place, including calibration's unowned, cross-user
    queries in scanner/insights.py."""
    return queryset.filter(ledger_type="resale")


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
    # Deferred import: selling.py sits on top of both this module and
    # scanner/dashboard.py (which itself imports ledger_metrics) - a
    # module-level import here would be circular.
    from .selling import aging_row
    rows = [aging_row(e) for e in unsold_entries]
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
        exclude_personal(LedgerEntry.objects.filter(owner=owner))
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
    """selling.markdown_status()'s "due" flag - timed from listed_at (or
    flagged as "not listed" past 7 days, for an item with no listed_at
    yet) - the one shared definition dashboard_tiles()'s markdown_count
    and the Ledger/Personal Items aging views all use."""
    from .selling import needs_markdown_action
    return needs_markdown_action(entry)


def realized_summary(owner, now=None):
    """The dashboard's "Realized" block: a few headline numbers, not the
    full Scorecard breakdown."""
    now = now or timezone.now()
    month_start = timezone.localdate(now).replace(day=1)

    entries = list(exclude_personal(LedgerEntry.objects.filter(owner=owner)).prefetch_related("sales"))
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


def net_cash_flow(owner, ledger_type="resale"):
    """Money in minus money out across every matching ledger entry for
    this owner, regardless of status. Unlike dashboard_tiles()'s P&L
    (which only counts sold-out/written-off outcomes, deliberately
    excluding unsold inventory's cost), this counts the full buy-side
    cost of inventory that's still sitting unsold too - that cash already
    went out the door, whether or not anything's sold yet to offset it.
    Revenue is LedgerSale.sale_price (itemized sales) or the legacy
    sold_for; costs are each entry's buy_side_cost (0 for a personal
    item, which already folds in any unsplit legacy fees/shipping for a
    resale one) plus its sell_side_cost (LedgerSale fees/shipping/
    packaging, or the legacy sell_fees/sell_shipping fields).

    ledger_type: "resale" (default - the Reselling tab's own tile),
    "personal", or None for both ledgers combined (the Overview tab)."""
    qs = LedgerEntry.objects.filter(owner=owner)
    if ledger_type is not None:
        qs = qs.filter(ledger_type=ledger_type)
    entries = list(qs.prefetch_related("sales"))
    revenue = sum((e.total_realized for e in entries if e.total_realized is not None), Decimal("0"))
    costs = sum((e.buy_side_cost + e.sell_side_cost for e in entries), Decimal("0"))
    return {"revenue": revenue, "costs": costs, "net": revenue - costs}


def ledger_summary_tiles(owner, now=None):
    """The Ledger page's own 3 top-of-page tiles (unsold inventory cost,
    all-time P&L, net cash flow) - the first two picked straight out of
    dashboard_tiles()'s figures rather than recomputed, so they can never
    drift from what the reseller dashboard's top row (and, through it,
    the Scorecard) already shows for the same owner. Always computed
    across every entry for this owner, regardless of any status filter
    the Ledger page's own table is currently applying."""
    tiles = dashboard_tiles(owner, now=now)
    cash_flow = net_cash_flow(owner)
    return {
        "inventory_cost": tiles["inventory_cost"],
        "inventory_count": tiles["inventory_count"],
        "all_time_profit": tiles["all_time_profit"],
        "profit_this_month": tiles["profit_this_month"],
        "net_cash_flow": cash_flow["net"],
        "revenue": cash_flow["revenue"],
        "costs": cash_flow["costs"],
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

    entries = list(exclude_personal(LedgerEntry.objects.filter(owner=owner)).prefetch_related("sales"))
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


# ---------------------------------------------------------------------------
# Personal Items tab
# ---------------------------------------------------------------------------

def personal_items_context(owner, now=None):
    """The Personal Items tab: its 3 top tiles (all-time + this-month
    revenue, net after selling fees/shipping/packaging, items still
    listed/holding) and its own aging rows - mirrors dashboard_tiles()'s
    this-month/aging rules, but scoped to ledger_type="personal" instead
    of excluding it. A personal entry's buy_side_cost is always 0 (those
    fields are hidden on its forms), so entry.profit already equals
    "revenue minus selling costs" with nothing extra to subtract."""
    now = now or timezone.now()
    month_start = timezone.localdate(now).replace(day=1)

    entries = list(LedgerEntry.objects.filter(owner=owner, ledger_type="personal").prefetch_related("sales"))
    unsold = [e for e in entries if e.status in UNSOLD_STATUSES]
    sold_this_month = [e for e in entries if _is_sold_this_month(e, month_start)]

    revenue_all_time = sum((e.total_realized for e in entries if e.total_realized is not None), Decimal("0"))
    revenue_this_month = sum(
        (e.total_realized for e in sold_this_month if e.total_realized is not None), Decimal("0"),
    )
    net_after_fees = sum((e.profit for e in entries if e.is_realized), Decimal("0"))

    return {
        "entries": entries,
        "tiles": {
            "revenue_all_time": revenue_all_time,
            "revenue_this_month": revenue_this_month,
            "net_after_fees": net_after_fees,
            "items_listed_count": len(unsold),
        },
        "aging": _aging_rows(unsold),
    }


# ---------------------------------------------------------------------------
# Overview tab
# ---------------------------------------------------------------------------

def _sold_in_month(entry, month_start, month_end):
    """Like _is_sold_this_month, but for an arbitrary (not just the
    current) month - needs an explicit upper bound too, since a sale from
    3 months ago would otherwise match "sold since" any earlier month's
    start as well."""
    if not entry.is_realized:
        return False
    if entry.has_itemized_sales:
        return any(month_start <= s.sale_date < month_end for s in entry.sales.all())
    if entry.sold_for is not None:
        return bool(entry.sold_date and month_start <= entry.sold_date < month_end)
    return entry.status == "written_off" and month_start <= entry.updated_at.date() < month_end


def _last_12_months(now):
    """(year, month) pairs for the 12 calendar months ending with the
    current one, oldest first."""
    months = []
    year, month = now.year, now.month
    for _ in range(12):
        months.append((year, month))
        month -= 1
        if month == 0:
            month, year = 12, year - 1
    return list(reversed(months))


def _month_bounds(year, month):
    start = date(year, month, 1)
    end = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    return start, end


def _ledger_totals_row(label, entries):
    revenue = sum((e.total_realized for e in entries if e.total_realized is not None), Decimal("0"))
    costs = sum((e.buy_side_cost + e.sell_side_cost for e in entries), Decimal("0"))
    return {
        "label": label,
        "revenue": revenue,
        "costs": costs,
        "net": revenue - costs,
        "items_sold": sum(1 for e in entries if e.total_realized is not None),
        "items_held": sum(1 for e in entries if e.status in UNSOLD_STATUSES),
    }


def overview_context(owner, now=None):
    """The Ledger page's Overview tab: top tiles (combined revenue/costs/
    net/items-sold across both ledgers), a per-ledger breakdown + total
    row, and a month-by-month breakdown (revenue/net per ledger and
    combined) for the last 12 months. "Net" per entry per month reuses
    entry.profit (the same figure dashboard_tiles()'s profit_this_month
    uses) - for a personal entry that's already exactly revenue minus
    selling costs, since its buy-side cost is always 0."""
    now = now or timezone.now()

    resale_entries = list(LedgerEntry.objects.filter(owner=owner, ledger_type="resale").prefetch_related("sales"))
    personal_entries = list(
        LedgerEntry.objects.filter(owner=owner, ledger_type="personal").prefetch_related("sales")
    )

    resale_row = _ledger_totals_row("Reselling", resale_entries)
    personal_row = _ledger_totals_row("Personal", personal_entries)
    total_row = {
        "label": "Total",
        "revenue": resale_row["revenue"] + personal_row["revenue"],
        "costs": resale_row["costs"] + personal_row["costs"],
        "net": resale_row["net"] + personal_row["net"],
        "items_sold": resale_row["items_sold"] + personal_row["items_sold"],
        "items_held": resale_row["items_held"] + personal_row["items_held"],
    }

    monthly_rows = []
    for year, month in _last_12_months(now):
        month_start, month_end = _month_bounds(year, month)
        resale_month = [e for e in resale_entries if _sold_in_month(e, month_start, month_end)]
        personal_month = [e for e in personal_entries if _sold_in_month(e, month_start, month_end)]
        resale_revenue = sum((e.total_realized for e in resale_month if e.total_realized is not None), Decimal("0"))
        resale_net = sum((e.profit for e in resale_month), Decimal("0"))
        personal_revenue = sum(
            (e.total_realized for e in personal_month if e.total_realized is not None), Decimal("0"),
        )
        personal_net = sum((e.profit for e in personal_month), Decimal("0"))
        monthly_rows.append({
            "month": month_start,
            "resale_revenue": resale_revenue, "resale_net": resale_net,
            "personal_revenue": personal_revenue, "personal_net": personal_net,
            "combined_revenue": resale_revenue + personal_revenue,
            "combined_net": resale_net + personal_net,
        })

    return {
        "tiles": {
            "total_revenue": total_row["revenue"],
            "total_costs": total_row["costs"],
            "net": total_row["net"],
            "items_sold": total_row["items_sold"],
        },
        "rows": [resale_row, personal_row],
        "total_row": total_row,
        "monthly_rows": monthly_rows,
    }
