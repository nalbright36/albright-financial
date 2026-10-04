"""Logic behind the Insights page (insights_views.py): calibration-override
lookups used by the pipeline/max-bid/ledger/AI-review code, market-history
stats over closed lots, the calibrate command's suggestion-building, and
AI review accuracy. Kept separate from the view - same pattern as
scanner/dashboard.py and scanner/review_history.py - so all of this is
testable without going through a request/response cycle.
"""
import statistics
from collections import defaultdict
from datetime import timedelta
from decimal import Decimal

from django.utils import timezone

from ..models import LedgerEntry
from ..scanner_models import AIReview, CalibrationOverride, CalibrationSuggestion, SourcedLot

DEFAULT_HISTORY_DAYS = 30
MIN_SOLD_SAMPLE = 5       # calibrate: minimum Ledger sales before suggesting a change
MIN_CLOSED_SAMPLE = 5     # calibrate: minimum closed lots before suggesting a change
MIN_SELLER_SAMPLE = 5     # market history: minimum closed lots before a seller/auctioneer is ranked
SUGGEST_THRESHOLD_PCT = 5.0   # calibrate: only suggest a change bigger than this
MAX_STEP_PCT = 25.0           # calibrate: never move a value more than this in one step
TOP_SELLERS_LIMIT = 10


# ---------------------------------------------------------------------------
# Calibration overrides - read first, fall back to settings.py
# ---------------------------------------------------------------------------

def get_calibrated(key, default):
    """The one place every override-aware config read goes through: the
    CalibrationOverride for this exact key if one exists, else the
    caller's own settings.py-derived default, unchanged. Used directly by
    scanner/pipeline.py, ai_review_service.py, and ledger_views.py for
    single-value reads (a source's buyer_premium_pct/sales_tax_pct).
    Cast to float, same as _apply_overrides below - these values all end
    up in float arithmetic (BuyCosts/max_bid), which raises on a stray
    Decimal (CalibrationOverride.value's own type)."""
    override = CalibrationOverride.objects.filter(key=key).first()
    return float(override.value) if override is not None else default


def _apply_overrides(prefix, base_dict):
    """Every CalibrationOverride under "{prefix}." substituted into a copy
    of base_dict, keyed by what follows the prefix - one query for the
    whole dict (coins.py's estimate_resale()/the merged CATEGORY_FEES dict
    can look up any key in here per item), not one query per key per lot.
    Same override-wins-else-default rule as get_calibrated above, just
    evaluated against one prefetched batch instead of a single row."""
    rows = CalibrationOverride.objects.filter(key__startswith=f"{prefix}.").values_list("key", "value")
    # Cast to float - base_dict's own values are plain floats (straight out
    # of settings.py), and coins.py/max_bid.py do float arithmetic on
    # whatever comes out of this dict; leaving an override as a Decimal
    # would raise on the first float * Decimal they hit.
    overrides = {key[len(prefix) + 1:]: float(value) for key, value in rows}
    return {k: overrides.get(k, v) for k, v in base_dict.items()}


def calibrated_multipliers(base_multipliers):
    """RESALE_MULTIPLIERS with any CalibrationOverride values swapped in."""
    return _apply_overrides("RESALE_MULTIPLIERS", base_multipliers)


def calibrated_category_fees(category, base_fees):
    """A merged FEES/CATEGORY_FEES[category] dict with any
    CalibrationOverride values swapped in, keyed "CATEGORY_FEES.{category}.*"."""
    return _apply_overrides(f"CATEGORY_FEES.{category}", base_fees)


# ---------------------------------------------------------------------------
# Part A: market history over closed lots
# ---------------------------------------------------------------------------

