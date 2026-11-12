"""Albright Planner pages (/planner/): Day, Week and Month views (the last one
you used reopens), quick add, the full item form, check-off, drag-to-move and
sharing.

Who can do what - checked here, on the server:
  - see an item: its owner and the people it's shared with; anyone else gets 404;
  - check it off, edit or move it, skip one occurrence: the owner and people it's shared with "Can edit";
  - share it, delete it (or end a series): the owner only;
  - attach a note: someone who can edit the item, and only a note they can see (Notes access needed).
Items another app keeps up to date (sources.py) are brought up to date when a view opens; planned meals
are read live from Meals for people who can open it (and haven't hidden them).
Opening the Planner at all needs access in Admin > Users (albright_labs.app_access)."""
import json
from datetime import date, datetime, timedelta
from urllib.parse import urlencode
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST

from albright_labs import sharing

from . import checklist, planner, quickadd, recurrence, sources, weather
from .forms import FromNoteForm, GroceryTaskForm, ItemForm
from .models import NOTES_MAX, PlannerSettings, ScheduleItem, home_zone

VIEWS = ("day", "week", "month")
VIEW_COOKIE = "planner_view"
FIRST_HOUR, LAST_HOUR = 7, 20


def _day(value, default=None):
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        pass
    # <input type="month"> submits YYYY-MM; treat it as the first of that month.
    if isinstance(value, str) and len(value) == 7 and value[4] == "-":
        try:
            return date(int(value[:4]), int(value[5:]), 1)
        except ValueError:
            pass
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
    return get_object_or_404(ScheduleItem.objects.visible_to(request.user).select_related("owner", "parent"), pk=pk)


def _editable(request, pk):
    item = _visible(request, pk)
    if not item.can_edit(request.user):
        raise Http404              # view-only for this user: no write endpoint admits it exists for them
    return item


def _owned(request, pk):
    return get_object_or_404(ScheduleItem, pk=pk, owner=request.user)


def _view_url(item, day, back=""):
    """The item's view panel for one occurrence: /planner/item/<id>/?date=YYYY-MM-DD[&next=...]."""
    params = {"date": day.isoformat(), **({"next": back} if back else {})}
    return reverse("albright_planner:item", args=[item.pk]) + "?" + urlencode(params)


# ---------------------------------------------------------------------------
# Day / Week / Month
# ---------------------------------------------------------------------------

def _options(request):
    """The views' filters and the Meals overlay: View options' saved choices (PlannerSettings - the same on every
    device), unless this request says otherwise (?kind= / ?done= / ?who= / ?meals= - links that carry a filter,
    and View options' live preview before Save)."""
    if not hasattr(request, "_planner_options"):
        prefs, get = PlannerSettings.for_user(request.user), request.GET
        kind = get["kind"] if "kind" in get else prefs.filter_kind
        who = get["who"] if "who" in get else prefs.filter_who
        if get.get("shared") == "1":                # the old "Shared with me" checkbox's links
            who = planner.SHARED
        request._planner_options = {
            "kind": kind if kind in (ScheduleItem.TASK, ScheduleItem.EVENT) else "",
            "show_done": get["done"] != "0" if "done" in get else not prefs.hide_done,
            "who": who if who in dict(planner.WHO) else "",
            "meals": get["meals"] == "1" if "meals" in get else prefs.show_meals,
            "prefs": prefs}
    return request._planner_options


def _filters(request):
    """planner.occurrences()'s filters for this request."""
    o = _options(request)
    return {"kind": o["kind"], "show_done": o["show_done"], "who": o["who"]}


def _filter_params(o):
    """The query that keeps this page's filters on its links: only what differs from the saved choices."""
    prefs, out = o["prefs"], {}
    if o["kind"] != prefs.filter_kind:
        out["kind"] = o["kind"]
    if o["show_done"] == prefs.hide_done:
        out["done"] = "1" if o["show_done"] else "0"
    if o["who"] != prefs.filter_who:
        out["who"] = o["who"]
    if o["meals"] != prefs.show_meals:
        out["meals"] = "1" if o["meals"] else "0"
    return out


def is_filtered(options, meals_toggle):
    """View options' dot: something is filtered out compared with the defaults (everything, done items shown, all
    people, meals shown)."""
    return bool(options["kind"] or not options["show_done"] or options["who"] or (meals_toggle and not options["meals"]))


KIND_CHOICES = [("", "Tasks and events"), (ScheduleItem.TASK, "Tasks only"), (ScheduleItem.EVENT, "Events only")]


