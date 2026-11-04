"""Progress insights (the Insights page, the dashboard's "This week" card, GET fitness/summary) - all the owner's own.

- A week: workouts done vs planned, training volume (lb lifted in done working sets) and its change vs the week
  before, new records, average daily protein and calories (over the days with food logged) vs targets, days logged.
- Training: working sets per muscle group per week (8 weeks), the best estimated 1RM per week for the main lifts,
  and exercises done before but not in the last 14 days.
- Nutrition: 4-week averages vs targets; the foods and recipes logged most.
- Consistency: the streak, done vs planned per week, and which days had a workout (a calendar).
Wording is neutral: numbers and plain changes, never "missed" or "failed".
"""
from collections import Counter, defaultdict
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal

from django.db.models import Count, Max, Sum
from django.utils import timezone

from . import stats, training
from .models import MACROS, MUSCLES, Exercise, FoodLogEntry, NutritionProfile, PersonalRecord, Workout, WorkoutSet

MAIN_LIFTS = ["Barbell Back Squat", "Barbell Bench Press", "Deadlift", "Overhead Press"]
UNTRAINED_DAYS = 14
LOOKBACK_DAYS = 180


def _whole(value):
    return int(Decimal(value).to_integral_value(ROUND_HALF_UP))


def _working_sets(user, start, end):
    return (WorkoutSet.objects.filter(workout_exercise__workout__owner=user, workout_exercise__workout__date__gte=start,
                                      workout_exercise__workout__date__lte=end, done=True)
            .exclude(set_type=WorkoutSet.WARMUP).select_related("workout_exercise__exercise", "workout_exercise__workout"))


def volume(user, start, end):
    """Pounds lifted (weight x reps) in done working sets between the dates."""
    return _whole(sum((s.weight_lb * s.reps for s in _working_sets(user, start, end) if s.weight_lb and s.reps), Decimal(0)))


def food_days(user, start, end):
    """{date: {calories, protein, carbs, fat, fiber}} for the days with anything logged."""
    rows = (FoodLogEntry.objects.filter(owner=user, date__gte=start, date__lte=end).values("date")
            .annotate(**{n: Sum(n) for n in MACROS}))
    return {r["date"]: {n: r[n] or Decimal(0) for n in MACROS} for r in rows}


def averages(days, names=MACROS):
    if not days:
        return {n: None for n in names}
    return {n: _whole(sum(d[n] for d in days.values()) / len(days)) for n in names}


def week(user, monday, today=None):
    """One week's summary (Monday to Sunday) -> dict."""
    today = today or timezone.localdate()
    sunday = monday + timedelta(days=6)
    profile = NutritionProfile.load(user)
    this, last = volume(user, monday, sunday), volume(user, monday - timedelta(days=7), monday - timedelta(days=1))
    change = None
    if last:
        change = _whole(Decimal(this - last) * 100 / last)
    records = list(PersonalRecord.objects.filter(owner=user, achieved_on__gte=monday, achieved_on__lte=sunday)
                   .select_related("exercise"))
    days = food_days(user, monday, min(sunday, today))
    avg = averages(days, ("calories", "protein"))
    return {"start": monday, "end": sunday, "done": stats.done_in_week(user, monday), "planned": stats.planned_in_week(user, monday),
            "volume": this, "volume_last": last, "volume_change": change,
            "records": [{"exercise": r.exercise.name, "text": training.record_text(r), "date": r.achieved_on} for r in records],
            "days_logged": len(days), "avg_calories": avg["calories"], "avg_protein": avg["protein"],
            "target_calories": profile.target_calories, "target_protein": profile.target_protein}


def volume_change_text(summary):
    """"Volume up 12% on last week" / "down 5%" / "about the same" / "" (nothing to compare)."""
    change = summary["volume_change"]
    if change is None:
        return ""
    if abs(change) < 2:
        return "about the same as last week"
    return f"{'up' if change > 0 else 'down'} {abs(change)}% on last week"


def weeks(user, count=4, today=None):
    """The last `count` weeks, this week first."""
    today = today or timezone.localdate()
    monday = stats.week_start(today)
    return [week(user, monday - timedelta(weeks=i), today) for i in range(count)]


# ---------------------------------------------------------------------------
# Training
# ---------------------------------------------------------------------------

