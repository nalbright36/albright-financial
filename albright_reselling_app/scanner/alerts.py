"""Telegram alerting for the scanner.

send_telegram() is the raw HTTP call; everything else here decides which
live lots are worth a push notification and sends them, run once at the
end of every scan_lots command (see management/commands/scan_lots.py) plus
the separately-scheduled alert_digest command.

Telegram credentials come from the environment (TELEGRAM_BOT_TOKEN,
TELEGRAM_CHAT_ID), not settings.py/RESELLING_SCANNER - like OPENAI_API_KEY,
they're secrets and don't belong in version-controlled config. If either is
missing, send_telegram() logs a warning and does nothing; a broken or
unconfigured bot must never break a scan.
"""
import logging
import os
from datetime import datetime, timedelta

import requests
from django.conf import settings
from django.utils import timezone

from ..scanner_models import AlertSent, LotEvaluation, SourcedLot
from .dashboard import _source_display, _time_remaining

log = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"
REQUEST_TIMEOUT_SECONDS = 15

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


def _format_message(ev, kind):
    lot = ev.lot
    lines = [f"<b>{(ev.category or 'lot').title()}</b> · {_source_display(lot.source)}", lot.title[:120]]
    lines.append(f"Bid: ${lot.current_price:.2f}")
    if kind == "lead":
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


def run_alerts(now=None):
    """Called once at the end of every scan_lots run. Evaluates every kind
    listed in ALERTS["kinds"] against the whole live table - not just lots
    touched by this particular run, since a category-scoped scan shouldn't
    silently skip alerting on a candidate an earlier run already evaluated
    - sends the soonest-ending qualifying lots first up to max_per_run
    (a cap on this run's total sends, across all kinds combined), and
    records each send in AlertSent so it's never repeated for that kind.
    Returns the list of (lot_id, kind) pairs actually sent."""
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

    candidates.sort(key=lambda pair: pair[1].lot.end_time)
    to_send = candidates[: cfg.get("max_per_run", 10)]

    sent = []
    for kind, ev in to_send:
        send_telegram(_format_message(ev, kind))
        AlertSent.objects.create(lot=ev.lot, kind=kind)
        sent.append((ev.lot_id, kind))
    return sent


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


def build_digest_message(now=None):
    now = now or timezone.now()
    results = _yesterday_results(now)
    candidate_count, lead_count = _today_live_counts(now)

    lines = ["<b>Scanner Daily Digest</b>", "Yesterday's results:"]
    if results:
        for (source, category), stats in sorted(results.items()):
            lines.append(f"  {_source_display(source)} {category}: {stats['under']} under max, {stats['over']} over max")
    else:
        lines.append("  No lots closed with a max bid yesterday.")
    lines.append(f"Live now: {candidate_count} candidate(s), {lead_count} lead(s)")
    return "\n".join(lines)


def send_daily_digest(now=None):
    message = build_digest_message(now)
    send_telegram(message)
    return message
