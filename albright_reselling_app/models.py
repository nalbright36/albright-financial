from decimal import Decimal

from django.conf import settings
from django.db import models
from django.utils import timezone


# Thresholds (days held) for the Scorecard's inventory-aging markdown
# suggestions - see LedgerEntry.aging_suggestion().
AGING_THRESHOLDS = [
    (90, "Bundle, list locally, or donate"),
    (60, "Drop price again (~10%)"),
    (30, "Drop price (~10%)"),
    (14, "Offer to watchers"),
]

SALE_CHANNEL_CHOICES = [
    ("ebay", "eBay"),
    ("scrap_gold", "Scrap/Gold Buyer"),
    ("local_facebook", "Local/Facebook"),
    ("etsy", "Etsy"),
    ("other", "Other"),
]

PREDICTION_SOURCE_CHOICES = [
    ("scanner", "Scanner"),
    ("ai_review", "AI Review"),
]

STATUS_CHOICES = [
    ("holding", "Holding"),
    ("partially_sold", "Partially Sold"),
    ("sold_out", "Sold Out"),
    ("written_off", "Written Off"),
]


class LedgerEntry(models.Model):
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="ledger_entries"
    )
    item = models.CharField(max_length=255)
    cost = models.DecimalField(max_digits=10, decimal_places=2, default=0)

    # Legacy, pre-scanner-integration fields. Every row created before this
    # feature has its buy/sell costs lumped into these two - they are never
    # written to by new code (the "I won this" flow, the quick-add form, or
    # the formset below all use the split fields instead). A row with a
    # nonzero legacy amount shows a "split this" prompt (see
    # ledger_views.split_legacy) until the owner moves it into the new
    # buy_fees/sell_fees or inbound_shipping/sell_shipping fields.
    fees = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    shipping = models.DecimalField(max_digits=10, decimal_places=2, default=0)

    sold_for = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    sold_date = models.DateField(null=True, blank=True)  # paired with sold_for - needed for days_to_sell
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    # --- Scanner integration: optional links, filled in by the "I won
    # this" flow. Entries bought outside the scanner leave these null and
    # behave exactly as before. ---
    scanner_lot = models.ForeignKey(
        "SourcedLot", on_delete=models.SET_NULL, null=True, blank=True, related_name="ledger_entries",
    )
    ai_review = models.ForeignKey(
        "AIReview", on_delete=models.SET_NULL, null=True, blank=True, related_name="ledger_entries",
    )
    purchase_date = models.DateField(null=True, blank=True)
    source = models.CharField(max_length=40, blank=True)
    category = models.CharField(max_length=30, blank=True)
    listing_url = models.URLField(max_length=500, blank=True)

    # Snapshots taken at "I won this" time, so later re-scans/re-evaluations
    # of the lot (or the lot being deleted) never change what the ledger
    # says the prediction was.
    predicted_sale_low = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    predicted_sale_high = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    predicted_sale_source = models.CharField(max_length=10, choices=PREDICTION_SOURCE_CHOICES, blank=True)
    scanner_max_bid = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    ai_suggested_max_bid = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    melt_value = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)

    # Buy-side costs, computed the same way the scanner's max bid is
    # (scanner/max_bid.py's BuyCosts/all_in_cost), shown pre-filled but
    # editable on the "I won this" form so they can be corrected against
    # the real invoice.
    buyer_premium = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    sales_tax = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    buy_fees = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    inbound_shipping = models.DecimalField(max_digits=10, decimal_places=2, default=0)

    # Sell-side costs for the simple single-sale path (sold_for). An entry
    # with itemized LedgerSale rows ignores these in favor of each sale's
    # own selling_fees/outbound_shipping - see sell_side_cost below.
    sell_fees = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    sell_shipping = models.DecimalField(max_digits=10, decimal_places=2, default=0)

    purchase_hours = models.DecimalField(max_digits=6, decimal_places=2, default=0)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="holding")

    class Meta:
        ordering = ["-created_at"]

    # ---------- Buy-side ----------

    @property
    def total(self):
        """Full buy-side cost. Extended beyond the original cost+fees+shipping
        formula to include every new buy-side field, all of which default to
        0/blank - so for a row created before this feature existed, this is
        numerically identical to what `total` always returned."""
        return (
            (self.cost or 0) + (self.buyer_premium or 0) + (self.sales_tax or 0) + (self.buy_fees or 0)
            + (self.inbound_shipping or 0) + (self.fees or 0) + (self.shipping or 0)
        )

    @property
    def buy_side_cost(self):
        """Alias of `total` - clearer name for new (sell-side-aware) code."""
        return self.total

    @property
    def has_legacy_amounts(self):
        return bool(self.fees) or bool(self.shipping)

    # ---------- Sell-side ----------

    @property
    def has_itemized_sales(self):
        return self.sales.exists()

    @property
    def has_conflicting_sale_data(self):
        """Both an itemized sale AND the legacy single-sale field are set -
        ambiguous, and total_realized below silently prefers the itemized
        sales, so this needs to be surfaced instead of silently ignored."""
        return self.has_itemized_sales and self.sold_for is not None

    @property
    def total_realized(self):
        """What actually came in from selling this item - None if nothing
        has sold yet. Itemized sales win over the legacy sold_for field
        when both are present (see has_conflicting_sale_data)."""
        if self.has_itemized_sales:
            return sum((s.sale_price for s in self.sales.all()), Decimal("0"))
        return self.sold_for

    @property
    def sell_side_cost(self):
        if self.has_itemized_sales:
            fees = sum((s.selling_fees or 0 for s in self.sales.all()), Decimal("0"))
            shipping = sum((s.outbound_shipping or 0 for s in self.sales.all()), Decimal("0"))
            return fees + shipping
        return (self.sell_fees or 0) + (self.sell_shipping or 0)

    @property
    def total_hours(self):
        sale_hours = sum((s.hours_spent or 0 for s in self.sales.all()), Decimal("0"))
        return (self.purchase_hours or 0) + sale_hours

    # ---------- Outcome ----------

    @property
    def profit(self):
        """Extended the same way `total` is: for a legacy row (no itemized
        sales, no written-off status, every new field at its default), this
        reduces to exactly the original sold_for - total (or -total)
        formula - see tests.test_ledger.LegacyProfitUnchangedTests."""
        if self.status == "written_off":
            return -self.buy_side_cost
        realized = self.total_realized
        if realized is None:
            return -self.buy_side_cost
        return realized - self.buy_side_cost - self.sell_side_cost

    @property
    def is_realized(self):
        """Whether this item has an actual outcome yet (sold, fully or in
        part, or written off) - vs. still just sitting in inventory."""
        return self.status == "written_off" or self.total_realized is not None

    @property
    def roi_pct(self):
        if not self.buy_side_cost:
            return None
        return float(self.profit / self.buy_side_cost) * 100

    @property
    def days_to_sell(self):
        """None unless the item reached a fully-resolved state: sold_out via
        itemized sales (measured to the last sale date), or the legacy
        sold_for path with a sold_date. Still-partial sales and still-held
        items haven't finished the clock yet."""
        start = self.purchase_date or timezone.localtime(self.created_at).date()
        if self.has_itemized_sales:
            if self.status != "sold_out":
                return None
            last_sale = max(s.sale_date for s in self.sales.all())
            return (last_sale - start).days
        if self.sold_for is not None and self.sold_date is not None:
            return (self.sold_date - start).days
        return None

    @property
    def profit_per_hour(self):
        if not self.is_realized or not self.total_hours:
            return None
        return float(self.profit / self.total_hours)

    @property
    def prediction_mid(self):
        """Single comparable predicted figure: the midpoint of the saved
        range (for a scanner-sourced prediction, low == high, so this is
        just that value)."""
        if self.predicted_sale_low is not None and self.predicted_sale_high is not None:
            return (self.predicted_sale_low + self.predicted_sale_high) / 2
        return self.predicted_sale_low if self.predicted_sale_low is not None else self.predicted_sale_high

    @property
    def prediction_error_dollars(self):
        if self.total_realized is None or self.prediction_mid is None:
            return None
        return self.total_realized - self.prediction_mid

    @property
    def prediction_error_pct(self):
        error = self.prediction_error_dollars
        if error is None or not self.prediction_mid:
            return None
        return float(error / self.prediction_mid) * 100

    # ---------- Aging (unsold inventory only) ----------

    @property
    def days_held(self):
        start = self.purchase_date or timezone.localtime(self.created_at).date()
        return (timezone.localdate() - start).days

    @property
    def aging_suggestion(self):
        if self.status not in ("holding", "partially_sold"):
            return None
        days = self.days_held
        for threshold, suggestion in AGING_THRESHOLDS:
            if days >= threshold:
                return suggestion
        return None

    def __str__(self):
        return self.item


