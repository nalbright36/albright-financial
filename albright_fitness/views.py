"""Albright Fitness pages (/fitness/). Opening any of them needs Fitness access (Admin > Users - albright_labs.
app_access 404s everyone else first). Everything is its owner's: other people's workouts, programs, records and
exercises are 404s - except a workout shared with you, which you can view (never change)."""
from calendar import Calendar
from datetime import date, timedelta

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import Max, Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST

from . import programs, stats, training
from .models import (EQUIPMENT, MUSCLES, Exercise, PersonalRecord, Program, ProgramDay, ProgramExercise, Workout,
                     WorkoutExercise, WorkoutSet, WorkoutShare)

WEEKDAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def _json(request):
    return request.headers.get("X-Requested-With") == "XMLHttpRequest"


def _mine(request, pk):
    """One of my workouts (to change) - anyone else's is a 404, even one shared with me."""
    return get_object_or_404(Workout, pk=pk, owner=request.user)


def _set_of_mine(request, pk):
    return get_object_or_404(WorkoutSet.objects.select_related("workout_exercise__workout", "workout_exercise__exercise"),
                             pk=pk, workout_exercise__workout__owner=request.user)


def fitness_users():
    """Everyone who can open Fitness - who a workout can be shared with."""
    return get_user_model().objects.filter(is_active=True).filter(
        Q(is_superuser=True) | Q(labs_preference__allowed_apps__contains=["fitness"])).order_by("username")


def _person(user):
    return (user.first_name or user.get_username()).strip()


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

@login_required
@never_cache
def dashboard(request):
    data = stats.dashboard(request.user)
    top = max([r["sets"] for r in data["volume"]] or [1])
    open_workout = Workout.objects.filter(owner=request.user, finished_at__isnull=True).first()
    return render(request, "albright_fitness/dashboard.html", {
        **data, "volume_max": top, "open_workout": open_workout,
        "records_text": [(r, training.record_text(r)) for r in data["records"]],
        "recent": Workout.objects.filter(owner=request.user, finished_at__isnull=False).select_related("program_day")[:5]})


# ---------------------------------------------------------------------------
# The exercise library
# ---------------------------------------------------------------------------

@login_required
def exercises(request):
    user = request.user
    if request.method == "POST":
        name = " ".join(request.POST.get("name", "").split())[:100]
        kind = request.POST.get("kind", Exercise.STRENGTH)
        muscles = [m for m in request.POST.getlist("primary") if m in dict(MUSCLES)]
        equipment = request.POST.get("equipment", "other")
        if not name:
            messages.error(request, "Give the exercise a name.")
        elif Exercise.objects.visible_to(user).filter(name__iexact=name).exists():
            messages.error(request, f"There's already an exercise called “{name}”.")
        else:
            Exercise.objects.create(owner=user, name=name, kind=kind if kind in dict(Exercise.KINDS) else Exercise.STRENGTH,
                                    primary_muscles=muscles, equipment=equipment if equipment in dict(EQUIPMENT) else "other",
                                    instructions=request.POST.get("instructions", "").strip()[:4000])
            messages.success(request, f"Added {name}.")
        return redirect("albright_fitness:exercises")
    q = request.GET.get("q", "").strip()[:60]
    muscle, equipment = request.GET.get("muscle", ""), request.GET.get("equipment", "")
    qs = Exercise.objects.visible_to(user)
    if q:
        qs = qs.filter(name__icontains=q)
    if equipment in dict(EQUIPMENT):
        qs = qs.filter(equipment=equipment)
    rows = [e for e in qs if not muscle or muscle in (e.primary_muscles or []) or muscle in (e.secondary_muscles or [])]
    return render(request, "albright_fitness/exercises.html", {
        "exercises": rows, "q": q, "muscle": muscle, "equipment": equipment, "muscles": MUSCLES,
        "equipment_choices": EQUIPMENT, "kinds": Exercise.KINDS, "muscle_names": dict(MUSCLES)})


@login_required
def exercise(request, pk):
    ex = get_object_or_404(Exercise.objects.visible_to(request.user), pk=pk)
    history, unit = stats.exercise_history(request.user, ex)
    records = PersonalRecord.objects.filter(owner=request.user, exercise=ex)[:10]
    return render(request, "albright_fitness/exercise.html", {
        "ex": ex, "history": history, "unit": unit, "chart": _chart(history),
        "records": [(r, training.record_text(r)) for r in records], "muscle_names": dict(MUSCLES),
        "tip": training.suggest(request.user, ex)})