def _closed_lots_queryset(source="", category="", date_from=None, date_to=None):
    qs = (
        SourcedLot.objects.filter(is_closed=True, final_price__isnull=False)
        .select_related("evaluation")
    )
    if source:
        qs = qs.filter(source=source)
    if category:
        qs = qs.filter(evaluation__category=category)
    if date_from:
        qs = qs.filter(end_time__gte=date_from)
    if date_to:
        qs = qs.filter(end_time__lte=date_to)
    return qs


def _median(values):
    return statistics.median(values) if values else None


def _seller_key(lot):
    """The best available seller/auctioneer identity for this lot's
    source: ShopGoodwill's seller_id, MaxSold's auction_id, HiBid's
    auctioneer - whichever this source's features actually recorded."""
    features = lot.features or {}
    if lot.source == "shopgoodwill":
        return features.get("seller_id")
    if lot.source == "maxsold":
        return features.get("auction_id")
    if lot.source == "hibid":
        return features.get("auctioneer") or features.get("auctioneer_id")
    return None


def _week_bucket(end_time):
    """Monday of the ISO week end_time falls in, as a date - the "trend by
    week" grouping key."""
    local = timezone.localtime(end_time)
    return local.date() - timedelta(days=local.date().weekday())


def source_category_stats(source="", category="", date_from=None, date_to=None):
    """Per source+category: closed-lot count, % closed at/under max bid,
    median final/melt%, median final/expected%, and a count-by-week trend.
    One pass over the filtered queryset (each stat only uses the lots that
    actually have the field it needs)."""
    lots = list(_closed_lots_queryset(source, category, date_from, date_to))

    groups = defaultdict(list)
    for lot in lots:
        ev = getattr(lot, "evaluation", None)
        groups[(lot.source, ev.category if ev else "unknown")].append(lot)

    rows = []
    for (src, cat), group in sorted(groups.items()):
        under_max = over_max = 0
        melt_pcts, expected_pcts = [], []
        by_week = defaultdict(int)

        for lot in group:
            ev = lot.evaluation
            if ev.max_bid:
                under_max += lot.final_price <= ev.max_bid
                over_max += lot.final_price > ev.max_bid
            if ev.melt_value:
                melt_pcts.append(float(lot.final_price) / float(ev.melt_value) * 100)
            if ev.expected_sale:
                expected_pcts.append(float(lot.final_price) / float(ev.expected_sale) * 100)
            if lot.end_time:
                by_week[_week_bucket(lot.end_time)] += 1

        with_max_bid = under_max + over_max
        rows.append({
            "source": src,
            "category": cat,
            "closed_count": len(group),
            "pct_at_or_under_max": round(under_max / with_max_bid * 100, 1) if with_max_bid else None,
            "median_final_melt_pct": _median(melt_pcts),
            "median_final_expected_pct": _median(expected_pcts),
            "trend_by_week": sorted(by_week.items()),
        })
    return rows


def top_sellers(source="", category="", date_from=None, date_to=None, limit=TOP_SELLERS_LIMIT):
    """Top sellers/auctioneers by lowest median final/melt% (the best
    sources of underpriced lots), minimum MIN_SELLER_SAMPLE closed lots
    with a usable melt value each."""
    lots = _closed_lots_queryset(source, category, date_from, date_to).filter(evaluation__melt_value__gt=0)

    by_seller = defaultdict(list)
    for lot in lots:
        seller = _seller_key(lot)
        if seller is None:
            continue
        pct = float(lot.final_price) / float(lot.evaluation.melt_value) * 100
        by_seller[(lot.source, seller)].append(pct)

    rows = [
        {"source": src, "seller": seller, "lot_count": len(pcts), "median_final_melt_pct": _median(pcts)}
        for (src, seller), pcts in by_seller.items()
        if len(pcts) >= MIN_SELLER_SAMPLE
    ]
    rows.sort(key=lambda r: r["median_final_melt_pct"])
    return rows[:limit]


