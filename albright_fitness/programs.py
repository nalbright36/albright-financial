"""Programs: the built-in templates (copied into a person's own Program), starting a workout from a program day,
and putting a program on the Planner.

Planner link - only through the Planner's service helpers (albright_planner.services), never its models:
  - "Schedule this program": one private repeating task per program day (owned by the person, source "fitness",
    one per day: source_id "day-<pk>"), every week on the day's weekdays, with the day's exercises as its
    checklist, ending after the program's weeks (no weeks given: OPEN_WEEKS - schedule it again to go on).
  - Finishing a workout started from one of those tasks checks off that occurrence.
  - Turning a program off (or "Remove from Planner") ends its tasks after today: past ones and their check-offs
    stay. Pausing remembers the Planner choice (Program.on_planner), so Resume puts the tasks back.

The guided setup (program_views) makes a program as an off draft and finish_setup() turns it on with its start
date, length, Planner choice and optional time - so a half-built program never shows on Today.
"""
from datetime import timedelta

from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from . import training
from .models import Exercise, Program, ProgramDay, ProgramExercise, Workout, WorkoutExercise

SOURCE = "fitness"
OPEN_WEEKS = 52

# key -> (name, weeks, [(day name, weekdays, [(exercise, sets, reps, seconds, miles)])])
TEMPLATES = {
    "full_body": ("Full body 3x/week", None, [
        ("Full body A", [0, 4], [("Barbell Back Squat", 3, 5, None, None), ("Barbell Bench Press", 3, 8, None, None),
                                 ("Barbell Row", 3, 8, None, None), ("Plank", 3, None, 45, None)]),
        ("Full body B", [2], [("Deadlift", 3, 5, None, None), ("Overhead Press", 3, 8, None, None),
                              ("Lat Pulldown", 3, 10, None, None), ("Walking Lunge", 3, 10, None, None)]),
    ]),
    "upper_lower": ("Upper/Lower 4x/week", None, [
        ("Upper A", [0], [("Barbell Bench Press", 4, 6, None, None), ("Barbell Row", 4, 6, None, None),
                          ("Overhead Press", 3, 8, None, None), ("Lat Pulldown", 3, 10, None, None),
                          ("Dumbbell Curl", 3, 12, None, None)]),
        ("Lower A", [1], [("Barbell Back Squat", 4, 6, None, None), ("Romanian Deadlift", 3, 8, None, None),
                          ("Leg Press", 3, 10, None, None), ("Standing Calf Raise", 3, 12, None, None)]),
        ("Upper B", [3], [("Incline Dumbbell Press", 3, 10, None, None), ("Pull-Up", 3, 8, None, None),
                          ("Dumbbell Shoulder Press", 3, 10, None, None), ("Seated Cable Row", 3, 10, None, None),
                          ("Triceps Pushdown", 3, 12, None, None)]),
        ("Lower B", [4], [("Deadlift", 3, 5, None, None), ("Bulgarian Split Squat", 3, 10, None, None),
                          ("Lying Leg Curl", 3, 12, None, None), ("Hanging Leg Raise", 3, 12, None, None)]),
    ]),
    "ppl": ("Push/Pull/Legs 6x/week", None, [
        ("Push", [0, 3], [("Barbell Bench Press", 4, 6, None, None), ("Overhead Press", 3, 8, None, None),
                          ("Incline Dumbbell Press", 3, 10, None, None), ("Lateral Raise", 3, 15, None, None),
                          ("Triceps Pushdown", 3, 12, None, None)]),
        ("Pull", [1, 4], [("Deadlift", 3, 5, None, None), ("Pull-Up", 3, 8, None, None),
                          ("Seated Cable Row", 3, 10, None, None), ("Face Pull", 3, 15, None, None),
                          ("Dumbbell Curl", 3, 12, None, None)]),
        ("Legs", [2, 5], [("Barbell Back Squat", 4, 6, None, None), ("Romanian Deadlift", 3, 8, None, None),
                          ("Leg Press", 3, 10, None, None), ("Lying Leg Curl", 3, 12, None, None),
                          ("Standing Calf Raise", 4, 12, None, None)]),
    ]),
    "five_by_five": ("Beginner 5x5", 12, [
        ("Workout A", [0, 4], [("Barbell Back Squat", 5, 5, None, None), ("Barbell Bench Press", 5, 5, None, None),
                               ("Barbell Row", 5, 5, None, None)]),
        ("Workout B", [2], [("Barbell Back Squat", 5, 5, None, None), ("Overhead Press", 5, 5, None, None),
                            ("Deadlift", 1, 5, None, None)]),
    ]),
    "running": ("Running 3x/week", 8, [
        ("Easy run", [1], [("Running", 1, None, 30 * 60, None)]),
        ("Tempo run", [3], [("Running", 1, None, 25 * 60, None)]),
        ("Long run", [5], [("Running", 1, None, None, 5)]),
    ]),
}


