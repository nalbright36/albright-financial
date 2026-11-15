"""Programs, made easy (/fitness/programs/...).

  - The list: mine (Draft / Paused / On Planner), "New program", "Start from a template".
  - The guided setup, three steps that are also how a program is edited later:
      1. Days      the name, and each workout with its weekdays (tap Mo-Su); "Start from a template" is a link here
      2. Exercises one tab per workout: type to search the library (or make my own in one tap), tap to add with a
                   sensible target, change targets in place, move up / down, remove
      3. Schedule  start date, how many weeks, "Add to my Planner" and an optional time - Save turns it on
    A new program is an off draft until step 3 is saved (programs.finish_setup), so it never shows on Today half-built.
  - The program page: read-only - Start the next workout, Edit (the steps), Pause / Resume, the Planner switch.
  - The exercise type-ahead (JSON) the setup's search box uses.
Everything is the owner's own - anyone else's program or exercise is a 404."""
from datetime import date, datetime, timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import Case, IntegerField, Value, When
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST

from . import programs, training
from .models import EQUIPMENT, Exercise, Program, ProgramDay, ProgramExercise

WEEKDAYS = [(0, "Mo", "Monday"), (1, "Tu", "Tuesday"), (2, "We", "Wednesday"), (3, "Th", "Thursday"), (4, "Fr", "Friday"),
            (5, "Sa", "Saturday"), (6, "Su", "Sunday")]
WEEKS = [("", "No end"), ("4", "4 weeks"), ("6", "6 weeks"), ("8", "8 weeks"), ("12", "12 weeks"), ("16", "16 weeks")]
MAX_DAYS = 7

SEARCH_LIMIT = 12
KIND_EQUIPMENT = {Exercise.CARDIO: "cardio_machine", Exercise.BODYWEIGHT: "bodyweight", Exercise.MOBILITY: "none"}


def search_exercises(user, q, limit=SEARCH_LIMIT):
    """Library + my own exercises whose name has every word, names that start with the text first."""
    q = " ".join((q or "").split())[:60]
    if not q:
        return []
    qs = Exercise.objects.visible_to(user)
    for word in q.split():
        qs = qs.filter(name__icontains=word)
    qs = qs.annotate(rank=Case(When(name__iexact=q, then=Value(0)), When(name__istartswith=q, then=Value(1)),
                               default=Value(2), output_field=IntegerField()))
    return list(qs.order_by("rank", "name")[:limit])


def exercise_json(e):
    return {"id": e.pk, "name": e.name, "kind": e.kind, "kind_label": e.get_kind_display(), "mine": e.owner_id is not None}


def create_exercise(user, name, kind=Exercise.STRENGTH):
    """My own exercise by that name - or the one I can already see by that name (any case). -> (exercise, created)"""
    name = " ".join((name or "").split())[:100]
    if not name:
        return None, False
    existing = Exercise.objects.visible_to(user).filter(name__iexact=name).first()
    if existing:
        return existing, False
    kind = kind if kind in dict(Exercise.KINDS) else Exercise.STRENGTH
    equipment = KIND_EQUIPMENT.get(kind, "other")
    return Exercise.objects.create(owner=user, name=name, kind=kind,
                                   equipment=equipment if equipment in dict(EQUIPMENT) else "other"), True


@login_required
@never_cache
def exercise_search(request):
    """GET ?q= -> {"results": [...], "create": the typed name when nothing matches it exactly (or null)}."""
    q = " ".join(request.GET.get("q", "").split())[:60]
    found = search_exercises(request.user, q)
    exact = any(e.name.lower() == q.lower() for e in found)
    return JsonResponse({"results": [exercise_json(e) for e in found], "create": q if q and not exact else None})


@login_required
@require_POST
def exercise_create(request):
    """POST name (+ kind) -> {"exercise": {...}, "created": bool}. Making one that already exists returns it."""
    ex, created = create_exercise(request.user, request.POST.get("name"), request.POST.get("kind", Exercise.STRENGTH))
    if ex is None:
        return JsonResponse({"error": "Give the exercise a name."}, status=400)
    return JsonResponse({"exercise": exercise_json(ex), "created": created}, status=201 if created else 200)


# ---------------------------------------------------------------------------
# The list and the program page
# ---------------------------------------------------------------------------

def _status(p):
    if p.is_draft:
        return "Draft"
    return "Paused" if not p.active else ""