def final_melt_by_hour_and_weekday(source="", category="", date_from=None, date_to=None):
    """Median final/melt% grouped by the lot's end hour of day and by
    weekday, from the closing-time features track_closed saves
    (end_hour_local/end_weekday_local) - needs a lot to have actually
    closed through track_closed (not just is_closed/final_price set some
    other way) to have those keys at all."""
    lots = _closed_lots_queryset(source, category, date_from, date_to).filter(evaluation__melt_value__gt=0)

    by_hour, by_weekday = defaultdict(list), defaultdict(list)
    for lot in lots:
        features = lot.features or {}
        hour, weekday = features.get("end_hour_local"), features.get("end_weekday_local")
        if hour is None and weekday is None:
            continue
        pct = float(lot.final_price) / float(lot.evaluation.melt_value) * 100
        if hour is not None:
            by_hour[hour].append(pct)
        if weekday is not None:
            by_weekday[weekday].append(pct)

    return (
        {hour: {"median_final_melt_pct": _median(pcts), "n": len(pcts)} for hour, pcts in sorted(by_hour.items())},
        {wd: {"median_final_melt_pct": _median(pcts), "n": len(pcts)} for wd, pcts in sorted(by_weekday.items())},
    )


def market_history(source="", category="", date_from=None, date_to=None):
    """Everything Part A of the Insights page shows, one call. date_from/
    date_to default to the last DEFAULT_HISTORY_DAYS days when not given."""
    now = timezone.now()
    if date_from is None and date_to is None:
        date_from = now - timedelta(days=DEFAULT_HISTORY_DAYS)

    by_hour, by_weekday = final_melt_by_hour_and_weekday(source, category, date_from, date_to)
    return {
        "date_from": date_from,
        "date_to": date_to,
        "by_source_category": source_category_stats(source, category, date_from, date_to),
        "top_sellers": top_sellers(source, category, date_from, date_to),
        "final_melt_by_hour": by_hour,
        "final_melt_by_weekday": by_weekday,
    }


# ---------------------------------------------------------------------------
# Part B: calibration suggestions (management/commands/calibrate.py)
# ---------------------------------------------------------------------------

def _pct_change(current, suggested):
    if not current:
        return None
    return abs(float(suggested) - float(current)) / abs(float(current)) * 100


def _capped_suggestion(current, target):
    """The suggested value, capped so it never moves more than MAX_STEP_PCT
    from the current value in one step - a big observed gap is approached
    gradually across several calibrate runs, not jumped to all at once."""
    current, target = float(current), float(target)
    max_step = abs(current) * (MAX_STEP_PCT / 100) if current else abs(target) * (MAX_STEP_PCT / 100)
    if target > current:
        return min(target, current + max_step)
    return max(target, current - max_step)


def _save_suggestion(key, current_value, suggested_value, sample_size, evidence):
    """Skips a key that already has a pending suggestion - calibrate is
    meant to be run repeatedly (e.g. nightly); it shouldn't pile up
    duplicate asks for the same change before the first is actioned."""
    if CalibrationSuggestion.objects.filter(key=key, status="pending").exists():
        return None
    pct_change = _pct_change(current_value, suggested_value)
    if pct_change is None or pct_change <= SUGGEST_THRESHOLD_PCT:
        return None
    capped = _capped_suggestion(current_value, suggested_value)
    return CalibrationSuggestion.objects.create(
        key=key, current_value=Decimal(str(round(current_value, 4))),
        suggested_value=Decimal(str(round(capped, 4))), sample_size=sample_size, evidence=evidence,
    )


def _multiplier_key_for(coin_keys, multipliers):
    """Which RESALE_MULTIPLIERS key coins.py's estimate_resale() would
    actually have used for a lot with this (possibly multiple,
    comma-joined) LotEvaluation.coin_keys string: exact key, then its
    family (the key with its last "_segment" stripped, e.g.
    "jewelry_gold_14k" -> "jewelry_gold"), then "default" - the same
    fallback chain coins.py uses internally, reimplemented here (not
    imported - scanner/coins.py is off limits for this task) so a
    suggestion is attributed to the multiplier that was actually applied,
    not just the lot's broad category. Only the first coin_key is used for
    a mixed lot (rare - see jewelry.py's own comments on mixed karats)."""
    if not coin_keys:
        return "default"
    first = coin_keys.split(",")[0]
    if first in multipliers:
        return first
    family = first.rsplit("_", 1)[0]
    return family if family in multipliers else "default"