@transaction.atomic
def copy_template(user, key, active=True):
    """The template as the user's own Program (on, not yet on the Planner) - or, for the guided setup, an off draft."""
    name, weeks, days = TEMPLATES[key]
    builtin = {e.name: e for e in Exercise.objects.filter(owner__isnull=True)}
    program = Program.objects.create(owner=user, name=name, weeks=weeks, template=key, active=active)
    for n, (day_name, weekdays, rows) in enumerate(days):
        day = ProgramDay.objects.create(program=program, order=n, name=day_name, weekdays=weekdays)
        ProgramExercise.objects.bulk_create([
            ProgramExercise(day=day, order=i, exercise=builtin[ex], target_sets=sets, target_reps=reps,
                            target_seconds=seconds, target_distance=miles)
            for i, (ex, sets, reps, seconds, miles) in enumerate(rows) if ex in builtin])
    return program


def day_checklist(day):
    """The Planner task's checklist: "Barbell Back Squat - 3 × 5"."""
    return [f"{pe.exercise.name} - {pe.target_text}" for pe in day.exercises.select_related("exercise")]


def start_url(day):
    return reverse("albright_fitness:start") + f"?day={day.pk}"


# ---------------------------------------------------------------------------
# Starting a workout
# ---------------------------------------------------------------------------

@transaction.atomic
def start_workout(user, day=None, planner_item=None, planner_date=None, name="", rows=None):
    """A new workout - from a program day it's pre-filled with the day's exercises and targets (and last time's
    progression); `rows` (schedule.rows_for, or one exercise) pre-fill it the same way. Started from a Planner
    task, finishing it checks off that occurrence."""
    now = timezone.localtime()
    workout = Workout.objects.create(owner=user, date=planner_date or now.date(), start_time=now.time().replace(microsecond=0),
                                     name=name or (day.name if day else ""), program_day=day,
                                     planner_item=planner_item, planner_date=planner_date if planner_item else None)
    from albright_labs import sharing
    sharing.apply_defaults(workout, user)              # shared the way its owner shares new workouts
    if day is not None:
        for pe in day.exercises.select_related("exercise"):
            we = WorkoutExercise.objects.create(workout=workout, exercise=pe.exercise, order=pe.order,
                                                target_sets=pe.target_sets, target_reps=pe.target_reps)
            training.prefill(we, user, target_sets=pe.target_sets, target_reps=pe.target_reps,
                             target_seconds=pe.target_seconds, target_distance=pe.target_distance)
    for order, row in enumerate(rows or []):
        we = WorkoutExercise.objects.create(workout=workout, exercise=row["exercise"], order=order,
                                            target_sets=row.get("sets"), target_reps=row.get("reps"))
        training.prefill(we, user, target_sets=row.get("sets"), target_reps=row.get("reps"),
                         target_seconds=row.get("seconds"), target_distance=row.get("distance"))
    return workout


def match_today(user, exercises, today=None):
    """Today's planned program day (not yet done) that shares the most exercises with what was logged, or None."""
    from . import stats
    logged = {e.pk for e in exercises}
    best = None
    for day, done in stats.todays(user, today):
        if done:
            continue
        overlap = len(logged & set(day.exercises.values_list("exercise_id", flat=True)))
        if overlap and (best is None or overlap > best[0]):
            best = (overlap, day)
    return best[1] if best else None


