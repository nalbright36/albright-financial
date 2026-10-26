"""Usage:
    python manage.py prune_resume_chats            # delete conversations past the retention period
    python manage.py prune_resume_chats --dry-run  # just count them

Deletes resume-chat conversations (and their messages) whose last message is
older than Admin > Resume > Chatbot > "keep conversations for N days"
(default 90). Run daily (PythonAnywhere Tasks tab).
"""
from django.core.management.base import BaseCommand
from django.db.models.functions import Coalesce
from django.utils import timezone

from albright_control.jobs import tracked_job
from albright_resume import conversations
from albright_resume.models import ChatConversation, ResumeSettings


class Command(BaseCommand):
    help = "Delete resume chat conversations older than the retention setting."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Count what would be deleted; delete nothing.")

    @tracked_job("prune_resume_chats")   # shows on Admin > System health
    def handle(self, *args, dry_run=False, **opts):
        site = ResumeSettings.load()
        if dry_run:
            from datetime import timedelta
            cutoff = timezone.now() - timedelta(days=max(1, int(site.chat_retention_days or 0)))
            n = (ChatConversation.objects.annotate(last=Coalesce("last_message_at", "started_at"))
                 .filter(last__lt=cutoff).count())
            self.stdout.write(f"{n} conversation(s) older than {site.chat_retention_days} days would be deleted.")
            return
        n = conversations.prune(site)
        self.stdout.write(f"Deleted {n} conversation(s) older than {site.chat_retention_days} days.")