def _jump_label(view, day, today):
    """Compact label on the toolbar date button: 'Wed, Nov 4', 'Nov 2 – 8', 'November 2026'."""
    if view == "day":
        text = f"{day:%a, %b} {day.day}"
        return f"{text}, {day.year}" if day.year != today.year else text
    if view == "week":
        start = planner.week_start(day)
        end = start + timedelta(days=6)
        text = f"{start:%b} {start.day} – " + (f"{end.day}" if end.month == start.month else f"{end:%b} {end.day}")
        if start.year != end.year or end.year != today.year:
            text += f", {end.year}"
        return text
    return f"{day:%B %Y}"


def _on_today(view, day, today):
    """True when the Day / Week / Month being shown is the one that contains today."""
    if view == "day":
        return day == today
    if view == "week":
        start = planner.week_start(day)
        return start <= today <= start + timedelta(days=6)
    return day.month == today.month and day.year == today.year


def _frame(request, view, day, prev_day, next_day, title):
    """What every view's header needs: the title, Day / Week / Month (plain links that keep this date),
    arrows, a date button that opens the date (or month) jump, Today when you are not already there,
    View options (with its dot) and New item."""
    o = _options(request)
    keep = _filter_params(o)
    query = urlencode(keep)

    def link(name, when):
        params = {**({"date": when.isoformat()} if when else {}), **keep}
        return reverse(f"albright_planner:{name}") + ("?" + urlencode(params) if params else "")
    meals_toggle = sources.can_open(request.user, "meals")
    here = request.get_full_path()
    today = planner.today(request.user)
    return {
        "view": view, "day": day, "today": today, "title": title, "filters": o, "query": query,
        "tabs": [(v, v.title(), link(v, day)) for v in VIEWS],
        "prev_url": link(view, prev_day), "next_url": link(view, next_day), "today_url": link(view, None),
        "view_path": reverse(f"albright_planner:{view}"),
        "filter_pairs": list(keep.items()),
        "jump_kind": "month" if view == "month" else "date",
        "jump_value": f"{day.year:04d}-{day.month:02d}" if view == "month" else day.isoformat(),
        "jump_label": _jump_label(view, day, today),
        "on_today": _on_today(view, day, today),
        "here": here,
        "day_url": lambda d: link("day", d),
        "meals_toggle": meals_toggle,
        "can_check_groceries": meals_toggle,                               # checklist rows linked to the list
        "meals_on": o["meals"], "filtered": is_filtered(o, meals_toggle),
        "who_choices": planner.WHO, "kind_choices": KIND_CHOICES,
        "new_url": reverse("albright_planner:new") + "?" + urlencode({"date": day.isoformat(), "next": here}),
        **_default_sharing(request.user),
    }


def _default_sharing(user):
    """New items' sharing (My account > Circles > Sharing defaults - View options links there): quick add uses it."""
    from albright_labs import sharing
    text = sharing.default_text(user, "planner")
    return {"default_sharing": text, "quick_share_label": "Only me" if text == "Only me" else f"Shared with {text}"}


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
    today = planner.today(request.user)
    day = _day(request.GET.get("date"), today)
    f = _filters(request)
    occs = planner.occurrences(request.user, day, day, **f)
    overdue_occs = planner.overdue(request.user, day, kind=f["kind"], who=f["who"]) if day == today else []
    checklist.attach(occs + overdue_occs)
    top, timed = _split(occs)
    meals = sources.meals(request.user, day, day, show=_options(request)["meals"])
    top = meals + top
    overdue = overdue_occs
    day_wx = weather.attach(request.user, occs + overdue_occs, [day])
    first = min([FIRST_HOUR] + [o.start_time.hour for o in timed])
    last = max([LAST_HOUR] + [o.start_time.hour for o in timed])
    hours = [{"hour": h, "label": datetime(2000, 1, 1, h).strftime("%I %p").lstrip("0"),
              "items": [o for o in timed if o.start_time.hour == h]} for h in range(first, last + 1)]
    context = _frame(request, "day", day, day - timedelta(days=1), day + timedelta(days=1),
                     f"{day:%A, %B} {day.day}" + (f", {day.year}" if day.year != today.year else ""))
    context.update({"top": top, "hours": hours, "overdue": overdue, "is_today": day == today,
                    "empty": not (occs or overdue or meals),
                    "day_weather": day_wx.get(day), "weather_place": weather.home_label(request.user)})
    return _remember(render(request, "albright_planner/day.html", context), "day")


