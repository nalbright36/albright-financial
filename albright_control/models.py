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
