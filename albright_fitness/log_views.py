"""Logging from anywhere and scheduling (Fitness):

  - The exercise page's "Log this exercise" card (also a bottom sheet from the Exercises list and the Today page's
    recent-exercise chips): last time, the suggested target and set rows pre-filled from it. Save adds the exercise
    to today's workout in progress, or logs it as a new (finished) workout for today; new records come back.
  - The Today page's quick log preview: what the text will save, with a picker for names it doesn't know.
  - Schedule: an exercise, a program or a program day onto my Planner (schedule.py), Edit and Remove.
Everything is the person's own - other people's exercises, programs and schedules are 404s.
"""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import Max, Q
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST

from . import programs, schedule, stats, training
from .models import (DISTANCE_UNITS, MI, UNITS, Exercise, PersonalRecord, Program, ProgramDay, ScheduledSession,
                     Workout, WorkoutExercise, WorkoutSet)

MAX_ROWS = 30


def _json(request):
    return request.headers.get("X-Requested-With") == "XMLHttpRequest"


def _next(request, default):
    nxt = request.POST.get("next") or request.GET.get("next") or ""
    if nxt and url_has_allowed_host_and_scheme(nxt, {request.get_host()}, require_https=request.is_secure()):
        return nxt
    return default


def open_today(user, today=None):
    """Today's workout in progress (the newest), or None."""
    return Workout.objects.filter(owner=user, finished_at__isnull=True, date=today or timezone.localdate()) \
        .order_by("-pk").first()


# ---------------------------------------------------------------------------
# The Log card
# ---------------------------------------------------------------------------

def card_context(user, ex, today=None):
    """Everything the Log card shows: last time, the suggestion, the pre-filled rows and where Save goes."""
    tip = training.suggest(user, ex)
    rows = training.suggested_rows(user, ex, tip=tip)
    for row in rows:
        if ex.is_cardio:
            row["duration"] = training.clock(row.get("duration_seconds")) if row.get("duration_seconds") else ""
            row["distance_text"] = training.fmt_weight(row.get("distance")) if row.get("distance") else ""
        else:
            row["weight_text"] = training.fmt_weight(row.get("weight")) if row.get("weight") is not None else ""
    last_we = training.last_session(user, ex)
    return {"ex": ex, "tip": tip, "rows": rows, "today_workout": open_today(user, today),
            "last_text": ", ".join(training.set_text(s) for s in tip.last) if tip else "",
            "last_date": last_we.workout.date if last_we else None, "units": UNITS, "distance_units": DISTANCE_UNITS,
            "unit": rows[0].get("unit", "lb") if rows else "lb",
            "distance_unit": rows[0].get("distance_unit", MI) if rows else MI}


def _posted_rows(request, ex):
    """The card's set rows, cleaned: [{...set fields}], and the problems found."""
    post, rows, problems = request.POST, [], []
    unit = post.get("unit") if post.get("unit") in dict(UNITS) else "lb"
    distance_unit = post.get("distance_unit") if post.get("distance_unit") in dict(DISTANCE_UNITS) else MI
    if ex.is_cardio:
        durations, distances = post.getlist("duration")[:MAX_ROWS], post.getlist("distance")[:MAX_ROWS]
        for n in range(max(len(durations), len(distances))):
            raw_time = durations[n].strip() if n < len(durations) else ""
            raw_dist = distances[n].strip() if n < len(distances) else ""
            if not raw_time and not raw_dist:
                continue
            seconds = training.parse_clock(raw_time) if raw_time else None
            distance = training.to_decimal(raw_dist) if raw_dist else None
            if (raw_time and seconds is None) or (raw_dist and (distance is None or not 0 < distance < 100000)):
                problems.append(f"Set {n + 1}: check the time (28:30) and distance.")
                continue
            rows.append({"duration_seconds": seconds, "distance": distance, "distance_unit": distance_unit})
    else:
        reps_list, weights = post.getlist("reps")[:MAX_ROWS], post.getlist("weight")[:MAX_ROWS]
        for n in range(max(len(reps_list), len(weights))):
            raw_reps = reps_list[n].strip() if n < len(reps_list) else ""
            raw_weight = weights[n].strip() if n < len(weights) else ""
            if not raw_reps and not raw_weight:
                continue
            reps = int(raw_reps) if raw_reps.isdigit() and 0 < int(raw_reps) <= 1000 else None
            weight = training.to_decimal(raw_weight) if raw_weight else None
            if reps is None or (raw_weight and (weight is None or not 0 <= weight < 100000)):
                problems.append(f"Set {n + 1}: enter the reps{' and a weight' if raw_weight else ''}.")
                continue
            rows.append({"reps": reps, "weight": weight, "unit": unit})
    return rows, problems


