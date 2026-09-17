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

    class Meta:
        ordering = ["-interest_score"]

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

    class Meta:
        ordering = ["-harvested_at"]
        indexes = [
            models.Index(fields=["category"]),
            models.Index(fields=["auctioneer_name"]),
        ]