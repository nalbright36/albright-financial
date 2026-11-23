"""Making scheduled briefings and pinging their owner (push / email). send_roundups runs this every few minutes.

  run_due: every roundup that's on and due (next_run_at passed). Each is claimed by moving next_run_at on with a
           conditional UPDATE, so two overlapping runs never both make it. The issue for the period it was due for is
           (re)built; a ping goes once (issue.sent_at). More than LATE late (the task was off): made, no ping.
  catch_up: the same for one person when they open Home or /briefing/, GRACE after it was due and with no ping -
           so Home still shows the new one if the scheduled task isn't set up.
  ping: Web Push to the owner's own devices (push.send_to, tag "roundup-<id>" - a new one replaces the last), never
        in their quiet hours; email to their own address. Only ever the owner.
"""
import logging
from datetime import timedelta

from django.urls import reverse
from django.utils import timezone

from . import build, schedule, service
from .models import EMAIL, PUSH, Roundup

log = logging.getLogger(__name__)
LATE = timedelta(hours=3)
GRACE = timedelta(minutes=10)


def ping(roundup, issue, now=None):
    """Push and/or email the owner. Returns what went: {"push": n devices, "email": True}."""
    from albright_labs import activity, emails, push
    from albright_labs.models import PushSubscription
    owner, p = roundup.owner, issue.payload
    url = reverse("roundup:briefing") + f"?issue={issue.pk}"
    out = {}
    if PUSH in (roundup.channels or []) and push.vapid_enabled() \
            and not activity.in_quiet_hours(activity.prefs_for(owner), now):
        subs = list(PushSubscription.objects.filter(user=owner))
        for sub in subs:
            push.send_to(sub, title=p.get("title", "Your briefing"), body=build.push_text(p), url=url,
                         tag=f"roundup-{roundup.pk}")
        if subs:
            out["push"] = len(subs)
    if EMAIL in (roundup.channels or []) and owner.email:
        from albright_labs import sharing
        ok, _msg = emails.send("roundup", owner.email, {
            "name": sharing.person_name(owner), "p": p, "summary": issue.summary,
            "link": emails.site_url(url), "button": "Open briefing",
            "settings_link": emails.site_url(reverse("roundup:briefing") + "?schedule=1")})
        if ok:
            out["email"] = True
    return out


def _claim(roundup, now):
    """Move next_run_at on (from the later of now and when it was due). False: another run got it first."""
    due_at = roundup.next_run_at
    nxt = schedule.next_run(roundup, max(now, due_at))
    return bool(Roundup.objects.filter(pk=roundup.pk, next_run_at=due_at).update(next_run_at=nxt))


def run_due(now=None, user=None, pings=True, grace=timedelta(0)):
    """Make every due briefing. Returns (made, pinged)."""
    now = now or timezone.now()
    qs = Roundup.objects.filter(enabled=True, next_run_at__lte=now - grace).select_related("owner")
    if user is not None:
        qs = qs.filter(owner=user)
    made = pinged = 0
    for r in qs.order_by("next_run_at"):
        due_at = r.next_run_at
        if not _claim(r, now):
            continue
        try:
            if not (r.owner.is_active and service.can_use(r.owner)):
                continue
            start, end = schedule.period(r.scope, schedule.local(r.owner, due_at))
            issue = service.issue_for(r, start, end, rebuild=True)
            made += 1
            if issue.sent_at or (r.skip_if_empty and issue.payload.get("empty")):
                continue
            went = ping(r, issue, now) if pings and now - due_at <= LATE else {}
            pinged += bool(went)
            issue.sent_at, issue.delivered = now, went
            issue.save(update_fields=["sent_at", "delivered"])
            Roundup.objects.filter(pk=r.pk).update(last_sent_at=now)
        except Exception:  # noqa: BLE001 - one person's failure never stops the rest
            log.exception("Roundup %s failed", r.pk)
    return made, pinged


def catch_up(user, now=None):
    """Due briefings for this person, made now without a ping (Home and /briefing/). Never raises."""
    try:
        return run_due(now, user=user, pings=False, grace=GRACE)
    except Exception:  # noqa: BLE001
        log.exception("Roundup catch-up failed")
        return 0, 0
