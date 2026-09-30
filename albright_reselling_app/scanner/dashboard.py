"""All data/query logic for the "Auction Scanner" section of the reselling
app's dashboard lives here. The dashboard view (views.dashboard) only calls
get_scanner_dashboard_context() and merges the result into its own context -
it does not run any of these queries itself, so this module can be tested
independently of the view/template.

Multi-source (ShopGoodwill, MaxSold), multi-category (coins, jewelry, games,
cards): candidate/closest-call/lead rows carry a "source", a "category", and
for pickup-based sources like MaxSold, a "pickup" dict pulled from the lot's
raw["_pickup"]. Health is tracked per source, since sources run on different
schedules and have different staleness expectations.

Read-only: nothing here starts a scan. Scans only run from the scheduled
`scan_lots` task.
"""
import re
import statistics
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.utils import timezone

from ..scanner_models import AIReview, LotEvaluation, ScanRun, SourcedLot, SpotPrice
from .ai_review_service import months_review_cost, todays_review_count

STALE_RUN_AFTER = timedelta(hours=2)
CLOSEST_CALLS_LIMIT = 10
RECENT_RUNS_LIMIT = 10
LEADS_TO_REVIEW_LIMIT = 50
RESULTS_WINDOW_DAYS = 7
RECENT_RESULTS_LIMIT = 20

# Human-readable labels for scanner/coins.py CoinType keys and the
# jewelry_gold_{karat}k / jewelry_sterling keys from scanner/jewelry.py.
# Kept here (not in coins.py/jewelry.py) since it's purely a display
# concern - add an entry whenever a new coin type is added there.
ITEM_LABELS = {
    "double_eagle_20": "$20 gold double eagle",
    "eagle_10_gold": "$10 gold eagle",
    "modern_10_gold": "$10 gold eagle (modern)",
    "half_eagle_5_gold": "$5 gold half eagle",
    "modern_5_gold": "$5 gold eagle (modern)",
    "quarter_eagle_gold": "$2.50 gold quarter eagle",
    "krugerrand": "Krugerrand",
    "gold_buffalo": "Gold buffalo",
    "gold_eagle_1oz": "Gold eagle",
    "morgan_dollar": "Morgan dollar",
    "peace_dollar": "Peace dollar",
    "silver_eagle": "Silver eagle",
    "silver_maple": "Silver maple",
    "eisenhower_40": "Eisenhower dollar (40% silver)",
    "kennedy_90": "Kennedy half (90% silver)",
    "kennedy_40": "Kennedy half (40% silver)",
    "walking_liberty_half": "Walking Liberty half",
    "franklin_half": "Franklin half",
    "barber_half": "Barber half",
    "washington_quarter_90": "Washington quarter (silver)",
    "standing_liberty_quarter": "Standing Liberty quarter",
    "barber_quarter": "Barber quarter",
    "mercury_dime": "Mercury dime",
    "roosevelt_dime_90": "Roosevelt dime (silver)",
    "barber_dime": "Barber dime",
    "war_nickel": "War nickel",
    "junk_silver_face": "Junk silver (face value)",
    "generic_silver": "Silver bar/round",
    "generic_gold": "Gold bar/round",
    "jewelry_sterling": "Sterling silver jewelry",
}
JEWELRY_GOLD_KEY_RE = re.compile(r"^jewelry_gold_(\d+)k$")


def _item_label(coin_key):
    if coin_key in ITEM_LABELS:
        return ITEM_LABELS[coin_key]
    match = JEWELRY_GOLD_KEY_RE.match(coin_key)
    if match:
        return f"{match.group(1)}k gold jewelry"
    return coin_key.replace("_", " ").capitalize()  # unmapped key: best-effort fallback


def _item_labels(coin_keys):
    """coin_keys is the comma-joined LotEvaluation.coin_keys string (can
    hold more than one key for a mixed lot)."""
    if not coin_keys:
        return ""
    return ", ".join(_item_label(k) for k in coin_keys.split(","))


def _metal_label(silver_oz, gold_oz):
    """"1.09 oz Ag" / "0.64 oz Au" / "1.09 oz Ag, 0.64 oz Au" for the rare
    mixed lot / "" when there's no metal content at all (games, cards,
    "none")."""
    parts = []
    if silver_oz:
        parts.append(f"{silver_oz:.2f} oz Ag")
    if gold_oz:
        parts.append(f"{gold_oz:.2f} oz Au")
    return ", ".join(parts)


# Sources whose staleness ("last run was over 2 hours ago") is checked on
# the dashboard. MaxSold isn't on a firm enough schedule yet to flag it.
STALENESS_CHECKED_SOURCES = {"shopgoodwill"}

SOURCE_DISPLAY_NAMES = {"shopgoodwill": "ShopGoodwill", "maxsold": "MaxSold"}


