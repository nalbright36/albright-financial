"""Day-to-day reselling triage: the Selling Board (/ledger/board/) and the
Today page (/today/), plus the one shared markdown-step/aging definition
both the dashboard's Needs Action tile and the Ledger's aging view use (so
their counts can never drift apart). Kept out of ledger_metrics.py/
scanner/dashboard.py since this is a new, separate layer on top of both.

Nothing here is imported by (or imports) scanner/coins.py, jewelry.py,
leads.py, valuers.py, ai_review.py, or simple_calc.py.
"""
from datetime import datetime, timedelta
from decimal import Decimal

from django.conf import settings
from django.contrib.auth import get_user_model
from django.utils import timezone

from .models import LedgerEntry
from .scanner.dashboard import get_scanner_dashboard_context
from .scanner_models import BidWatch

# ---------------------------------------------------------------------------
# Markdown steps / aging - the one shared definition
# ---------------------------------------------------------------------------

MARKDOWN_STEPS = [
    (14, "Send offers to watchers"),
    (30, "Lower price about 10%"),
    (60, "Lower price about 10% again"),
    (90, "Sell locally, bundle, or donate"),
]
NOT_LISTED_FLAG_DAYS = 7
SHIPPED_WINDOW_DAYS = 30
DAILY_LIST_TARGET = 2
AI_BUDGET_WARNING_PCT = 80  # flag as a Today-page warning once this much of the monthly budget is used


def markdown_status(entry, today=None):
    """What (if anything) this unsold entry needs right now - either the
    next markdown step (listed items, timed from listed_at) or a "not
    listed" flag (unlisted items, timed from purchase/created date). The
    one shared definition: dashboard_tiles()'s markdown_count, the Ledger/
    Personal Items aging views, the Selling Board, and the Today page all
    call this (via needs_markdown_action()/aging_row() below), so their
    counts and labels can never drift apart.

    Returns {"listed": bool, "days": int, "due": bool, "label": str|None,
    "step": int|None, "due_date": date|None} - "due" is "needs attention
    right now"; "label"/"step"/"due_date" describe the next relevant step
    (or the "not listed" message) whether or not it's due yet, since the
    Board's cards want to show that even before it's due."""
    today = today or timezone.localdate()

    if entry.listed_at is None:
        start = entry.purchase_date or timezone.localtime(entry.created_at).date()
        days = (today - start).days
        plural = "" if days == 1 else "s"
        return {
            "listed": False, "days": days, "due": days > NOT_LISTED_FLAG_DAYS,
            "label": f"Not listed for {days} day{plural}", "step": None, "due_date": None,
        }

    days_listed = (today - entry.listed_at).days
    last_done = entry.last_markdown_step or 0
    for step_days, label in MARKDOWN_STEPS:
        if step_days <= last_done:
            continue
        due_date = entry.listed_at + timedelta(days=step_days)
        if days_listed >= step_days:
            return {"listed": True, "days": days_listed, "due": True, "label": label,
                     "step": step_days, "due_date": due_date}
        # Not due yet, but it's the next upcoming step - report it for display.
        return {"listed": True, "days": days_listed, "due": False, "label": label,
                 "step": step_days, "due_date": due_date}
    return {"listed": True, "days": days_listed, "due": False, "label": None, "step": None, "due_date": None}


def needs_markdown_action(entry, today=None):
    return markdown_status(entry, today=today)["due"]


def aging_row(entry, today=None):
    """Same shape ledger_metrics._aging_rows() always returned (entry/
    days_held/suggested_action/value_at_cost) - scorecard.html and
    ledger_personal.html keep working unchanged; only how the values are
    computed changed (markdown_status(), not the old purchase-date-only
    aging_suggestion)."""
    status = markdown_status(entry, today=today)
    return {
        "entry": entry,
        "days_held": status["days"],
        "suggested_action": status["label"] if status["due"] else None,
        "value_at_cost": entry.buy_side_cost,
    }


# ---------------------------------------------------------------------------
# Selling Board
# ---------------------------------------------------------------------------

#: steps whose action is "Done" only (no price change) vs. a price change
#: ("Lower price") - the Board/Today card's button depends on which.
MARKDOWN_STEPS_NO_PRICE_CHANGE = (14, 90)

def _card(entry, today):
    status = markdown_status(entry, today=today)
    return {
        "entry": entry,
        "is_personal": entry.ledger_type == "personal",
        "buy_side_cost": entry.buy_side_cost if entry.ledger_type != "personal" else None,
        "list_price": entry.list_price,
        "days": status["days"],
        "listed": status["listed"],
        "next_step": status["step"],
        "next_step_label": status["label"],
        "next_step_due_date": status["due_date"],
        "step_due": status["due"],
        "step_is_price_change": bool(status["step"]) and status["step"] not in MARKDOWN_STEPS_NO_PRICE_CHANGE,
    }


