"""Usage:
    python manage.py track_closed                            # ShopGoodwill, every category, last 2 days
    python manage.py track_closed --source maxsold             # MaxSold instead
    python manage.py track_closed --category jewelry           # one category only
    python manage.py track_closed --days-back 5                 # look back further (ShopGoodwill only)

Updates lots we already scanned with their final (closed/sold) price, so we
can see how our max-bid math actually held up. Never creates new lots -
only SourcedLot rows already in the database (matched on source +
external_id) get updated.
"""
import statistics
from datetime import timedelta
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.utils import timezone

from albright_reselling_app.scanner.adapters.base import SourceBlocked, SourceUnavailable
from albright_reselling_app.scanner.adapters.maxsold import MaxSoldAdapter
from albright_reselling_app.scanner.adapters.shopgoodwill import ShopGoodwillAdapter
from albright_reselling_app.scanner.pipeline import _categories_for, _cfg, _keywords_for
from albright_reselling_app.scanner_models import SourcedLot

ADAPTERS = {"shopgoodwill": ShopGoodwillAdapter, "maxsold": MaxSoldAdapter}
RESULTS_WINDOW_DAYS = 7


def _search_closed(adapter, source, keyword, days_back):
    """search_closed()'s signature isn't identical across adapters - only
    ShopGoodwill supports a lookback window (MaxSold's API has no
    equivalent parameter)."""
    if source == "shopgoodwill":
        return adapter.search_closed(keyword, days_back=days_back)
    return adapter.search_closed(keyword)


class Command(BaseCommand):
    help = "Record final (closed) prices for already-scanned lots, and summarize how they closed vs. max bid."

    def add_arguments(self, parser):
        parser.add_argument("--source", default="shopgoodwill")
        parser.add_argument("--category", default=None,
                             help="Limit to one category (coins/jewelry/games/cards). Default: every category.")
        parser.add_argument("--days-back", type=int, default=2,
                             help="How many days of closed auctions to search (ShopGoodwill only).")

    def handle(self, *args, **opts):
        source = opts["source"]
        adapter = ADAPTERS[source]()
        cfg = _cfg()
        categories = [opts["category"]] if opts["category"] else _categories_for(cfg, source)
        keyword_list = [kw for cat in categories for kw in _keywords_for(cfg, source, cat)]

        now = timezone.now()
        updated = 0
        consecutive_failures = 0

        for keyword in keyword_list:
            try:
                for raw in _search_closed(adapter, source, keyword, opts["days_back"]):
                    lot = SourcedLot.objects.filter(source=source, external_id=raw.external_id).first()
                    if lot is None or lot.is_closed:
                        continue  # never create new lots here; skip lots already recorded as closed
                    lot.final_price = Decimal(str(raw.current_price)).quantize(Decimal("0.01"))
                    lot.is_closed = True
                    lot.final_checked_at = now
                    lot.save(update_fields=["final_price", "is_closed", "final_checked_at"])
                    updated += 1
            except SourceBlocked as exc:
                self.stderr.write(self.style.ERROR(f"Stopping: {exc}"))
                break
            except SourceUnavailable as exc:
                self.stderr.write(self.style.WARNING(f"Keyword {keyword!r} failed, moving on: {exc}"))
                consecutive_failures += 1
                if consecutive_failures >= 2:
                    self.stderr.write(self.style.ERROR(
                        "Stopping: 2 consecutive keywords failed with SourceUnavailable - "
                        "the site is probably down or throttling us"
                    ))
                    break
                adapter.pause()
                continue
            else:
                consecutive_failures = 0
            adapter.pause()

        self.stdout.write(f"Lots updated: {updated}")
        self._print_results_summary(source, now)

    def _print_results_summary(self, source, now):
        cutoff = now - timedelta(days=RESULTS_WINDOW_DAYS)
        closed_with_bid = (
            SourcedLot.objects.filter(
                source=source, is_closed=True, final_price__isnull=False, end_time__gte=cutoff,
                evaluation__max_bid__gt=0,
            ).select_related("evaluation")
        )

        by_category = {}
        for lot in closed_with_bid:
            ev = lot.evaluation
            stats = by_category.setdefault(ev.category, {"under": 0, "over": 0, "melt_pcts": []})
            if lot.final_price <= ev.max_bid:
                stats["under"] += 1
            else:
                stats["over"] += 1
            if ev.melt_value and ev.melt_value > 0:
                stats["melt_pcts"].append(float(lot.final_price) / float(ev.melt_value) * 100)

        if not by_category:
            self.stdout.write(f"No results with a max bid closed in the last {RESULTS_WINDOW_DAYS} days.")
            return

        self.stdout.write(f"Results (last {RESULTS_WINDOW_DAYS} days):")
        for category, stats in sorted(by_category.items()):
            median_pct = statistics.median(stats["melt_pcts"]) if stats["melt_pcts"] else None
            pct_text = f"{median_pct:.0f}%" if median_pct is not None else "n/a"
            self.stdout.write(
                f"  {category}: {stats['under']} at/under max, {stats['over']} over max"
                f" | median final/melt {pct_text}"
            )