@transaction.atomic
def log_parsed(user, parsed, name="Quick log", today=None):
    """A finished workout today from parsed exercises (training.parse_quick, or the Bot API's structured ones) ->
    (workout, new records, matched program day or None). When it matches today's planned program day, it counts as
    that day's workout and its Planner occurrence is checked off."""
    today = today or timezone.localdate()
    day = match_today(user, [p.exercise for p in parsed], today)
    item, when = planner_occurrence(user, day, today) if day else (None, None)
    workout = Workout.objects.create(owner=user, date=today, name=name if not day else "", program_day=day,
                                     planner_item=item, planner_date=when)
    from albright_labs import sharing
    sharing.apply_defaults(workout, user)
    training.save_quick(workout, parsed)
    return workout, finish(workout), day


def planner_occurrence(user, day, requested_date=None):
    """(the Planner task for this program day, the occurrence to check off) - today's (or the requested date's)
    when the task is scheduled then; else (None, None)."""
    from albright_planner import recurrence, services
    item = services.source_item(user, SOURCE, f"day-{day.pk}")
    when = requested_date or timezone.localdate()
    if item is None or not recurrence.is_scheduled(item, when):
        return None, None
    return item, when


@transaction.atomic
def finish(workout):
    """Finish a workout: its end time, new personal records, and - started from a Planner task - that occurrence
    checked off. Returns the new records."""
    from albright_planner import services
    now = timezone.localtime()
    workout.finished_at = timezone.now()
    workout.end_time = workout.end_time or now.time().replace(microsecond=0)
    workout.save(update_fields=["finished_at", "end_time"])
    records = training.detect_records(workout)
    if workout.planner_item_id and workout.planner_date and workout.planner_item.owner_id == workout.owner_id:
        services.complete_occurrence(workout.planner_item, workout.planner_date, workout.owner)
    from . import schedule
    schedule.complete_matching(workout)                # the same exercise / program day scheduled that day
    _record_finish(workout, records)
    return records


def _record_finish(workout, records):
    """A SHARED workout finished, and its new PRs, show in the activity of those it's shared with. Only workouts:
    the food diary, targets, body metrics and weight never produce activity."""
    from albright_labs import activity
    activity.record_activity("fitness", "completed", f"finished the workout {activity.quote(workout.title)}", obj=workout,
                             actor=workout.owner)
    if records:
        first = training.record_text(records[0])
        more = f" and {len(records) - 1} more" if len(records) > 1 else ""
        activity.record_activity("fitness", "pr", f"set a new PR in {activity.quote(workout.title)}: {first}{more}",
                                 obj=workout, actor=workout.owner)


# ---------------------------------------------------------------------------
# The Planner
# ---------------------------------------------------------------------------

@transaction.atomic
def schedule(program, start=None):
    """Put the program on the owner's Planner (or bring its tasks up to date) - from its start date (today if that's
    passed), at its time. Returns how many days were placed."""
    from albright_planner import services
    today = timezone.localdate()
    start = start or (max(today, program.start_date) if program.start_date else today)
    weeks = program.weeks or OPEN_WEEKS
    until = start + timedelta(weeks=weeks) - timedelta(days=1)
    placed, keep = 0, []
    for day in program.days.prefetch_related("exercises__exercise"):
        if not day.weekdays:
            continue
        item, _created = services.upsert_weekly_source_item(
            program.owner, SOURCE, f"day-{day.pk}", f"{day.name} ({program.name})" if program.days.count() > 1 else day.name,
            start, day.weekdays, until=until, checklist=day_checklist(day), url=start_url(day), start_time=program.planner_time)
        if item is not None:
            placed += 1
            keep.append(f"day-{day.pk}")
    stale = [f"day-{pk}" for pk in program.days.values_list("pk", flat=True) if f"day-{pk}" not in keep]
    if stale:
        services.end_source_items(program.owner, SOURCE, stale, start - timedelta(days=1))
    program.scheduled_from = program.scheduled_from or start
    program.on_planner = True
    program.save(update_fields=["scheduled_from", "on_planner"])
    return placed