def board_columns(owner, ledger_type=None, today=None):
    """{"to_list": [...], "listed": [...], "to_ship": [...], "shipped": [...]}
    - each a list of _card() dicts, oldest first. A sold_out entry with no
    itemized sales (a legacy single-sale entry, from before shipped_at
    existed) has nothing to track here and is simply left off the board."""
    today = today or timezone.localdate()
    qs = LedgerEntry.objects.filter(owner=owner, status__in=("holding", "partially_sold", "sold_out"))
    if ledger_type:
        qs = qs.filter(ledger_type=ledger_type)
    entries = list(qs.prefetch_related("sales"))

    columns = {"to_list": [], "listed": [], "to_ship": [], "shipped": []}
    shipped_cutoff = today - timedelta(days=SHIPPED_WINDOW_DAYS)

    for entry in entries:
        if entry.has_itemized_sales:
            sales = list(entry.sales.all())
            if any(s.shipped_at is None for s in sales):
                columns["to_ship"].append(_card(entry, today))
            else:
                last_shipped = max(s.shipped_at for s in sales)
                if last_shipped.date() >= shipped_cutoff:
                    columns["shipped"].append(_card(entry, today))
                # else: shipped more than 30 days ago - drop off the board.
            continue
        if entry.status != "holding":
            continue  # sold_out/partially_sold with no itemized sales - nothing left to track
        if entry.listed_at is None:
            columns["to_list"].append(_card(entry, today))
        else:
            columns["listed"].append(_card(entry, today))

    for cards in columns.values():
        cards.sort(key=lambda c: -c["days"])
    return columns


def listed_today_count(owner, today=None):
    today = today or timezone.localdate()
    return LedgerEntry.objects.filter(owner=owner, listed_at=today).count()


# ---------------------------------------------------------------------------
# Sell-down goal progress
# ---------------------------------------------------------------------------

def sell_goal_progress(owner, now=None):
    """Items sold and $ recovered since SELL_GOAL's start_date, across
    both ledgers for this owner - a sale is "since start_date" by its own
    sale_date (itemized) or sold_date (legacy), not when the item was
    originally bought/listed."""
    now = now or timezone.now()
    cfg = settings.RESELLING_SCANNER["SELL_GOAL"]
    start_date = datetime.strptime(cfg["start_date"], "%Y-%m-%d").date()
    goal_items = cfg["items"]
    goal_recover = Decimal(str(cfg["recover_usd"]))

    items_sold = 0
    recovered = Decimal("0")
    for entry in LedgerEntry.objects.filter(owner=owner).prefetch_related("sales"):
        if entry.has_itemized_sales:
            for sale in entry.sales.all():
                if sale.sale_date >= start_date:
                    items_sold += 1
                    recovered += sale.sale_price or 0
        elif entry.sold_for is not None and entry.sold_date is not None and entry.sold_date >= start_date:
            items_sold += 1
            recovered += entry.sold_for

    items_pct = min(100, round(items_sold / goal_items * 100)) if goal_items else 0
    recover_pct = min(100, round(float(recovered / goal_recover * 100))) if goal_recover else 0

    return {
        "items_sold": items_sold, "goal_items": goal_items,
        "recovered": recovered, "goal_recover": goal_recover,
        "items_pct": items_pct, "recover_pct": recover_pct,
        "goal_met": items_sold >= goal_items or recovered >= goal_recover,
    }


# ---------------------------------------------------------------------------
# Today page
# ---------------------------------------------------------------------------

def closing_soon_watches(now=None):
    now = now or timezone.now()
    cutoff = now + timedelta(hours=24)
    return list(
        BidWatch.objects.active().filter(lot__end_time__gte=now, lot__end_time__lte=cutoff)
        .select_related("lot").order_by("lot__end_time")
    )


def likely_won_not_logged(owner):
    """Same definition as ledger_metrics.dashboard_tiles()'s wins_to_log."""
    logged_lot_ids = set(
        LedgerEntry.objects.filter(owner=owner, scanner_lot_id__isnull=False)
        .values_list("scanner_lot_id", flat=True)
    )
    return list(
        BidWatch.objects.filter(status="likely_won").exclude(lot_id__in=logged_lot_ids).select_related("lot")
    )