def _chart(rows):
    """The progress chart's geometry, in percent of the plot box: points ({"x", "y", ...row}), the line, and the
    y-axis ticks. The plot stretches to its box; labels and points are true-size HTML on top (fitness.css)."""
    if len(rows) < 2:
        return None
    values = [float(r["value"]) for r in rows]
    low, high = min(values), max(values)
    span = (high - low) or max(high * 0.1, 1)
    low, high = max(0, low - span * 0.15), high + span * 0.15
    points = [{**r, "x": round(100 * i / (len(rows) - 1), 2),
               "y": round(100 - (float(r["value"]) - low) / (high - low) * 100, 2)} for i, r in enumerate(rows)]
    ticks = [{"y": round(100 - 100 * n / 3, 2), "label": f"{low + (high - low) * n / 3:,.0f}"} for n in range(4)]
    return {"points": points, "line": " ".join(f"{p['x']},{p['y']}" for p in points), "ticks": ticks,
            "first": rows[0]["date"], "last": rows[-1]["date"], "last_label": rows[-1]["label"]}


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

@login_required
def start(request):
    """Start a workout - from a program day (pre-filled; GET shows what's in it, POST starts it), or blank."""
    user = request.user
    day = None
    if request.GET.get("day") or request.POST.get("day"):
        day = get_object_or_404(ProgramDay.objects.select_related("program"), pk=request.GET.get("day") or request.POST.get("day"),
                                program__owner=user)
    requested = None
    try:
        requested = date.fromisoformat(request.GET.get("date") or request.POST.get("date") or "")
    except ValueError:
        pass
    item, when = programs.planner_occurrence(user, day, requested) if day else (None, None)
    if request.method == "POST":
        workout = programs.start_workout(user, day=day, planner_item=item, planner_date=when,
                                         name=request.POST.get("name", "").strip()[:100])
        return redirect("albright_fitness:workout", pk=workout.pk)
    return render(request, "albright_fitness/start.html", {
        "day": day, "planner_date": when, "rows": day.exercises.select_related("exercise") if day else [],
        "requested": requested})


@login_required
@require_POST
def quick(request):
    """The quick text log: a finished workout, today, from "squat 225x5x3, bench 185x8,8,7, run 3.1mi 28:30"."""
    parsed = training.parse_quick(request.POST.get("text", "")[:2000], request.user)
    good = [p for p in parsed if not p.problem]
    for p in parsed:
        if p.problem:
            messages.error(request, f"“{p.text}”: {p.problem}.")
    if not good:
        return redirect("albright_fitness:dashboard")
    with transaction.atomic():
        workout = Workout.objects.create(owner=request.user, date=timezone.localdate(), name="Quick log")
        training.save_quick(workout, good)
        programs.finish(workout)
    return redirect("albright_fitness:summary", pk=workout.pk)


def _blocks(workout, user, editable):
    """Each exercise with its sets, "last time" and the suggested target."""
    out = []
    for we in workout.exercises.select_related("exercise").prefetch_related("sets"):
        tip = training.suggest(workout.owner, we.exercise, workout, we.target_reps, we.target_sets) if editable else None
        out.append({"we": we, "sets": list(we.sets.all()), "tip": tip,
                    "last": ", ".join(_set_text(s) for s in tip.last) if tip else ""})
    return out


def _set_text(s):
    if s.distance or s.duration_seconds:
        return " ".join(p for p in (f"{training.fmt_weight(s.distance)} {s.distance_unit}" if s.distance else "",
                                     training.clock(s.duration_seconds) if s.duration_seconds else "") if p)
    if s.weight is not None and s.reps:
        return f"{training.fmt_weight(s.weight)}×{s.reps}"
    return f"{s.reps or 0} reps"


@login_required
@never_cache
def workout(request, pk):
    w = get_object_or_404(Workout.objects.visible_to(request.user).select_related("owner", "program_day"), pk=pk)
    editable = w.owner_id == request.user.pk
    library = Exercise.objects.visible_to(request.user) if editable else []
    return render(request, "albright_fitness/workout.html", {
        "w": w, "editable": editable, "blocks": _blocks(w, request.user, editable), "library": library,
        "kinds": Exercise.KINDS, "owner_name": _person(w.owner),
        "shares": w.shares.select_related("user") if editable else [],
        "share_choices": fitness_users().exclude(pk=request.user.pk) if editable else []})


