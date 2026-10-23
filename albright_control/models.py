"""The Admin area's (/control/) own records: every scheduled job's runs,
the admin audit log, and integration test results."""
from datetime import datetime, timezone as dt_timezone

from django.conf import settings
from django.db import models


def utc_now():
    """Job bookkeeping's own clock - deliberately not django.utils.timezone.now,
    which tests patch to drive a command's schedule; recording a run must
    never consume (or depend on) those patched values."""
    return datetime.now(dt_timezone.utc)


class JobRun(models.Model):
    """One run of a scheduled management command (jobs.tracked_job). ok is
    None while it's running; error holds the exception message on failure."""
    job = models.CharField(max_length=80, db_index=True)       # e.g. "track_closed:shopgoodwill", "calibrate"
    started_at = models.DateTimeField(default=utc_now, db_index=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    ok = models.BooleanField(null=True)
    error = models.TextField(blank=True)

    class Meta:
        ordering = ["-started_at", "-pk"]

    def __str__(self):
        return f"{self.job} @ {self.started_at:%Y-%m-%d %H:%M} ({'running' if self.ok is None else 'ok' if self.ok else 'failed'})"


class AuditEntry(models.Model):
    """An admin action (token created/revoked, suggestion status changed,
    backup downloaded, integration test run...)."""
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                             related_name="+")
    action = models.CharField(max_length=60)
    details = models.CharField(max_length=500, blank=True)

    class Meta:
        ordering = ["-created_at", "-pk"]


class IntegrationCheck(models.Model):
    """The result of an Integrations page test button - the latest one
    decides whether an integration shows as Failing."""
    integration = models.CharField(max_length=20, db_index=True)
    checked_at = models.DateTimeField(auto_now_add=True)
    ok = models.BooleanField()
    message = models.CharField(max_length=300, blank=True)

    class Meta:
        ordering = ["-checked_at", "-pk"]


class Idea(models.Model):
    """One of my ideas - sent to the Grok bot by message (it posts them via
    the Bot API, ideas:write), entered by hand, or moved from a Bot
    suggestion. Reviewed on Admin > Ideas."""
    APPS = [("trade", "Trading"), ("resell", "Reselling"), ("opps", "Opportunities"), ("admin", "Admin"),
            ("all", "Whole site")]
    CATEGORIES = [("feature", "Feature"), ("improvement", "Improvement"), ("bug", "Bug"),
                  ("business_idea", "Business idea"), ("research", "Research")]
    PRIORITIES = [("low", "Low"), ("medium", "Medium"), ("high", "High"), ("urgent", "Urgent")]
    IMPACTS = [("low", "Low"), ("medium", "Medium"), ("high", "High")]
    EFFORTS = [("small", "Small"), ("medium", "Medium"), ("large", "Large")]
    STATUSES = [("new", "New"), ("considering", "Considering"), ("planned", "Planned"),
                ("in_progress", "In progress"), ("done", "Done"), ("declined", "Declined")]
    SOURCES = [("grok", "Grok"), ("manual", "Manual"), ("moved_from_suggestion", "Moved from suggestion")]
    CLOSED_STATUSES = ("done", "declined")
    # Fallbacks when the bot sends a value that isn't one of the choices.
    DEFAULTS = {"app": "all", "category": "feature", "priority": "medium", "impact": "medium", "effort": "medium"}

    title = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    original_message = models.TextField(blank=True)     # exactly what I told the bot (read-only on the page)
    app = models.CharField(max_length=6, choices=APPS, default="all")
    category = models.CharField(max_length=14, choices=CATEGORIES, default="feature")
    priority = models.CharField(max_length=6, choices=PRIORITIES, default="medium")
    impact = models.CharField(max_length=6, choices=IMPACTS, default="medium")
    effort = models.CharField(max_length=6, choices=EFFORTS, default="medium")
    status = models.CharField(max_length=11, choices=STATUSES, default="new", db_index=True)
    tags = models.JSONField(default=list, blank=True)
    links = models.JSONField(default=list, blank=True)
    my_notes = models.TextField(blank=True)
    target_date = models.DateField(null=True, blank=True)
    source = models.CharField(max_length=21, choices=SOURCES, default="manual")
    # Set when a new idea's title closely matches an open one - "possible duplicate of #N", to merge or ignore.
    possible_duplicate_of = models.ForeignKey("self", on_delete=models.SET_NULL, null=True, blank=True,
                                              related_name="possible_duplicates")
    token = models.ForeignKey("albright_bot_api.BotToken", on_delete=models.SET_NULL, null=True, blank=True,
                              related_name="+")   # which bot token sent it (source "grok")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    status_changed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-pk"]

    def __str__(self):
        return f"#{self.pk} {self.title}"

    @property
    def is_open(self):
        return self.status not in self.CLOSED_STATUSES

    @classmethod
    def from_db(cls, db, field_names, values):
        instance = super().from_db(db, field_names, values)
        instance._loaded_status = instance.status if "status" in field_names else None
        return instance

    def save(self, *args, **kwargs):
        """status_changed_at follows every status change (and the first save)."""
        from django.utils import timezone
        if self._state.adding or getattr(self, "_loaded_status", self.status) != self.status:
            self.status_changed_at = timezone.now()
            if kwargs.get("update_fields") is not None:
                kwargs["update_fields"] = list(kwargs["update_fields"]) + ["status_changed_at"]
        super().save(*args, **kwargs)
        self._loaded_status = self.status
