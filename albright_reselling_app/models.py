from django.conf import settings
from django.db import models


class LedgerEntry(models.Model):
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="ledger_entries"
    )
    item = models.CharField(max_length=255)
    cost = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    fees = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    shipping = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    sold_for = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    @property
    def total(self):
        return (self.cost or 0) + (self.fees or 0) + (self.shipping or 0)

    @property
    def profit(self):
        if self.sold_for is None:
            return -self.total
        return self.sold_for - self.total

    def __str__(self):
        return self.item


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