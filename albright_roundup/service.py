"""Making briefings: issue_for builds (or reuses) one RoundupIssue for a roundup and a period; current() is the one
Home and /briefing/ show. Every query here is filtered by owner - a roundup and its issues are only ever its
owner's."""
from django.db import IntegrityError, transaction
from django.utils import timezone

from albright_planner import planner

from . import build, schedule
from .models import RULES, RoundupIssue


def can_use(user):
    """Roundups need the Planner (their days come from it)."""
    from albright_planner.sources import can_open
    return bool(user and user.is_authenticated and user.is_active and can_open(user, "planner"))


def make_payload(roundup, start, end):
    return build.build(roundup.owner, roundup.scope, start, end, roundup.sections)


def write_summary(roundup, payload):
    """(text, source) - the rule-based summary (AI comes later, and always falls back to this)."""
    return build.summary(payload), RULES


def issue_for(roundup, start=None, end=None, rebuild=False, now=None):
    """The issue for this roundup's period (default: the current one) - made now if missing; rebuild: refreshed."""
    if start is None:
        start, end = schedule.current_period(roundup, now)
    issue = RoundupIssue.objects.filter(roundup=roundup, period_start=start).first()
    if issue is not None and not rebuild:
        return issue
    payload = make_payload(roundup, start, end)
    text, source = write_summary(roundup, payload)
    fields = {"owner": roundup.owner, "scope": roundup.scope, "period_end": end, "payload": payload,
              "summary": text, "summary_source": source, "built_at": timezone.now()}
    if issue is not None:
        for k, v in fields.items():
            setattr(issue, k, v)
        issue.save()
        return issue
    try:
        with transaction.atomic():
            return RoundupIssue.objects.create(roundup=roundup, period_start=start, **fields)
    except IntegrityError:        # made at the same moment by the scheduled task
        return RoundupIssue.objects.get(roundup=roundup, period_start=start)


def current(user, scope=None, enabled=False):
    """The newest issue still covering today (of this scope, if given; of a roundup that's on, if enabled), or None."""
    qs = RoundupIssue.objects.filter(owner=user, period_end__gte=planner.today(user))
    if scope:
        qs = qs.filter(scope=scope)
    if enabled:
        qs = qs.filter(roundup__enabled=True)
    return qs.order_by("-built_at", "-pk").select_related("roundup").first()


def history(user, limit=12):
    return list(RoundupIssue.objects.filter(owner=user).order_by("-built_at", "-pk")
                .only("pk", "scope", "period_start", "period_end", "built_at", "payload", "read_at")[:limit])
