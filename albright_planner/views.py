"""Albright Planner pages (/planner/): Day, Week and Month views (the last one
you used reopens), quick add, the full item form, check-off, drag-to-move and
sharing.

Who can do what - checked here, on the server:
  - see an item: its owner and the people it's shared with; anyone else gets 404;
  - check it off, edit or move it, skip one occurrence: the owner and people it's shared with "Can edit";
  - share it, delete it (or end a series): the owner only.
Opening the Planner at all needs access in Admin > Users (albright_labs.app_access)."""
import json
from datetime import date, datetime, timedelta
from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST

from . import planner, quickadd, recurrence
from .forms import ItemForm
from .models import ItemShare, ScheduleItem

VIEWS = ("day", "week", "month")
VIEW_COOKIE = "planner_view"
FIRST_HOUR, LAST_HOUR = 7, 20


def _day(value, default=None):
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        return default


def _wants_json(request):
    return request.headers.get("X-Requested-With") == "XMLHttpRequest"


def _json(request):
    try:
        data = json.loads(request.body or b"{}")
    except (ValueError, UnicodeDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _next(request, fallback="albright_planner:home"):
    target = request.POST.get("next") or request.GET.get("next") or ""
    if target and url_has_allowed_host_and_scheme(target, {request.get_host()}, require_https=request.is_secure()):
        return target
    return reverse(fallback)


def _visible(request, pk):
    return get_object_or_404(ScheduleItem.objects.visible_to(request.user).select_related("owner"), pk=pk)


def _editable(request, pk):
    item = _visible(request, pk)
    if not item.can_edit(request.user):
        raise Http404              # view-only for this user: no write endpoint admits it exists for them
    return item


def _owned(request, pk):
    return get_object_or_404(ScheduleItem, pk=pk, owner=request.user)


# ---------------------------------------------------------------------------
# Day / Week / Month
# ---------------------------------------------------------------------------

def _filters(request):
    kind = request.GET.get("kind", "")
    return {"kind": kind if kind in (ScheduleItem.TASK, ScheduleItem.EVENT) else "",
            "show_done": request.GET.get("done") != "0", "shared_only": request.GET.get("shared") == "1"}


def _filter_params(f):
    return {k: v for k, v in (("kind", f["kind"]), ("done", "" if f["show_done"] else "0"),
                              ("shared", "1" if f["shared_only"] else "")) if v}


def _frame(request, view, day, prev_day, next_day, title):
    """What every view's header needs: tabs, Today / arrows, quick add, filters."""
    f = _filters(request)
    keep = _filter_params(f)
    query = urlencode(keep)

    def link(name, when):
        params = {**({"date": when.isoformat()} if when else {}), **keep}
        return reverse(f"albright_planner:{name}") + ("?" + urlencode(params) if params else "")
    return {
        "view": view, "day": day, "today": planner.today(), "title": title, "filters": f, "query": query,
        "tabs": [(v, v.title(), link(v, day)) for v in VIEWS],
        "prev_url": link(view, prev_day), "next_url": link(view, next_day), "today_url": link(view, None),
        "here": request.get_full_path(),
        "day_url": lambda d: link("day", d),
    }


def _remember(response, view):
    response.set_cookie(VIEW_COOKIE, view, max_age=365 * 24 * 3600, samesite="Lax")
    return response


@login_required
def home(request):
    """/planner/: the view you used last (Day the first time)."""
    view = request.COOKIES.get(VIEW_COOKIE)
    target = reverse(f"albright_planner:{view if view in VIEWS else 'day'}")
    query = request.META.get("QUERY_STRING", "")
    return redirect(f"{target}?{query}" if query else target)


def _split(occs):
    """(all-day items and untimed tasks, timed items)."""
    return [o for o in occs if o.start_time is None], [o for o in occs if o.start_time is not None]


@login_required
@never_cache
def day_view(request):
    today = planner.today()
    day = _day(request.GET.get("date"), today)
    f = _filters(request)
    occs = planner.occurrences(request.user, day, day, **f)
    top, timed = _split(occs)
    overdue = planner.overdue(request.user, day, kind=f["kind"], shared_only=f["shared_only"]) if day == today else []
    first = min([FIRST_HOUR] + [o.start_time.hour for o in timed])
    last = max([LAST_HOUR] + [o.start_time.hour for o in timed])
    hours = [{"hour": h, "label": datetime(2000, 1, 1, h).strftime("%I %p").lstrip("0"),
              "items": [o for o in timed if o.start_time.hour == h]} for h in range(first, last + 1)]
    context = _frame(request, "day", day, day - timedelta(days=1), day + timedelta(days=1),
                     f"{day:%A, %B} {day.day}" + (f", {day.year}" if day.year != today.year else ""))
    context.update({"top": top, "hours": hours, "overdue": overdue, "is_today": day == today,
                    "empty": not (occs or overdue)})
    return _remember(render(request, "albright_planner/day.html", context), "day")


@login_required
@never_cache
def week_view(request):
    today = planner.today()
    day = _day(request.GET.get("date"), today)
    start = planner.week_start(day)
    end = start + timedelta(days=6)
    occs = planner.occurrences(request.user, start, end, **_filters(request))
    days = []
    for i in range(7):
        d = start + timedelta(days=i)
        top, timed = _split([o for o in occs if o.date == d])
        days.append({"date": d, "top": top, "timed": timed, "is_today": d == today})
    title = f"{start:%b} {start.day} – " + (f"{end.day}" if end.month == start.month else f"{end:%b} {end.day}") + f", {end.year}"
    context = _frame(request, "week", day, day - timedelta(days=7), day + timedelta(days=7), title)
    context["days"] = days
    return _remember(render(request, "albright_planner/week.html", context), "week")


def _add_months(day, n):
    month = day.month - 1 + n
    return date(day.year + month // 12, month % 12 + 1, 1)


@login_required
@never_cache
def month_view(request):
    today = planner.today()
    day = _day(request.GET.get("date"), today)
    first = day.replace(day=1)
    grid_start = planner.week_start(first)
    last = _add_months(first, 1) - timedelta(days=1)
    grid_end = planner.week_start(last) + timedelta(days=6)
    occs = planner.occurrences(request.user, grid_start, grid_end, **_filters(request))
    context = _frame(request, "month", day, _add_months(first, -1), _add_months(first, 1), f"{first:%B %Y}")
    weeks, d = [], grid_start
    while d <= grid_end:
        week = []
        for _ in range(7):
            items = [o for o in occs if o.date == d]
            week.append({"date": d, "items": items[:3], "more": max(0, len(items) - 3), "in_month": d.month == first.month,
                         "is_today": d == today, "url": context["day_url"](d)})
            d += timedelta(days=1)
        weeks.append(week)
    context.update({"weeks": weeks, "weekdays": recurrence.DAY_NAMES})
    return _remember(render(request, "albright_planner/month.html", context), "month")


# ---------------------------------------------------------------------------
# Adding and editing
# ---------------------------------------------------------------------------

@login_required
@require_POST
def quick_add(request):
    text = request.POST.get("text", "").strip()[:500]
    if not text:
        messages.error(request, "Type what to add, e.g. “Call dentist tomorrow 3pm”.")
        return redirect(_next(request))
    fields = quickadd.parse(text, planner.today())
    item = ScheduleItem.objects.create(owner=request.user, created_by=request.user.get_username(), **fields)
    when = recurrence.summary(item.recurrence, item.date, item.recurrence_until, item.recurrence_count) or \
        f"{item.date:%a %b} {item.date.day}"
    if item.start_time:
        when += " at " + item.start_time.strftime("%I:%M %p").lstrip("0")
    messages.success(request, f"Added “{item.title}” – {when}.")
    return redirect(_next(request))


@login_required
def new_item(request):
    initial = {"date": _day(request.GET.get("date"), planner.today()), "kind": request.GET.get("kind") or "task"}
    form = ItemForm(request.POST or None, initial=initial)
    if request.method == "POST" and form.is_valid():
        changes = form.changes()
        changes.pop("date")
        item = ScheduleItem(owner=request.user, created_by=request.user.get_username(), date=form.cleaned_data["date"])
        for name, value in changes.items():
            setattr(item, name, value)
        item.save()
        messages.success(request, f"Added “{item.title}”.")
        return redirect(_next(request))
    return render(request, "albright_planner/form.html", {"form": form, "item": None, "next": _next(request)})


@login_required
def edit_item(request, pk):
    item = _visible(request, pk)
    occurrence = _day(request.GET.get("date") or request.POST.get("occurrence"), item.date)
    if item.repeats and not recurrence.is_scheduled(item, occurrence):
        occurrence = item.date
    can_edit, is_owner = item.can_edit(request.user), item.owner_id == request.user.pk
    form = ItemForm(request.POST or None, initial=ItemForm.initial_for(item, occurrence), show_scope=item.repeats)
    if request.method == "POST":
        if not can_edit:
            raise Http404
        if form.is_valid():
            scope = form.cleaned_data.get("scope") or planner.ALL
            planner.change(item, occurrence, scope, form.changes())
            messages.success(request, "Saved." if not item.repeats else
                             {"this": "Saved this occurrence.", "following": "Saved this and the following occurrences.",
                              "all": "Saved every occurrence."}[scope])
            return redirect(_next(request))
    shares = list(item.shares.select_related("user")) if is_owner else []
    taken = {s.user_id for s in shares} | {item.owner_id}
    return render(request, "albright_planner/form.html", {
        "form": form, "item": item, "occurrence": occurrence, "can_edit": can_edit, "is_owner": is_owner,
        "summary": recurrence.summary(item.recurrence, item.date, item.recurrence_until, item.recurrence_count),
        "done": item.completions.filter(occurrence_date=occurrence).exists(), "next": _next(request),
        "shares": shares, "permissions": ItemShare.PERMISSIONS,
        "share_choices": [u for u in planner.planner_users() if u.pk not in taken] if is_owner else [],
    })


@login_required
@require_POST
def toggle(request, pk):
    """Check off (or undo) one occurrence: POST date=<the occurrence's scheduled date>."""
    item = _editable(request, pk)
    occurrence = _day(request.POST.get("date"))
    done = planner.toggle(item, occurrence, request.user) if occurrence else None
    if done is None:
        if _wants_json(request):
            return JsonResponse({"ok": False, "error": "Not scheduled that day."}, status=400)
        messages.error(request, "That item isn't scheduled that day.")
        return redirect(_next(request))
    if _wants_json(request):
        return JsonResponse({"ok": True, "done": done})
    return redirect(_next(request))


@login_required
@require_POST
def move(request, pk):
    """Drag-to-move (JSON {date, to, scope?}): one-off items just move; a repeating one needs the scope."""
    item = _editable(request, pk)
    data = _json(request) if request.content_type == "application/json" else request.POST
    occurrence, to = _day(data.get("date")), _day(data.get("to"))
    scope = data.get("scope") or (planner.THIS if item.repeats else planner.ALL)
    if not occurrence or not to or scope not in dict(planner.SCOPES) or not recurrence.is_scheduled(item, occurrence):
        return JsonResponse({"ok": False, "error": "Bad move."}, status=400)
    planner.change(item, occurrence, scope, {"date": to})
    return JsonResponse({"ok": True})


@login_required
@require_POST
def remove(request, pk):
    """Delete (one-off), or for a series: skip this occurrence (editors too), end it here, or delete it all (owner)."""
    item = _visible(request, pk)
    occurrence = _day(request.POST.get("date"), item.date)
    scope = request.POST.get("scope") or planner.ALL
    owner = item.owner_id == request.user.pk
    if scope not in dict(planner.SCOPES) or not item.can_edit(request.user) or \
            (not owner and not (item.repeats and scope == planner.THIS)):
        raise Http404
    title = item.title
    planner.remove(item, occurrence, scope)
    messages.success(request, f"Skipped “{title}” on {occurrence:%b} {occurrence.day}." if item.repeats and scope == planner.THIS
                     else f"Deleted “{title}”.")
    return redirect(_next(request))


# ---------------------------------------------------------------------------
# Sharing (owner only)
# ---------------------------------------------------------------------------

@login_required
@require_POST
def share_add(request, pk):
    item = _owned(request, pk)
    permission = request.POST.get("permission", ItemShare.VIEW)
    target = planner.planner_users().exclude(pk=request.user.pk).filter(pk=request.POST.get("user") or 0).first()
    if target is None or permission not in dict(ItemShare.PERMISSIONS):
        messages.error(request, "Pick someone who can open the Planner.")
    else:
        ItemShare.objects.update_or_create(item=item, user=target, defaults={"permission": permission})
        messages.success(request, f"Shared with {target.get_username()}.")
    return redirect(reverse("albright_planner:edit", args=[item.pk]) + "#sharing")


@login_required
@require_POST
def share_update(request, pk, share_id):
    item = _owned(request, pk)
    share = get_object_or_404(ItemShare, pk=share_id, item=item)
    if request.POST.get("remove"):
        share.delete()
        messages.success(request, f"Stopped sharing with {share.user.get_username()}.")
    elif request.POST.get("permission") in dict(ItemShare.PERMISSIONS):
        share.permission = request.POST["permission"]
        share.save(update_fields=["permission"])
    return redirect(reverse("albright_planner:edit", args=[item.pk]) + "#sharing")
