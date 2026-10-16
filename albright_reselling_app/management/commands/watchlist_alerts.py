"""Usage:
    python manage.py watchlist_alerts                               # one check, then exit
    python manage.py watchlist_alerts --loop-minutes 55 --every 5    # check every 5 min for 55 min, then exit

Sends any due watchlist "closing soon" alerts (see
scanner.alerts.send_watchlist_alerts()) - the only place that alert comes
from; scan_lots no longer sends it (see that command for why). Reads the
database only - never makes a request to any auction site - so it's safe
to run far more often than the hourly scans (a 60-minute closing window
needs checking more than once an hour) and safe to overlap with a
scan_lots run or another invocation of this same command: AlertSent's
unique_together on (lot, kind) is the real dedup guarantee (see
send_watchlist_alerts()), so two overlapping checks can never double-send.

Meant to be scheduled frequently - e.g. every 5 minutes via cron, or (if
the scheduler only grants one long-running slot, as on PythonAnywhere's
free tier) a single scheduled run of --loop-minutes 55 --every 5 once an
hour, so it's still checking between the hourly scan_lots runs.
"""
import time
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from albright_reselling_app.scanner.alerts import send_watchlist_alerts


class Command(BaseCommand):
    help = "Send any due watchlist closing-soon alerts (database-only, safe to run frequently or overlap)."

    def add_arguments(self, parser):
        parser.add_argument("--loop-minutes", type=float, default=None,
                             help="Keep checking for this many minutes total, then exit (default: check once).")
        parser.add_argument("--every", type=float, default=5,
                             help="Minutes between checks while looping (default 5).")

    def handle(self, *args, **opts):
        loop_minutes = opts["loop_minutes"]
        every_minutes = opts["every"]

        if loop_minutes is None:
            self._check_once()
            return

        deadline = timezone.now() + timedelta(minutes=loop_minutes)
        while True:
            self._check_once()
            if timezone.now() >= deadline:
                break
            time.sleep(every_minutes * 60)

    def _check_once(self):
        sent = send_watchlist_alerts()
        if sent:
            self.stdout.write(f"Watchlist alerts sent: {len(sent)}")
        else:
            self.stdout.write("No watchlist alerts due.")
