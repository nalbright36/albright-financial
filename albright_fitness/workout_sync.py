"""One done state for a scheduled workout, wherever it's checked off: the Planner / Home task and Fitness agree.

  - A workout task is a Fitness Planner task that isn't a weigh-in: a program day's repeating task (source_id
    "day-<pk>", programs.schedule) or one I scheduled myself ("session-<pk>", schedule.py).
  - complete_scheduled_workout(): the task was checked off (the Planner, Home's Today panel, the Bot API - all
    through planner.toggle, which calls on_planner_toggle). A finished workout that already covers it (linked to
    the task, or that program day's / those exercises done that date) counts as is; a workout started for it that
    day is finished (programs.finish); otherwise a finished "Completed from Planner" workout is made for that date
    (from_planner, no sets), tied to the task and the program day - so Fitness Today shows it done.
  - workout_finished(): a workout finished in Fitness (Finish, the quick log, the Bot API) checks off the task it
    was started from, and anything else scheduled that day it covers (schedule.complete_matching).
  - reopen_scheduled_workout(): the task was unchecked. An auto-made "Completed from Planner" workout with no sets
    is deleted; a workout with real sets is kept (the caller shows KEPT_NOTE).
  - skip_scheduled_workout(): "Skip" (the Planner / Home menu, Fitness Today, the Bot API): the occurrence is
    checked off with status "skipped" (+ an optional reason) and a "skipped" Workout with no sets is recorded for
    that date - History shows "Skipped - Rest day"; Workout.objects (stats, volume, streaks, insights) never sees
    it, and the program's schedule doesn't change. Unchecking reopens it and deletes that record (reopen_...).
    Logging the workout after all turns the skip into a plain done (_clear_skip).
  - Weigh-ins follow the same rule (task done <=> weight logged): weigh_in.on_toggle / weigh_in's receivers.
  - backfill(): tasks already checked off with no workout get their "Completed from Planner" one (the
    sync_workout_task_completion command and migration 0018).
"""
import threading
from contextlib import contextmanager

from django.db import transaction
from django.utils import timezone

from .models import ProgramDay, ScheduledSession, Workout, WorkoutSet

class SkipError(Exception):
    pass


SOURCE = "fitness"
AUTO_LABEL = "Completed from Planner"
KEPT_NOTE = "Workout kept - edit it in Fitness."

_state = threading.local()


@contextmanager
def from_fitness():
    """Fitness itself is checking tasks off (a workout finished, an exercise logged, a weigh-in saved): it already
    did the Fitness side, so planner.toggle's hook leaves it alone (an in-progress workout is never finished)."""
    _state.depth = getattr(_state, "depth", 0) + 1
    try:
        yield
    finally:
        _state.depth -= 1


def _quiet():
    return getattr(_state, "depth", 0) > 0


# ---------------------------------------------------------------------------
# Which workout a task is for
# ---------------------------------------------------------------------------

def is_workout_task(item):
    return item is not None and item.source_app == SOURCE and not item.source_id.startswith("weighin-")


def target(item):
    """(program day or None, scheduled session or None) a Fitness workout task stands for."""
    if not is_workout_task(item):
        return None, None
    sid = item.source_id
    if sid.startswith("day-") and sid[4:].isdigit():
        from django.db.models import Q                     # mine, or a program I joined (ProgramMember)
        return ProgramDay.objects.filter(Q(program__owner=item.owner) | Q(program__members__user=item.owner),
                                         pk=sid[4:]).select_related("program").distinct().first(), None
    session = ScheduledSession.objects.filter(planner_item=item).select_related(
        "exercise", "program", "program_day__program").first()
    return (session.program_day if session else None), session


def _workout_date(item, day):
    """The date the workout happened: the occurrence's - or today, when an upcoming one is checked off early."""
    from albright_planner.planner import today
    return min(day, today(item.owner))


def _covers(workout, day_obj, session, exercise_ids):
    if day_obj is not None:
        if workout.program_day_id == day_obj.pk:
            return True
        wanted = set(day_obj.exercises.values_list("exercise_id", flat=True))
        return bool(wanted) and wanted <= exercise_ids
    if session is not None:
        from .schedule import _matches
        return _matches(session, workout, exercise_ids)
    return False


def _find(item, day, finished):
    """The owner's workout (finished, or still open) for this occurrence: linked to it, else one that covers it on
    the date it happened. Finished: its done sets count; open: every exercise in it does."""
    day_obj, session = target(item)
    on = _workout_date(item, day)
    rows = Workout.objects.filter(owner=item.owner, finished_at__isnull=not finished).select_related("program_day")
    linked = rows.filter(planner_item=item, planner_date=day).first()
    if linked is not None:
        return linked
    for workout in rows.filter(date=on):
        exercises = workout.exercises.filter(sets__done=True) if finished else workout.exercises.all()
        if _covers(workout, day_obj, session, set(exercises.values_list("exercise_id", flat=True))):
            return workout
    return None