def _source_display(source):
    return SOURCE_DISPLAY_NAMES.get(source, source.title())


def _has_maxsold(rows):
    """Whether a table's row list has any MaxSold lots - the template uses
    this to only show the Pickup column on tables where it means anything."""
    return any(row.get("source") == "maxsold" for row in rows)


def _format_duration(delta):
    """"3h 12m" style - used both for time remaining on a lot and time
    since the last scan. Never negative: callers pass an already-positive
    delta, or check the sign themselves first."""
    total_seconds = int(delta.total_seconds())
    if total_seconds <= 0:
        return "0m"
    hours, remainder = divmod(total_seconds, 3600)
    minutes = remainder // 60
    if hours and minutes:
        return f"{hours}h {minutes}m"
    if hours:
        return f"{hours}h"
    return f"{minutes}m"


def _time_remaining(lot, now):
    remaining = lot.end_time - now
    return _format_duration(remaining) if remaining.total_seconds() > 0 else "Ended"


def _end_time_epoch_ms(lot):
    return int(lot.end_time.timestamp() * 1000) if lot.end_time else None


def _lot_row(evaluation, now):
    lot = evaluation.lot
    return {
        "lot_id": lot.pk,
        "title": lot.title,
        "lot_url": lot.url,
        "source": lot.source,
        "source_display": _source_display(lot.source),
        "category": evaluation.category,
        "current_bid": lot.current_price,
        "max_bid": evaluation.max_bid,
        "headroom": evaluation.headroom,
        "confidence": evaluation.confidence,
        "coin_keys": evaluation.coin_keys,
        "item_label": _item_labels(evaluation.coin_keys),
        "silver_oz": evaluation.silver_oz,
        "gold_oz": evaluation.gold_oz,
        "metal_label": _metal_label(evaluation.silver_oz, evaluation.gold_oz),
        "flags": evaluation.flags,
        "time_remaining": _time_remaining(lot, now),
        "end_time_epoch_ms": _end_time_epoch_ms(lot),
        # Pickup-based sources (MaxSold) carry drive/estate info here; other
        # sources (ShopGoodwill) simply have nothing under "_pickup".
        "pickup": (lot.raw or {}).get("_pickup"),
    }


def _lead_row(evaluation, now):
    lot = evaluation.lot
    return {
        "lot_id": lot.pk,
        "category": evaluation.category,
        "title": lot.title,
        "lot_url": lot.url,
        "source": lot.source,
        "source_display": _source_display(lot.source),
        "confidence": evaluation.confidence,
        "current_bid": lot.current_price,
        "lead_reason": evaluation.lead_reason,
        "time_remaining": _time_remaining(lot, now),
        "end_time_epoch_ms": _end_time_epoch_ms(lot),
        "pickup": (lot.raw or {}).get("_pickup"),
    }


def _split_by_window(live_qs, now, window_hours):
    """Live candidates ending within window_hours (soonest first - the
    queryset is already ordered by end_time), and everything else. Early
    bids on something ending in days are meaningless, so this is what
    actually deserves attention right now vs. what's just worth tracking."""
    cutoff = now + timedelta(hours=window_hours)
    ending_soon, ending_later = [], []
    for ev in live_qs:
        row = _lot_row(ev, now)
        (ending_soon if ev.lot.end_time <= cutoff else ending_later).append(row)
    return ending_soon, ending_later


def _check_by_hand(now):
    """Live, priced, affordable (headroom >= 0) lots the parser itself
    flagged as low-confidence - not disqualified, just worth a human look
    before bidding. evaluation.flags carries why (e.g. weight_mismatch,
    key_date_verify, numismatic_upside)."""
    check_qs = (
        LotEvaluation.objects.select_related("lot")
        .filter(confidence="low", max_bid__gt=0, headroom__gte=0, lot__end_time__gt=now)
        .order_by("lot__end_time")
    )
    return [_lot_row(ev, now) for ev in check_qs]


def _source_health(source, now, check_staleness):
    last_run = ScanRun.objects.filter(source=source).order_by("-started_at").first()
    warnings = []

    if last_run is None:
        warnings.append(f"No {source} scans have run yet - check the scheduled task.")
    else:
        if last_run.error:
            warnings.append(f"Last {source} scan failed: {last_run.error}")
        if last_run.failed_keywords:
            warnings.append(f"Last {source} scan had failed keywords: {', '.join(last_run.failed_keywords)}")
        if check_staleness:
            age = now - last_run.started_at
            if age > STALE_RUN_AFTER:
                warnings.append(f"Last {source} scan was {_format_duration(age)} ago - check the scheduled task.")

    return {
        "source": source,
        "source_display": _source_display(source),
        "last_run": last_run,
        "last_run_started_at": timezone.localtime(last_run.started_at) if last_run else None,
        "warnings": warnings,
    }