def muscle_weeks(user, count=8, today=None):
    """Working sets per primary muscle per week, oldest week first -> {"weeks": [monday], "rows": [{"label",
    "sets": [n per week], "total"}], "max"}."""
    today = today or timezone.localdate()
    first = stats.week_start(today) - timedelta(weeks=count - 1)
    mondays = [first + timedelta(weeks=i) for i in range(count)]
    counts = defaultdict(lambda: [0] * count)
    for s in _working_sets(user, first, today):
        index = (s.workout_exercise.workout.date - first).days // 7
        for muscle in s.workout_exercise.exercise.primary_muscles or []:
            counts[muscle][index] += 1
    labels = dict(MUSCLES)
    rows = sorted(({"muscle": m, "label": labels.get(m, m), "sets": v, "total": sum(v)} for m, v in counts.items()),
                  key=lambda r: (-r["total"], r["label"]))
    return {"weeks": mondays, "rows": rows, "max": max((n for r in rows for n in r["sets"]), default=0)}


def lift_trends(user, count=12, today=None):
    """The best estimated 1RM per week for each main lift the user has done -> [{"name", "points": [{"date",
    "value", "label"}], "first", "last", "change"}]."""
    today = today or timezone.localdate()
    since = stats.week_start(today) - timedelta(weeks=count - 1)
    out = []
    for name in MAIN_LIFTS:
        best = {}
        for s in _working_sets(user, since, today).filter(workout_exercise__exercise__name=name,
                                                          workout_exercise__exercise__owner__isnull=True):
            e1rm = training.epley(s.weight_lb, s.reps)
            if e1rm:
                monday = stats.week_start(s.workout_exercise.workout.date)
                best[monday] = max(best.get(monday, 0), e1rm)
        if not best:
            continue
        points = [{"date": d, "value": v, "label": f"{training.fmt_weight(v)} lb"} for d, v in sorted(best.items())]
        out.append({"name": name, "points": points, "first": points[0]["value"], "last": points[-1]["value"],
                    "change": points[-1]["value"] - points[0]["value"] if len(points) > 1 else None})
    return out


def untrained(user, days=UNTRAINED_DAYS, today=None):
    """Exercises done in the last 6 months but not in the last `days` days -> [{"exercise", "last", "days"}]."""
    today = today or timezone.localdate()
    rows = (Workout.objects.filter(owner=user, finished_at__isnull=False, date__gte=today - timedelta(days=LOOKBACK_DAYS))
            .values("exercises__exercise").annotate(last=Max("date")).filter(exercises__exercise__isnull=False))
    stale = {r["exercises__exercise"]: r["last"] for r in rows if (today - r["last"]).days >= days}
    names = Exercise.objects.in_bulk(list(stale))
    return sorted(({"exercise": names[pk], "last": last, "days": (today - last).days} for pk, last in stale.items() if pk in names),
                  key=lambda r: (-r["days"], r["exercise"].name))


# ---------------------------------------------------------------------------
# Nutrition and consistency
# ---------------------------------------------------------------------------

def nutrition(user, today=None, days=28):
    today = today or timezone.localdate()
    profile = NutritionProfile.load(user)
    logged = food_days(user, today - timedelta(days=days - 1), today)
    avg = averages(logged)
    rows = [{"name": n, "average": avg[n], "target": profile.target(n)} for n in MACROS     # nothing logged and no
            if n in (profile.tracked or []) and avg[n] is not None and (avg[n] or profile.target(n))]   # target: left out
    entries = FoodLogEntry.objects.filter(owner=user, date__gte=today - timedelta(days=days - 1), date__lte=today)
    foods = (entries.exclude(source=FoodLogEntry.RECIPE).values("label").annotate(times=Count("pk")).order_by("-times", "label")[:5])
    recipes = (entries.filter(source=FoodLogEntry.RECIPE).values("label").annotate(times=Count("pk")).order_by("-times", "label")[:5])
    return {"days_logged": len(logged), "rows": rows, "foods": list(foods), "recipes": list(recipes)}


def consistency(user, count=12, today=None):
    """The streak, done vs planned for each of the last `count` weeks, and a calendar of workout days (weeks as
    columns, Monday to Sunday down)."""
    today = today or timezone.localdate()
    first = stats.week_start(today) - timedelta(weeks=count - 1)
    worked = Counter(Workout.objects.filter(owner=user, finished_at__isnull=False, date__gte=first, date__lte=today)
                     .values_list("date", flat=True))
    columns = []
    for i in range(count):
        monday = first + timedelta(weeks=i)
        columns.append({"monday": monday, "done": stats.done_in_week(user, monday), "planned": stats.planned_in_week(user, monday),
                        "days": [{"date": monday + timedelta(days=d), "count": worked.get(monday + timedelta(days=d), 0),
                                  "future": monday + timedelta(days=d) > today} for d in range(7)]})
    return {"streak": stats.streak(user, today), "weeks": columns, "active_days": len(worked)}
