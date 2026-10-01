"""Usage:
    python manage.py alert_digest

Sends one Telegram message summarizing yesterday's closed-lot results
(under/over max bid, per source+category) and today's live candidate/lead
counts. Meant to be scheduled once a day (e.g. on PythonAnywhere) -
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