def finished_workout(item, day):
    return _find(item, day, finished=True)


def has_sets(workout):
    return WorkoutSet.objects.filter(workout_exercise__workout=workout).exists()


# ---------------------------------------------------------------------------
# Task checked off <-> workout finished
# ---------------------------------------------------------------------------

@transaction.atomic
def complete_scheduled_workout(item, day, user=None):
    """The task's occurrence on `day` was checked off: make sure a finished workout counts for it. Returns it."""
    from . import programs
    if not is_workout_task(item):
        return None
    done = finished_workout(item, day)
    if done is not None:
        return done
    started = _find(item, day, finished=False)
    if started is not None:                                  # started that day: finish THAT one, no duplicate
        if not started.planner_item_id:
            started.planner_item, started.planner_date = item, day
            started.save(update_fields=["planner_item", "planner_date"])
        programs.finish(started)
        return started
    day_obj, session = target(item)
    now = timezone.localtime()
    workout = Workout.objects.create(
        owner=item.owner, date=_workout_date(item, day), program_day=day_obj, planner_item=item, planner_date=day,
        name="" if day_obj else (session.title if session else item.title)[:100], from_planner=True,
        end_time=now.time().replace(microsecond=0), finished_at=timezone.now())
    from albright_labs import sharing
    sharing.apply_defaults(workout, item.owner)               # shared the way its owner shares new workouts
    return workout


@transaction.atomic
def skip_scheduled_workout(item, day, user=None, reason="", note=""):
    """Skip the task's occurrence on `day`: checked off as "skipped" (reason: ItemCompletion.SKIP_REASONS key, note
    optional) and a skipped Workout (no sets) recorded for that date. Already skipped: the reason is updated.
    Returns the skipped Workout, or None if it isn't scheduled then. SkipError when it's already done."""
    from albright_planner import planner, services
    if not is_workout_task(item):
        return None
    completion = item.completions.filter(occurrence_date=day).first()
    if completion is not None and not completion.skipped:
        if finished_workout(item, day) is not None and has_sets(finished_workout(item, day)):
            raise SkipError("It's already done - uncheck it first to skip it.")
        with from_fitness():                                    # an empty "Completed from Planner": reopen, then skip
            planner.toggle(item, day, user, done=False)
        reopen_scheduled_workout(item, day, user)
        completion = None
    if completion is None:
        with from_fitness():
            if planner.toggle(item, day, user, done=True) is None:
                return None
    reason, note = services.clean_skip(reason, note)
    services.set_skipped(item, day, reason, note)
    day_obj, session = target(item)
    workout, _created = Workout.all_objects.update_or_create(
        owner=item.owner, planner_item=item, planner_date=day, status=Workout.SKIPPED,
        defaults={"date": day, "program_day": day_obj, "from_planner": True, "finished_at": timezone.now(),
                  "name": "" if day_obj else (session.title if session else item.title)[:100],
                  "skip_reason": reason, "skip_note": note})
    return workout


@transaction.atomic
def skip_program_day(user, day_obj, on_date, reason="", note=""):
    """Fitness Today's Skip: through its Planner task when the program is on the Planner, else just the skipped
    record. Returns the skipped Workout (None: not scheduled that day)."""
    from . import programs
    item, when = programs.planner_occurrence(user, day_obj, on_date)
    if item is not None:
        return skip_scheduled_workout(item, when, user, reason, note)
    from albright_planner import services
    reason, note = services.clean_skip(reason, note)
    workout, _created = Workout.all_objects.update_or_create(
        owner=user, program_day=day_obj, date=on_date, status=Workout.SKIPPED, planner_item=None,
        defaults={"from_planner": True, "finished_at": timezone.now(), "skip_reason": reason, "skip_note": note})
    return workout


@transaction.atomic
def unskip_program_day(user, day_obj, on_date):
    """Undo Fitness Today's Skip: reopen its task (which removes the record), or remove the record."""
    from albright_planner import planner

    from . import programs
    item, when = programs.planner_occurrence(user, day_obj, on_date)
    if item is not None and item.completions.filter(occurrence_date=when, status="skipped").exists():
        planner.toggle(item, when, user, done=False)
    for workout in Workout.all_objects.filter(owner=user, program_day=day_obj, date=on_date, status=Workout.SKIPPED):
        if not has_sets(workout):
            workout.delete()


def _clear_skip(item, day):
    """The occurrence was skipped, but the workout happened after all: plain done, and the skipped record goes."""
    from albright_planner import services
    if services.clear_skipped(item, day):
        for workout in Workout.all_objects.filter(owner=item.owner, planner_item=item, planner_date=day,
                                                  status=Workout.SKIPPED):
            if not has_sets(workout):
                workout.delete()


