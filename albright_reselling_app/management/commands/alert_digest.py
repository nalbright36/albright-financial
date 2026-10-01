"""Usage:
    python manage.py alert_digest

Sends one Telegram message: any problems needing attention (spot price
fetch failures or big moves, scan health, budget warnings) up top, then
spot prices, 24h system health per source, yesterday's closed-lot results,
today's live candidate/lead counts, and "my bids" status. Also re-checks
each source for a stale scan cadence (see scanner.alerts.check_stale_source)
as a backstop in case the hourly scan_lots task has stopped running
entirely. Meant to be scheduled once a day (e.g. on PythonAnywhere),
separately from the hourly scan_lots alert pass. See scanner/alerts.py for
the message-building logic, which is tested without hitting Telegram.
"""
from django.core.management.base import BaseCommand

from albright_reselling_app.scanner.alerts import send_daily_digest


class Command(BaseCommand):
    help = "Send the daily Telegram digest of yesterday's results and today's live counts."

    def handle(self, *args, **opts):
        message = send_daily_digest()
        self.stdout.write(message)
