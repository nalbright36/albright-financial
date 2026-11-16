"""Delete attachments whose task or note no longer exists.

Deleting a task or note normally deletes its attachments (albright_labs.attachments.connect_signals); this sweeps
up any a raw delete or an old bug left behind. Their bytes go with them (AttachmentBlob, on_delete=CASCADE).

    python manage.py cleanup_orphan_attachments --dry-run     # list what would go
    python manage.py cleanup_orphan_attachments
"""
from django.core.management.base import BaseCommand

from albright_labs.attachments import orphans, size_label


class Command(BaseCommand):
    help = "Delete attachments whose Planner task or note no longer exists (--dry-run lists them only)."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="List what would be deleted; delete nothing.")

    def handle(self, *args, dry_run=False, **options):
        stale = list(orphans().select_related("content_type").order_by("pk"))
        total = sum(a.size_bytes for a in stale)
        for a in stale:
            self.stdout.write(f"{'would delete' if dry_run else 'deleting'} #{a.pk} {a.original_name} "
                              f"({a.content_type.model} #{a.object_id}, {size_label(a.size_bytes)})")
        if not dry_run and stale:
            from albright_labs.models import Attachment
            Attachment.objects.filter(pk__in=[a.pk for a in stale]).delete()
        verb = "Would delete" if dry_run else "Deleted"
        self.stdout.write(self.style.SUCCESS(f"{verb} {len(stale)} orphan attachment{'s' if len(stale) != 1 else ''} "
                                             f"({size_label(total)})."))