@login_required
@require_POST
def add_exercise(request, pk):
    w = _mine(request, pk)
    ex = get_object_or_404(Exercise.objects.visible_to(request.user), pk=request.POST.get("exercise") or 0)
    order = (w.exercises.aggregate(m=Max("order"))["m"] or -1) + 1
    we = WorkoutExercise.objects.create(workout=w, exercise=ex, order=order)
    training.prefill(we, request.user)
    return redirect(reverse("albright_fitness:workout", args=[w.pk]) + f"#ex-{we.pk}")


@login_required
@require_POST
def remove_exercise(request, pk, we_pk):
    w = _mine(request, pk)
    get_object_or_404(WorkoutExercise, pk=we_pk, workout=w).delete()
    return redirect("albright_fitness:workout", pk=w.pk)


@login_required
@require_POST
def add_set(request, we_pk):
    """"+ Set": a copy of the exercise's last set (or a blank one)."""
    we = get_object_or_404(WorkoutExercise.objects.select_related("exercise", "workout"), pk=we_pk,
                           workout__owner=request.user)
    last = we.sets.order_by("-order", "-pk").first()
    order = training.next_order(we)
    new = training.copy_set(last, order) if last else WorkoutSet.objects.create(workout_exercise=we, order=order)
    if _json(request):
        html = render_to_string("albright_fitness/_set_row.html", {"s": new, "we": we, "n": we.sets.count(),
                                                                  "editable": True}, request=request)
        return JsonResponse({"ok": True, "html": html, "id": new.pk})
    return redirect(reverse("albright_fitness:workout", args=[we.workout_id]) + f"#ex-{we.pk}")


@login_required
@require_POST
def update_set(request, pk):
    """One set's fields (only those posted): reps, weight, unit, duration ("28:30"), distance, rpe, set_type, done."""
    s = _set_of_mine(request, pk)
    post, errors = request.POST, []
    if "reps" in post:
        s.reps = int(post["reps"]) if post["reps"].strip().isdigit() and int(post["reps"]) <= 1000 else None
    if "weight" in post:
        value = training.to_decimal(post["weight"]) if post["weight"].strip() else None
        if value is not None and not (0 <= value < 100000):
            errors.append("weight")
        else:
            s.weight = value
    if post.get("unit") in ("lb", "kg"):
        s.unit = post["unit"]
    if "duration" in post:
        s.duration_seconds = training.parse_clock(post["duration"])
    if "distance" in post:
        value = training.to_decimal(post["distance"]) if post["distance"].strip() else None
        s.distance = value if value is None or 0 <= value < 100000 else s.distance
    if post.get("distance_unit") in ("mi", "km"):
        s.distance_unit = post["distance_unit"]
    if "rpe" in post:
        value = training.to_decimal(post["rpe"], 1) if post["rpe"].strip() else None
        s.rpe = value if value is None or 1 <= value <= 10 else s.rpe
    if post.get("set_type") in dict(WorkoutSet.TYPES):
        s.set_type = post["set_type"]
    if "toggle_done" in post:                           # the box, without JS
        s.done = not s.done
    elif "done" in post:
        s.done = post["done"] in ("1", "true", "on")
    s.save()
    if _json(request):
        return JsonResponse({"ok": not errors, "done": s.done, "set_type": s.set_type, "errors": errors})
    return redirect(reverse("albright_fitness:workout", args=[s.workout_exercise.workout_id]) + f"#ex-{s.workout_exercise_id}")


@login_required
@require_POST
def delete_set(request, pk):
    s = _set_of_mine(request, pk)
    workout_pk, we_pk = s.workout_exercise.workout_id, s.workout_exercise_id
    s.delete()
    if _json(request):
        return JsonResponse({"ok": True})
    return redirect(reverse("albright_fitness:workout", args=[workout_pk]) + f"#ex-{we_pk}")


@login_required
@require_POST
def finish(request, pk):
    w = _mine(request, pk)
    if not w.finished:
        programs.finish(w)
    return redirect("albright_fitness:summary", pk=w.pk)