def _maxsold_estates(now):
    """Live MaxSold candidates grouped by auction (estate) - winning several
    lots at one estate means one pickup trip, so this is the view that
    actually matters for deciding whether a trip out is worth it."""
    live_qs = (
        LotEvaluation.objects.select_related("lot")
        .filter(is_candidate=True, lot__end_time__gt=now, lot__source="maxsold")
    )

    estates = {}
    for ev in live_qs:
        pickup = (ev.lot.raw or {}).get("_pickup") or {}
        auction_id = pickup.get("auction_id")
        if auction_id is None:
            continue  # shouldn't happen for a maxsold lot, but don't blow up the dashboard if it does
        estate = estates.setdefault(auction_id, {
            "auction_id": auction_id,
            "auction_title": pickup.get("auction_title") or "",
            "city": pickup.get("city") or "",
            "distance_miles": pickup.get("distance_miles"),
            "lot_count": 0,
            "total_headroom": Decimal("0"),
        })
        estate["lot_count"] += 1
        estate["total_headroom"] += ev.headroom or Decimal("0")

    return sorted(estates.values(), key=lambda e: e["total_headroom"], reverse=True)


def _leads_to_review(now, window_hours):
    """Leads worth a look right now: early bids on a lead are meaningless
    (nobody's seriously bid yet), so only lots ending soon - within
    window_hours - are shown. One query, evaluated once, so both the capped
    row list and the full per-category counts come from the same data."""
    cutoff = now + timedelta(hours=window_hours)
    evaluations = list(
        LotEvaluation.objects.select_related("lot")
        .filter(is_lead=True, lot__end_time__gt=now, lot__end_time__lte=cutoff)
        .order_by("lot__end_time")
    )

    counts_by_category = {}
    for ev in evaluations:
        counts_by_category[ev.category] = counts_by_category.get(ev.category, 0) + 1

    rows = [_lead_row(ev, now) for ev in evaluations[:LEADS_TO_REVIEW_LIMIT]]
    return rows, counts_by_category


def _closed_results(now):
    """Lots that closed (sold) in the last RESULTS_WINDOW_DAYS and had a max
    bid - i.e. lots where the pipeline actually made a call worth checking
    against reality. Per-category under/over-max counts and the median
    final-price-as-percent-of-melt, plus the most recent closed lots for the
    detail table. One query, evaluated once, so both come from the same data."""
    cutoff = now - timedelta(days=RESULTS_WINDOW_DAYS)
    lots = list(
        SourcedLot.objects.filter(
            is_closed=True, final_price__isnull=False, end_time__gte=cutoff, evaluation__max_bid__gt=0,
        ).select_related("evaluation").order_by("-end_time")
    )

    by_category = {}
    for lot in lots:
        ev = lot.evaluation
        stats = by_category.setdefault(ev.category, {"under": 0, "over": 0, "melt_pcts": []})
        if lot.final_price <= ev.max_bid:
            stats["under"] += 1
        else:
            stats["over"] += 1
        if ev.melt_value and ev.melt_value > 0:
            stats["melt_pcts"].append(float(lot.final_price) / float(ev.melt_value) * 100)

    summary = [
        {
            "category": category,
            "under": stats["under"],
            "over": stats["over"],
            "median_melt_pct": statistics.median(stats["melt_pcts"]) if stats["melt_pcts"] else None,
        }
        for category, stats in sorted(by_category.items())
    ]

    rows = [
        {
            "title": lot.title,
            "lot_url": lot.url,
            "source": lot.source,
            "source_display": _source_display(lot.source),
            "category": lot.evaluation.category,
            "confidence": lot.evaluation.confidence,
            "final_price": lot.final_price,
            "max_bid": lot.evaluation.max_bid,
            "melt_value": lot.evaluation.melt_value,
            "under_max": lot.final_price <= lot.evaluation.max_bid,
        }
        for lot in lots[:RECENT_RESULTS_LIMIT]
    ]

    return summary, rows


def _spot_price_status(now, max_age_days):
    statuses = {}
    for metal in ("silver", "gold"):
        latest = SpotPrice.objects.filter(metal=metal).exclude(source="goldapi_failed").first()
        if latest is None:
            statuses[metal] = None
            continue
        statuses[metal] = {
            "price": latest.price_usd,
            "fetched_at": timezone.localtime(latest.fetched_at),
            "is_stale": (now - latest.fetched_at) > timedelta(days=max_age_days),
        }
    return statuses


def _latest_ai_reviews(lot_ids):
    """Most recent successful AIReview per lot id, for the dashboard badge
    ("AI: $95-$130"). One query regardless of how many rows need it."""
    lot_ids = {lid for lid in lot_ids if lid is not None}
    if not lot_ids:
        return {}
    latest = {}
    reviews = AIReview.objects.filter(lot_id__in=lot_ids, status="done").order_by("lot_id", "-created_at")
    for review in reviews:
        latest.setdefault(review.lot_id, review)  # first hit per lot_id is the most recent (see order_by)
    return latest