def unschedule(program, last_day=None, forget=True):
    """Its Planner tasks end after `last_day` (today by default); history stays. forget=False (pausing) keeps the
    Planner choice for Resume."""
    from albright_planner import services
    last_day = last_day or timezone.localdate()
    ended = services.end_source_items(program.owner, SOURCE, [f"day-{pk}" for pk in program.days.values_list("pk", flat=True)],
                                      last_day)
    program.scheduled_from = None
    if forget:
        program.on_planner = False
    program.save(update_fields=["scheduled_from", "on_planner"])
    return ended


def set_active(program, active):
    """Pause / resume. Pausing ends its Planner tasks after today (keeps history and the Planner choice); resuming
    puts them back when it was on the Planner."""
    program.active = active
    program.start_date = program.start_date or timezone.localtime(program.created_at).date()   # never a "draft" again
    program.save(update_fields=["active", "start_date"])
    if not active and program.scheduled_from:
        unschedule(program, forget=False)
    elif active and program.on_planner and any(d.weekdays for d in program.days.all()):
        schedule(program)


@transaction.atomic
def finish_setup(program, start_date, weeks=None, on_planner=False, planner_time=None):
    """The guided setup's last step: on, from start_date, for `weeks` (None: no end), on the Planner or not, at a
    time or all day. Returns how many days were put on the Planner."""
    program.start_date, program.weeks, program.planner_time, program.active = start_date, weeks, planner_time, True
    program.save(update_fields=["start_date", "weeks", "planner_time", "active"])
    if on_planner:
        return schedule(program, start=max(timezone.localdate(), start_date))
    if program.scheduled_from:
        unschedule(program)
    elif program.on_planner:
        program.on_planner = False
        program.save(update_fields=["on_planner"])
    return 0


def set_planner(program, on):
    """The program page's Planner switch. Off ends the tasks after today; on (while it's on) puts them back."""
    if not on:
        if program.scheduled_from:
            return unschedule(program)
        program.on_planner = False
        program.save(update_fields=["on_planner"])
        return 0
    if not program.active:
        program.on_planner = True
        program.save(update_fields=["on_planner"])
        return 0
    return schedule(program)


# ---------------------------------------------------------------------------
# Editing a day's exercises (the guided setup's step 2)
# ---------------------------------------------------------------------------

DEFAULTS = {Exercise.CARDIO: {"target_sets": 1, "target_seconds": 20 * 60}, Exercise.MOBILITY: {"target_sets": 2, "target_seconds": 30}}


def add_exercise(day, exercise):
    """Added at the end with a sensible target: 3 × 10, cardio 20 min, mobility 2 × 30 s."""
    target = DEFAULTS.get(exercise.kind, {"target_sets": 3, "target_reps": 10})
    return ProgramExercise.objects.create(day=day, exercise=exercise, order=day.exercises.count(), **target)


def set_target(pe, sets=None, reps=None, minutes=None, miles=None):
    """New target for one exercise: sets (1-20), reps (1-100), minutes or miles - None leaves a field empty."""
    pe.target_sets = min(max(int(sets), 1), 20) if sets else pe.target_sets
    pe.target_reps = min(int(reps), 100) if reps else None
    pe.target_seconds = int(minutes * 60) if minutes else None
    pe.target_distance = miles if miles else None
    pe.save(update_fields=["target_sets", "target_reps", "target_seconds", "target_distance"])


def move_exercise(pe, step):
    """Up (-1) or down (+1) one place in its day; orders are renumbered 0..n."""
    rows = list(pe.day.exercises.all())
    i = next(n for n, r in enumerate(rows) if r.pk == pe.pk)
    j = i + step
    if 0 <= j < len(rows):
        rows[i], rows[j] = rows[j], rows[i]
    for n, r in enumerate(rows):
        if r.order != n:
            r.order = n
            r.save(update_fields=["order"])
