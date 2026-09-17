import time
import traceback

from django.core.management.base import BaseCommand
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from albright_reselling_app.models import (
    ScanRequest,
    ScannedLot,
    ReconciliationRequest,
    DeepDiveRequest,
    HarvestRequest,
    HistoricalLot,
)
from albright_reselling_app.scan_service import (
    scrape_and_score,
    fetch_lot_status,
    run_deep_dive,
    harvest_closed_lots,
)


class Command(BaseCommand):
    help = (
        "Persistent worker: polls for pending ScanRequests, ReconciliationRequests, "
        "DeepDiveRequests, and HarvestRequests, and processes them one at a time."
    )
    requires_system_checks = []

    def handle(self, *args, **options):
        self.stdout.write("Scan worker started.")
        while True:
            scan = ScanRequest.objects.filter(status="pending").order_by("created_at").first()
            if scan:
                self._process_scan(scan)
                continue

            reconciliation = ReconciliationRequest.objects.filter(status="pending").order_by("created_at").first()
            if reconciliation:
                self._process_reconciliation(reconciliation)
                continue

            deep_dive = DeepDiveRequest.objects.filter(status="pending").order_by("created_at").first()
            if deep_dive:
                self._process_deep_dive(deep_dive)
                continue

            harvest = HarvestRequest.objects.filter(status="pending").order_by("created_at").first()
            if harvest:
                self._process_harvest(harvest)
                continue

            time.sleep(5)

    def _process_scan(self, scan):
        scan.status = "running"
        scan.save(update_fields=["status"])
        try:
            lots_data = scrape_and_score(scan.source_url, max_pages=scan.max_pages)
            for lot in lots_data:
                close_dt_str = lot.pop("auction_close_datetime", None)
                close_dt = parse_datetime(close_dt_str) if close_dt_str else None
                ScannedLot.objects.create(scan_request=scan, auction_close_datetime=close_dt, **lot)
            scan.status = "complete"
        except Exception as e:
            scan.status = "failed"
            scan.error_message = str(e)
            traceback.print_exc()
        scan.completed_at = timezone.now()
        scan.save()

    def _process_reconciliation(self, reconciliation):
        reconciliation.status = "running"
        reconciliation.save(update_fields=["status"])
        try:
            unreconciled_lots = reconciliation.scan_request.lots.filter(actual_price_realized__isnull=True)
            for lot in unreconciled_lots:
                if not lot.lot_url:
                    continue
                try:
                    result = fetch_lot_status(lot.lot_url)
                except Exception:
                    continue  # skip this lot, keep going — one bad URL shouldn't fail the whole batch
                if result["is_closed"]:
                    lot.is_closed = True
                    lot.actual_price_realized = result["price_realized"]
                    lot.final_bid_count = result["final_bid_count"]
                    if result.get("auctioneer_name") and not lot.auctioneer_name:
                        lot.auctioneer_name = result["auctioneer_name"]
                    lot.reconciled_at = timezone.now()
                    lot.save()
            reconciliation.status = "complete"
        except Exception as e:
            reconciliation.status = "failed"
            reconciliation.error_message = str(e)
            traceback.print_exc()
        reconciliation.completed_at = timezone.now()
        reconciliation.save()

    def _process_deep_dive(self, deep_dive):
        deep_dive.status = "running"
        deep_dive.save(update_fields=["status"])
        try:
            result = run_deep_dive(deep_dive.lot)
            deep_dive.resale_low = result["resale_low"]
            deep_dive.resale_high = result["resale_high"]
            deep_dive.max_hammer = result["max_hammer"]
            deep_dive.confidence = result["confidence"]
            deep_dive.analysis = result["analysis"]
            deep_dive.images_analyzed = result["images_analyzed"]
            deep_dive.status = "complete"
        except Exception as e:
            deep_dive.status = "failed"
            deep_dive.error_message = str(e)
            traceback.print_exc()
        deep_dive.completed_at = timezone.now()
        deep_dive.save()

    def _process_harvest(self, harvest):
        harvest.status = "running"
        harvest.save(update_fields=["status"])
        try:
            lots_data = harvest_closed_lots(harvest.source_url, max_pages=harvest.max_pages)
            count = 0
            for lot in lots_data:
                close_dt_str = lot.pop("auction_close_datetime", None)
                close_dt = parse_datetime(close_dt_str) if close_dt_str else None
                HistoricalLot.objects.create(
                    owner=harvest.owner,
                    harvest_request=harvest,
                    auction_close_datetime=close_dt,
                    **lot,
                )
                count += 1
            harvest.lots_harvested = count
            harvest.status = "complete"
        except Exception as e:
            harvest.status = "failed"
            harvest.error_message = str(e)
            traceback.print_exc()
        harvest.completed_at = timezone.now()
        harvest.save()