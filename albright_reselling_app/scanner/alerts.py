"""Telegram alerting for the scanner.

send_telegram() is the raw HTTP call. Three kinds of alert sit on top of it:
  - run_alerts(): per-lot "worth a look" pushes (candidate/lead/check_by_hand/
    zero_bid/relisted), run once at the end of every scan_lots command.
    candidate/lead/zero_bid/relisted are held during ALERTS["quiet_hours"].
  - send_critical_alert() / check_stale_source() / check_zero_lots(): immediate,
    rate-limited, source-level problem alerts (blocked, zero lots, stale),
    called from scan_lots and alert_digest.
  - send_daily_digest(): one daily summary - spot prices, 24h system health,
    yesterday's results, live counts, and "my bids" status.

Telegram credentials come from the environment (TELEGRAM_BOT_TOKEN,
TELEGRAM_CHAT_ID), not settings.py/RESELLING_SCANNER - like OPENAI_API_KEY,
they're secrets and don't belong in version-controlled config. If either is
missing, send_telegram() logs a warning and does nothing; a broken or
unconfigured bot must never break a scan.
"""
import logging
import os
from datetime import datetime, timedelta
from decimal import Decimal

import requests
from django.conf import settings
from django.db.models import Count, Sum
from django.utils import timezone

from ..scanner_models import (
    AIReview, AlertSent, BidWatch, CriticalAlertSent, LotEvaluation, ScanRun, SourcedLot, SpotPrice,
)
from .ai_review_service import months_review_cost
from .dashboard import _expected_interval_hours, _source_display, _time_remaining

log = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"
REQUEST_TIMEOUT_SECONDS = 15

CRITICAL_ALERT_COOLDOWN = timedelta(hours=6)
STALE_ALERT_GRACE_HOURS = 2  # critical alert fires once a source is this far past its own expected interval
SPOT_CHANGE_WARN_PCT = 3.0
AI_REVIEW_BUDGET_WARN_PCT = 0.8

# "a link to run an AI review on the dashboard" - the scheduled scan_lots
# run (and this module generally) has no request object to build an
# absolute URL from, so the host is read from the environment, falling
# back to the PythonAnywhere deployment this is actually scheduled on.
SITE_BASE_URL = os.environ.get("SITE_BASE_URL", "https://natealbright36.pythonanywhere.com")
DASHBOARD_PATH = "/albright_reselling_app/"


