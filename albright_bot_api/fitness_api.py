"""Bot API v1 - Fitness (/api/bot/v1/fitness/...).

Always the token owner's own Fitness data - their workouts, food diary and weigh-ins - never anyone else's (not even
someone in the same circle). fitness:read reads (today, the weekly summary); fitness:write logs a workout, food or a
weigh-in. The owner needs Fitness access and API keys allowed (auth.bot_endpoint checks on every call, and logs it).

Food is never guessed: a recipe or "my food" name must match exactly (any case) - otherwise nothing is logged and
the answer lists suggestions (close recipe / my food names, and USDA foods to pick from on the site).
"""
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from django.urls import reverse
from django.utils import timezone

from .auth import ApiError, bot_endpoint, json_body

MAX_TEXT = 2000
MAX_EXERCISES = 30
MAX_SETS = 50
MAX_SERVINGS = Decimal(50)
MAX_WEEKS = 12


def _str(data, key, limit, required=False):
    value = data.get(key)
    if value in (None, ""):
        if required:
            raise ApiError(400, f'"{key}" is required.', f"rejected: missing {key}")
        return ""
    if not isinstance(value, str):
        raise ApiError(400, f'"{key}" must be a string.', f"rejected: {key} not a string")
    value = " ".join(value.split())
    if len(value) > limit:
        raise ApiError(400, f'"{key}" is longer than {limit} characters.', f"rejected: {key} too long")
    return value


def _number(data, key, low, high, required=False):
    value = data.get(key)
    if value in (None, ""):
        if required:
            raise ApiError(400, f'"{key}" is required.', f"rejected: missing {key}")
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ApiError(400, f'"{key}" must be a number.', f"rejected: bad {key}")
    try:
        number = Decimal(str(value).strip())
    except InvalidOperation as exc:
        raise ApiError(400, f'"{key}" must be a number.', f"rejected: bad {key}") from exc
    if not number.is_finite() or not low <= number <= high:
        raise ApiError(400, f'"{key}" must be from {low} to {high}.', f"rejected: bad {key}")
    return number


def _date(data, key="date", days_back=365):
    value = data.get(key)
    today = timezone.localdate()
    if value in (None, ""):
        return today
    try:
        day = date.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        day = None
    if day is None or day > today or day < today - timedelta(days=days_back):
        raise ApiError(400, f'"{key}" must be YYYY-MM-DD, today or up to {days_back} days back.', f"rejected: bad {key}")
    return day


def _set_json(s):
    row = {"reps": s.reps, "weight": f"{s.weight:f}" if s.weight is not None else None, "unit": s.unit if s.weight is not None else None}
    if s.distance is not None or s.duration_seconds:
        row = {"distance": f"{s.distance:f}" if s.distance is not None else None, "distance_unit": s.distance_unit,
               "duration_seconds": s.duration_seconds}
    return row


def _amounts(obj):
    return {n: f"{getattr(obj, n):.1f}" if getattr(obj, n) is not None else None
            for n in ("calories", "protein", "carbs", "fat", "fiber")}


# ---------------------------------------------------------------------------
# Workouts
# ---------------------------------------------------------------------------

def _structured(user, exercises):
    """[{"exercise": name, "sets": [{"reps", "weight", "unit"} or {"distance", "distance_unit", "duration_seconds"}]}]
    -> [training.Parsed] (unknown names keep a problem, for confirmation)."""
    from albright_fitness import training
    if not isinstance(exercises, list) or not exercises or len(exercises) > MAX_EXERCISES:
        raise ApiError(400, f'"exercises" must be a list of 1-{MAX_EXERCISES} exercises.', "rejected: bad exercises")
    out = []
    for row in exercises:
        if not isinstance(row, dict):
            raise ApiError(400, 'Each exercise must be {"exercise": name, "sets": [...]}.', "rejected: bad exercise")
        name = _str(row, "exercise", 100, required=True)
        sets = row.get("sets")
        if not isinstance(sets, list) or not sets or len(sets) > MAX_SETS:
            raise ApiError(400, f'"sets" must be a list of 1-{MAX_SETS} sets.', "rejected: bad sets")
        item = training.Parsed(name, exercise=training.find_exercise(user, name))
        if item.exercise is None:
            item.problem = f"couldn't find an exercise called “{name}”"
        for s in sets:
            if not isinstance(s, dict):
                raise ApiError(400, "Each set must be an object.", "rejected: bad set")
            reps = _number(s, "reps", 1, 1000)
            weight = _number(s, "weight", 0, 2000)
            unit = s.get("unit") or "lb"
            distance = _number(s, "distance", 0, 1000)
            seconds = _number(s, "duration_seconds", 1, 86400)
            if unit not in ("lb", "kg") or (s.get("distance_unit") or "mi") not in ("mi", "km"):
                raise ApiError(400, 'unit must be "lb" or "kg"; distance_unit "mi" or "km".', "rejected: bad unit")
            if reps is None and distance is None and seconds is None:
                raise ApiError(400, "Each set needs reps, a distance or a duration.", "rejected: empty set")
            fields = {}
            if reps is not None:
                fields["reps"] = int(reps)
            if weight is not None:
                fields.update(weight=weight, unit=unit)
            if distance is not None:
                fields.update(distance=distance, distance_unit=s.get("distance_unit") or "mi")
            if seconds is not None:
                fields["duration_seconds"] = int(seconds)
            item.sets.append(fields)
        out.append(item)
    return out