@login_required
def program_list(request):
    if request.method == "POST":                             # older forms: copy a template / make one by name
        key = request.POST.get("template", "")
        if key in programs.TEMPLATES:
            return _from_template(request, key)
        name = " ".join(request.POST.get("name", "").split())[:100]
        if not name:
            messages.error(request, "Give the program a name.")
            return redirect("albright_fitness:programs")
        p = Program.objects.create(owner=request.user, name=name, active=False)
        return redirect("albright_fitness:program_setup", pk=p.pk)
    mine = list(Program.objects.filter(owner=request.user).prefetch_related("days"))
    return render(request, "albright_fitness/programs.html", {"programs": [(p, _status(p)) for p in mine]})


def _next_day(p, today):
    """(program day, date) of its next planned workout from today, or (None, None)."""
    days = [d for d in p.days.all() if d.weekdays]
    start = max(today, p.start_date or today)
    for offset in range(14):
        when = start + timedelta(days=offset)
        for d in days:
            if when.weekday() in d.weekdays:
                return d, when
    return None, None


@login_required
def program(request, pk):
    from . import log_views, views
    p = get_object_or_404(Program, pk=pk, owner=request.user)
    if request.method == "POST":
        action = request.POST.get("action")
        if action in ("pause", "resume"):
            programs.set_active(p, action == "resume")
            messages.success(request, "Paused - it's off Today and your Planner after today." if action == "pause" else
                             "Back on." + (" Its Planner tasks are back too." if p.on_planner else ""))
        elif action == "planner":
            programs.set_planner(p, request.POST.get("on") == "1")
            p.refresh_from_db()
            messages.success(request, "On your Planner." if p.on_planner else "Off your Planner after today - earlier ones stay.")
        else:
            views._program_post(request, p)                 # the older actions still work
        return redirect("albright_fitness:program", pk=p.pk)
    today = timezone.localdate()
    days = list(p.days.prefetch_related("exercises__exercise"))
    nxt, when = _next_day(p, today) if p.active else (None, None)
    return render(request, "albright_fitness/program.html", {
        "p": p, "days": days, "status": _status(p), "next_day": nxt, "next_when": when, "today": today,
        "ends": p.start_date + timedelta(weeks=p.weeks) - timedelta(days=1) if p.start_date and p.weeks else None,
        "start_url": programs.start_url(nxt) if nxt else "",
        "scheduled": log_views.scheduled_for(request.user, program=p)})   # older one-off sessions: still listed to remove


# ---------------------------------------------------------------------------
# The guided setup
# ---------------------------------------------------------------------------

def _steps(p, current):
    urls = [reverse("albright_fitness:program_setup", args=[p.pk]) if p else reverse("albright_fitness:program_new"),
            reverse("albright_fitness:program_setup_exercises", args=[p.pk]) if p else "",
            reverse("albright_fitness:program_setup_schedule", args=[p.pk]) if p else ""]
    return [{"n": n + 1, "label": label, "url": urls[n], "current": n + 1 == current}
            for n, label in enumerate(("Days", "Exercises", "Schedule"))]


def _rows_from_post(post):
    rows = []
    for i in range(min(int(post.get("rows") or 0) if (post.get("rows") or "").isdigit() else 0, MAX_DAYS)):
        rows.append({"i": i, "id": post.get(f"id_{i}", ""), "name": " ".join(post.get(f"name_{i}", "").split())[:60],
                     "weekdays": sorted({int(d) for d in post.getlist(f"weekdays_{i}") if d.isdigit() and int(d) < 7}),
                     "remove": bool(post.get(f"remove_{i}"))})
    return rows


@login_required
def program_new(request):
    return _setup_days(request, None)


@login_required
def setup_days(request, pk):
    return _setup_days(request, get_object_or_404(Program, pk=pk, owner=request.user))


