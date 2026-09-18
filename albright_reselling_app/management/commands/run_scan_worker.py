import time
import traceback

from django.core.management.base import BaseCommand
from django.db.models import Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from albright_reselling_app.models import (
    ScanRequest,
    ScannedLot,
    ReconciliationRequest,
    DeepDiveRequest,
    HarvestRequest,
    HistoricalLot,
    AnalysisRequest,
)
from albright_reselling_app.scan_service import (
    scrape_and_score,
    fetch_lot_status,
    run_deep_dive,
    harvest_closed_lots,
    estimate_historical_resale,
    score_discount_likelihood,
)


class Command(BaseCommand):
    help = (
        "Persistent worker: polls for pending ScanRequests, ReconciliationRequests, "
        "DeepDiveRequests, HarvestRequests, and AnalysisRequests, processing one at a time."
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

            analysis = AnalysisRequest.objects.filter(status="pending").order_by("created_at").first()
            if analysis:
                self._process_analysis(analysis)
                continue

            time.sleep(5)

    def _build_historical_data(self, analysis_request):
        """Pulls the referenced AnalysisRequest's analyzed HistoricalLot rows
        into plain data for score_discount_likelihood: a flat list of
        {"title", "is_hit", "margin_pct"} dicts (used for item-to-item
        similarity matching — the PRIMARY signal) plus per-auctioneer
        aggregate stats (the SECONDARY, fallback signal). Category is
        deliberately not aggregated here anymore — on catalog-sourced
        harvests it's often a coarse whole-auction guess, not a real
        per-item label, so it doesn't discriminate well between different
        kinds of items within one auction.
        """
        lots = analysis_request.analyzed_lots.filter(estimated_resale_low__isnull=False, final_price__isnull=False)

        historical_items = []
        auctioneer_agg = {}

        for lot in lots:
            margin = lot.margin
            is_hit = margin is not None and margin > 0
            margin_pct = float(lot.margin_pct) if lot.margin_pct is not None else 0.0

            historical_items.append({"title": lot.title, "is_hit": is_hit, "margin_pct": margin_pct})

            if lot.auctioneer_name:
                entry = auctioneer_agg.setdefault(
                    lot.auctioneer_name, {"count": 0, "hits": 0, "margin_pct_sum": 0.0}
                )
                entry["count"] += 1
                if is_hit:
                    entry["hits"] += 1
                entry["margin_pct_sum"] += margin_pct

        auctioneer_stats = {
            name: {
                "count": v["count"],
                "hit_rate": v["hits"] / v["count"],
                "avg_margin_pct": v["margin_pct_sum"] / v["count"],
            }
            for name, v in auctioneer_agg.items()
            if v["count"]
        }

        return historical_items, auctioneer_stats

    def _process_scan(self, scan):
        scan.status = "running"
        scan.save(update_fields=["status"])
        try:
            lots_data = scrape_and_score(scan.source_url, max_pages=scan.max_pages)

            historical_items, auctioneer_stats = [], {}
            if scan.reference_analysis_id:
                historical_items, auctioneer_stats = self._build_historical_data(scan.reference_analysis)

            for lot in lots_data:
                close_dt_str = lot.pop("auction_close_datetime", None)
                close_dt = parse_datetime(close_dt_str) if close_dt_str else None

                discount_score, discount_reasoning = None, ""
                if scan.reference_analysis_id:
                    discount_score, discount_reasoning = score_discount_likelihood(
                        lot.get("title"), lot.get("auctioneer_name"), historical_items, auctioneer_stats
                    )

                ScannedLot.objects.create(
                    scan_request=scan,
                    auction_close_datetime=close_dt,
                    discount_likelihood_score=discount_score,
                    discount_likelihood_reasoning=discount_reasoning,
                    **lot,
                )
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

    def _process_analysis(self, analysis):
        analysis.status = "running"
        analysis.save(update_fields=["status"])
        try:
            qs = HistoricalLot.objects.filter(owner=analysis.owner, final_price__isnull=False)
            if analysis.category:
                qs = qs.filter(category__icontains=analysis.category)
            if analysis.auctioneer_name:
                qs = qs.filter(auctioneer_name__icontains=analysis.auctioneer_name)
            if analysis.keyword:
                qs = qs.filter(Q(title__icontains=analysis.keyword) | Q(description__icontains=analysis.keyword))
            if analysis.min_final_price is not None:
                qs = qs.filter(final_price__gte=analysis.min_final_price)
            if analysis.max_final_price is not None:
                qs = qs.filter(final_price__lte=analysis.max_final_price)

            lots = list(qs.order_by("-harvested_at")[: analysis.max_lots])
            count = 0
            for lot in lots:
                result = estimate_historical_resale(lot.title, lot.description, lot.category)
                lot.estimated_resale_low = result["resale_low"]
                lot.estimated_resale_high = result["resale_high"]
                lot.analysis_reasoning = result["reasoning"]
                lot.analyzed_at = timezone.now()
                lot.last_analysis_request = analysis
                lot.save()
                count += 1
                time.sleep(0.3)  # light rate-limit pacing
            analysis.lots_analyzed = count
            analysis.status = "complete"
        except Exception as e:
            analysis.status = "failed"
            analysis.error_message = str(e)
            traceback.print_exc()
        analysis.completed_at = timezone.now()
        analysis.save()