@login_required
@never_cache
def week_view(request):
    today = planner.today(request.user)
    day = _day(request.GET.get("date"), today)
    start = planner.week_start(day)
    end = start + timedelta(days=6)
    f = _filters(request)
    occs = checklist.attach(planner.occurrences(request.user, start, end, **f))
    meals = sources.meals(request.user, start, end, show=_options(request)["meals"])
    # Overdue one-offs / one-at-a-time tasks from before this week: pin onto Today (Day view already does this;
    # without it, Week hid anything whose date fell outside the seven days).
    overdue_outside = []
    if start <= today <= end:
        overdue_outside = [o for o in planner.overdue(request.user, today, kind=f["kind"], who=f["who"])
                           if not (start <= o.date <= end)]
        if overdue_outside:
            checklist.attach(overdue_outside)
    week_days = [start + timedelta(days=i) for i in range(7)]
    home_wx = weather.attach(request.user, list(occs) + overdue_outside, week_days)
    days = []
    for d in week_days:
        top, timed = _split([o for o in occs if o.date == d])
        if d == today and overdue_outside:
            top = overdue_outside + top
        top = [m for m in meals if m.date == d] + top
        days.append({"date": d, "top": top, "timed": timed, "is_today": d == today,
                     "weather": home_wx.get(d)})
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
    today = planner.today(request.user)
    day = _day(request.GET.get("date"), today)
    first = day.replace(day=1)
    grid_start = planner.week_start(first)
    last = _add_months(first, 1) - timedelta(days=1)
    grid_end = planner.week_start(last) + timedelta(days=6)
    occs = checklist.attach(planner.occurrences(request.user, grid_start, grid_end, **_filters(request)))
    meals = sources.meals(request.user, grid_start, grid_end, show=_options(request)["meals"])
    context = _frame(request, "month", day, _add_months(first, -1), _add_months(first, 1), f"{first:%B %Y}")
    weeks, d = [], grid_start
    while d <= grid_end:
        week = []
        for _ in range(7):
            items = [m for m in meals if m.date == d] + [o for o in occs if o.date == d]
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
    text, subtasks = quickadd.split_checklist(text)
    fields = quickadd.parse(text, planner.today(request.user))
    if subtasks:
        fields.update(kind=ScheduleItem.TASK, checkable=True)
    item = ScheduleItem.objects.create(owner=request.user, created_by=request.user.get_username(),
                                       timezone=home_zone(request.user), **fields)
    checklist.create_rows(item, subtasks)
    planner.apply_default_shares(item, request.user)             # shared the way new Planner items are
    when = recurrence.summary(item.recurrence, item.date, item.recurrence_until, item.recurrence_count) or \
        f"{item.date:%a %b} {item.date.day}"
    if item.start_time:
        when += " at " + item.start_time.strftime("%I:%M %p").lstrip("0")
    messages.success(request, f"Added “{item.title}” – {when}" + (f", with {len(subtasks)} checklist items." if subtasks
                                                                    else "."))
    return redirect(_next(request))


@login_required
def new_item(request):
    initial = {"date": _day(request.GET.get("date"), planner.today(request.user)), "kind": request.GET.get("kind") or "task"}
    form = ItemForm(request.POST or None, initial=initial)
    if request.method == "POST" and form.is_valid():
        changes = form.changes()
        changes.pop("date")
        item = ScheduleItem(owner=request.user, created_by=request.user.get_username(), date=form.cleaned_data["date"],
                            timezone=home_zone(request.user))
        for name, value in changes.items():
            setattr(item, name, value)
        item.save()
        if item.kind == ScheduleItem.TASK:
            checklist.create_rows(item, [text for _pk, text in checklist.posted_rows(request.POST)])
        _share_new(request, item)
        messages.success(request, f"Added “{item.title}”.")
        return redirect(_next(request))
    # ?panel=1: just the form - the New item dialog's "More options" (planner.js); it posts here like the page.
    template = "albright_planner/_item_form.html" if request.method == "GET" and request.GET.get("panel") == "1" \
        else "albright_planner/form.html"
    return render(request, template, {"form": form, "item": None, "next": _next(request),
                                      "share": sharing.control(request.user, None, "planner"),
                                      "checklist_edit": _editor_rows(request, [])})


def _editor_rows(request, rows):
    """The form's Checklist section: what was posted (a form with errors), else the subtasks - [{"pk", "text",
    "grocery"}]."""
    if request.method == "POST" and request.POST.get("cl_section"):
        by_pk = {r.pk: r for r in rows}
        return [{"pk": pk or "", "text": text, "grocery": bool(pk in by_pk and by_pk[pk].grocery_item_id)}
                for pk, text in checklist.posted_rows(request.POST) if text.strip() or pk in by_pk]
    return [{"pk": r.pk, "text": r.text, "grocery": bool(r.grocery_item_id)} for r in rows]


