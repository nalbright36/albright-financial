"""Add to the bottom of albright_reselling_app/admin.py:

    from .scanner_admin import *  # noqa: E402,F401,F403
"""
from django.contrib import admin
from django.db.models import Q
from django.utils import timezone
from django.utils.html import format_html

from .scanner_models import LotEvaluation, ScanRun, SourcedLot, SpotPrice


class AuctionStatusFilter(admin.SimpleListFilter):
    title = "Auction status"
    parameter_name = "auction_status"

    def lookups(self, request, model_admin):
        return (("live", "Live"), ("ended", "Ended"))

    def queryset(self, request, queryset):
        now = timezone.now()
        if self.value() == "live":
            return queryset.filter(lot__end_time__gt=now)
        if self.value() == "ended":
            return queryset.filter(Q(lot__end_time__lte=now) | Q(lot__end_time__isnull=True))
        return queryset


class EvaluationInline(admin.StackedInline):
    model = LotEvaluation
    extra = 0
    readonly_fields = [f.name for f in LotEvaluation._meta.fields]


@admin.register(SourcedLot)
class SourcedLotAdmin(admin.ModelAdmin):
    list_display = ("title", "source", "current_price", "bid_count", "end_time", "link")
    list_filter = ("source", "matched_keyword")
    search_fields = ("title",)
    inlines = [EvaluationInline]

    @admin.display(description="Open")
    def link(self, obj):
        return format_html('<a href="{}" target="_blank">view</a>', obj.url)


@admin.register(LotEvaluation)
class LotEvaluationAdmin(admin.ModelAdmin):
    list_display = ("lot_title", "current", "max_bid", "headroom", "confidence",
                    "coin_keys", "silver_oz", "gold_oz", "method", "is_candidate", "ends", "link")
    list_filter = (AuctionStatusFilter, "is_candidate", "confidence", "method")
    search_fields = ("lot__title",)
    list_select_related = ("lot",)

    def lot_title(self, obj):
        return obj.lot.title[:70]

    def current(self, obj):
        return obj.lot.current_price

    @admin.display(description="Ends", ordering="lot__end_time")
    def ends(self, obj):
        return obj.lot.end_time

    @admin.display(description="Open")
    def link(self, obj):
        return format_html('<a href="{}" target="_blank">view</a>', obj.lot.url)


admin.site.register(SpotPrice)


@admin.register(ScanRun)
class ScanRunAdmin(admin.ModelAdmin):
    """Read-only: these rows are the scan_lots command's own run log, not
    something to hand-edit."""
    list_display = ("started_at", "source", "lots_seen", "candidates", "failed_keywords", "error")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


# ---------------------------------------------------------------------------
# settings.py - paste this block (adjust numbers to your real invoices):
#
# RESELLING_SCANNER = {
#     "SOURCES": {
#         "shopgoodwill": {
#             "buyer_premium_pct": 0.0,        # verify on a ShopGoodwill invoice
#             "sales_tax_pct": 0.07,           # use the rate on your actual invoice
#             "default_inbound_shipping": 10.0,  # small-package estimate until we pull real quotes
#             "timezone": "America/Los_Angeles",  # verify: compare one lot's end time vs the site
#         },
#     },
#     "KEYWORDS": {
#         "coins": ["morgan dollar", "peace dollar", "silver eagle", "90% silver", "junk silver",
#                   "silver half dollar", "walking liberty", "franklin half", "kennedy half 1964",
#                   "mercury dime", "silver quarter", "war nickel", "silver bar", "silver round",
#                   "troy oz", "gold coin", "krugerrand"],
#     },
#     "FEES": {"ebay_fee_pct": 0.1325, "ebay_fixed_fee": 0.40, "outbound_shipping": 5.00,
#              "packaging": 0.50, "min_profit": 10.00, "min_profit_pct": 0.20},
#     "RESALE_MULTIPLIERS": {   # expected eBay sale as a multiple of melt; calibrate from your Ledger
#         "default": 1.00, "junk_silver_face": 1.00, "silver_eagle": 1.08,
#         "morgan_dollar": 1.00, "peace_dollar": 1.00,   # melt floor only; upside is flagged for you
#         "generic_silver": 0.97, "generic_gold": 0.97,
#     },
#     "CANDIDATE_CONFIDENCE": ["high", "medium"],
#     "LLM": {"enabled": True, "model": "gpt-4o-mini", "max_calls_per_run": 25},
#     "SPOT": {"cache_hours": 6, "manual": {"silver": None, "gold": None}},
# }
