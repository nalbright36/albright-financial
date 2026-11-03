"""The dashboard's numbers: today's planned workout, this week done vs planned, weekly volume by muscle group,
the streak, and one exercise's history for its chart.

Planned = the active programs' days on their weekdays (from the week the program was made). A week counts toward
the streak when every planned workout that week was done (as many finished workouts as planned ones); the
current week counts once it's complete, and doesn't break the streak while it's still under way.
"""
from collections import defaultdict
from datetime import timedelta
from decimal import Decimal

from django.utils import timezone

from . import training
from .models import MUSCLES, PersonalRecord, Program, ProgramDay, Workout, WorkoutExercise, WorkoutSet

MAX_STREAK_WEEKS = 104


def week_start(day):
    return day - timedelta(days=day.weekday())


def active_days(user):
    return list(ProgramDay.objects.filter(program__owner=user, program__active=True).select_related("program")
                .order_by("program__name", "order"))


def todays(user, today=None):
    """[(program day, finished workout or None)] planned for today."""
    today = today or timezone.localdate()
    days = [d for d in active_days(user) if today.weekday() in (d.weekdays or [])]
    done = {w.program_day_id: w for w in Workout.objects.filter(owner=user, date=today, finished_at__isnull=False,
                                                                   program_day__in=days)}
    return [(d, done.get(d.pk)) for d in days]


def status_line(user):
    """The launcher's and Home's line: "Today: Upper A" or "Rest day"."""
    plan = todays(user)
    if not plan:
        return "Rest day"
    names = ", ".join(d.name for d, _w in plan)
    return f"Done today: {names}" if all(w for _d, w in plan) else f"Today: {names}"


def planned_in_week(user, monday, days=None):
    days = active_days(user) if days is None else days
    sunday = monday + timedelta(days=6)
    return sum(len(d.weekdays or []) for d in days if timezone.localtime(d.program.created_at).date() <= sunday)


def done_in_week(user, monday):
    return Workout.objects.filter(owner=user, date__gte=monday, date__lte=monday + timedelta(days=6),
                                  finished_at__isnull=False).count()


def streak(user, today=None):
    """Weeks in a row with every planned workout done."""
    today = today or timezone.localdate()
    days = active_days(user)
    count, monday = 0, week_start(today)
    planned, done = planned_in_week(user, monday, days), done_in_week(user, monday)
    if planned and done >= planned:
        count += 1                                       # this week's already complete
    for _ in range(MAX_STREAK_WEEKS):
        monday -= timedelta(days=7)
        planned = planned_in_week(user, monday, days)
        if not planned or done_in_week(user, monday) < planned:
            break
        count += 1
    return count


def volume_by_muscle(user, today=None):
    """This week's done working sets by primary muscle: [{"muscle", "label", "sets", "pounds"}], most first."""
    today = today or timezone.localdate()
    monday = week_start(today)
    sets = WorkoutSet.objects.filter(workout_exercise__workout__owner=user, workout_exercise__workout__date__gte=monday,
                                     workout_exercise__workout__date__lte=monday + timedelta(days=6), done=True) \
        .exclude(set_type=WorkoutSet.WARMUP).select_related("workout_exercise__exercise")
    count, pounds = defaultdict(int), defaultdict(Decimal)
    for s in sets:
        for muscle in s.workout_exercise.exercise.primary_muscles or []:
            count[muscle] += 1
            if s.weight_lb and s.reps:
                pounds[muscle] += s.weight_lb * s.reps
    labels = dict(MUSCLES)
    rows = [{"muscle": m, "label": labels.get(m, m), "sets": n, "pounds": pounds[m]} for m, n in count.items()]
    return sorted(rows, key=lambda r: (-r["sets"], r["label"]))


def dashboard(user):
    today = timezone.localdate()
    monday = week_start(today)
    return {"today": todays(user, today), "planned": planned_in_week(user, monday), "done": done_in_week(user, monday),
            "streak": streak(user, today), "volume": volume_by_muscle(user, today),
            "records": PersonalRecord.objects.filter(owner=user).select_related("exercise")[:6],
            "programs": Program.objects.filter(owner=user, active=True).count()}


def exercise_history(user, exercise, limit=40):
    """[{"date", "value", "label", "workout"}] oldest first: the best estimated 1RM per session (strength), the
    most reps (bodyweight), or the longest distance / time (cardio). And the unit for the chart's title."""
    rows = []
    sessions = WorkoutExercise.objects.filter(workout__owner=user, exercise=exercise, workout__finished_at__isnull=False) \
        .select_related("workout").prefetch_related("sets").order_by("-workout__date", "-pk")[:limit]
    unit = "lb (est. 1RM)"
    for we in reversed(list(sessions)):
        done = [s for s in we.sets.all() if s.done and s.set_type != WorkoutSet.WARMUP]
        if not done:
            continue
        if exercise.is_cardio:
            miles = max((s.miles or 0 for s in done), default=0)
            if miles:
                unit, value, label = "mi", miles, f"{training.fmt_weight(miles)} mi"
            else:
                seconds = max((s.duration_seconds or 0 for s in done), default=0)
                unit, value, label = "min", Decimal(seconds) / 60, training.clock(seconds)
        else:
            best = max((training.epley(s.weight_lb, s.reps) or 0 for s in done), default=0)
            if best:
                value, label = best, f"{training.fmt_weight(best)} lb"
            else:
                unit = "reps"
                value = max((s.reps or 0 for s in done), default=0)
                label = f"{value} reps"
        if value:
            rows.append({"date": we.workout.date, "value": Decimal(value), "label": label, "workout": we.workout})
    return rows, unit