def _share_new(request, item):
    """The New form's Sharing (albright_labs/_share.html): the default, Only me, a circle or a person."""
    try:
        sharing.apply_posted(request, item)
    except sharing.ShareError as exc:
        messages.error(request, f"Not shared: {exc}")


@login_required
@require_POST
def notes_preview(request):
    """The notes box's Preview (templates/_md_editor.html): its Markdown as the view panel will show it - rendered
    and sanitized exactly like a note (albright_notes.render)."""
    from albright_notes.render import render as render_markdown
    return JsonResponse({"html": render_markdown(request.POST.get("notes", "")[:NOTES_MAX])})


@login_required
@never_cache
def view_item(request, pk):
    """An item's view panel for one occurrence (?date=, default its current / first one): what it is at a glance -
    check it off and tick its checklist here, Edit opens the form. planner.js opens it in a dialog over the
    calendar (?panel=1: just the panel); the page itself is for direct links and no JavaScript."""
    item = _visible(request, pk)
    occurrence = _day(request.GET.get("date"))
    if not item.repeats:
        occurrence = item.date
    elif occurrence is None or not planner.valid_occurrence(item, occurrence):
        occurrence = planner.current_or_first(item)
    back = _next(request)
    occ = planner.occurrence_for(item, occurrence, request.user)
    weather.attach(request.user, [occ], [occ.date])
    can_edit, is_owner = occ.can_edit, item.owner_id == request.user.pk
    rows = checklist.occurrence_rows(item, occurrence)
    shared_with = planner.shared_names(item) if is_owner else []
    can_skip = item.repeats and can_edit
    can_delete = is_owner and (can_edit or item.locked)
    template = "albright_planner/_view_panel.html" if request.GET.get("panel") == "1" else "albright_planner/item.html"
    return render(request, template, {
        "item": item, "o": occ, "occurrence": occurrence, "can_edit": can_edit, "is_owner": is_owner,
        "today": planner.today(request.user),
        "back": back, "self_url": _view_url(item, occurrence, back),
        "edit_url": reverse("albright_planner:edit", args=[item.pk]) + "?" + urlencode(
            {"date": occurrence.isoformat(), "next": back, "then": "view"}) if can_edit else "",
        "repeat_text": planner.plain_repeat(item), "shared_with": shared_with,
        "share": sharing.control(request.user, item),
        "checklist": rows, "checklist_progress": checklist.progress(rows) if rows else None,
        "can_check_groceries": sources.can_open(request.user, "meals"),
        "linked_note": planner.linked_note(item, request.user, full=True),
        "can_skip": can_skip, "can_move": can_edit, "can_delete": can_delete,
        "is_current": item.one_at_a_time and occurrence == planner.current_occurrence(item),
        "subtasks": list(ScheduleItem.objects.visible_to(request.user).filter(parent=item, archived=False)
                         .order_by("date", "pk")),
    })


def _after_edit(request, item, changes, scope, occurrence):
    """Where saving the form goes: back to the view panel it was opened from (then=view: the panel's page;
    then=panel: the calendar with the panel open over it - planner.js reads #pl-view=<id>-<date>), else `next`."""
    then = request.POST.get("then") or request.GET.get("then")
    if then not in ("view", "panel"):
        return _next(request)
    if not item.repeats:
        day = item.date
    else:
        day = occurrence if scope == planner.THIS else (changes.get("date") or occurrence)
        if not planner.valid_occurrence(item, day):
            day = planner.current_or_first(item)
    if then == "panel":
        return _next(request) + f"#pl-view={item.pk}-{day.isoformat()}"
    return _view_url(item, day, _next(request))