def _suggest_exercises(user, text):
    from albright_fitness.models import Exercise
    words = [w for w in text.lower().split() if len(w) > 2][:1]
    if not words:
        return []
    return list(Exercise.objects.visible_to(user).filter(name__icontains=words[0]).values_list("name", flat=True)[:5])


@bot_endpoint("fitness:write", methods=("POST",))
def workouts(request, token):
    from albright_fitness import programs, training
    user = token.owner
    data = json_body(request)
    if data.get("text") not in (None, ""):
        parsed = training.parse_quick(_str(data, "text", MAX_TEXT, required=True), user)
    elif data.get("exercises") is not None:
        parsed = _structured(user, data["exercises"])
    else:
        raise ApiError(400, 'Send {"text": "squat 225x5x3, bench 185x8,8,7"} or {"exercises": [...]}.', "rejected: nothing to log")
    good = [p for p in parsed if not p.problem]
    unknown = [{"text": p.text, "problem": p.problem, "suggestions": _suggest_exercises(user, p.text)} for p in parsed if p.problem]
    if not good:
        raise ApiError(422, "Nothing could be logged - check the exercise names.", "rejected: nothing parsed",
                       extra={"unknown": unknown})
    from . import share_field
    share_field.validate(token, "fitness", data)
    workout, records, day = programs.log_parsed(user, good, name=_str(data, "name", 100) or "Quick log")
    shared = share_field.apply(token, workout, data)
    exercises = [{"exercise": we.exercise.name, "sets": [_set_json(s) for s in we.sets.all()]}
                 for we in workout.exercises.select_related("exercise").prefetch_related("sets")]
    return ({"id": workout.pk, "date": workout.date.isoformat(), "name": workout.title, "exercises": exercises,
             "unknown": unknown, "records": [{"exercise": r.exercise.name, "kind": r.kind, "text": training.record_text(r)}
                                             for r in records],
             "program_day": day.name if day else None, "planner_completed": bool(workout.planner_item_id), "shared_with": shared,
             "url": reverse("albright_fitness:summary", args=[workout.pk])},
            201, f"logged workout #{workout.pk} ({len(exercises)} exercises{', ' + str(len(unknown)) + ' unknown' if unknown else ''})")


# ---------------------------------------------------------------------------
# Food
# ---------------------------------------------------------------------------

def _usda_suggestions(name):
    """USDA foods to pick from on the site - never logged automatically. Only from the cache plus one search when the
    key is set; nothing on any error."""
    from albright_fitness import usda
    try:
        return [{"fdc_id": f.fdc_id, "name": str(f)} for f in usda.search(name)[:5]] if usda.configured() else []
    except usda.UsdaError:
        return []


@bot_endpoint("fitness:write", methods=("POST",))
def food(request, token):
    from albright_fitness import food_views, nutrition
    from albright_fitness.models import MACROS, CustomFood, FoodLogEntry
    user = token.owner
    data = json_body(request)
    on = _date(data, days_back=60)
    slot = data.get("slot") or FoodLogEntry.SNACK
    if slot not in dict(FoodLogEntry.SLOTS):
        raise ApiError(400, '"slot" must be breakfast, lunch, dinner or snack.', "rejected: bad slot")
    servings = _number(data, "servings", Decimal("0.01"), MAX_SERVINGS) or Decimal(1)
    entry = None
    if data.get("recipe_id") not in (None, "") or data.get("recipe"):
        from albright_meals import scope
        readable = scope.readable(user).select_related("nutrition")
        if data.get("recipe_id") not in (None, ""):
            if isinstance(data["recipe_id"], bool) or not isinstance(data["recipe_id"], int):
                raise ApiError(400, '"recipe_id" must be a number.', "rejected: bad recipe_id")
            recipe = readable.filter(pk=data["recipe_id"]).first()
            if recipe is None:
                raise ApiError(404, "No recipe with that id that you can see.", "rejected: recipe not found")
        else:
            name = _str(data, "recipe", 200, required=True)
            recipe = readable.filter(title__iexact=name).first()
            if recipe is None:
                close = list(readable.filter(title__icontains=name.split()[0]).values_list("pk", "title")[:5]) if name.split() else []
                raise ApiError(422, f"No recipe called “{name}” - nothing was logged.", "rejected: recipe not matched",
                               extra={"suggestions": {"recipes": [{"id": pk, "title": t} for pk, t in close]}})
        entry = food_views._log_recipe(user, recipe, servings, on, slot)
        if entry is None:
            raise ApiError(422, f"{recipe.title} doesn't have nutrition yet - calculate it on the recipe page first.",
                           "rejected: recipe without nutrition")
    elif data.get("my_food"):
        name = _str(data, "my_food", 100, required=True)
        mine = CustomFood.objects.filter(owner=user, name__iexact=name).first()
        if mine is None:
            close = list(CustomFood.objects.filter(owner=user, name__icontains=name.split()[0]).values_list("name", flat=True)[:5]) \
                if name.split() else []
            raise ApiError(422, f"None of your foods is called “{name}” - nothing was logged.", "rejected: my food not matched",
                           extra={"suggestions": {"my_foods": close, "usda": _usda_suggestions(name)}})
        count = f"{servings.normalize():f}"
        entry = FoodLogEntry.objects.create(
            owner=user, date=on, slot=slot, source=FoodLogEntry.CUSTOM, custom_food=mine, label=mine.name, servings=servings,
            portion_label=(mine.serving_label if servings == 1 else f"{count} × {mine.serving_label}")[:60],
            **nutrition.scaled({n: getattr(mine, n) for n in MACROS}, servings))
    elif any(data.get(n) not in (None, "") for n in ("calories", "protein", "carbs", "fat")):
        values = {n: _number(data, n, 0, 10000) for n in MACROS}
        entry = FoodLogEntry.objects.create(owner=user, date=on, slot=slot, source=FoodLogEntry.QUICK,
                                            label=_str(data, "label", 200) or "Quick add", **{
                                                n: v.quantize(Decimal("0.01")) if v is not None else None for n, v in values.items()})
    elif data.get("food") or data.get("name"):
        name = _str(data, "food" if data.get("food") else "name", 100)
        raise ApiError(422, "Foods from USDA aren't logged automatically - pick one on the site, or send its macros.",
                       "rejected: usda food not guessed", extra={"suggestions": {"usda": _usda_suggestions(name)}})
    else:
        raise ApiError(400, 'Send {"recipe_id" or "recipe", "servings"}, {"my_food", "servings"} or {"label", "calories", '
                            '"protein", "carbs", "fat"}.', "rejected: nothing to log")
    return ({"id": entry.pk, "date": entry.date.isoformat(), "slot": entry.slot, "label": entry.label, "source": entry.source,
             "servings": f"{entry.servings:f}" if entry.servings is not None else None, **_amounts(entry)},
            201, f"logged food #{entry.pk} ({entry.source})")