@login_required
@require_POST
def edit_workout(request, pk):
    w = _mine(request, pk)
    w.name = request.POST.get("name", w.name).strip()[:100]
    w.notes = request.POST.get("notes", w.notes).strip()[:4000]
    w.save(update_fields=["name", "notes"])
    return redirect("albright_fitness:workout", pk=w.pk)


@login_required
@require_POST
def delete_workout(request, pk):
    _mine(request, pk).delete()
    messages.success(request, "Workout deleted.")
    return redirect("albright_fitness:history")


@login_required
def summary(request, pk):
    w = get_object_or_404(Workout.objects.visible_to(request.user), pk=pk)
    blocks = [{"we": we, "sets": [s for s in we.sets.all() if s.done]}
              for we in w.exercises.select_related("exercise").prefetch_related("sets")]
    done = [s for b in blocks for s in b["sets"]]
    pounds = sum((s.weight_lb * s.reps for s in done if s.weight_lb and s.reps), 0)
    minutes = None
    if w.start_time and w.end_time:
        start = w.start_time.hour * 60 + w.start_time.minute
        end = w.end_time.hour * 60 + w.end_time.minute
        minutes = (end - start) % (24 * 60)
    records = w.records.select_related("exercise") if w.owner_id == request.user.pk else []
    return render(request, "albright_fitness/summary.html", {
        "w": w, "blocks": blocks, "sets_done": len(done), "pounds": pounds, "minutes": minutes,
        "records": [(r, training.record_text(r)) for r in records], "set_text": _set_text,
        "lines": [(b["we"], ", ".join(_set_text(s) for s in b["sets"])) for b in blocks],
        "editable": w.owner_id == request.user.pk})


@login_required
@require_POST
def share(request, pk):
    """Show this workout (view only) to someone with Fitness access - or stop showing it."""
    w = _mine(request, pk)
    other = get_object_or_404(fitness_users().exclude(pk=request.user.pk), pk=request.POST.get("user") or 0)
    if request.POST.get("action") == "remove":
        WorkoutShare.objects.filter(workout=w, user=other).delete()
        messages.success(request, f"No longer shared with {_person(other)}.")
    else:
        WorkoutShare.objects.get_or_create(workout=w, user=other)
        messages.success(request, f"Shared with {_person(other)} (view only).")
    return redirect("albright_fitness:workout", pk=w.pk)


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------