@login_required
def edit_item(request, pk):
    item = _visible(request, pk)
    occurrence = _day(request.GET.get("date") or request.POST.get("occurrence"))
    if occurrence is None or (item.repeats and not planner.valid_occurrence(item, occurrence)):
        occurrence = planner.current_or_first(item)
    can_edit, is_owner = item.can_edit(request.user), item.owner_id == request.user.pk
    viewer = planner.user_zone(request.user)
    saved_place = ((item.location_label, item.location_lat, item.location_lon)
                   if item.location_lat is not None else None)
    form = ItemForm(request.POST or None, initial=ItemForm.initial_for(item, occurrence, viewer),
                    show_scope=item.repeats, saved_place=saved_place)
    if request.method == "POST":
        if not can_edit:
            raise Http404
        if form.is_valid():
            scope = form.cleaned_data.get("scope") or planner.ALL
            changes = form.changes()
            if changes.get("start_time"):
                # Entered in my zone: back into the item's own, so the series keeps its time there.
                changes["date"], changes["start_time"], changes["end_time"] = planner.convert(
                    changes["date"], changes["start_time"], changes["end_time"], viewer, planner.zone(item.timezone))
            changed = planner.change(item, occurrence, scope, changes)
            if request.POST.get("cl_section") and changed.kind == ScheduleItem.TASK:
                checklist.save_rows(changed, checklist.posted_rows(request.POST))
            messages.success(request, "Saved." if not item.repeats else
                             {"this": "Saved this occurrence.", "following": "Saved this and the following occurrences.",
                              "all": "Saved every occurrence."}[scope])
            return redirect(_after_edit(request, changed, changes, scope, occurrence))
    shared_with = planner.shared_names(item) if is_owner else []
    completion = item.completions.select_related("completed_by").filter(occurrence_date=occurrence).first()
    can_note = can_edit and sources.can_open(request.user, "notes")
    rows = checklist.occurrence_rows(item, occurrence)
    return render(request, "albright_planner/form.html", {
        "checklist": rows, "checklist_progress": checklist.progress(rows) if rows else None,
        "checklist_edit": _editor_rows(request, checklist.current_rows(item)),
        "checklist_from": checklist.edit_from(item), "show_done_by": is_owner and bool(shared_with) or not is_owner,
        "can_check_groceries": sources.can_open(request.user, "meals"),
        "linked_note": planner.linked_note(item, request.user), "can_note": can_note,
        "note_choices": planner.attachable_notes(request.user) if can_note else [],
        "form": form, "item": item, "occurrence": occurrence, "can_edit": can_edit, "is_owner": is_owner,
        "can_remove": can_edit or (is_owner and item.locked),
        "summary": recurrence.summary(item.recurrence, item.date, item.recurrence_until, item.recurrence_count),
        "repeat_text": planner.repeat_text(item) if item.one_at_a_time else "",
        "is_current": item.one_at_a_time and occurrence == planner.current_occurrence(item),
        "done": completion is not None, "done_by": planner.person(completion.completed_by) if completion else "",
        "owner_name": planner.person(item.owner), "next": _next(request), **_then(request, item, occurrence),
        "shared_with": shared_with, "share": sharing.control(request.user, item),
    })


def _then(request, item, occurrence):
    """The form opened from a view panel (then=view / panel): Back and Cancel return to that panel."""
    then = request.POST.get("then") or request.GET.get("then")
    if then == "view":
        return {"then": then, "back_url": _view_url(item, occurrence, _next(request))}
    if then == "panel":
        return {"then": then, "back_url": _next(request) + f"#pl-view={item.pk}-{occurrence.isoformat()}"}
    return {"then": "", "back_url": _next(request)}


@login_required
@require_POST
def toggle(request, pk):
    """Check off (or undo) one occurrence: POST date=<the occurrence's scheduled date>."""
    item = _editable(request, pk)
    occurrence = _day(request.POST.get("date"))
    if occurrence and request.POST.get("all_subtasks") == "1" and planner.valid_occurrence(item, occurrence) \
            and not item.completions.filter(occurrence_date=occurrence).exists():
        # "Mark all subtasks done too?" - Yes (a grocery row needs Meals, as checking it anywhere does)
        groceries_ok = all(_can_check_grocery(request.user, row.grocery_item)
                           for row in item.checklist.filter(grocery_item__isnull=False).select_related("grocery_item"))
        if groceries_ok:
            checklist.mark_all(item, occurrence, request.user)
    done = planner.toggle(item, occurrence, request.user, done=True if request.POST.get("all_subtasks") == "1"
                          else None) if occurrence else None
    if done is None:
        if _wants_json(request):
            return JsonResponse({"ok": False, "error": "Not scheduled that day."}, status=400)
        messages.error(request, "That item isn't scheduled that day.")
        return redirect(_next(request))
    if _wants_json(request):
        rows = checklist.occurrence_rows(item, occurrence)
        shown = checklist.progress(rows) if rows else None
        return JsonResponse({"ok": True, "done": done, **({"progress": shown["label"], "progress_title": shown["title"]}
                                                           if shown else {})})
    return redirect(_next(request))


def _can_check_grocery(user, line):
    """A grocery row checks the line on a Meals grocery list itself: only someone who sees that line's space in Meals
    (albright_labs.circles - a task shared with anyone else still shows them the row, read-only)."""
    from albright_meals.scope import space_ids
    return sources.can_open(user, "meals") and line is not None and line.space_id in space_ids(user)


