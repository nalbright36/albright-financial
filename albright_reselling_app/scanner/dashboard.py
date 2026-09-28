"""All data/query logic for the "ShopGoodwill Coin Scanner" section of the
reselling app's dashboard lives here. The dashboard view (views.dashboard)
only calls get_scanner_dashboard_context() and merges the result into its
own context - it does not run any of these queries itself, so this module
can be tested independently of the view/template.

Read-only: nothing here starts a scan. Scans only run from the scheduled
`scan_lots` task.
"""
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from ..scanner_models import LotEvaluation, ScanRun, SpotPrice

STALE_RUN_AFTER = timedelta(hours=2)
CLOSEST_CALLS_LIMIT = 10
RECENT_RUNS_LIMIT = 10


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


def _lot_row(evaluation, now):
    lot = evaluation.lot
    remaining = lot.end_time - now
    return {
        "title": lot.title,
        "lot_url": lot.url,
        "current_bid": lot.current_price,
        "max_bid": evaluation.max_bid,
        "headroom": evaluation.headroom,
        "confidence": evaluation.confidence,
        "coin_keys": evaluation.coin_keys,
        "silver_oz": evaluation.silver_oz,
        "gold_oz": evaluation.gold_oz,
        "time_remaining": _format_duration(remaining) if remaining.total_seconds() > 0 else "Ended",
    }


def _scanner_health(now):
    last_run = ScanRun.objects.first()  # Meta.ordering = ["-started_at"]
    warnings = []

    if last_run is None:
        warnings.append("No scans have run yet - check the scheduled task.")
    else:
        if last_run.error:
            warnings.append(f"Last scan failed: {last_run.error}")
        if last_run.failed_keywords:
            warnings.append(f"Last scan had failed keywords: {', '.join(last_run.failed_keywords)}")
        age = now - last_run.started_at
        if age > STALE_RUN_AFTER:
            warnings.append(f"Last scan was {_format_duration(age)} ago - check the scheduled task.")

    return {
        "last_run": last_run,
        "last_run_started_at": timezone.localtime(last_run.started_at) if last_run else None,
        "warnings": warnings,
    }


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


def get_scanner_dashboard_context():
    now = timezone.now()
    max_age_days = settings.RESELLING_SCANNER["SPOT"]["max_age_days"]

    live_qs = (
        LotEvaluation.objects.select_related("lot")
        .filter(is_candidate=True, lot__end_time__gt=now)
        .order_by("lot__end_time")
    )
    live_candidates = [_lot_row(ev, now) for ev in live_qs]

    closest_qs = (
        LotEvaluation.objects.select_related("lot")
        .filter(is_candidate=False, max_bid__gt=0, lot__end_time__gt=now)
        .order_by("-headroom")[:CLOSEST_CALLS_LIMIT]
    )
    closest_calls = [_lot_row(ev, now) for ev in closest_qs]

    recent_runs = [
        {
            "started_at": timezone.localtime(run.started_at),
            "lots_seen": run.lots_seen,
            "candidates": run.candidates,
            "failed_keywords": run.failed_keywords,
            "has_error": bool(run.error),
        }
        for run in ScanRun.objects.all()[:RECENT_RUNS_LIMIT]
    ]

    health = _scanner_health(now)

    return {
        "scanner_health": health,
        "spot_prices": _spot_price_status(now, max_age_days),
        "live_candidates": live_candidates,
        "closest_calls": closest_calls,
        "recent_runs": recent_runs,
        "last_run_lots_seen": health["last_run"].lots_seen if health["last_run"] else 0,
    }
