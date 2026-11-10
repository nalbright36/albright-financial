"""Usage:
    python manage.py prune_activity            # delete activity older than 90 days
    python manage.py prune_activity --dry-run  # just count it

Activity (albright_labs.activity) is kept 90 days (KEEP_DAYS). Run daily (PythonAnywhere Tasks tab).
"""
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from albright_control.jobs import tracked_job
from albright_labs import activity
from albright_labs.models import ActivityEvent


class Command(BaseCommand):
    help = "Delete activity events older than 90 days."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Count what would be deleted; delete nothing.")

    @tracked_job("prune_activity")   # shows on Admin > System health
    def handle(self, *args, dry_run=False, **opts):
        if dry_run:
            cutoff = timezone.now() - timedelta(days=activity.KEEP_DAYS)
            n = ActivityEvent.objects.filter(updated_at__lt=cutoff).count()
            self.stdout.write(f"{n} activity event(s) older than {activity.KEEP_DAYS} days would be deleted.")
            return
        n = activity.prune()
        self.stdout.write(f"Deleted {n} activity event(s) older than {activity.KEEP_DAYS} days.")