@login_required
@require_POST
def checklist_toggle(request, pk, row_id):
    """Tick or untick one subtask on one occurrence (task editors): POST date=<the occurrence's scheduled date>
    (default: a one-off task's date / a one-at-a-time task's current one). A grocery row checks the line on the
    grocery list itself, so it also needs Meals."""
    item = _editable(request, pk)
    row = get_object_or_404(item.checklist.select_related("grocery_item"), pk=row_id)
    occurrence = _day(request.POST.get("date")) or planner.current_or_first(item)
    if (row.grocery_item_id and not _can_check_grocery(request.user, row.grocery_item)) \
            or not row.applies_to(occurrence) or not planner.valid_occurrence(item, occurrence):
        raise Http404
    before = {r.pk: r.checked for r in checklist.occurrence_rows(item, occurrence)}
    checklist.set_checked(row, occurrence, not before.get(row.pk), request.user)
    if _wants_json(request):
        rows = checklist.occurrence_rows(item, occurrence)
        now = next(r for r in rows if r.pk == row.pk)
        shared = item.owner_id != request.user.pk or bool(planner.shared_names(item))   # "Done by <name>" only then
        return JsonResponse({"ok": True, "checked": now.checked, "progress": checklist.progress(rows)["label"],
                             "open": checklist.progress(rows)["open"], "progress_title": checklist.progress(rows)["title"],
                             "done_by": planner.person(now.done_by) if now.checked and now.done_by and shared else "",
                             "task_done": item.completions.filter(occurrence_date=occurrence).exists()})
    return redirect(_next(request))


@login_required
@require_POST
def checklist_add(request, pk):
    """Add a row to a task's checklist (task editors - Edit sharing included)."""
    item = _editable(request, pk)
    checklist.add(item, request.POST.get("text") or "")    # a new open row reopens a finished occurrence
    return redirect(_next(request))


@login_required
@require_POST
def from_groceries(request):
    """Meals' grocery list "Create task": the picked lines (item=<pk>...) become a task owned by this user, its
    checklist reading and writing those lines. Needs Meals too (it reads the list)."""
    if not sources.can_open(request.user, "meals"):
        raise Http404
    from albright_meals.models import GroceryItem
    back = _next(request, "albright_meals:groceries")
    ids = [int(pk) for pk in request.POST.getlist("item") if pk.isdigit()]
    from albright_meals.scope import space_ids
    # Only lists in the user's own Meals spaces (anyone else's lines are never picked up).
    lines = list(GroceryItem.objects.active().filter(pk__in=ids, space_id__in=space_ids(request.user))
                 .order_by("aisle", "checked", "name", "pk"))
    form = GroceryTaskForm(request.POST)
    if not lines:
        messages.error(request, "Pick at least one item for the task.")
        return redirect(back)
    if not form.is_valid():
        messages.error(request, "Pick a date for the task" + (" (and a valid time)." if "time" in form.errors else "."))
        return redirect(back)
    data = form.cleaned_data
    task = checklist.task_from_groceries(request.user, lines, data["title"], data["date"], data["time"], data["note"])
    messages.success(request, f"Created \u201c{task.title}\u201d for {task.date:%a %b} {task.date.day} with "
                              f"{len(lines)} item{'s' if len(lines) != 1 else ''}.")
    return redirect(back)


@login_required
@require_POST
def move(request, pk):
    """Drag-to-move (JSON {date, to, scope?}): one-off items just move; a repeating one needs the scope. The view
    panel's Move posts a plain form (date, to) - a repeating item's occurrence moves alone - and goes back to next."""
    item = _editable(request, pk)
    as_form = request.content_type != "application/json" and not _wants_json(request)
    data = _json(request) if request.content_type == "application/json" else request.POST
    occurrence, to = _day(data.get("date")), _day(data.get("to"))
    scope = data.get("scope") or (planner.THIS if item.repeats else planner.ALL)
    if not occurrence or not to or scope not in dict(planner.SCOPES) or not planner.valid_occurrence(item, occurrence):
        if as_form:
            messages.error(request, "Pick the day to move it to.")
            return redirect(_next(request))
        return JsonResponse({"ok": False, "error": "Bad move."}, status=400)
    planner.change(item, occurrence, scope, {"date": to})
    if as_form:
        messages.success(request, f"Moved “{item.title}” to {to:%a %b} {to.day}.")
        return redirect(_next(request))
    return JsonResponse({"ok": True})