class LedgerSale(models.Model):
    """One sale transaction against a LedgerEntry. Most items have exactly
    one (or zero, if still unsold); a lot split up and sold piece by piece
    (e.g. a box of coins sold to several buyers) has several."""
    entry = models.ForeignKey(LedgerEntry, on_delete=models.CASCADE, related_name="sales")
    sale_date = models.DateField()
    sale_price = models.DecimalField(max_digits=10, decimal_places=2)
    channel = models.CharField(max_length=20, choices=SALE_CHANNEL_CHOICES, default="other")
    selling_fees = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    outbound_shipping = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    hours_spent = models.DecimalField(max_digits=6, decimal_places=2, default=0)
    notes = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-sale_date"]

    def __str__(self):
        return f"{self.entry.item} sold {self.sale_date} for {self.sale_price}"


class ScanRequest(models.Model):
    STATUS_CHOICES = [
        ("pending", "Pending"),
        ("running", "Running"),
        ("complete", "Complete"),
        ("failed", "Failed"),
    ]
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="scan_requests")
    source_url = models.URLField()
    max_pages = models.PositiveIntegerField(default=40)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default="pending")
    error_message = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    reference_analysis = models.ForeignKey(
        "AnalysisRequest", on_delete=models.SET_NULL, null=True, blank=True, related_name="scans_scored_against"
    )

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.source_url} ({self.status})"