def _ledger_multiplier_suggestions(cfg):
    """RESALE_MULTIPLIERS.{key}: for each multiplier key with at least
    MIN_SOLD_SAMPLE sold/written-off-excluded Ledger entries attributable
    to it (via the originating scanner lot's coin_keys, when the entry
    came from "I won this" - entries added by hand have no lot to
    attribute to and are skipped), compare the actual realized sale price
    to the lot's on-record melt value."""
    created = []
    entries = (
        LedgerEntry.objects.filter(melt_value__isnull=False, melt_value__gt=0, scanner_lot__isnull=False)
        .select_related("scanner_lot__evaluation").prefetch_related("sales")
    )
    multipliers = cfg["RESALE_MULTIPLIERS"]
    by_key = defaultdict(list)
    for entry in entries:
        if not entry.is_realized or entry.status == "written_off":
            continue
        realized = entry.total_realized
        if realized is None:
            continue
        evaluation = getattr(entry.scanner_lot, "evaluation", None)
        coin_keys = evaluation.coin_keys if evaluation else ""
        mult_key = _multiplier_key_for(coin_keys, multipliers)
        by_key[mult_key].append(float(realized) / float(entry.melt_value))

    for mult_key, ratios in by_key.items():
        if len(ratios) < MIN_SOLD_SAMPLE:
            continue
        current = multipliers[mult_key]
        suggestion = _save_suggestion(
            key=f"RESALE_MULTIPLIERS.{mult_key}", current_value=current, suggested_value=_median(ratios),
            sample_size=len(ratios),
            evidence=f"{len(ratios)} sold Ledger item(s) priced with multiplier {mult_key!r} realized a "
                     f"median {_median(ratios):.3f}x melt value vs. the configured {current}x.",
        )
        if suggestion:
            created.append(suggestion)
    return created


def _closed_lot_multiplier_suggestions(cfg):
    """RESALE_MULTIPLIERS.{key}: compare closed-lot final prices to
    expected_sale (which already has the current multiplier baked in, so
    final/expected is directly the ratio by which it under/over-shot),
    grouped by the multiplier key that was actually applied, when at
    least MIN_CLOSED_SAMPLE closed lots used that key."""
    created = []
    lots = (
        SourcedLot.objects.filter(is_closed=True, final_price__isnull=False, evaluation__expected_sale__gt=0)
        .select_related("evaluation")
    )
    multipliers = cfg["RESALE_MULTIPLIERS"]
    by_key = defaultdict(list)
    for lot in lots:
        ev = lot.evaluation
        mult_key = _multiplier_key_for(ev.coin_keys, multipliers)
        by_key[mult_key].append(float(lot.final_price) / float(ev.expected_sale))

    for mult_key, ratios in by_key.items():
        if len(ratios) < MIN_CLOSED_SAMPLE:
            continue
        current = multipliers[mult_key]
        suggested = current * _median(ratios)
        suggestion = _save_suggestion(
            key=f"RESALE_MULTIPLIERS.{mult_key}", current_value=current, suggested_value=suggested,
            sample_size=len(ratios),
            evidence=f"{len(ratios)} closed lot(s) priced with multiplier {mult_key!r} closed at a median "
                     f"{_median(ratios):.3f}x their expected sale price.",
        )
        if suggestion:
            created.append(suggestion)
    return created