def system_warnings(dashboard_context):
    """Stale scans, a failed/missing/stale spot fetch, and a near-exhausted
    AI review budget - reusing the dashboard's own health-check data
    (scanner/dashboard.get_scanner_dashboard_context()) rather than
    recomputing any of it."""
    warnings = []
    for health in dashboard_context.get("scanner_health", {}).values():
        warnings.extend(health.get("warnings", []))

    for metal, spot in dashboard_context.get("spot_prices", {}).items():
        if spot is None:
            warnings.append(f"No {metal} spot price on file - check the fetch_spot_prices scheduled task.")
        elif spot.get("is_stale"):
            warnings.append(f"{metal.capitalize()} spot price is stale.")

    ai_stats = dashboard_context.get("ai_review_stats") or {}
    budget = ai_stats.get("monthly_budget") or 0
    spent = ai_stats.get("month_cost") or 0
    if budget and spent / budget * 100 >= AI_BUDGET_WARNING_PCT:
        warnings.append(f"AI review budget is at ${spent:.2f} of ${budget:.2f} this month.")

    return warnings


def today_sections(owner, now=None):
    """Everything the Today page needs, in the task's own order: to ship,
    markdowns due, to list (+ daily target), bids/wins, candidates ending
    soon (top 5 by headroom), and system warnings. "N things to do today"
    only counts the time-sensitive sections (a/b/d/e below plus the
    "not listed" flag) - the to-list queue and the ending-soon candidates
    are everyday browsing, not something overdue."""
    now = now or timezone.now()
    today = timezone.localdate(now)

    board = board_columns(owner, today=today)
    to_ship = board["to_ship"]
    markdowns_due = [card for card in board["listed"] if card["step_due"]]
    not_listed_flagged = [card for card in board["to_list"] if card["days"] > NOT_LISTED_FLAG_DAYS]
    to_list = board["to_list"]

    dashboard_context = get_scanner_dashboard_context(user=owner)
    candidates = sorted(
        dashboard_context.get("live_candidates_ending_soon", []),
        key=lambda row: -(row.get("headroom") or 0),
    )[:5]
    candidates_total = len(dashboard_context.get("live_candidates_ending_soon", []))

    closing_soon = closing_soon_watches(now=now)
    wins_to_log = likely_won_not_logged(owner)
    warnings = system_warnings(dashboard_context)

    todo_count = len(to_ship) + len(markdowns_due) + len(not_listed_flagged) + len(closing_soon) + len(wins_to_log)

    return {
        "goal": sell_goal_progress(owner, now=now),
        "to_ship": to_ship,
        "markdowns_due": markdowns_due,
        "not_listed_flagged": not_listed_flagged,
        "to_list": to_list,
        "listed_today_count": listed_today_count(owner, today=today),
        "daily_list_target": DAILY_LIST_TARGET,
        "closing_soon": closing_soon,
        "wins_to_log": wins_to_log,
        "candidates": candidates,
        "candidates_total": candidates_total,
        "warnings": warnings,
        "todo_count": todo_count,
    }


# ---------------------------------------------------------------------------
# Telegram digest (scanner/alerts.py adds this section to the daily message)
# ---------------------------------------------------------------------------

def selling_digest_lines(now=None):
    """The digest's "Selling" section - not owner-scoped like the rest of
    this module (the digest is one message for the whole app, and in
    practice there's one real user), so this sums straight across every
    LedgerEntry rather than taking an owner."""
    now = now or timezone.now()
    today = timezone.localdate(now)

    to_ship = 0
    markdowns_due = 0
    not_listed = 0
    for entry in LedgerEntry.objects.filter(status__in=("holding", "partially_sold", "sold_out")).prefetch_related("sales"):
        if entry.has_itemized_sales:
            if any(s.shipped_at is None for s in entry.sales.all()):
                to_ship += 1
            continue
        if entry.status != "holding":
            continue
        if needs_markdown_action(entry, today=today):
            if entry.listed_at is None:
                not_listed += 1
            else:
                markdowns_due += 1

    User = get_user_model()
    goal_lines = []
    for user in User.objects.all():
        progress = sell_goal_progress(user, now=now)
        if progress["items_sold"] or progress["recovered"]:
            goal_lines.append(
                f"  {user.username}: {progress['items_sold']}/{progress['goal_items']} items, "
                f"${progress['recovered']:.2f}/${progress['goal_recover']:.2f} recovered"
                + (" - buying freeze can lift!" if progress["goal_met"] else "")
            )

    lines = [
        "<b>Selling</b>",
        f"  To ship: {to_ship}",
        f"  Markdowns due: {markdowns_due}",
        f"  Not listed (7+ days): {not_listed}",
    ]
    lines += goal_lines or ["  Sell goal: no sales recorded yet."]
    return lines