class ScannedLot(models.Model):
    scan_request = models.ForeignKey(ScanRequest, on_delete=models.CASCADE, related_name="lots")
    lot_id = models.CharField(max_length=100, blank=True)
    title = models.CharField(max_length=500)
    description = models.TextField(blank=True)
    category = models.CharField(max_length=200, blank=True)
    current_bid = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    bid_count = models.IntegerField(null=True, blank=True)
    image_url = models.URLField(blank=True)
    lot_url = models.URLField(blank=True)
    interest_score = models.IntegerField(default=0)
    score_reasons = models.TextField(blank=True)
    estimated_resale_low = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    estimated_resale_high = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    max_hammer = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    discount_likelihood_score = models.IntegerField(null=True, blank=True)
    discount_likelihood_reasoning = models.TextField(blank=True)

    # --- Reconciliation fields (populated after the auction closes) ---
    actual_price_realized = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    is_closed = models.BooleanField(default=False)
    reconciled_at = models.DateTimeField(null=True, blank=True)
    auctioneer_name = models.CharField(max_length=255, blank=True)
    auction_close_datetime = models.DateTimeField(null=True, blank=True)
    final_bid_count = models.IntegerField(null=True, blank=True)

    class Meta:
        ordering = ["-interest_score"]

    def __str__(self):
        return self.title


class ReconciliationRequest(models.Model):
    STATUS_CHOICES = ScanRequest.STATUS_CHOICES
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="reconciliation_requests"
    )
    scan_request = models.ForeignKey(ScanRequest, on_delete=models.CASCADE, related_name="reconciliation_requests")
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default="pending")
    error_message = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Reconcile scan #{self.scan_request_id} ({self.status})"