def _refreshed(request, user, ex):
    """The exercise page's Progress and Records cards, re-rendered after a save."""
    history, unit = stats.exercise_history(user, ex, include_today=True)
    from .views import _chart
    records = PersonalRecord.objects.filter(owner=user, exercise=ex)[:10]
    ctx = {"ex": ex, "history": history, "unit": unit, "chart": _chart(history),
           "records": [(r, training.record_text(r)) for r in records]}
    return (render_to_string("albright_fitness/_exercise_progress.html", ctx, request=request),
            render_to_string("albright_fitness/_exercise_records.html", ctx, request=request))


@login_required
@never_cache
def exercise_log(request, pk):
    """GET: the Log card on its own (the bottom sheet). POST: save it - into today's workout in progress, or as a
    new workout for today (finished: records, Planner check-offs)."""
    user = request.user
    ex = get_object_or_404(Exercise.objects.visible_to(user), pk=pk)
    back = _next(request, reverse("albright_fitness:exercise", args=[ex.pk]))
    if request.method != "POST":
        return render(request, "albright_fitness/_log_card.html", {**card_context(user, ex), "sheet": True, "back": back})
    rows, problems = _posted_rows(request, ex)
    if problems or not rows:
        error = " ".join(problems) or "Add at least one set."
        if _json(request):
            return JsonResponse({"ok": False, "error": error}, status=400)
        messages.error(request, error)
        return redirect(back)
    with transaction.atomic():
        workout = open_today(user)
        added_to = workout is not None
        if workout is None:
            workout = Workout.objects.create(owner=user, date=timezone.localdate(), name=ex.name)
            from albright_labs import sharing
            sharing.apply_defaults(workout, user)
        order = (workout.exercises.aggregate(m=Max("order"))["m"] or -1) + 1
        we = WorkoutExercise.objects.create(workout=workout, exercise=ex, order=order)
        WorkoutSet.objects.bulk_create([WorkoutSet(workout_exercise=we, order=n, done=True, **row) for n, row in enumerate(rows)])
        if added_to:
            records = training.detect_records(workout, exercises=[ex])
            schedule.complete_matching(workout, exercises=[ex])
        else:
            records = programs.finish(workout)
    texts = [training.record_text(r) for r in records]
    count = f"{len(rows)} set{'s' if len(rows) != 1 else ''}"
    message = (f"Added {ex.name} ({count}) to {workout.title}." if added_to else f"Logged {ex.name} ({count}) as today's workout.")
    if _json(request):
        progress, records_html = _refreshed(request, user, ex)
        return JsonResponse({"ok": True, "message": message, "records": texts, "progress_html": progress,
                             "records_html": records_html, "workout_url": reverse("albright_fitness:workout", args=[workout.pk]),
                             "added_to_open": added_to})
    messages.success(request, message + (" New record: " + "; ".join(texts) if texts else ""))
    return redirect(back)


# ---------------------------------------------------------------------------
# Quick log preview (the Today page)
# ---------------------------------------------------------------------------

def posted_matches(request):
    """{index: Exercise} from the preview's pickers (match-<index>), my visible exercises only."""
    picked = {}
    for key, value in request.POST.items():
        if key.startswith("match-") and key[6:].isdigit() and value.isdigit():
            ex = Exercise.objects.visible_to(request.user).filter(pk=value).first()
            if ex:
                picked[int(key[6:])] = ex
    return picked