def _setup_days(request, p):
    """Step 1: the name and each workout's weekdays."""
    errors = []
    if request.method == "POST":
        name = " ".join(request.POST.get("name", "").split())[:100]
        rows = _rows_from_post(request.POST)
        if request.POST.get("action") == "more" and len(rows) < MAX_DAYS:
            rows.append({"i": len(rows), "id": "", "name": "", "weekdays": [], "remove": False})
        else:
            kept = [r for r in rows if not r["remove"] and (r["name"] or r["id"])]
            if not name:
                errors.append("Give the program a name.")
            if not any(r["name"] for r in kept):
                errors.append("Add at least one workout - for example “Upper body” or “Workout A”.")
            for r in kept:
                if not r["name"]:
                    errors.append("Every workout needs a name (or tick Remove).")
                elif not r["weekdays"]:
                    errors.append(f"Pick the days for {r['name']}.")
            if not errors:
                p = _save_days(request.user, p, name, rows)
                return redirect("albright_fitness:program_setup_exercises", pk=p.pk)
    elif p:
        name = p.name
        rows = [{"i": i, "id": str(d.pk), "name": d.name, "weekdays": d.weekdays or [], "remove": False}
                for i, d in enumerate(p.days.all())] or [{"i": 0, "id": "", "name": "Workout A", "weekdays": [], "remove": False}]
    else:
        name, rows = "", [{"i": 0, "id": "", "name": "Workout A", "weekdays": [], "remove": False}]
    for r in rows:
        r["days"] = [(n, short, long, n in r["weekdays"]) for n, short, long in WEEKDAYS]
    return render(request, "albright_fitness/program_setup_days.html", {
        "p": p, "name": name, "rows": rows, "count": len(rows), "errors": errors, "steps": _steps(p, 1),
        "can_add": len(rows) < MAX_DAYS})


@transaction.atomic
def _save_days(user, p, name, rows):
    if p is None:
        p = Program.objects.create(owner=user, name=name, active=False)
    else:
        p.name = name
        p.save(update_fields=["name"])
    existing = {str(d.pk): d for d in p.days.all()}
    order = 0
    for r in rows:
        day = existing.get(r["id"])
        if r["remove"]:
            if day:
                day.delete()
            continue
        if not r["name"]:
            continue
        if day:
            day.name, day.weekdays, day.order = r["name"], r["weekdays"], order
            day.save(update_fields=["name", "weekdays", "order"])
        else:
            ProgramDay.objects.create(program=p, name=r["name"], weekdays=r["weekdays"], order=order)
        order += 1
    if p.active and p.scheduled_from:
        programs.schedule(p)                                  # the Planner follows the changes
    return p


def _number(text, cast=int, high=None):
    value = training.to_decimal(text) if cast is not int else (int(text) if (text or "").strip().isdigit() else None)
    if value is None or value <= 0 or (high is not None and value > high):
        return None
    return value


@login_required
def setup_exercises(request, pk):
    """Step 2: one tab per workout - search and add, change targets, move up / down, remove."""
    p = get_object_or_404(Program, pk=pk, owner=request.user)
    days = list(p.days.all())
    if not days:
        messages.error(request, "Add a workout first.")
        return redirect("albright_fitness:program_setup", pk=p.pk)
    by_pk = {str(d.pk): d for d in days}
    day = by_pk.get(request.GET.get("day") or request.POST.get("day") or "", days[0])
    here = reverse("albright_fitness:program_setup_exercises", args=[p.pk]) + f"?day={day.pk}"
    if request.method == "POST":
        for pe in day.exercises.all():                        # every button saves the targets first
            if f"sets_{pe.pk}" in request.POST or f"minutes_{pe.pk}" in request.POST:
                programs.set_target(pe, sets=_number(request.POST.get(f"sets_{pe.pk}"), high=20),
                                    reps=_number(request.POST.get(f"reps_{pe.pk}"), high=100),
                                    minutes=_number(request.POST.get(f"minutes_{pe.pk}"), cast=None, high=1440),
                                    miles=_number(request.POST.get(f"miles_{pe.pk}"), cast=None, high=500))
        action, _sep, arg = (request.POST.get("action") or "save").partition(":")
        if action == "add":
            ex = get_object_or_404(Exercise.objects.visible_to(request.user), pk=arg if arg.isdigit() else 0)
            programs.add_exercise(day, ex)
        elif action == "create":
            ex, _created = create_exercise(request.user, request.POST.get("new_name"), request.POST.get("new_kind"))
            if ex:
                programs.add_exercise(day, ex)
        elif action in ("up", "down", "remove"):
            pe = get_object_or_404(ProgramExercise, pk=arg if arg.isdigit() else 0, day=day)
            if action == "remove":
                pe.delete()
            else:
                programs.move_exercise(pe, -1 if action == "up" else 1)
        if p.active and p.scheduled_from:
            programs.schedule(p)
        if action == "next":
            i = days.index(day)
            if i + 1 < len(days):
                return redirect(reverse("albright_fitness:program_setup_exercises", args=[p.pk]) + f"?day={days[i + 1].pk}")
            return redirect("albright_fitness:program_setup_schedule", pk=p.pk)
        if action == "schedule":
            return redirect("albright_fitness:program_setup_schedule", pk=p.pk)
        return redirect(here + ("#fx-pe-list" if action != "save" else ""))
    q = " ".join(request.GET.get("q", "").split())[:60]
    found = search_exercises(request.user, q)
    rows = [{"pe": pe, "minutes": (training.to_decimal(pe.target_seconds) / 60).normalize() if pe.target_seconds else "",
             "timed": pe.exercise.kind in (Exercise.CARDIO, Exercise.MOBILITY)}
            for pe in day.exercises.select_related("exercise")]
    last = days.index(day) == len(days) - 1
    return render(request, "albright_fitness/program_setup_exercises.html", {
        "p": p, "days": days, "day": day, "rows": rows, "q": q, "found": found, "steps": _steps(p, 2), "last": last,
        "create": q if q and not any(e.name.lower() == q.lower() for e in found) else "", "kinds": Exercise.KINDS})


