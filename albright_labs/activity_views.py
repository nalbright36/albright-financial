"""Notifications and activity pages (the rules: albright_labs.activity).

  - the header bell's panel (panel): recent notifications, loaded when the bell opens;
  - open (a notification): marks it read and goes to the thing - only for events the person can see right now;
  - Mark all read;
  - My account > Activity: the full feed (my own actions included), filtered by circle, app and person, or unread;
  - My account > Notifications: per app and per circle All / Only things that involve me / Off, quiet hours and the
    daily email digest.
"""
from datetime import time

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.http import Http404, JsonResponse
from django.shortcuts import redirect, render
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST

from . import activity, circles, sharing
from .models import CIRCLE_APPS, NotificationSettings

PANEL_SIZE = 20
PAGE_SIZE = 50


def _apps(user):
    return [(key, activity.APP_LABELS[key]) for key in CIRCLE_APPS if circles.can_use_app(user, key)] + \
        [("circles", activity.APP_LABELS["circles"])]


@login_required
@never_cache
def panel(request):
    """The bell's panel (labs/notifications.js fetches it when the bell opens)."""
    prefs = activity.prefs_for(request.user)
    events = activity.notifications(request.user, prefs)[:PANEL_SIZE]
    rows = activity.rows(request.user, events, prefs)
    return render(request, "albright_labs/_notifications.html", {
        "rows": rows, "unread": activity.unread_count(request.user)})


@login_required
def open_event(request, pk):
    """Tapping a notification: mark it read and open the thing (404 if they can't see it any more)."""
    event = activity.visible(request.user).filter(pk=pk).first()
    if event is None:
        raise Http404
    activity.mark_read(request.user, event)
    target = event.url
    if not target or not url_has_allowed_host_and_scheme(target, allowed_hosts={request.get_host()},
                                                         require_https=request.is_secure()):
        target = "labs:activity"
    return redirect(target)


@login_required
@require_POST
def read_all(request):
    activity.mark_all_read(request.user)
    if request.headers.get("x-requested-with") == "XMLHttpRequest" or "application/json" in request.headers.get("accept", ""):
        return JsonResponse({"ok": True, "unread": 0})
    messages.success(request, "All caught up.")
    nxt = request.POST.get("next", "")
    if nxt and url_has_allowed_host_and_scheme(nxt, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
        return redirect(nxt)
    return redirect("labs:activity")


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


@login_required
def activity_page(request):
    """My account > Activity: everything I can see - mine too - newest first, with filters."""
    user = request.user
    prefs = activity.prefs_for(user)
    apps = _apps(user)
    app = request.GET.get("app", "")
    app = app if app in dict(apps) else ""
    my_circles = [c for c, _m in circles.my_circles(user)]
    circle = _int(request.GET.get("circle"))
    circle = circle if circle in {c.pk for c in my_circles} else None
    person = _int(request.GET.get("person"))
    unread_only = request.GET.get("unread") == "1"
    qs = activity.visible(user)
    people_ids = set(qs.exclude(actor__isnull=True).values_list("actor_id", flat=True).distinct()[:200])
    people = sorted(get_user_model().objects.filter(pk__in=people_ids), key=lambda u: sharing.person_name(u).lower())
    if app:
        qs = qs.filter(app=app)
    if circle:
        qs = qs.filter(circle_id=circle)
    if person:
        qs = qs.filter(actor_id=person)
    if unread_only:
        qs = activity.unread(user, qs, prefs)
    page = Paginator(qs, PAGE_SIZE).get_page(request.GET.get("page"))
    return render(request, "albright_labs/activity.html", {
        "rows": activity.rows(user, page.object_list, prefs), "page": page, "apps": apps, "app": app,
        "circles": my_circles, "circle": circle, "people": [(u.pk, sharing.person_name(u)) for u in people],
        "person": person, "unread_only": unread_only, "filtered": bool(app or circle or person or unread_only),
        "unread_total": activity.unread_count(user)})


def _time(value):
    try:
        return time.fromisoformat(value) if value else None
    except ValueError:
        return None


@login_required
def notification_settings(request):
    """My account > Notifications."""
    user = request.user
    prefs = NotificationSettings.for_user(user)
    apps = _apps(user)
    my_circles = [c for c, _m in circles.my_circles(user)]
    keys = [f"app:{key}" for key, _label in apps] + [f"circle:{c.pk}" for c in my_circles]
    if request.method == "POST":
        levels = dict(prefs.levels or {})
        for key in keys:
            value = request.POST.get(key, NotificationSettings.ALL)
            if value == NotificationSettings.ALL:
                levels.pop(key, None)
            elif value in (NotificationSettings.MINE, NotificationSettings.OFF):
                levels[key] = value
        prefs.levels = levels
        if request.POST.get("quiet") == "1":
            start, end = _time(request.POST.get("quiet_start")), _time(request.POST.get("quiet_end"))
            if start is None or end is None or start == end:
                messages.error(request, "Pick when quiet hours start and end (different times).")
                return redirect("labs:notification_settings")
            prefs.quiet_start, prefs.quiet_end = start, end
        else:
            prefs.quiet_start = prefs.quiet_end = None
        prefs.digest = request.POST.get("digest") == "1"
        prefs.save()
        messages.success(request, "Notification settings saved.")
        return redirect("labs:notification_settings")
    level_rows = [{"key": f"app:{key}", "label": label, "value": activity.level(prefs, f"app:{key}")} for key, label in apps]
    circle_rows = [{"key": f"circle:{c.pk}", "label": c.name, "value": activity.level(prefs, f"circle:{c.pk}")}
                   for c in my_circles]
    return render(request, "albright_labs/notification_settings.html", {
        "prefs": prefs, "app_rows": level_rows, "circle_rows": circle_rows, "levels": NotificationSettings.LEVELS,
        "has_email": bool(user.email), "quiet_now": activity.in_quiet_hours(prefs)})


def recent_for_circle(user, circle, limit=15):
    """A circle page's own recent activity (what this person can see of it)."""
    return activity.rows(user, activity.visible(user).filter(circle=circle)[:limit])