def send_telegram(text):
    """POSTs one message. Never raises - a Telegram outage or a missing
    token must never fail (or stop) a scan; callers get a bool back
    instead so e.g. test_alert can report success/failure to the user."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        log.warning("Telegram alert skipped - TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID not set")
        return False

    try:
        resp = requests.post(
            TELEGRAM_API.format(token=token),
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": False},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
        return True
    except Exception as exc:  # noqa: BLE001 - a Telegram failure must never break a scan
        log.warning("Telegram send failed: %s", exc)
        return False


# First line of the Telegram message for the kinds that need a headline
# the others don't - candidate/lead/check_by_hand are self-explanatory
# from the category + bid lines that follow, these two aren't.
KIND_HEADLINES = {"zero_bid": "No bids yet", "relisted": "Relisted"}


def _format_message(ev, kind):
    lot = ev.lot
    lines = []
    if kind in KIND_HEADLINES:
        lines.append(f"<b>{KIND_HEADLINES[kind]}</b>")
    lines.append(f"<b>{(ev.category or 'lot').title()}</b> · {_source_display(lot.source)}")
    lines.append(lot.title[:120])
    lines.append(f"Bid: ${lot.current_price:.2f}")
    # A lead has no valuation (max_bid/headroom are always 0 - see
    # pipeline._apply_lead), so show why it's a lead instead - true for
    # kind="lead" itself, and for a zero_bid/relisted lot that qualified
    # via the "or it's a lead" branch rather than an affordable max bid.
    if ev.is_lead and not (ev.max_bid and ev.max_bid > 0):
        lines.append(f"Lead: {ev.lead_reason or '—'}")
    else:
        lines.append(f"Max bid: ${ev.max_bid:.2f} (headroom +${ev.headroom:.2f})")
    if lot.end_time:
        lines.append(f"Ends in: {_time_remaining(lot, timezone.now())}")
    pickup = (lot.raw or {}).get("_pickup") or {}
    if pickup.get("city"):
        miles = pickup.get("distance_miles")
        lines.append(f"Pickup: {pickup['city']}" + (f" · {miles}mi" if miles is not None else ""))
    lines.append(f'<a href="{lot.url}">View listing</a>')
    lines.append(f'<a href="{SITE_BASE_URL}{DASHBOARD_PATH}">Run AI review on dashboard</a>')
    return "\n".join(lines)


def _already_alerted_lot_ids(kind):
    return set(AlertSent.objects.filter(kind=kind).values_list("lot_id", flat=True))


def _candidates_to_consider(now, cutoff, cfg):
    qs = (
        LotEvaluation.objects.select_related("lot")
        .filter(is_candidate=True, confidence__in=cfg.get("candidate_confidence", []),
                headroom__gte=cfg.get("min_headroom", 0), lot__end_time__gt=now, lot__end_time__lte=cutoff)
        .exclude(lot_id__in=_already_alerted_lot_ids("candidate"))
    )
    return [("candidate", ev) for ev in qs]


def _leads_to_consider(now, cutoff):
    qs = (
        LotEvaluation.objects.select_related("lot")
        .filter(is_lead=True, lot__end_time__gt=now, lot__end_time__lte=cutoff)
        .exclude(lot_id__in=_already_alerted_lot_ids("lead"))
    )
    return [("lead", ev) for ev in qs]


def _check_by_hand_to_consider(now, cutoff):
    qs = (
        LotEvaluation.objects.select_related("lot")
        .filter(confidence="low", max_bid__gt=0, headroom__gte=0, lot__end_time__gt=now, lot__end_time__lte=cutoff)
        .exclude(lot_id__in=_already_alerted_lot_ids("check_by_hand"))
    )
    return [("check_by_hand", ev) for ev in qs]


def _zero_bid_to_consider(now, cutoff):
    """A live, priced lot nobody's bid on yet, worth flagging before it
    closes with no competition: not "none" category, bid_count exactly 0
    (not unknown/null), ending within the window, and either affordable
    (a priced max bid at or above the current bid) or a lead (no price to
    check against, but still worth a look at zero bids)."""
    qs = (
        LotEvaluation.objects.select_related("lot")
        .exclude(category="none")
        .filter(lot__bid_count=0, lot__end_time__gt=now, lot__end_time__lte=cutoff)
        .exclude(lot_id__in=_already_alerted_lot_ids("zero_bid"))
    )
    qualifying = []
    for ev in qs:
        affordable = ev.max_bid > 0 and ev.lot.current_price <= ev.max_bid
        if affordable or ev.is_lead:
            qualifying.append(("zero_bid", ev))
    return qualifying


def _relisted_to_consider(now, cutoff):
    """A live ShopGoodwill lot the seller has relisted (scanner/features.py's
    is_relisted, from a positive "relistId" on the raw item) - not "none"
    category, ending within the window, and either current bid <= max bid
    or a lead. Checked in Python (not a JSONField query) since features is
    a loosely-shaped JSONField and this keeps the same style as the
    affordability check above."""
    qs = (
        LotEvaluation.objects.select_related("lot")
        .exclude(category="none")
        .filter(lot__source="shopgoodwill", lot__end_time__gt=now, lot__end_time__lte=cutoff)
        .exclude(lot_id__in=_already_alerted_lot_ids("relisted"))
    )
    qualifying = []
    for ev in qs:
        if not (ev.lot.features or {}).get("is_relisted"):
            continue
        if ev.lot.current_price <= ev.max_bid or ev.is_lead:
            qualifying.append(("relisted", ev))
    return qualifying


# Kinds held back during quiet hours (not sent, not marked AlertSent, so
# they're reconsidered - and can still go out - on the next run_alerts()
# call once quiet hours end). check_by_hand is deliberately not included -
# BidWatch alerts (scanner/bid_watch.py) and critical alerts
# (send_critical_alert) never go through run_alerts() at all, so they're
# unaffected by this regardless.
QUIET_HOURS_GATED_KINDS = {"candidate", "lead", "zero_bid", "relisted"}


def _in_quiet_hours(now, cfg):
    quiet = cfg.get("quiet_hours")
    if not quiet:
        return False
    start_hour, end_hour = quiet
    local_hour = timezone.localtime(now).hour
    if start_hour <= end_hour:
        return start_hour <= local_hour < end_hour
    return local_hour >= start_hour or local_hour < end_hour  # wraps past midnight, e.g. 23 -> 7


def run_alerts(now=None):
    """Called once at the end of every scan_lots run. Evaluates every kind
    listed in ALERTS["kinds"] against the whole live table - not just lots
    touched by this particular run, since a category-scoped scan shouldn't
    silently skip alerting on a candidate an earlier run already evaluated
    - sends the soonest-ending qualifying lots first up to max_per_run
    (a cap on this run's total sends, across all kinds combined), and
    records each send in AlertSent so it's never repeated for that kind.
    During ALERTS["quiet_hours"], candidate/lead/zero_bid/relisted are held
    back entirely (not sent, not recorded) rather than sent silently or
    dropped for good - see QUIET_HOURS_GATED_KINDS. Returns the list of
    (lot_id, kind) pairs actually sent."""
    cfg = settings.RESELLING_SCANNER.get("ALERTS", {})
    if not cfg.get("enabled"):
        return []

    now = now or timezone.now()
    cutoff = now + timedelta(minutes=cfg.get("window_minutes", 90))
    kinds = cfg.get("kinds", [])

    candidates = []
    if "candidate" in kinds:
        candidates += _candidates_to_consider(now, cutoff, cfg)
    if "lead" in kinds:
        candidates += _leads_to_consider(now, cutoff)
    if "check_by_hand" in kinds:
        candidates += _check_by_hand_to_consider(now, cutoff)
    if "zero_bid" in kinds:
        candidates += _zero_bid_to_consider(now, cutoff)
    if "relisted" in kinds:
        candidates += _relisted_to_consider(now, cutoff)

    if _in_quiet_hours(now, cfg):
        candidates = [pair for pair in candidates if pair[0] not in QUIET_HOURS_GATED_KINDS]

    candidates.sort(key=lambda pair: pair[1].lot.end_time)
    to_send = candidates[: cfg.get("max_per_run", 10)]

    sent = []
    for kind, ev in to_send:
        send_telegram(_format_message(ev, kind))
        AlertSent.objects.create(lot=ev.lot, kind=kind)
        sent.append((ev.lot_id, kind))
    return sent


# ---------------------------------------------------------------------------
# Immediate critical alerts: scan blocked, zero lots, stale source
# ---------------------------------------------------------------------------

def send_critical_alert(source, alert_type, message, now=None):
    """Immediate, out-of-band alert for a source-level problem - distinct
    from the per-lot alerts above, and gated by its own setting
    (ALERTS["critical_alerts"]). Rate-limited to one per source+alert_type
    every CRITICAL_ALERT_COOLDOWN via CriticalAlertSent, recorded
    regardless of whether the send itself succeeds - so a Telegram outage
    doesn't turn into a retry-every-run spam burst once it recovers."""
    cfg = settings.RESELLING_SCANNER.get("ALERTS", {})
    if not cfg.get("critical_alerts"):
        return False

    now = now or timezone.now()
    cutoff = now - CRITICAL_ALERT_COOLDOWN
    if CriticalAlertSent.objects.filter(source=source, alert_type=alert_type, sent_at__gte=cutoff).exists():
        return False

    sent = send_telegram(f"<b>CRITICAL · {_source_display(source)}</b>\n{message}")
    CriticalAlertSent.objects.create(source=source, alert_type=alert_type)
    return sent


def check_zero_lots(source, seen, now=None):
    """Call right after a scan_lots run finishes, with that run's seen
    count."""
    if seen > 0:
        return False
    return send_critical_alert(
        source, "zero_lots", "Scan saw 0 lots - the site may have changed its data format.", now=now,
    )


def check_stale_source(source, now=None):
    """"No scan has completed for a source in its expected_interval_hours
    (RESELLING_SCANNER["SOURCES"][source], default 1) plus a grace period"
    - called at the start of every scan_lots run (catching a broken cadence
    before the next scan even starts) and from alert_digest (a backstop in
    case scan_lots has stopped running on its own schedule entirely, in
    which case the start-of-run check never fires at all)."""
    now = now or timezone.now()
    last_finished = (
        ScanRun.objects.filter(source=source, finished_at__isnull=False).order_by("-finished_at").first()
    )
    if last_finished is None:
        return False  # never completed a run at all - a different, pre-existing problem
    stale_after = timedelta(hours=_expected_interval_hours(source) + STALE_ALERT_GRACE_HOURS)
    if now - last_finished.finished_at > stale_after:
        hours = stale_after.total_seconds() / 3600
        return send_critical_alert(
            source, "stale", f"No completed scan in over {hours:.0f}h - check the scheduled task.", now=now,
        )
    return False


# ---------------------------------------------------------------------------
# Daily digest (management/commands/alert_digest.py)
# ---------------------------------------------------------------------------

def _yesterday_range(now):
    today = timezone.localdate(now)
    yesterday_start = timezone.make_aware(datetime.combine(today - timedelta(days=1), datetime.min.time()))
    today_start = timezone.make_aware(datetime.combine(today, datetime.min.time()))
    return yesterday_start, today_start


def _yesterday_results(now):
    """{(source, category): {"under": n, "over": n}} for lots that closed
    yesterday with a max bid on record - mirrors track_closed's own
    under/over summary, just grouped by source+category and date-scoped."""
    start, end = _yesterday_range(now)
    lots = (
        SourcedLot.objects.filter(is_closed=True, final_price__isnull=False, end_time__gte=start, end_time__lt=end)
        .select_related("evaluation")
    )
    by_key = {}
    for lot in lots:
        ev = getattr(lot, "evaluation", None)
        if ev is None or not ev.max_bid:
            continue
        stats = by_key.setdefault((lot.source, ev.category), {"under": 0, "over": 0})
        stats["under" if lot.final_price <= ev.max_bid else "over"] += 1
    return by_key


def _today_live_counts(now):
    return (
        LotEvaluation.objects.filter(is_candidate=True, lot__end_time__gt=now).count(),
        LotEvaluation.objects.filter(is_lead=True, lot__end_time__gt=now).count(),
    )


def _my_bids_counts(now):
    """"My bids": real BidWatch rows (created from the "I bid on this"
    button), not scanner candidates - watching is a live snapshot count;
    likely_won/lost are windowed by resolved_at, since a watch can stay
    "watching" for a while after its lot closes (resolution needs a
    search_closed() lookup, see scanner/bid_watch.py)."""
    cutoff = now - timedelta(hours=24)
    watching = BidWatch.objects.filter(status="watching").count()
    likely_won = BidWatch.objects.filter(status="likely_won", resolved_at__gte=cutoff).count()
    lost = BidWatch.objects.filter(status="lost", resolved_at__gte=cutoff).count()
    return watching, likely_won, lost


def _spot_section(now):
    """Today's silver/gold price, change vs. yesterday ($ and %), and a
    warning if either moved more than SPOT_CHANGE_WARN_PCT."""
    today = timezone.localdate(now)
    lines, warnings = [], []

    for metal in ("silver", "gold"):
        today_row = SpotPrice.objects.filter(metal=metal, fetched_at__date=today).order_by("-fetched_at").first()
        if today_row is None:
            lines.append(f"{metal.title()}: no price fetched today")
            warnings.append(f"{metal.title()} spot price not fetched today")
            continue
        if today_row.source == "goldapi_failed":
            lines.append(f"{metal.title()}: fetch FAILED today")
            warnings.append(f"{metal.title()} spot price fetch failed today")
            continue

        prior_row = (
            SpotPrice.objects.filter(metal=metal, source="goldapi", fetched_at__date__lt=today)
            .order_by("-fetched_at").first()
        )
        if prior_row is None:
            lines.append(f"{metal.title()}: ${today_row.price_usd:.2f} (no prior price to compare)")
            continue

        delta = today_row.price_usd - prior_row.price_usd
        pct = float(delta / prior_row.price_usd) * 100 if prior_row.price_usd else 0.0
        sign = "+" if delta >= 0 else ""
        lines.append(f"{metal.title()}: ${today_row.price_usd:.2f} ({sign}{delta:.2f}, {sign}{pct:.1f}%)")
        if abs(pct) > SPOT_CHANGE_WARN_PCT:
            warnings.append(f"{metal.title()} moved {sign}{pct:.1f}% vs. yesterday - max bids have shifted")

    return lines, warnings


def _source_health_lines(source, now):
    cutoff = now - timedelta(hours=24)
    runs = list(ScanRun.objects.filter(source=source, started_at__gte=cutoff))
    lines, warnings = [], []
    label = _source_display(source)
    expected_runs = 24 / _expected_interval_hours(source)

    lines.append(f"{label}: {len(runs)} run(s) in last 24h (expect ~{expected_runs:g})")

    last_run = ScanRun.objects.filter(source=source).order_by("-started_at").first()
    if last_run:
        lines.append(f"  Last run: {timezone.localtime(last_run.started_at).strftime('%b %d, %I:%M %p')}")
    else:
        lines.append("  Last run: never")
        warnings.append(f"{label} has never run a scan")

    lines.append(f"  Lots seen (24h): {sum(r.lots_seen for r in runs)}")

    error_runs = [r for r in runs if r.error]
    if error_runs:
        lines.append(f"  Runs with errors: {len(error_runs)}")
        warnings.append(f"{label}: {len(error_runs)} run(s) with errors in the last 24h")

    failed_keywords = sorted({kw for r in runs for kw in (r.failed_keywords or [])})
    if failed_keywords:
        lines.append(f"  Failed keywords: {', '.join(failed_keywords)}")
        warnings.append(f"{label}: failed keywords in last 24h: {', '.join(failed_keywords)}")

    zero_lot_runs = [r for r in runs if r.lots_seen == 0]
    if zero_lot_runs:
        warnings.append(
            f"{label}: {len(zero_lot_runs)} run(s) saw 0 lots - the site may have changed its data format."
        )

    return lines, warnings


def _spot_fetch_health_lines(now):
    cutoff = now - timedelta(hours=24)
    failed_24h = SpotPrice.objects.filter(source="goldapi_failed", fetched_at__gte=cutoff).count()
    month_start = timezone.localdate(now).replace(day=1)
    attempts_this_month = SpotPrice.objects.filter(
        source__in=["goldapi", "goldapi_failed"], fetched_at__date__gte=month_start,
    ).count()
    limit = settings.RESELLING_SCANNER["SPOT"]["monthly_api_limit"]

    lines = [f"Spot fetch: {attempts_this_month}/{limit} API calls this month"]
    warnings = []
    if failed_24h:
        lines.append(f"  Failed fetches (24h): {failed_24h}")
        warnings.append(f"Spot price fetch failed {failed_24h} time(s) in the last 24h")
    if attempts_this_month >= limit:
        warnings.append(f"Spot price API monthly limit reached ({attempts_this_month}/{limit})")
    return lines, warnings


def _track_closed_health_lines(now):
    cutoff = now - timedelta(hours=24)
    updated = SourcedLot.objects.filter(final_checked_at__gte=cutoff).count()
    lines = [f"track_closed: {updated} lot(s) updated in last 24h"]
    warnings = []
    if updated == 0:
        warnings.append("track_closed: no lots updated in the last 24h - it may not have run")
    return lines, warnings


def _ai_review_health_lines(now):
    today = timezone.localdate(now)
    cfg = settings.RESELLING_SCANNER["AI_REVIEW"]
    today_count = AIReview.objects.filter(created_at__date=today).count()
    today_cost = AIReview.objects.filter(created_at__date=today).aggregate(t=Sum("cost_usd"))["t"] or Decimal("0")
    month_cost = months_review_cost(today)
    budget = Decimal(str(cfg["monthly_budget_usd"]))

    lines = [f"AI reviews: {today_count} today (${today_cost:.2f}) · ${month_cost:.2f} of ${budget:.2f} "
             f"this month"]
    warnings = []
    if budget and month_cost >= budget * Decimal(str(AI_REVIEW_BUDGET_WARN_PCT)):
        pct = float(month_cost / budget) * 100
        warnings.append(f"AI review spend at {pct:.0f}% of monthly budget (${month_cost:.2f} of ${budget:.2f})")
    return lines, warnings


def _alerts_sent_lines(now):
    cutoff = now - timedelta(hours=24)
    counts = {
        row["kind"]: row["n"]
        for row in AlertSent.objects.filter(sent_at__gte=cutoff).values("kind").annotate(n=Count("kind"))
    }
    if not counts:
        return ["Alerts sent (24h): none"]
    return ["Alerts sent (24h): " + ", ".join(f"{kind}: {n}" for kind, n in sorted(counts.items()))]


def build_digest_message(now=None):
    now = now or timezone.now()
    cfg = settings.RESELLING_SCANNER
    sources = list(cfg["SOURCES"].keys())

    warnings = []

    spot_lines, spot_warnings = _spot_section(now)
    warnings += spot_warnings

    health_lines = []
    for source in sources:
        lines, source_warnings = _source_health_lines(source, now)
        health_lines += lines
        warnings += source_warnings
    for section_fn in (_spot_fetch_health_lines, _track_closed_health_lines, _ai_review_health_lines):
        lines, section_warnings = section_fn(now)
        health_lines += lines
        warnings += section_warnings
    health_lines += _alerts_sent_lines(now)

    results = _yesterday_results(now)
    candidate_count, lead_count = _today_live_counts(now)
    watching, likely_won, lost = _my_bids_counts(now)

    lines = ["<b>Scanner Daily Digest</b>", ""]

    if warnings:
        lines.append("<b>⚠ Needs attention</b>")
        lines += [f"• {w}" for w in warnings]
    else:
        lines.append("✅ All systems OK")
    lines.append("")

    lines.append("<b>Spot Prices</b>")
    lines += spot_lines
    lines.append("")

    lines.append("<b>System Health (24h)</b>")
    lines += health_lines
    lines.append("")

    lines.append("<b>Yesterday's Results</b>")
    if results:
        for (source, category), stats in sorted(results.items()):
            lines.append(f"  {_source_display(source)} {category}: {stats['under']} under max, "
                         f"{stats['over']} over max")
    else:
        lines.append("  No lots closed with a max bid yesterday.")
    lines.append("")

    lines.append(f"Live now: {candidate_count} candidate(s), {lead_count} lead(s)")
    lines.append(f"My bids: {watching} watching, {likely_won} likely won, {lost} lost (last 24h)")

    return "\n".join(lines)


def send_daily_digest(now=None):
    now = now or timezone.now()
    cfg = settings.RESELLING_SCANNER
    for source in cfg["SOURCES"].keys():
        check_stale_source(source, now=now)

    message = build_digest_message(now=now)
    send_telegram(message)
    return message
