"""Calories burned by a day's workouts - added to the food diary's calorie goal (nutrition.allowance).

kcal = (MET - 1) x body weight kg x hours. Net of resting (the "- 1"): the base calorie target already pays for the
resting rate, so counting it again would overstate a workout. METs from the 2011 Compendium of Physical Activities.

- Weight: the latest weigh-in on or before the day, else the Targets weight, else 75 kg (the page says so).
- Time: a cardio exercise's set durations; lifting / bodyweight work the workout's start-to-end time less its cardio
  (at most 3 hours), else 3 minutes a done set (with rest); a workout checked off on the Planner with no sets
  its program day's targets.
- A workout's own calories_burned (typed - a watch's number) wins over the estimate.
Only finished workouts count (skipped ones aren't in Workout.objects).
"""
from decimal import ROUND_HALF_UP, Decimal

from .models import KG_TO_LB, NutritionProfile, WeightEntry, Workout

FALLBACK_KG = Decimal(75)
SET_MINUTES = Decimal(3)
MAX_MINUTES = Decimal(180)
CARDIO_METS = {"running": Decimal("9.8"), "treadmill run": Decimal("9.8"), "walking": Decimal("3.5"),
               "cycling": Decimal("7.5"), "stationary bike": Decimal("6.8"), "rowing machine": Decimal("7.0"),
               "elliptical": Decimal("5.0"), "stair climber": Decimal("9.0"), "swimming": Decimal("6.0"),
               "jump rope": Decimal("11.8")}
KIND_METS = {"strength": Decimal("5.0"), "bodyweight": Decimal("3.8"), "cardio": Decimal("7.0"), "mobility": Decimal("2.3")}
RUN_PACE_METS = [(Decimal(5), Decimal("8.3")), (Decimal(6), Decimal("9.8")), (Decimal(7), Decimal("11.0")),
                 (Decimal(8), Decimal("11.8")), (Decimal(9), Decimal("12.8")), (Decimal(10), Decimal("14.5"))]
RUNS = {"running", "treadmill run"}


def whole(value):
    return int(Decimal(value).to_integral_value(ROUND_HALF_UP))


def body_kg(user, on):
    """(kg, where it came from: "weigh-in" / "targets" / "default")."""
    entry = WeightEntry.objects.filter(owner=user, date__lte=on).order_by("-date").first()
    if entry is not None:
        return Decimal(entry.weight_kg), "weigh-in"
    profile = NutritionProfile.objects.filter(user=user).first()
    if profile is not None and profile.weight_kg:
        return Decimal(profile.weight_kg), "targets"
    return FALLBACK_KG, "default"


def run_met(mph):
    met = RUN_PACE_METS[0][1]
    for speed, value in RUN_PACE_METS:
        if mph >= speed:
            met = value
    return met


def _minutes_between(w):
    if not (w.start_time and w.end_time):
        return None
    start, end = w.start_time.hour * 60 + w.start_time.minute, w.end_time.hour * 60 + w.end_time.minute
    return Decimal((end - start) % 1440)


def estimate(w, kg):
    """(kcal, minutes, miles, sets) for one workout - an estimate, ignoring calories_burned."""
    kcal, cardio_minutes, miles, lift_sets, lift_met = Decimal(0), Decimal(0), Decimal(0), 0, None
    for we in w.exercises.all():
        ex, done = we.exercise, [s for s in we.sets.all() if s.done]
        if ex.kind == "cardio":
            seconds = sum((s.duration_seconds or 0) for s in done)
            dist = sum((s.miles or 0) for s in done)
            miles += dist
            if not seconds:
                continue
            met = CARDIO_METS.get(ex.name.lower(), KIND_METS["cardio"])
            if ex.name.lower() in RUNS and dist:
                met = run_met(Decimal(dist) / (Decimal(seconds) / 3600))
            kcal += (met - 1) * kg * Decimal(seconds) / 3600
            cardio_minutes += Decimal(seconds) / 60
        elif done:
            lift_sets += len(done)
            met = KIND_METS.get(ex.kind, KIND_METS["strength"])
            lift_met = max(lift_met or met, met)
    lift_minutes = Decimal(0)
    if lift_sets:
        between = _minutes_between(w)
        lift_minutes = min(max(between - cardio_minutes, 0), MAX_MINUTES) if between else 0
        lift_minutes = lift_minutes or lift_sets * SET_MINUTES
        kcal += (lift_met - 1) * kg * lift_minutes / 60
    elif not cardio_minutes and w.program_day_id:              # checked off on the Planner: the day's targets
        for pe in w.program_day.exercises.select_related("exercise"):
            if pe.exercise.kind == "cardio" and pe.target_seconds:
                met = CARDIO_METS.get(pe.exercise.name.lower(), KIND_METS["cardio"])
                kcal += (met - 1) * kg * Decimal(pe.target_seconds) / 3600
                cardio_minutes += Decimal(pe.target_seconds) / 60
            else:
                minutes = Decimal(pe.target_sets or 0) * SET_MINUTES
                kcal += (KIND_METS.get(pe.exercise.kind, KIND_METS["strength"]) - 1) * kg * minutes / 60
                lift_minutes += minutes
    return whole(kcal), whole(cardio_minutes + lift_minutes), miles, lift_sets


def day(user, on):
    """{"rows": [{"workout", "kcal", "estimate", "typed", "detail"}], "total", "kg", "lb", "weight_from"}."""
    kg, weight_from = body_kg(user, on)
    rows = []
    workouts = (Workout.objects.filter(owner=user, date=on, finished_at__isnull=False).order_by("finished_at", "pk")
                .select_related("program_day").prefetch_related("exercises__exercise", "exercises__sets"))
    for w in workouts:
        kcal, minutes, miles, sets = estimate(w, kg)
        between = _minutes_between(w)
        shown = whole(between) if between and sets else minutes
        detail = " · ".join(x for x in (f"{shown} min" if shown else "", f"{Decimal(miles).quantize(Decimal('0.1')).normalize():f} mi" if miles else "",
                                        f"{sets} sets" if sets else "") if x)
        typed = w.calories_burned is not None
        rows.append({"workout": w, "estimate": kcal, "typed": typed, "kcal": w.calories_burned if typed else kcal,
                     "detail": detail})
    return {"rows": rows, "total": sum(r["kcal"] for r in rows), "kg": kg, "weight_from": weight_from,
            "lb": whole(kg * KG_TO_LB)}