# ---------------------------------------------------------------------------
# Weight
# ---------------------------------------------------------------------------

@bot_endpoint("fitness:write", methods=("POST",))
def weight(request, token):
    from albright_fitness import body
    from albright_fitness.models import NutritionProfile
    data = json_body(request)
    profile = NutritionProfile.load(token.owner)
    unit = data.get("unit") or profile.weight_unit
    if unit not in ("lb", "kg"):
        raise ApiError(400, '"unit" must be "lb" or "kg".', "rejected: bad unit")
    low, high = (45, 880) if unit == "lb" else (20, 400)
    value = _number(data, "weight", low, high, required=True)
    entry, created = body.log_weight(token.owner, _date(data), value, unit, _str(data, "note", 200))
    return ({"id": entry.pk, "date": entry.date.isoformat(), "weight": f"{body.shown(entry.weight_kg, unit)}", "unit": unit,
             "replaced": not created}, 201 if created else 200, f"logged weight for {entry.date}")


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

@bot_endpoint("fitness:read")
def today(request, token):
    from albright_fitness import nutrition, stats
    from albright_fitness.models import MACROS, FoodLogEntry, NutritionProfile, Workout
    user, on = token.owner, timezone.localdate()
    profile = NutritionProfile.load(user)
    eaten = nutrition.totals(FoodLogEntry.objects.filter(owner=user, date=on))
    macros = {}
    for n in MACROS:
        target = profile.target(n)
        macros[n] = {"eaten": f"{eaten[n]:.0f}", "target": target,
                     "remaining": int(target - eaten[n]) if target else None, "tracked": n in (profile.tracked or [])}
    done = Workout.objects.filter(owner=user, date=on, finished_at__isnull=False).select_related("program_day")
    return ({"date": on.isoformat(), "planned": [{"name": d.name, "program": d.program.name, "done": bool(w)} for d, w in stats.todays(user, on)],
             "workouts_done": [{"id": w.pk, "name": w.title} for w in done], "macros": macros},
            200, "read fitness today")


@bot_endpoint("fitness:read")
def summary(request, token):
    from albright_fitness import insights
    raw = request.GET.get("weeks", "4")
    if not raw.isdigit() or not 1 <= int(raw) <= MAX_WEEKS:
        raise ApiError(400, f'"weeks" must be 1-{MAX_WEEKS}.', "rejected: bad weeks")
    rows = insights.weeks(token.owner, int(raw))
    out = [{"week_of": w["start"].isoformat(), "workouts_done": w["done"], "workouts_planned": w["planned"], "volume_lb": w["volume"],
            "volume_change_pct": w["volume_change"], "new_records": [{"exercise": r["exercise"], "text": r["text"]} for r in w["records"]],
            "days_food_logged": w["days_logged"], "avg_calories": w["avg_calories"], "avg_protein": w["avg_protein"],
            "target_calories": w["target_calories"], "target_protein": w["target_protein"]} for w in rows]
    return {"weeks": out}, 200, f"read fitness summary ({raw} weeks)"