@login_required
@never_cache
def history(request):
    user = request.user
    today = timezone.localdate()
    try:
        month = date.fromisoformat((request.GET.get("month") or "") + "-01")
    except ValueError:
        month = today.replace(day=1)
    end = (month.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    workouts = Workout.objects.visible_to(user).filter(date__gte=month - timedelta(days=7), date__lte=end + timedelta(days=7)) \
        .select_related("owner", "program_day")
    by_day = {}
    for w in workouts:
        by_day.setdefault(w.date, []).append(w)
    weeks = [[{"date": d, "in_month": d.month == month.month, "is_today": d == today, "workouts": by_day.get(d, [])}
              for d in week] for week in Calendar().monthdatescalendar(month.year, month.month)]
    listed = [w for w in workouts if month <= w.date <= end]
    prev = (month - timedelta(days=1)).replace(day=1)
    return render(request, "albright_fitness/history.html", {
        "month": month, "weeks": weeks, "workouts": sorted(listed, key=lambda w: (w.date, w.pk), reverse=True),
        "prev": prev, "next": end + timedelta(days=1), "weekdays": WEEKDAY_NAMES,
        "charted": Exercise.objects.filter(pk__in=WorkoutExercise.objects.filter(workout__owner=user,
                                                                                  workout__finished_at__isnull=False)
                                           .values("exercise")).order_by("name")})


# ---------------------------------------------------------------------------
# Programs
# ---------------------------------------------------------------------------

@login_required
def program_list(request):
    if request.method == "POST":
        key = request.POST.get("template", "")
        if key in programs.TEMPLATES:
            program = programs.copy_template(request.user, key)
            messages.success(request, f"Copied “{program.name}” - change anything, then put it on your Planner.")
            return redirect("albright_fitness:program", pk=program.pk)
        name = request.POST.get("name", "").strip()[:100]
        if not name:
            messages.error(request, "Give the program a name.")
            return redirect("albright_fitness:programs")
        program = Program.objects.create(owner=request.user, name=name)
        return redirect("albright_fitness:program", pk=program.pk)
    return render(request, "albright_fitness/programs.html", {
        "programs": Program.objects.filter(owner=request.user).prefetch_related("days"),
        "templates": [(key, name, weeks, days) for key, (name, weeks, days) in programs.TEMPLATES.items()],
        "weekday_names": WEEKDAY_NAMES})


@login_required
def program(request, pk):
    p = get_object_or_404(Program, pk=pk, owner=request.user)
    if request.method == "POST":
        _program_post(request, p)
        return redirect(reverse("albright_fitness:program", args=[p.pk]) + request.POST.get("anchor", ""))
    days = list(p.days.prefetch_related("exercises__exercise"))
    from albright_planner import services
    return render(request, "albright_fitness/program.html", {
        "p": p, "days": days, "weekday_names": list(enumerate(WEEKDAY_NAMES)),
        "library": Exercise.objects.visible_to(request.user), "kinds": Exercise.KINDS,
        "on_planner": [d for d in days if services.source_item(request.user, programs.SOURCE, f"day-{d.pk}")]})


def _program_post(request, p):
    action, post = request.POST.get("action"), request.POST
    if action == "rename":
        p.name = post.get("name", "").strip()[:100] or p.name
        weeks = post.get("weeks", "").strip()
        p.weeks = int(weeks) if weeks.isdigit() and 1 <= int(weeks) <= 104 else None
        p.save(update_fields=["name", "weeks"])
    elif action == "active":
        programs.set_active(p, post.get("active") == "1")
        messages.success(request, "Turned on." if p.active else "Turned off - its Planner tasks end after today.")
    elif action == "schedule":
        if not p.active:
            programs.set_active(p, True)
        placed = programs.schedule(p)
        if placed:
            messages.success(request, f"On your Planner: {placed} repeating task{'s' if placed != 1 else ''} (private), "
                                      "with each day's exercises as its checklist.")
        else:
            messages.error(request, "Give at least one day its weekdays first.")
    elif action == "unschedule":
        programs.unschedule(p)
        messages.success(request, "Taken off your Planner after today - earlier ones stay.")
    elif action == "add_day":
        name = post.get("name", "").strip()[:60]
        if name:
            ProgramDay.objects.create(program=p, name=name, order=p.days.count(),
                                      weekdays=sorted({int(d) for d in post.getlist("weekdays") if d.isdigit() and int(d) < 7}))
    elif action == "edit_day":
        day = get_object_or_404(ProgramDay, pk=post.get("day") or 0, program=p)
        day.name = post.get("name", "").strip()[:60] or day.name
        day.weekdays = sorted({int(d) for d in post.getlist("weekdays") if d.isdigit() and int(d) < 7})
        day.save(update_fields=["name", "weekdays"])
    elif action == "delete_day":
        get_object_or_404(ProgramDay, pk=post.get("day") or 0, program=p).delete()
    elif action == "add_exercise":
        day = get_object_or_404(ProgramDay, pk=post.get("day") or 0, program=p)
        ex = get_object_or_404(Exercise.objects.visible_to(request.user), pk=post.get("exercise") or 0)
        sets = post.get("sets", "3")
        reps, minutes, miles = post.get("reps", ""), post.get("minutes", ""), post.get("miles", "")
        ProgramExercise.objects.create(
            day=day, exercise=ex, order=day.exercises.count(),
            target_sets=min(int(sets), 20) if sets.isdigit() and int(sets) > 0 else 3,
            target_reps=min(int(reps), 100) if reps.isdigit() and int(reps) > 0 else None,
            target_seconds=int(training.to_decimal(minutes) * 60) if training.to_decimal(minutes) else None,
            target_distance=training.to_decimal(miles) if training.to_decimal(miles) else None)
    elif action == "remove_exercise":
        get_object_or_404(ProgramExercise, pk=post.get("pe") or 0, day__program=p).delete()
    if action in ("add_day", "edit_day", "delete_day", "add_exercise", "remove_exercise") and p.scheduled_from and p.active:
        programs.schedule(p)                               # the Planner follows the changes


@login_required
@require_POST
def program_delete(request, pk):
    p = get_object_or_404(Program, pk=pk, owner=request.user)
    if p.scheduled_from:
        programs.unschedule(p)
    name = p.name
    p.delete()
    messages.success(request, f"Deleted {name} - its past Planner tasks and your workouts stay.")
    return redirect("albright_fitness:programs")

