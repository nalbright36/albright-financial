"""Usage:
    python manage.py send_roundups                               # make what's due, then exit
    python manage.py send_roundups --loop-minutes 55 --every 5    # check every 5 min for 55 min, then exit

Makes every briefing that's due (albright_roundup.deliver.run_due) and pings its owner (push / email). Safe to
overlap: each roundup is claimed before it's made. Schedule it hourly on PythonAnywhere (Tasks) with
--loop-minutes 55 --every 5, so a 6:00 PM briefing goes out by about 6:05.
"""
import time
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from albright_control.jobs import tracked_job
from albright_roundup import deliver


class Command(BaseCommand):
    help = "Make due briefings (roundups) and ping their owners."

    def add_arguments(self, parser):
        parser.add_argument("--loop-minutes", type=float, default=None,
                            help="Keep checking for this many minutes, then exit (default: once).")
        parser.add_argument("--every", type=float, default=5, help="Minutes between checks while looping (default 5).")

    @tracked_job("send_roundups")   # shows on Admin > System health
    def handle(self, *args, **opts):
        deadline = timezone.now() + timedelta(minutes=opts["loop_minutes"] or 0)
        while True:
            made, pinged = deliver.run_due()
            if made:
                self.stdout.write(f"Made {made} briefing(s); pinged {pinged}.")
            if opts["loop_minutes"] is None or timezone.now() >= deadline:
                break
            time.sleep(opts["every"] * 60)
        if not opts["loop_minutes"]:
            self.stdout.write("Done.")