@login_required
@require_POST
def quick_preview(request):
    """What the quick log text will save: each exercise and its sets; names it can't match get a picker."""
    text = request.POST.get("text", "")[:2000]
    parsed = training.parse_quick(text, request.user, posted_matches(request))
    library = Exercise.objects.visible_to(request.user) if any(p.unknown for p in parsed) else []
    html = render_to_string("albright_fitness/_quick_preview.html", {"parsed": parsed, "library": library}, request=request)
    return JsonResponse({"ok": True, "html": html, "ready": bool(parsed) and not any(p.problem for p in parsed)})


# ---------------------------------------------------------------------------
# Schedule
# ---------------------------------------------------------------------------

def _target(request, kind, pk):
    user = request.user
    if kind == "exercise":
        return {"exercise": get_object_or_404(Exercise.objects.visible_to(user), pk=pk)}
    if kind == "program":
        return {"program": get_object_or_404(Program, pk=pk, owner=user)}
    if kind == "day":
        return {"program_day": get_object_or_404(ProgramDay.objects.select_related("program"), pk=pk, program__owner=user)}
    raise Http404


def _target_page(target):
    if "exercise" in target:
        return reverse("albright_fitness:exercise", args=[target["exercise"].pk])
    program = target.get("program") or target["program_day"].program
    return reverse("albright_fitness:program", args=[program.pk])


def _target_name(target):
    if "exercise" in target:
        return target["exercise"].name
    if "program" in target:
        return target["program"].name
    return f"{target['program_day'].name} ({target['program_day'].program.name})"


@login_required
@never_cache
def schedule_new(request, kind, pk):
    """Schedule an exercise / program / program day: a date, optional time and repeat, optional sets × reps."""
    target = _target(request, kind, pk)
    back = _next(request, _target_page(target) + "#fx-scheduled")
    if request.method == "POST":
        form = schedule.ScheduleForm(request.POST)
        if form.is_valid():
            session = schedule.create(request.user, form, **target)
            when = form.cleaned_data["date"]
            messages.success(request, f"On your Planner: {session.title} on {when:%a} {when:%b} {when.day}"
                                      + (" (repeats)" if form.rule() else "") + ".")
            return redirect(back)
    else:
        form = schedule.ScheduleForm(initial={"date": timezone.localdate(), "repeat": "none", "interval": 2})
    return render(request, "albright_fitness/schedule.html", {"form": form, "name": _target_name(target), "back": back,
                                                              "action": request.path, "editing": False})


def _session(request, pk):
    return get_object_or_404(ScheduledSession.objects.select_related("planner_item", "exercise", "program",
                                                                     "program_day__program"),
                             pk=pk, owner=request.user, planner_item__isnull=False)


def _session_page(session):
    if session.exercise_id:
        return reverse("albright_fitness:exercise", args=[session.exercise_id])
    program = session.program or session.program_day.program
    return reverse("albright_fitness:program", args=[program.pk])


@login_required
@never_cache
def schedule_edit(request, pk):
    session = _session(request, pk)
    back = _next(request, _session_page(session) + "#fx-scheduled")
    if request.method == "POST":
        form = schedule.ScheduleForm(request.POST)
        if form.is_valid():
            schedule.update(session, form)
            messages.success(request, f"Updated {session.title} on your Planner.")
            return redirect(back)
    else:
        form = schedule.ScheduleForm(initial=schedule.ScheduleForm.initial_for(session))
    return render(request, "albright_fitness/schedule.html", {"form": form, "name": session.title, "back": back,
                                                              "action": request.path, "editing": True,
                                                              "session": session})


@login_required
@require_POST
def schedule_remove(request, pk):
    session = _session(request, pk)
    back = _next(request, _session_page(session) + "#fx-scheduled")
    title = session.title
    schedule.remove(session)
    messages.success(request, f"Removed {title} from your Planner.")
    return redirect(back)


def scheduled_for(user, **target):
    """The Scheduled section's rows for an exercise, or a program (with its days)."""
    today = timezone.localdate()
    if "program" in target:
        return schedule.upcoming(user, today, Q(program=target["program"]) | Q(program_day__program=target["program"]))
    return schedule.upcoming(user, today, **target)