def _attach_ai_reviews(*row_lists):
    """Adds an "ai_review" key (an AIReview or None) to every row across all
    the given lists, via one shared lookup query."""
    all_rows = [row for rows in row_lists for row in rows]
    latest = _latest_ai_reviews(row["lot_id"] for row in all_rows)
    for row in all_rows:
        row["ai_review"] = latest.get(row["lot_id"])


def _ai_review_stats(cfg):
    return {
        "today_count": todays_review_count(),
        "month_cost": months_review_cost(),
        "monthly_budget": Decimal(str(cfg["monthly_budget_usd"])),
        "daily_limit": cfg["daily_limit"],
    }


def get_scanner_dashboard_context():
    now = timezone.now()
    cfg = settings.RESELLING_SCANNER
    max_age_days = cfg["SPOT"]["max_age_days"]
    sources = tuple(cfg["SOURCES"].keys())  # whatever's configured, e.g. ("shopgoodwill", "maxsold")

    live_qs = (
        LotEvaluation.objects.select_related("lot")
        .filter(is_candidate=True, lot__end_time__gt=now)
        .order_by("lot__end_time")
    )
    candidate_window_hours = cfg["CANDIDATE_WINDOW_HOURS"]
    live_candidates_ending_soon, live_candidates_ending_later = _split_by_window(
        live_qs, now, candidate_window_hours
    )

    # "Closest calls": priced, live, over your max bid (headroom < 0) - the
    # ones where the market is closest to matching what you'd pay. A live
    # lot that's simply under-max-but-not-a-candidate (e.g. confidence too
    # low) belongs in "Check by hand" instead, not here.
    closest_qs = (
        LotEvaluation.objects.select_related("lot")
        .filter(is_candidate=False, max_bid__gt=0, headroom__lt=0, lot__end_time__gt=now)
        .order_by("-headroom")[:CLOSEST_CALLS_LIMIT]
    )
    closest_calls = [_lot_row(ev, now) for ev in closest_qs]

    check_by_hand = _check_by_hand(now)

    lead_window_hours = cfg["LEAD_WINDOW_HOURS"]
    leads_to_review, lead_counts_by_category = _leads_to_review(now, lead_window_hours)

    recent_runs = [
        {
            "started_at": timezone.localtime(run.started_at),
            "source": run.source,
            "source_display": _source_display(run.source),
            "lots_seen": run.lots_seen,
            "candidates": run.candidates,
            "failed_keywords": run.failed_keywords,
            "has_error": bool(run.error),
        }
        for run in ScanRun.objects.all()[:RECENT_RUNS_LIMIT]
    ]

    scanner_health = {
        source: _source_health(source, now, check_staleness=source in STALENESS_CHECKED_SOURCES)
        for source in sources
    }

    closed_results_summary, closed_results_rows = _closed_results(now)

    # AI review button/badge: only on the tables where you'd plausibly pay
    # for a second opinion (not Closest Calls - those are already over your
    # max bid regardless of what an AI review might say).
    _attach_ai_reviews(live_candidates_ending_soon, live_candidates_ending_later, check_by_hand, leads_to_review)

    return {
        "scanner_health": scanner_health,
        "spot_prices": _spot_price_status(now, max_age_days),
        "live_candidates_ending_soon": live_candidates_ending_soon,
        "live_candidates_ending_later": live_candidates_ending_later,
        "live_candidates_ending_soon_has_maxsold": _has_maxsold(live_candidates_ending_soon),
        "live_candidates_ending_later_has_maxsold": _has_maxsold(live_candidates_ending_later),
        "candidate_window_hours": candidate_window_hours,
        "check_by_hand": check_by_hand,
        "check_by_hand_has_maxsold": _has_maxsold(check_by_hand),
        "closest_calls": closest_calls,
        "closest_calls_has_maxsold": _has_maxsold(closest_calls),
        "leads_to_review": leads_to_review,
        "leads_to_review_has_maxsold": _has_maxsold(leads_to_review),
        "lead_counts_by_category": lead_counts_by_category,
        "lead_window_hours": lead_window_hours,
        "recent_runs": recent_runs,
        "maxsold_estates": _maxsold_estates(now),
        "closed_results_summary": closed_results_summary,
        "closed_results_rows": closed_results_rows,
        "closed_results_has_maxsold": _has_maxsold(closed_results_rows),
        "ai_review_stats": _ai_review_stats(cfg["AI_REVIEW"]),
        "last_run_lots_seen": sum(
            h["last_run"].lots_seen for h in scanner_health.values() if h["last_run"]
        ),
    }
