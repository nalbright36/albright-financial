"""Usage:
    python manage.py send_activity_digest

The daily email of unread activity (albright_labs.activity.send_digests) for everyone who turned it on in
My account > Notifications - only when there's something new since their last one, and never during their quiet
hours. Uses the site's email setup (albright_labs.emails). Run once a day (PythonAnywhere Tasks tab), e.g. 7:30 AM.
"""
from django.core.management.base import BaseCommand

from albright_control.jobs import tracked_job
from albright_labs import activity


class Command(BaseCommand):
    help = "Email each opted-in person a summary of their unread activity."

    @tracked_job("send_activity_digest")   # shows on Admin > System health
    def handle(self, *args, **opts):
        sent, skipped = activity.send_digests()
        self.stdout.write(f"Sent {sent} digest(s); {skipped} skipped (nothing new, quiet hours, no email, or not sent).")