def _category_fee_suggestions(cfg):
    """CATEGORY_FEES.{category}.min_profit_pct: per category, compare
    closed-lot final prices to expected_sale the other way - if lots are
    closing well below (or above) what the profit margin assumed they
    would, min_profit_pct (the fraction of expected_sale held back as
    required profit) is the fee-side knob that maps onto that gap."""
    created = []
    lots = (
        SourcedLot.objects.filter(is_closed=True, final_price__isnull=False, evaluation__expected_sale__gt=0)
        .select_related("evaluation")
    )
    by_category = defaultdict(list)
    for lot in lots:
        ev = lot.evaluation
        by_category[ev.category].append(float(lot.final_price) / float(ev.expected_sale))

    for category, ratios in by_category.items():
        if len(ratios) < MIN_CLOSED_SAMPLE:
            continue
        fees = {**cfg["FEES"], **cfg["CATEGORY_FEES"].get(category, {})}
        current = fees["min_profit_pct"]
        # Lots closing under expected (ratio < 1) got less margin than
        # planned - current * ratio nudges the required margin down to
        # match what the market actually supported, and vice versa.
        suggested = current * _median(ratios)
        suggestion = _save_suggestion(
            key=f"CATEGORY_FEES.{category}.min_profit_pct", current_value=current, suggested_value=suggested,
            sample_size=len(ratios),
            evidence=f"{len(ratios)} closed {category} lot(s) closed at a median {_median(ratios):.3f}x "
                     f"their expected sale price.",
        )
        if suggestion:
            created.append(suggestion)
    return created


def build_calibration_suggestions(cfg):
    """Runs every comparison the calibrate command makes, returns every
    CalibrationSuggestion actually created (pending-duplicates and
    below-threshold/sample-size comparisons are silently skipped, not
    returned as "created")."""
    return (
        _ledger_multiplier_suggestions(cfg)
        + _closed_lot_multiplier_suggestions(cfg)
        + _category_fee_suggestions(cfg)
    )


# ---------------------------------------------------------------------------
# Part D: AI review accuracy
# ---------------------------------------------------------------------------

def _review_outcome(review):
    """The real-world outcome price for a reviewed lot, preferring what it
    actually sold for in the Ledger (the most authoritative figure, if
    the lot was ever logged there as won) over the auction's own closing
    price. None if there's no outcome yet either way."""
    ledger_entry = review.ledger_entries.exclude(status="written_off").first()
    if ledger_entry is not None:
        realized = ledger_entry.total_realized
        if realized is not None:
            return realized
    lot = review.lot
    if lot.is_closed and lot.final_price is not None:
        return lot.final_price
    return None


def ai_review_accuracy(category=""):
    """For reviewed lots with a real-world outcome: by category, how many,
    what % landed inside the AI's own resale_low/resale_high range, and
    the median error (outcome vs. the range's midpoint, as a % of the
    midpoint)."""
    reviews = (
        AIReview.objects.filter(status="done", resale_low__isnull=False, resale_high__isnull=False)
        .select_related("lot", "lot__evaluation").prefetch_related("ledger_entries")
    )
    if category:
        reviews = reviews.filter(lot__evaluation__category=category)

    by_category = defaultdict(lambda: {"count": 0, "in_range": 0, "errors_pct": []})
    for review in reviews:
        outcome = _review_outcome(review)
        if outcome is None:
            continue
        ev = getattr(review.lot, "evaluation", None)
        cat = ev.category if ev else "unknown"
        stats = by_category[cat]
        stats["count"] += 1
        if review.resale_low <= outcome <= review.resale_high:
            stats["in_range"] += 1
        mid = (review.resale_low + review.resale_high) / 2
        if mid:
            stats["errors_pct"].append(float(outcome - mid) / float(mid) * 100)

    return {
        cat: {
            "count": stats["count"],
            "pct_in_range": round(stats["in_range"] / stats["count"] * 100, 1) if stats["count"] else None,
            "median_error_pct": _median(stats["errors_pct"]),
        }
        for cat, stats in sorted(by_category.items())
    }
