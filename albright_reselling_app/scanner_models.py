"""Scanner models. Add to the bottom of albright_reselling_app/models.py:

    from .scanner_models import SourcedLot, LotEvaluation, SpotPrice, ScanRun, AIReview, AlertSent  # noqa: E402,F401
"""
from django.db import models


class SourcedLot(models.Model):
    source = models.CharField(max_length=40)
    external_id = models.CharField(max_length=100)
    url = models.URLField(max_length=500)
    title = models.CharField(max_length=500)
    description = models.TextField(blank=True)
    image_url = models.URLField(max_length=500, blank=True)
    current_price = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    bid_count = models.IntegerField(null=True, blank=True)
    end_time = models.DateTimeField(null=True, blank=True)
    matched_keyword = models.CharField(max_length=100, blank=True)
    raw = models.JSONField(default=dict, blank=True)
    first_seen = models.DateTimeField(auto_now_add=True)
    last_seen = models.DateTimeField(auto_now=True)
    final_price = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    is_closed = models.BooleanField(default=False)
    final_checked_at = models.DateTimeField(null=True, blank=True)

    # Listing-feature tracking (scanner/features.py) - for the offline
    # "what predicts a cheap close" analysis (see export_features command).
    # first_seen_price is set once, on the scan that creates the row;
    # features is refreshed on every scan, then augmented with closing-time
    # keys (end_hour_local, at_close_*, etc.) by the track_closed command.
    features = models.JSONField(default=dict, blank=True)
    first_seen_price = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    bid_count_at_close = models.IntegerField(null=True, blank=True)

    class Meta:
        unique_together = ("source", "external_id")
        ordering = ["end_time"]

    def __str__(self):
        return f"[{self.source}] {self.title[:60]}"


class LotEvaluation(models.Model):
    lot = models.OneToOneField(SourcedLot, on_delete=models.CASCADE, related_name="evaluation")
    category = models.CharField(max_length=30, default="coins")
    method = models.CharField(max_length=10, default="regex")       # regex / llm
    coin_keys = models.CharField(max_length=200, blank=True)
    silver_oz = models.DecimalField(max_digits=10, decimal_places=4, default=0)
    gold_oz = models.DecimalField(max_digits=10, decimal_places=4, default=0)
    confidence = models.CharField(max_length=10, default="low")
    flags = models.JSONField(default=list, blank=True)
    excluded_reason = models.CharField(max_length=200, blank=True)
    melt_value = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    expected_sale = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    max_bid = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    headroom = models.DecimalField(max_digits=10, decimal_places=2, default=0)  # max_bid - current bid
    is_candidate = models.BooleanField(default=False)
    is_lead = models.BooleanField(default=False)
    lead_reason = models.CharField(max_length=300, blank=True)
    llm_title_hash = models.CharField(max_length=64, blank=True)  # avoid re-paying for the same text
    evaluated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-is_candidate", "-headroom"]


class SpotPrice(models.Model):
    metal = models.CharField(max_length=10)       # silver / gold
    price_usd = models.DecimalField(max_digits=10, decimal_places=2)
    source = models.CharField(max_length=30)
    fetched_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-fetched_at"]


class AIReview(models.Model):
    """One on-demand AI resale review of a single lot (scanner/ai_review.py's
    run_review), run only when the user clicks the review button - never
    from a scheduled task."""
    STATUS_CHOICES = [("done", "Done"), ("error", "Error")]

    lot = models.ForeignKey(SourcedLot, on_delete=models.CASCADE, related_name="ai_reviews")
    created_at = models.DateTimeField(auto_now_add=True)
    model_name = models.CharField(max_length=60, blank=True)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default="done")
    result = models.JSONField(default=dict, blank=True)  # ReviewResult.to_dict()
    resale_low = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    resale_high = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    suggested_max_bid = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    confidence = models.CharField(max_length=10, blank=True)
    cost_usd = models.DecimalField(max_digits=8, decimal_places=4, default=0)
    error = models.TextField(blank=True)

    # Snapshot of the lot/evaluation as of when the review ran - the live
    # values on SourcedLot/LotEvaluation move on (new bids, a re-scan), so
    # without this the history page couldn't show what the AI was actually
    # reacting to. Null on reviews saved before this field existed.
    bid_at_review = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    scanner_max_bid_at_review = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    melt_at_review = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    lot_end_time_at_review = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"AIReview({self.lot_id}, {self.status}) @ {self.created_at}"


class AlertSent(models.Model):
    """Records that a Telegram alert already went out for a given lot +
    kind, so the hourly scan_lots alert pass (scanner/alerts.py) never
    repeats one. unique_together is the actual dedup guarantee; the alert
    query also excludes already-sent lots so the common case never even
    reaches an IntegrityError."""
    KIND_CHOICES = [("candidate", "Candidate"), ("lead", "Lead"), ("check_by_hand", "Check by Hand")]

    lot = models.ForeignKey(SourcedLot, on_delete=models.CASCADE, related_name="alerts_sent")
    kind = models.CharField(max_length=20, choices=KIND_CHOICES)
    sent_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("lot", "kind")
        ordering = ["-sent_at"]

    def __str__(self):
        return f"AlertSent({self.lot_id}, {self.kind}) @ {self.sent_at}"


class ScanRun(models.Model):
    """One run of the scan_lots command - a log so the dashboard (and
    admin) can show whether the scheduled task is actually running."""
    source = models.CharField(max_length=40)
    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    lots_seen = models.IntegerField(default=0)
    candidates = models.IntegerField(default=0)
    leads = models.IntegerField(default=0)
    llm_calls = models.IntegerField(default=0)
    failed_keywords = models.JSONField(default=list, blank=True)
    stopped_early = models.BooleanField(default=False)
    error = models.TextField(blank=True)

    class Meta:
        ordering = ["-started_at"]

    def __str__(self):
        return f"{self.source} @ {self.started_at}"
