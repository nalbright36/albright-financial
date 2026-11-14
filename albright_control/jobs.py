"""Scheduled jobs: recording their runs (tracked_job) and the System
health table (health_rows).

tracked_job wraps a management command's handle(): a JobRun row is opened
when it starts and closed with ok / the error when it ends. Recording can
never break a job - if the database write fails, the job still runs.

Status per job, from its expected interval:
  failing - the most recent finished run failed
  late    - no successful run within interval x LATE_FACTOR (or never run)
  ok      - otherwise
"""
import functools
import logging
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from .models import JobRun, utc_now

log = logging.getLogger(__name__)
LATE_FACTOR = 1.5


def tracked_job(name):
    """Decorator for a command's handle(self, *args, **opts). name is a
    string, or a callable(opts) -> string (e.g. per --source). A run with
    --dry-run isn't recorded (it isn't the scheduled job doing its work)."""
    def decorate(handle):
        @functools.wraps(handle)
        def wrapper(self, *args, **opts):
            if opts.get("dry_run"):
                return handle(self, *args, **opts)
            job = name(opts) if callable(name) else name
            try:
                run = JobRun.objects.create(job=job)
            except Exception:  # noqa: BLE001 - never let bookkeeping stop the job
                log.exception("Couldn't record the start of %s", job)
                run = None
            try:
                result = handle(self, *args, **opts)
            except BaseException as exc:
                _finish(run, False, f"{type(exc).__name__}: {exc}")
                raise
            _finish(run, True, "")
            return result
        return wrapper
    return decorate


def _finish(run, ok, error):
    if run is None:
        return
    try:
        JobRun.objects.filter(pk=run.pk).update(ok=ok, error=error[:4000], finished_at=utc_now())
    except Exception:  # noqa: BLE001
        log.exception("Couldn't record the end of %s", run.job)


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

def job_status(last_finished_ok, last_success_at, interval, now=None):
    """'failing' | 'late' | 'ok'. last_finished_ok is the most recent
    finished run's outcome (None if it never finished a run)."""
    now = now or timezone.now()
    if last_finished_ok is False:
        return "failing"
    if last_success_at is None or now - last_success_at > interval * LATE_FACTOR:
        return "late"
    return "ok"


def interval_label(interval):
    hours = interval.total_seconds() / 3600
    if hours < 24 or hours % 24:
        return f"{hours:g} hour{'s' if hours != 1 else ''}"
    days = int(hours // 24)
    return f"{days} day{'s' if days != 1 else ''}"


def summarize(job, label, interval, events, running_since=None, note="", now=None):
    """One health row from a job's history. events: (time, ok, error)
    tuples for finished runs, from JobRun and/or whatever the app already
    recorded (ScanRun, final_checked_at, SpotPrice...)."""
    events = sorted((e for e in events if e[0] is not None), key=lambda e: e[0], reverse=True)
    last_finished_ok = events[0][1] if events else None
    last_success = next((t for t, ok, _err in events if ok), None)
    last_error = next((err for _t, ok, err in events if not ok), "")
    last_run = max(filter(None, [events[0][0] if events else None, running_since]), default=None)
    return {"job": job, "label": label, "interval": interval, "interval_label": interval_label(interval),
            "last_run": last_run, "last_success": last_success,
            "last_error": last_error, "running": running_since is not None and (not events or running_since > events[0][0]),
            "status": job_status(last_finished_ok, last_success, interval, now), "note": note}


def job_run_events(queryset):
    """(events, running_since) from JobRun rows (newest 50)."""
    runs = list(queryset.order_by("-started_at")[:50])
    events = [(r.started_at, r.ok, r.error) for r in runs if r.ok is not None]
    running = next((r.started_at for r in runs if r.ok is None), None)
    return events, running


INTERVALS = {   # expected hours between runs; settings.CONTROL_JOB_INTERVALS_HOURS can override any of these
    "watchlist_alerts": 1, "track_closed": 24, "fetch_spot_prices": 24, "alert_digest": 24,
    "calibrate": 24 * 7, "prune_lots": 24 * 7, "prune_resume_chats": 24, "prune_activity": 24,
    "send_activity_digest": 24,
}


def interval_for(job):
    return timedelta(hours={**INTERVALS, **getattr(settings, "CONTROL_JOB_INTERVALS_HOURS", {})}.get(job, 24))


def health_rows(now=None):
    from albright_reselling_app.scanner.dashboard import _expected_interval_hours, _source_display
    from albright_reselling_app.scanner_models import ScanRun, SourcedLot, SpotPrice

    now = now or timezone.now()
    rows = []
    # Reseller scans, one row per configured source (ScanRun is their own history).
    for source in settings.RESELLING_SCANNER["SOURCES"]:
        events, running = job_run_events(JobRun.objects.filter(job=f"scan_lots:{source}"))
        events += [(r.started_at, not r.error, r.error) for r in ScanRun.objects.filter(source=source)
                   .exclude(finished_at=None).order_by("-started_at")[:50]]
        rows.append(summarize(f"scan_lots:{source}", f"Reseller scan - {_source_display(source)}",
                              timedelta(hours=_expected_interval_hours(source)), events, running, now=now))
    # watchlist_alerts, alert_digest, calibrate, prune_lots: JobRun only.
    # track_closed / fetch_spot_prices: plus what they already leave behind.
    for job, label in (("watchlist_alerts", "Watchlist alerts"), ("track_closed", "Track closed lots"),
                       ("fetch_spot_prices", "Fetch spot prices"), ("alert_digest", "Daily digest"),
                       ("calibrate", "Calibrate max bids"), ("prune_lots", "Prune old lots"),
                       ("prune_resume_chats", "Prune resume chats"), ("prune_activity", "Prune old activity"),
                       ("send_activity_digest", "Activity digest emails")):
        events, running = job_run_events(JobRun.objects.filter(job__startswith=job))
        if job == "track_closed":
            last = SourcedLot.objects.exclude(final_checked_at=None).order_by("-final_checked_at").values_list(
                "final_checked_at", flat=True).first()
            events.append((last, True, ""))
        elif job == "fetch_spot_prices":
            events.append((SpotPrice.objects.order_by("-fetched_at").values_list("fetched_at", flat=True).first(), True, ""))
        rows.append(summarize(job, label, interval_for(job), events, running, now=now))
    return rows


