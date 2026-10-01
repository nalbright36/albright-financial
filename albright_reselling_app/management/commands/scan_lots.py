"""Usage:
    python manage.py scan_lots                          # ShopGoodwill, every category
    python manage.py scan_lots --category jewelry        # one category only
    python manage.py scan_lots --no-llm                 # regex only, zero API cost
    python manage.py scan_lots --keyword "morgan dollar"  # one keyword, handy for testing
"""
from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from albright_reselling_app.scanner.alerts import check_stale_source, check_zero_lots, run_alerts, \
    send_critical_alert
from albright_reselling_app.scanner.bid_watch import resolve_watches, send_closing_soon_alerts
from albright_reselling_app.scanner.pipeline import run_scan
from albright_reselling_app.scanner_models import ScanRun


def _format_remaining(delta):
    """"3h 12m" style. Only called with an already-positive delta (candidates
    ending soon are, by construction, still live)."""
    total_seconds = int(delta.total_seconds())
    hours, remainder = divmod(total_seconds, 3600)
    minutes = remainder // 60
    if hours and minutes:
        return f"{hours}h {minutes}m"
    if hours:
        return f"{hours}h"
    return f"{minutes}m"


def _split_by_window(candidates, now, window_hours):
    """Candidates ending within window_hours (sorted soonest-first), and
    everything else. A missing or already-past end_time doesn't count as
    "ending soon" - there's no meaningful countdown to show for it."""
    cutoff = now + timedelta(hours=window_hours)
    ending_soon = sorted(
        (ev for ev in candidates if ev.lot.end_time and now < ev.lot.end_time <= cutoff),
        key=lambda ev: ev.lot.end_time,
    )
    ending_soon_ids = {ev.pk for ev in ending_soon}
    ending_later = [ev for ev in candidates if ev.pk not in ending_soon_ids]
    return ending_soon, ending_later


def _stopped_early(keywords_requested, failed_keywords):
    """True if the scan broke off before attempting every requested keyword.
    With the current pipeline, that only happens when two keywords in a row
    fail with SourceUnavailable (a SourceBlocked stop isn't reflected in
    failed_keywords, so it isn't detected here)."""
    keywords_requested = list(keywords_requested)
    if len(failed_keywords) < 2:
        return False
    second_last, last = failed_keywords[-2], failed_keywords[-1]
    try:
        idx = keywords_requested.index(last)
    except ValueError:
        return False
    if idx == 0 or keywords_requested[idx - 1] != second_last:
        return False  # not actually back-to-back in the requested order
    return idx < len(keywords_requested) - 1  # keywords were left unattempted


class Command(BaseCommand):
    help = "Scan auction sources for lots under max bid, and for games/cards/jewelry leads"

    def add_arguments(self, parser):
        parser.add_argument("--source", default="shopgoodwill")
        parser.add_argument("--category", default=None,
                             help="Limit to one category (coins/jewelry/games/cards). Default: every category.")
        parser.add_argument("--no-llm", action="store_true")
        parser.add_argument("--keyword", action="append", help="Override configured keywords (repeatable)")

    def handle(self, *args, **opts):
        source = opts["source"]

        try:
            check_stale_source(source)  # before this run, so a broken cadence is caught, not masked by it
        except Exception as exc:  # noqa: BLE001 - alerting must never break the scan
            self.stderr.write(self.style.WARNING(f"Stale-source check failed: {exc}"))

        run = ScanRun.objects.create(source=source)
        try:
            result = run_scan(source, opts["category"], use_llm=not opts["no_llm"], keywords=opts["keyword"])
        except Exception as exc:
            run.error = str(exc)
            run.finished_at = timezone.now()
            run.save()
            raise

        failed_keywords = result.get("failed_keywords") or []

        run.finished_at = timezone.now()
        run.lots_seen = result["seen"]
        run.candidates = len(result["candidates"])
        run.leads = len(result["leads"])
        run.llm_calls = result["llm_calls"]
        run.failed_keywords = failed_keywords
        run.stopped_early = _stopped_early(result["keywords_scanned"], failed_keywords)
        run.save()

        spot = ", ".join(f"{k} ${v:,.2f}" for k, v in result["spot"].items())
        self.stdout.write(f"Spot: {spot}")
        self.stdout.write(f"Lots seen: {result['seen']}   LLM calls: {result['llm_calls']}")
        self.stdout.write(f"Candidates: {len(result['candidates'])}")
        self.stdout.write(f"Leads: {len(result['leads'])}")

        if failed_keywords:
            self.stdout.write(f"Failed keywords: {', '.join(failed_keywords)}")

        if run.stopped_early:
            self.stdout.write(
                "Scan stopped early: 2 consecutive keywords failed (site may be down or throttling us)"
            )

        window_hours = settings.RESELLING_SCANNER["CANDIDATE_WINDOW_HOURS"]
        now = timezone.now()
        ending_soon, ending_later = _split_by_window(result["candidates"], now, window_hours)

        if ending_soon:
            self.stdout.write(f"Ending within {window_hours}h:")
            for ev in ending_soon:
                lot = ev.lot
                remaining = _format_remaining(lot.end_time - now)
                self.stdout.write(
                    f"  [{remaining}] ${lot.current_price} now | max ${ev.max_bid} | +${ev.headroom}"
                    f" | {ev.confidence} | {lot.title[:70]}\n    {lot.url}"
                )
        if ending_later:
            self.stdout.write(f"{len(ending_later)} more candidates ending later (bids will likely rise)")

        try:
            if result.get("blocked"):
                send_critical_alert(
                    source, "blocked", "Scan was BLOCKED by the site (403/429) - stopped immediately."
                )
            check_zero_lots(source, result["seen"])
        except Exception as exc:  # noqa: BLE001 - alerting must never break the scan or lose its ScanRun record
            self.stderr.write(self.style.WARNING(f"Critical-alert check failed: {exc}"))

        try:
            alerts_sent = run_alerts()
            if alerts_sent:
                self.stdout.write(f"Alerts sent: {len(alerts_sent)}")
        except Exception as exc:  # noqa: BLE001 - alerting must never break the scan or lose its ScanRun record
            self.stderr.write(self.style.WARNING(f"Alert check failed: {exc}"))

        # BidWatch sweeps are global (every source, every run) - a lot
        # closing on another source still needs checking even if this
        # particular invocation was --source scoped to a different one.
        try:
            closing_sent = send_closing_soon_alerts()
            if closing_sent:
                self.stdout.write(f"Closing-soon alerts sent: {len(closing_sent)}")
        except Exception as exc:  # noqa: BLE001 - alerting must never break the scan
            self.stderr.write(self.style.WARNING(f"Closing-soon alert check failed: {exc}"))

        try:
            resolved = resolve_watches()
            if resolved:
                self.stdout.write(f"Bid watches resolved: {len(resolved)}")
        except Exception as exc:  # noqa: BLE001 - watch resolution must never break the scan
            self.stderr.write(self.style.WARNING(f"Watch resolution failed: {exc}"))
