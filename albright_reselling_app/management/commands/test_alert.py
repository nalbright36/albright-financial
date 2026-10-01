"""Usage:
    python manage.py test_alert

Sends a single test message via Telegram so you can confirm
TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID are set correctly end to end.
"""
from django.core.management.base import BaseCommand

from albright_reselling_app.scanner.alerts import send_telegram


class Command(BaseCommand):
    help = "Send a test Telegram message to confirm the alert setup works."

    def handle(self, *args, **opts):
        if send_telegram("Scanner alerts are working"):
            self.stdout.write(self.style.SUCCESS("Test alert sent."))
        else:
            self.stdout.write(self.style.WARNING(
                "Test alert NOT sent - check TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID and the logs."
            ))