@login_required
@require_POST
def duplicate(request, pk):
    """The view panel's Duplicate: a copy of an item you can see, owned by you (planner.duplicate)."""
    item = _visible(request, pk)
    copy = planner.duplicate(item, request.user)
    messages.success(request, f"Duplicated “{copy.title}”.")
    return redirect(_next(request))


@login_required
@require_POST
def remove(request, pk):
    """Delete (one-off), or for a series: skip this occurrence (editors too), end it here, or delete it all (owner)."""
    item = _visible(request, pk)
    occurrence = _day(request.POST.get("date"), item.date)
    scope = request.POST.get("scope") or planner.ALL
    owner = item.owner_id == request.user.pk
    if scope not in dict(planner.SCOPES) or not (item.can_edit(request.user) or (owner and item.locked)) or \
            (not owner and not (item.repeats and scope == planner.THIS)):
        raise Http404
    title, one_at_a_time = item.title, item.one_at_a_time
    planner.remove(item, occurrence, scope)
    if one_at_a_time and scope == planner.THIS:
        nxt = planner.current_occurrence(item)
        messages.success(request, f"Skipped “{title}”" + (f" – next: {nxt:%a %b} {nxt.day}." if nxt else "."))
    else:
        messages.success(request, f"Skipped “{title}” on {occurrence:%b} {occurrence.day}." if item.repeats
                         and scope == planner.THIS else f"Deleted “{title}”.")
    return redirect(_next(request))


# ---------------------------------------------------------------------------
# Notes (an item stores the note's id - edits in either app show in both)
# ---------------------------------------------------------------------------

@login_required
@require_POST
def item_note(request, pk):
    """action=attach (note=<id> of a note you can see), new (a note titled from the item, linked), or detach."""
    from albright_notes import notes
    from albright_notes.models import Note
    item = _editable(request, pk)
    action = request.POST.get("action")
    back = reverse("albright_planner:edit", args=[item.pk]) + "#note"
    if action == "detach":
        item.note, item.note_deleted = None, False
        item.save(update_fields=["note", "note_deleted", "updated_at"])
        messages.success(request, "Removed the note from this item (the note itself is unchanged).")
        return redirect(back)
    if action not in ("attach", "new") or not sources.can_open(request.user, "notes"):
        raise Http404
    if action == "attach":
        note = Note.objects.visible_to(request.user).filter(pk=request.POST.get("note") or 0).first()
        if note is None:
            messages.error(request, "Pick one of your notes (or one shared with you).")
            return redirect(back)
    else:
        note = notes.create(request.user, item.title, author=request.user)
    item.note, item.note_deleted = note, False
    item.save(update_fields=["note", "note_deleted", "updated_at"])
    if action == "new":
        messages.success(request, f"Created the note “{note.title}” and linked it.")
        return redirect(reverse("albright_notes:note", args=[note.pk]))
    messages.success(request, f"Linked the note “{note.title}”.")
    return redirect(back)


@login_required
@require_POST
def from_note(request, note_id):
    """Notes' "Add to schedule": an item titled from the note, linked to it (date, optional time and repeat)."""
    from albright_notes.models import Note
    note = get_object_or_404(Note.objects.visible_to(request.user), pk=note_id)
    back = reverse("albright_notes:note", args=[note.pk])
    form = FromNoteForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Pick a date to schedule it on.")
        return redirect(back)
    d = form.cleaned_data
    item = ScheduleItem.objects.create(
        owner=request.user, created_by=request.user.get_username(), title=note.title[:200], date=d["date"],
        start_time=d.get("time"), recurrence=recurrence.build(d["repeat"], d["date"]), note=note,
        timezone=home_zone(request.user))
    planner.apply_default_shares(item)
    when = recurrence.summary(item.recurrence, item.date) or f"{item.date:%a %b} {item.date.day}"
    messages.success(request, f"Scheduled “{item.title}” – {when}.")
    return redirect(back)


# ---------------------------------------------------------------------------
# Settings: the Meals overlay and which apps may add items
# ---------------------------------------------------------------------------

@login_required
@require_POST
def toggle_meals(request):
    """The views' "Show meals" switch - remembered (PlannerSettings.show_meals)."""
    prefs = PlannerSettings.for_user(request.user)
    prefs.show_meals = request.POST.get("show") == "1"
    prefs.save(update_fields=["show_meals"])
    return redirect(_next(request))