class DeepDiveRequest(models.Model):
    STATUS_CHOICES = ScanRequest.STATUS_CHOICES
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="deep_dive_requests")
    lot = models.ForeignKey(ScannedLot, on_delete=models.CASCADE, related_name="deep_dives")
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default="pending")
    error_message = models.TextField(blank=True)
    resale_low = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    resale_high = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    max_hammer = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    confidence = models.CharField(max_length=10, blank=True)
    analysis = models.TextField(blank=True)
    images_analyzed = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Deep dive: {self.lot.title} ({self.status})"


class HarvestRequest(models.Model):
    STATUS_CHOICES = ScanRequest.STATUS_CHOICES
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="harvest_requests")
    source_url = models.URLField()
    max_pages = models.PositiveIntegerField(default=100)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default="pending")
    error_message = models.TextField(blank=True)
    lots_harvested = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.source_url} ({self.status})"


class AnalysisRequest(models.Model):
    """A filtered, on-demand AI resale-analysis run over harvested
    HistoricalLot data. Filter criteria are stored on the request itself
    so each run's scope is recorded, not just its results."""
    STATUS_CHOICES = ScanRequest.STATUS_CHOICES
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="analysis_requests")

    # Filter criteria — all optional; blank/null means "no filter on this field"
    category = models.CharField(max_length=200, blank=True)
    auctioneer_name = models.CharField(max_length=255, blank=True)
    keyword = models.CharField(max_length=255, blank=True)
    min_final_price = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    max_final_price = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    max_lots = models.PositiveIntegerField(default=100)

    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default="pending")
    error_message = models.TextField(blank=True)
    lots_analyzed = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Analysis #{self.id} ({self.status})"


class HistoricalLot(models.Model):
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="historical_lots")
    harvest_request = models.ForeignKey(HarvestRequest, on_delete=models.CASCADE, related_name="lots")
    lot_id = models.CharField(max_length=100, blank=True)
    title = models.CharField(max_length=500)
    description = models.TextField(blank=True)
    category = models.CharField(max_length=200, blank=True)
    auctioneer_name = models.CharField(max_length=255, blank=True)
    final_price = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    final_bid_count = models.IntegerField(null=True, blank=True)
    auction_close_datetime = models.DateTimeField(null=True, blank=True)
    image_url = models.URLField(blank=True)
    lot_url = models.URLField(blank=True)
    harvested_at = models.DateTimeField(auto_now_add=True)

    # --- Resale-analysis fields (populated by an AnalysisRequest run) ---
    estimated_resale_low = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    estimated_resale_high = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    analysis_reasoning = models.TextField(blank=True)
    analyzed_at = models.DateTimeField(null=True, blank=True)
    last_analysis_request = models.ForeignKey(
        "AnalysisRequest", on_delete=models.SET_NULL, null=True, blank=True, related_name="analyzed_lots"
    )

    class Meta:
        ordering = ["-harvested_at"]
        indexes = [
            models.Index(fields=["category"]),
            models.Index(fields=["auctioneer_name"]),
        ]

    def __str__(self):
        return self.title

    @property
    def margin(self):
        """Positive = sold for less than the conservative resale estimate
        (a real sleeper). None if not yet analyzed or nothing to compare."""
        if self.estimated_resale_low is not None and self.final_price is not None:
            return self.estimated_resale_low - self.final_price
        return None

    @property
    def margin_pct(self):
        m = self.margin
        if m is not None and self.final_price:
            return (m / self.final_price) * 100
        return None


from .scanner_models import (  # noqa: E402,F401
    SourcedLot, LotEvaluation, SpotPrice, ScanRun, AIReview, AlertSent, CriticalAlertSent, BidWatch,
    CalibrationOverride, CalibrationSuggestion, LotFeedback,
)