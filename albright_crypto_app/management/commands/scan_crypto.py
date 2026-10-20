"""Usage:
    python manage.py scan_crypto                         # every active GitHub/RSS source
    python manage.py scan_crypto --no-ai                  # collect + safety/score only, zero AI cost
    python manage.py scan_crypto --min-interval-hours 4   # skip if the last good run was <4h ago

Collects new opportunities from every active source, extracts facts with
AI (within ai_daily_limit and the shared monthly AI budget), runs the
safety check and scoring, then sends new-opportunity alerts and 24h
deadline reminders. Read-only toward every outside site (HTTP GET only).
"""
from django.core.management.base import BaseCommand
from django.utils import timezone

from albright_crypto_app.alerts import send_deadline_reminders, send_new_opportunity_alerts
from albright_crypto_app.models import ScanRun
from albright_crypto_app.pipeline import scan_sources, sync_sources


def last_good_run():
    return ScanRun.objects.filter(finished_at__isnull=False, errors=[]).order_by("-started_at").first()


class Command(BaseCommand):
    help = "Scan crypto opportunity sources (GitHub bounties, RSS feeds), score them, and alert."

    def add_arguments(self, parser):
        parser.add_argument("--no-ai", action="store_true", help="Skip AI extraction (no API cost).")
        parser.add_argument("--min-interval-hours", type=float, default=None,
                            help="Skip this run if the last successful scan started less than this many hours ago.")

    def handle(self, *args, **opts):
        min_interval = opts["min_interval_hours"]
        if min_interval is not None:
            last = last_good_run()
            if last is not None:
                hours_since = (timezone.now() - last.started_at).total_seconds() / 3600
                if hours_since < min_interval:
                    self.stdout.write(f"Skipped: last crypto scan was {hours_since:.1f} hours ago "
                                      f"(minimum {min_interval:g})")
                    return

        sync_sources()
        run = ScanRun.objects.create()
        try:
            new, errors = scan_sources(use_ai=not opts["no_ai"])
        except Exception as exc:
            run.errors = [str(exc)]
            run.finished_at = timezone.now()
            run.save()
            raise
        run.finished_at = timezone.now()
        run.new_opportunities = len(new)
        run.errors = errors
        from albright_crypto_app.models import Source
        run.sources_checked = Source.objects.filter(active=True).exclude(kind="manual").count()
        run.save()

        alerts = send_new_opportunity_alerts(new)
        reminders = send_deadline_reminders()
        blocked = sum(1 for o in new if o.safety_verdict == "block")
        self.stdout.write(f"New opportunities: {len(new)} ({blocked} blocked)   Alerts: {len(alerts)}   "
                          f"Deadline reminders: {len(reminders)}")
        for error in errors:
            self.stdout.write(self.style.WARNING(error))