@login_required
@require_POST
def view_options(request):
    """View options' Save: the filters and Show meals - stored for the account,
    so they're the same on phone and computer - then the current view (remembered like any other visit;
    Day / Week / Month are switched on the main display, not in this panel)."""
    prefs = PlannerSettings.for_user(request.user)
    kind, who = request.POST.get("kind", ""), request.POST.get("who", "")
    prefs.filter_kind = kind if kind in (ScheduleItem.TASK, ScheduleItem.EVENT) else ""
    prefs.filter_who = who if who in dict(planner.WHO) else ""
    prefs.hide_done = request.POST.get("hide_done") == "1"
    fields = ["filter_kind", "filter_who", "hide_done"]
    if sources.can_open(request.user, "meals"):
        prefs.show_meals = request.POST.get("show_meals") == "1"
        fields.append("show_meals")
    prefs.save(update_fields=fields)
    view = request.POST.get("view")
    view = view if view in VIEWS else "day"
    day = _day(request.POST.get("date"), planner.today(request.user))
    target = reverse(f"albright_planner:{view}") + ("" if day == planner.today(request.user) else f"?date={day.isoformat()}")
    if _wants_json(request):
        return _remember(JsonResponse({"ok": True, "url": target}), view)
    return _remember(redirect(target), view)


@login_required
@require_POST
def set_timezone(request):
    """The browser's zone (base.html's script): {"timezone": "America/Denver"}. Saved only while the zone follows
    the browser (auto_timezone); an unknown zone is refused."""
    try:
        body = json.loads(request.body or "{}") if request.content_type == "application/json" else request.POST
    except json.JSONDecodeError:
        body = {}
    name = str((body.get("timezone") if hasattr(body, "get") else "") or "").strip()[:64]
    if not _real_zone(name):
        return JsonResponse({"ok": False, "error": "Unknown time zone."}, status=400)
    prefs = PlannerSettings.for_user(request.user)
    changed = prefs.auto_timezone and prefs.timezone != name
    if changed:
        prefs.timezone = name
        prefs.save(update_fields=["timezone"])
    return JsonResponse({"ok": True, "timezone": prefs.timezone, "auto": prefs.auto_timezone, "changed": changed})


def _real_zone(name):
    if not name:
        return False
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return False
    return True


def _save_weather(request, prefs):
    """Apply the home-location field on a settings POST. Returns an error string, or
    None. Missing field (older tests, a stale form): leave what's stored alone."""
    if "weather_location" not in request.POST:
        return None
    raw = request.POST.get("weather_location", "").strip()[:200]
    if not raw:
        prefs.weather_label, prefs.weather_lat, prefs.weather_lon = "", None, None
        return None
    if weather.same_place(prefs.weather_label, prefs.weather_lat, prefs.weather_lon, raw):
        return None
    try:
        found = weather.geocode(raw)
    except weather.LookupDown:
        return "Couldn't look up that place right now - try again in a moment."
    if not found:
        return "Couldn't find that place - try a city and state, like Lone Tree, CO."
    prefs.weather_label, prefs.weather_lat, prefs.weather_lon = found["label"], found["lat"], found["lon"]
    return None


@login_required
def settings_view(request):
    prefs = PlannerSettings.for_user(request.user)
    available = sources.available(request.user)
    meals_toggle = sources.can_open(request.user, "meals")
    if request.method == "POST":
        chosen = set(request.POST.getlist("source"))
        offered = {s.key for s in available}
        # Sources this user can't open keep whatever they were set to.
        was_on = set(prefs.sources_on or [])
        prefs.sources_on = [k for k in sources.SOURCE_KEYS if (k in chosen if k in offered else k in was_on)]
        if meals_toggle:
            prefs.show_meals = request.POST.get("show_meals") == "1"
        prefs.complete_when_checklist_done = request.POST.get("complete_when_checklist_done") == "1"
        prefs.auto_timezone = request.POST.get("auto_timezone") == "1"
        zone_name = request.POST.get("timezone", "").strip()[:64]
        if not prefs.auto_timezone and zone_name and zone_name != prefs.timezone:
            if _real_zone(zone_name):
                prefs.timezone = zone_name
            else:
                messages.error(request, f"“{zone_name}” isn't a time zone - try one like America/Denver.")
        weather_error = _save_weather(request, prefs)
        prefs.save(update_fields=["sources_on", "show_meals", "complete_when_checklist_done", "auto_timezone",
                                  "timezone", "weather_label", "weather_lat", "weather_lon"])
        sources.sync(request.user)              # the only place source items are added or updated
        if weather_error:
            messages.error(request, weather_error)
        else:
            messages.success(request, "Planner settings saved.")
        return redirect("albright_planner:settings")
    return render(request, "albright_planner/settings.html", {
        "sources": [{"source": s, "on": prefs.source_on(s.key)} for s in available], "prefs": prefs,
        "meals_toggle": meals_toggle})
