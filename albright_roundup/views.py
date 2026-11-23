"""/briefing/: the latest briefing (Today / Week / Month), its history, and the schedule sheet. Owner-only: every
lookup is filtered by request.user, so someone else's issue is a 404. Needs the Planner (service.can_use)."""
from datetime import time

from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from . import ai, deliver, schedule, service
from .models import EMAIL, MONTH, PUSH, SCOPES, TODAY, WEEK, Roundup, RoundupIssue

SCOPE_KEYS = [k for k, _ in SCOPES]


def _need(request):
    if not service.can_use(request.user):
        raise Http404


def _back(request, default):
    target = request.POST.get("next") or ""
    if target and url_has_allowed_host_and_scheme(target, allowed_hosts={request.get_host()},
                                                  require_https=request.is_secure()):
        return redirect(target)
    return redirect(default)


def present(issue, user):
    """Template-ready bits of an issue (the payload is stored as plain JSON)."""
    p = issue.payload or {}
    days = p.get("days", [])
    built = schedule.local(user, issue.built_at)
    counts = [len(d["items"]) for d in days]
    peak = max(counts + [1])
    top = counts.index(peak) if peak in counts else -1       # the busiest day (the first, on a tie)
    name = issue.roundup.name if issue.roundup_id else ""
    return {
        "issue": issue, "p": p, "days": days, "scope": issue.scope, "name": name,
        "built": f"{schedule.DAY_NAMES[built.weekday()]} {built:%b} {built.day}, {schedule.clock(built.time())}",
        "busy_days": [d for d in days if d["items"] or d["meals"]],
        "bars": [{"dow": d["dow"], "n": len(d["items"]), "h": 6 + round(30 * len(d["items"]) / peak),
                  "top": i == top} for i, d in enumerate(days)] if issue.scope == WEEK else [],
        "meals": [m for d in days for m in d["meals"]],
        "workouts": [d["dow"] for d in days for r in d["items"] if r["kind"] == "workout"],
        "weigh_ins": [d["dow"] for d in days for r in d["items"] if r["kind"] == "weigh_in"],
        "short": ". ".join(issue.summary.split(". ")[:2]).rstrip(".") + "." if issue.summary else "",
    }


def sheet_context(user):
    rows = schedule.ensure_presets(user)
    for r in rows:
        r.when = schedule.when_text(r)
    first = rows[0] if rows else None
    return {"rb_rows": rows, "rb_times": [(t.strftime("%H:%M"), schedule.clock(t)) for t in schedule.TIME_CHOICES],
            "rb_days": list(enumerate(schedule.DAY_NAMES)), "rb_month_days": list(range(1, 29)),
            "rb_push": bool(first and PUSH in first.channels), "rb_email": bool(first and EMAIL in first.channels),
            "rb_can_ai": ai.available(user) is not None, "rb_ai": bool(first and first.ai_summary)}


def home_card(request):
    """Home's Briefing card (hub view): None, {"intro": True} before they've chosen, or the latest issue."""
    user = request.user
    if not service.can_use(user):
        return None
    rows = list(Roundup.objects.filter(owner=user))
    if not rows:
        return {"intro": True}
    if not any(r.enabled for r in rows):
        return None
    deliver.catch_up(user)                   # a due one is made now if the scheduled task hasn't (no ping)
    rows = list(Roundup.objects.filter(owner=user))
    issue = service.current(user, enabled=True) or service.current(user)
    nxt = min((r.next_run_at for r in rows if r.enabled and r.next_run_at), default=None)
    card = {"next": schedule.local(user, nxt) if nxt else None}
    if card["next"]:
        n = card["next"]
        card["next_text"] = f"{schedule.DAY_NAMES[n.weekday()]} {schedule.clock(n.time())}"
    if issue is not None:
        card.update(present(issue, user))
    card.update(sheet_context(user))
    return card


@login_required
def briefing(request):
    _need(request)
    user = request.user
    deliver.catch_up(user)
    issue = None
    if request.GET.get("issue"):
        try:
            issue = get_object_or_404(RoundupIssue.objects.select_related("roundup"), pk=int(request.GET["issue"]),
                                      owner=user)
        except ValueError:
            raise Http404
        scope = issue.scope
    else:
        scope = request.GET.get("scope")
        if scope not in SCOPE_KEYS:
            latest = service.current(user)
            scope = latest.scope if latest else WEEK
        issue = service.current(user, scope) or service.issue_for(schedule.preset(user, scope))
    if issue.read_at is None:
        RoundupIssue.objects.filter(pk=issue.pk).update(read_at=timezone.now())
    return render(request, "roundup/briefing.html", {
        **present(issue, user), **sheet_context(user), "history": service.history(user),
        "scopes": SCOPES, "sheet_open": request.GET.get("schedule") == "1",
        "here": request.get_full_path()})


@login_required
@require_POST
def refresh(request):
    _need(request)
    scope = request.POST.get("scope")
    if scope not in SCOPE_KEYS:
        raise Http404
    service.issue_for(schedule.preset(request.user, scope), rebuild=True)
    return redirect(reverse("roundup:briefing") + f"?scope={scope}")


def _time(value, default):
    try:
        h, m = (int(x) for x in (value or "").split(":"))
        return time(h, m)
    except (TypeError, ValueError):
        return default


@login_required
@require_POST
def save_schedule(request):
    """The schedule sheet: each roundup on/off and when; push / email for all of them."""
    _need(request)
    post = request.POST
    channels = [c for c, on in ((PUSH, post.get("push")), (EMAIL, post.get("email"))) if on]
    can_ai = ai.available(request.user) is not None
    for r in schedule.ensure_presets(request.user):
        k = f"r{r.pk}_"
        r.enabled = bool(post.get(k + "on"))
        r.send_time = _time(post.get(k + "time"), r.send_time)
        if r.scope == WEEK and (post.get(k + "day") or "").isdigit():
            r.weekdays = [min(int(post[k + "day"]), 6)]
        if r.scope == TODAY and post.get(k + "freq") in ("daily", "weekdays"):
            r.freq = post[k + "freq"]
        if r.scope == MONTH and (post.get(k + "mday") or "").isdigit():
            r.month_day = min(max(int(post[k + "mday"]), 1), 28)
        r.channels = channels
        if can_ai:
            r.ai_summary = bool(post.get("ai"))
        schedule.reschedule(r, save=False)
        r.save()
    return _back(request, reverse("roundup:briefing"))


@login_required
@require_POST
def turn_on(request):
    """Home's intro card "Turn on": the Sunday "Week ahead", and this week's briefing made right away."""
    _need(request)
    r = schedule.preset(request.user, WEEK)
    r.enabled = True
    schedule.reschedule(r, save=False)
    r.save()
    service.issue_for(r)
    return _back(request, reverse("labs:hub"))


@login_required
@require_POST
def not_now(request):
    """Home's intro card "Not now": the presets are made (all off), so the intro doesn't come back."""
    _need(request)
    schedule.ensure_presets(request.user)
    return _back(request, reverse("labs:hub"))
