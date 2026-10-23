"""Moving an opportunity through the workflow: Inbox -> (K) Saved, or (S)
Skipped; then the Pipeline board's Saved -> Applied -> Talking/interview ->
Working -> Paid, or Closed (lost / declined / expired). Every move records
when the stage changed (the board's "days in stage"); the first move to
Applied records the applied date, which the 7-day follow-up reminder
(alerts.send_followup_reminders) counts from."""
from datetime import timedelta

from django.db.models import Count
from django.utils import timezone

from .models import CLOSE_REASONS, PIPELINE_STAGES, SKIP_REASONS, STATUSES, Opportunity

STAGE_LABELS = dict(STATUSES)
BOARD_COLUMNS = [(stage, STAGE_LABELS[stage]) for stage in PIPELINE_STAGES if stage != "closed"]
SKIP_REASON_LABELS = dict(SKIP_REASONS)
CLOSE_REASON_LABELS = dict(CLOSE_REASONS)


def move(opp, stage, reason="", now=None):
    """Sets the stage (and the dates/reasons that go with it) and saves."""
    if stage not in STAGE_LABELS:
        raise ValueError(stage)
    now = now or timezone.now()
    if stage != opp.status:
        opp.stage_changed_at = now
    opp.status = stage
    if stage == "applied" and opp.applied_at is None:
        opp.applied_at = now
    opp.close_reason = (reason if reason in CLOSE_REASON_LABELS else "lost") if stage == "closed" else ""
    opp.skip_reason = reason if stage == "skipped" and reason in SKIP_REASON_LABELS else ""
    opp.save(update_fields=["status", "stage_changed_at", "applied_at", "close_reason", "skip_reason"])
    return opp


def skip_summary(now=None, days=7):
    """[(reason label, count)] for this week's skips, most common first -
    shown in the Inbox so I can tune the filters."""
    now = now or timezone.now()
    rows = (Opportunity.objects.filter(status="skipped", stage_changed_at__gte=now - timedelta(days=days))
            .values("skip_reason").annotate(n=Count("pk")).order_by("-n", "skip_reason"))
    return [(SKIP_REASON_LABELS.get(r["skip_reason"], "no reason"), r["n"]) for r in rows]
