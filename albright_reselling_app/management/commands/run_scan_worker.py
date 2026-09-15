import time
from django.core.management.base import BaseCommand
from django.utils import timezone
from albright_reselling_app.models import ScanRequest, ScannedLot
from albright_reselling_app.scan_service import scrape_and_score  # adapted logic


class Command(BaseCommand):
    help = "Persistent worker: polls for pending ScanRequests and processes them."

    requires_system_checks = []

    def handle(self, *args, **options):
        self.stdout.write("Scan worker started.")
        while True:
            scan = ScanRequest.objects.filter(status="pending").order_by("created_at").first()
            if not scan:
                time.sleep(5)
                continue

            scan.status = "running"
            scan.save(update_fields=["status"])
            try:
                lots_data = scrape_and_score(scan.source_url)
                for lot in lots_data:
                    ScannedLot.objects.create(scan_request=scan, **lot)
                scan.status = "complete"
            except Exception as e:
                scan.status = "failed"
                scan.error_message = str(e)
            scan.completed_at = timezone.now()
            scan.save()