def _parse_time(text):
    try:
        return datetime.strptime((text or "").strip(), "%H:%M").time()
    except ValueError:
        return None


@login_required
def setup_schedule(request, pk):
    """Step 3: start date, how long, on my Planner (and when) - Save turns the program on."""
    p = get_object_or_404(Program, pk=pk, owner=request.user)
    today = timezone.localdate()
    errors = []
    start, weeks, planner, at = p.start_date or today, p.weeks, p.on_planner, p.planner_time
    if request.method == "POST":
        try:
            start = date.fromisoformat(request.POST.get("start_date") or "")
        except ValueError:
            start = None
        raw = (request.POST.get("weeks") or "").strip()
        weeks = int(raw) if raw.isdigit() and 1 <= int(raw) <= 104 else None
        planner = request.POST.get("planner") == "1"
        at = _parse_time(request.POST.get("time")) if planner else None
        if start is None or not today - timedelta(days=365) <= start <= today + timedelta(days=365):
            errors.append("Pick a start date within a year of today.")
            start = start or today
        if planner and (request.POST.get("time") or "").strip() and at is None:
            errors.append("Pick a time like 6:30 AM, or leave it empty for all day.")
        if planner and not any(d.weekdays for d in p.days.all()):
            errors.append("Give a workout its days first (step 1).")
        if not errors:
            was_draft = p.is_draft
            placed = programs.finish_setup(p, start, weeks=weeks, on_planner=planner, planner_time=at)
            note = f" {placed} repeating task{'s' if placed != 1 else ''} on your Planner." if placed else ""
            messages.success(request, (f"{p.name} is set up." if was_draft else "Saved.") + note)
            return redirect("albright_fitness:program", pk=p.pk)
    days = list(p.days.prefetch_related("exercises"))
    return render(request, "albright_fitness/program_setup_schedule.html", {
        "p": p, "days": days, "start": start, "weeks": "" if weeks is None else str(weeks), "planner": planner, "at": at,
        "week_choices": WEEKS if weeks in (None, 4, 6, 8, 12, 16) else WEEKS + [(str(weeks), f"{weeks} weeks")],
        "errors": errors, "steps": _steps(p, 3), "today": today})


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------

def _from_template(request, key):
    p = programs.copy_template(request.user, key, active=False)
    messages.success(request, f"Copied “{p.name}”. Check the exercises, then pick when.")
    return redirect("albright_fitness:program_setup_exercises", pk=p.pk)


@login_required
def program_templates(request):
    """The built-in templates, by days a week; Use this copies one as a draft and opens its exercises (step 2)."""
    if request.method == "POST":
        key = request.POST.get("template", "")
        if key not in programs.TEMPLATES:
            messages.error(request, "Pick a template.")
            return redirect("albright_fitness:program_templates")
        return _from_template(request, key)
    rows = []
    for key, (name, weeks, days) in programs.TEMPLATES.items():
        per_week = len({d for _n, wd, _r in days for d in wd})
        rows.append({"key": key, "name": name, "weeks": weeks, "per_week": per_week,
                     "days": [(n, wd, len(r)) for n, wd, r in days]})
    counts = sorted({r["per_week"] for r in rows})
    want = request.GET.get("days", "")
    shown = [r for r in rows if not want.isdigit() or r["per_week"] == int(want)]
    return render(request, "albright_fitness/program_templates.html", {"templates": shown, "counts": counts, "want": want})