@transaction.atomic
def reopen_scheduled_workout(item, day, user=None):
    """The task's occurrence on `day` was unchecked: an auto-made, empty "Completed from Planner" workout goes (a
    skipped record too); one with logged sets stays. Returns the workout kept, or None."""
    if not is_workout_task(item):
        return None
    for workout in Workout.all_objects.filter(owner=item.owner, planner_item=item, planner_date=day, from_planner=True,
                                          finished_at__isnull=False):
        if not has_sets(workout):
            workout.delete()
    return finished_workout(item, day)


def workout_finished(workout):
    """A workout finished in Fitness: check off the task it was started from and whatever else scheduled that day
    it covers. Returns the Planner items checked off."""
    from albright_planner import services

    from . import schedule
    done = []
    with from_fitness():
        if workout.planner_item_id and workout.planner_date and workout.planner_item.owner_id == workout.owner_id:
            _clear_skip(workout.planner_item, workout.planner_date)
            if services.complete_occurrence(workout.planner_item, workout.planner_date, workout.owner):
                done.append(workout.planner_item)
        done += schedule.complete_matching(workout)            # the same exercise / program day scheduled that day
        _check_program_day(workout, done)
    return done


def _check_program_day(workout, done):
    """A program day's workout that wasn't started from its task still checks that day's task off."""
    from albright_planner import services

    from . import programs
    if not workout.program_day_id or workout.planner_item_id:
        return
    item, when = programs.planner_occurrence(workout.owner, workout.program_day, workout.date)
    if item is not None and item not in done:
        _clear_skip(item, when)
    if item is not None and item not in done and not services.is_done(item, when):
        if services.complete_occurrence(item, when, workout.owner):
            done.append(item)


def kept_note(item, day):
    """After unchecking: the note to show when a real workout still counts for that occurrence."""
    return KEPT_NOTE if is_workout_task(item) and finished_workout(item, day) is not None else ""


def on_planner_toggle(item, day, done, user):
    """planner.toggle's hook - an occurrence of a Fitness task was checked off (done) or reopened."""
    if item.source_app != SOURCE or _quiet():
        return
    if item.source_id.startswith("weighin-"):
        from . import weigh_in
        weigh_in.on_toggle(item, day, done, user)
    elif done:
        complete_scheduled_workout(item, day, user)
    else:
        reopen_scheduled_workout(item, day, user)


# ---------------------------------------------------------------------------
# Backfill: tasks checked off before this sync existed
# ---------------------------------------------------------------------------

def backfill(get_model, since=None, dry_run=False):
    """Every checked-off Fitness workout occurrence with no finished workout for it gets a "Completed from Planner"
    one. get_model: django.apps.apps.get_model, or a migration's apps.get_model (so it only uses plain queries).
    since: only occurrences on or after this date. Returns how many were (or would be) made."""
    Completion = get_model("albright_planner", "ItemCompletion")
    WorkoutM = get_model("albright_fitness", "Workout")
    WorkoutExerciseM = get_model("albright_fitness", "WorkoutExercise")
    Session = get_model("albright_fitness", "ScheduledSession")
    Day = get_model("albright_fitness", "ProgramDay")
    DayExercise = get_model("albright_fitness", "ProgramExercise")
    completions = Completion.objects.filter(item__source_app=SOURCE).exclude(item__source_id__startswith="weighin-") \
        .select_related("item").order_by("occurrence_date", "pk")
    if since is not None:
        completions = completions.filter(occurrence_date__gte=since)
    made = 0
    for done in completions:
        item, day = done.item, done.occurrence_date
        day_obj = session = None
        if item.source_id.startswith("day-") and item.source_id[4:].isdigit():
            day_obj = Day.objects.filter(pk=item.source_id[4:], program__owner_id=item.owner_id).first()
        else:
            session = Session.objects.filter(planner_item_id=item.pk).first()
            if session is not None and session.program_day_id:
                day_obj = Day.objects.filter(pk=session.program_day_id).first()
        finished = WorkoutM.objects.filter(owner_id=item.owner_id, finished_at__isnull=False)
        if finished.filter(planner_item_id=item.pk, planner_date=day).exists():
            continue
        on_day = finished.filter(date=day)
        if day_obj is not None:
            if on_day.filter(program_day_id=day_obj.pk).exists():
                continue
            wanted = set(DayExercise.objects.filter(day_id=day_obj.pk).values_list("exercise_id", flat=True))
        elif session is not None and session.exercise_id:
            wanted = {session.exercise_id}
        else:
            wanted = None                      # a whole program / gone: any finished workout that day covers it
        logged_sets = WorkoutExerciseM.objects.filter(workout__in=on_day, sets__done=True)
        if wanted is None and on_day.exists():
            continue
        if wanted and wanted <= set(logged_sets.values_list("exercise_id", flat=True)):
            continue
        made += 1
        if dry_run:
            continue
        stamp = timezone.now()
        WorkoutM.objects.create(owner_id=item.owner_id, date=day, program_day_id=day_obj.pk if day_obj else None,
                                planner_item_id=item.pk, planner_date=day, from_planner=True, finished_at=stamp,
                                name="" if day_obj else (item.title or "")[:100])
    return made
