"""Bot API v1 - Fitness (/api/bot/v1/fitness/...).

Always the token owner's own Fitness data - their workouts, food diary and weigh-ins - never anyone else's (not even
someone in the same circle). fitness:read reads (today, the weekly summary); fitness:write logs a workout, food or a
weigh-in. The owner needs Fitness access and API keys allowed (auth.bot_endpoint checks on every call, and logs it).

Programs: fitness:read lists them; fitness:write creates one (the same steps as the site's guided setup, in one
call) and pauses / resumes one. Exercise names are never guessed either: an unknown name creates nothing and the
answer lists close names from the owner's library.

Food is never guessed: a recipe or "my food" name must match exactly (any case), a USDA food comes by its fdc_id
(from foods/search) and a unit must be one the food has - otherwise nothing is logged and the answer lists
suggestions (close recipe / my food names, USDA foods, or the food's units).
"""
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from django.urls import reverse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

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
    if data.get("fdc_id") not in (None, ""):
        entry = _log_usda(user, data, on, slot)
    elif data.get("my_food") and data.get("unit") not in (None, ""):
        name = _str(data, "my_food", 100, required=True)
        mine = CustomFood.objects.filter(owner=user, name__iexact=name).first()
        if mine is None:
            raise ApiError(422, f"None of your foods is called “{name}” - nothing was logged.", "rejected: my food not matched",
                           extra={"suggestions": {"my_foods": list(CustomFood.objects.filter(owner=user, name__icontains=name.split()[0])
                                                                   .values_list("name", flat=True)[:5]) if name.split() else []}})
        from albright_fitness import amounts
        entry = _log_amount(FoodLogEntry(owner=user, date=on, slot=slot, source=FoodLogEntry.CUSTOM, custom_food=mine, label=mine.name),
                            amounts.for_custom(mine), data)
    elif data.get("recipe_id") not in (None, "") or data.get("recipe"):
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
            quantity=servings, unit="serving",
            portion_label=(mine.serving_label if servings == 1 else f"{count} × {mine.serving_label}")[:60],
            **nutrition.scaled({n: getattr(mine, n) for n in MACROS}, servings))
    elif any(data.get(n) not in (None, "") for n in ("calories", "protein", "carbs", "fat")):
        values = {n: _number(data, n, 0, 10000) for n in MACROS}
        entry = FoodLogEntry.objects.create(owner=user, date=on, slot=slot, source=FoodLogEntry.QUICK,
                                            label=_str(data, "label", 200) or "Quick add", **{
                                                n: v.quantize(Decimal("0.01")) if v is not None else None for n, v in values.items()})
    elif data.get("food") or data.get("name"):
        name = _str(data, "food" if data.get("food") else "name", 100)
        raise ApiError(422, "USDA foods aren't picked by name - find it with GET fitness/foods/search and send its "
                            "fdc_id, quantity and unit (or send its macros).",
                       "rejected: usda food not guessed", extra={"suggestions": {"usda": _usda_suggestions(name)}})
    else:
        raise ApiError(400, 'Send {"recipe_id" or "recipe", "servings"}, {"my_food", "servings"} or {"label", "calories", '
                            '"protein", "carbs", "fat"}.', "rejected: nothing to log")
    return _entry_json(entry), 201, f"logged food #{entry.pk} ({entry.source})"


def _entry_json(entry):
    return {"id": entry.pk, "date": entry.date.isoformat(), "slot": entry.slot, "label": entry.label, "source": entry.source,
            "servings": f"{entry.servings:f}" if entry.servings is not None else None,
            "quantity": f"{entry.quantity.normalize():f}" if entry.quantity is not None else None, "unit": entry.unit or None,
            "amount": entry.portion_label or None, "grams": f"{entry.grams:f}" if entry.grams is not None else None,
            "fdc_id": entry.food.fdc_id if entry.food_id else None, **_amounts(entry)}


def _units_json(basis):
    return [{"unit": u.key, "label": u.label} for u in basis.units]


def _log_amount(entry, basis, data):
    """quantity ("1/4", 0.5, "1 1/2") of unit (a key or label from the food's units) - never a guessed unit."""
    from albright_fitness import amounts
    raw = data.get("quantity", 1)
    if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
        raise ApiError(400, '"quantity" must be a number or a fraction like "1/4".', "rejected: bad quantity")
    quantity = amounts.parse_quantity(raw)
    if quantity is None:
        raise ApiError(400, '"quantity" must be above 0 and at most 5000: 2, 1.5, 0.3, "1/4" or "1 1/2".', "rejected: bad quantity")
    unit = data.get("unit")
    if not isinstance(unit, str) or basis.unit(unit) is None:
        raise ApiError(422, "Pick one of this food's units - nothing was logged.", "rejected: unit not matched",
                       extra={"units": _units_json(basis)})
    if not amounts.apply(entry, basis, quantity, unit):
        raise ApiError(400, "That's more than 5 kg - check the amount.", "rejected: too much")
    entry.save()
    return entry


def _log_usda(user, data, on, slot):
    """A USDA food the bot picked by its fdc_id (from foods/search) - an explicit pick, never a guess."""
    from albright_fitness import amounts, usda
    from albright_fitness.models import Food, FoodLogEntry
    fdc_id = data.get("fdc_id")
    if isinstance(fdc_id, bool) or not isinstance(fdc_id, int) or fdc_id <= 0:
        raise ApiError(400, '"fdc_id" must be a number (from GET fitness/foods/search).', "rejected: bad fdc_id")
    try:
        food = usda.food(fdc_id)
    except usda.UsdaError:
        food = Food.objects.filter(fdc_id=fdc_id).first()
    if food is None:
        raise ApiError(422, "Couldn't look up that food right now - nothing was logged. Use one from GET "
                            "fitness/foods/search, or send its macros.", "rejected: usda food unavailable")
    if data.get("unit") in (None, ""):
        raise ApiError(422, 'Send "quantity" and "unit" (one of this food\'s units) - nothing was logged.',
                       "rejected: no unit", extra={"units": _units_json(amounts.for_food(food))})
    return _log_amount(FoodLogEntry(owner=user, date=on, slot=slot, source=FoodLogEntry.USDA, food=food, label=str(food)[:200]),
                       amounts.for_food(food), data)


def _per_serving(basis):
    """The numbers for one of the food's first unit: {"serving", "calories", "protein_g", "carbs_g", "fat_g", "fiber_g"}."""
    if basis is None or not basis.units:
        return None
    unit = basis.unit(basis.default) or basis.units[0]
    got = basis.amount(1, unit.key)
    if got is None:
        return None
    t = got["totals"]
    names = {"calories": "calories", "protein": "protein_g", "carbs": "carbs_g", "fat": "fat_g", "fiber": "fiber_g"}
    return {"serving": unit.label, "grams": float(got["grams"]) if got["grams"] is not None else None,
            **{out: float(t[n]) if t.get(n) is not None else None for n, out in names.items()}}


def _recipe_serving(recipe_id):
    from albright_fitness import amounts
    from albright_meals.models import Recipe
    from albright_meals.recipe_nutrition import per_serving
    recipe = Recipe.objects.filter(pk=recipe_id).select_related("nutrition").first()
    values = per_serving(getattr(recipe, "nutrition", None)) if recipe else None
    return _per_serving(amounts.for_recipe(values)) if values else None


@bot_endpoint(("fitness:read", "fitness:food:read"))
def food_search(request, token):
    """The site's one search box: Recent, My foods, recipes, all foods (USDA when the site has a key) - with each
    one's numbers per serving, so an AI can use real numbers before estimating."""
    from albright_fitness import amounts, food_find_views
    from albright_fitness.models import CustomFood, Food
    q = (request.GET.get("q") or "").strip()
    if not q or len(q) > 100:
        raise ApiError(400, '"q" is required (up to 100 characters).', "rejected: bad q")
    groups, note = food_find_views.search(token.owner, q)
    cached = {f.fdc_id: f for f in Food.objects.filter(fdc_id__in=[i["id"] for g in groups for i in g["items"] if i["kind"] == "usda"])}
    mine = {f.pk: f for f in CustomFood.objects.filter(owner=token.owner)}
    out = []
    for g in groups:
        items = []
        for i in g["items"]:
            row = {"kind": i["kind"], "label": i["label"], "detail": i["detail"]}
            if i["kind"] == "usda":
                food = cached.get(i["id"])
                row.update(fdc_id=i["id"], units=_units_json(amounts.for_food(food)) if food else [{"unit": "g", "label": "g"}],
                           per_serving=_per_serving(amounts.for_food(food)) if food else None)
                row["log"] = {"fdc_id": i["id"], "quantity": 1, "unit": row["units"][0]["unit"]}
            elif i["kind"] == "custom" and i["id"] in mine:
                row.update(my_food=mine[i["id"]].name, units=_units_json(amounts.for_custom(mine[i["id"]])),
                           per_serving=_per_serving(amounts.for_custom(mine[i["id"]])))
                row["log"] = {"my_food": mine[i["id"]].name, "quantity": 1, "unit": "serving"}
            elif i["kind"] == "recipe":
                row.update(recipe_id=i["id"], log={"recipe_id": i["id"], "servings": 1},
                           per_serving=_recipe_serving(i["id"]))
            else:
                continue                                       # a quick add from before: nothing to log by id
            items.append(row)
        if items:
            out.append({"group": g["key"], "title": g["title"], "items": items})
    return {"q": q, "groups": out, "note": note or None}, 200, f"searched foods ({sum(len(g['items']) for g in out)} found)"


@bot_endpoint("fitness:read")
def food_entries(request, token):
    """One day's diary, entry by entry, with each one's amount."""
    from albright_fitness import nutrition
    from albright_fitness.models import FoodLogEntry
    on = _date(request.GET, days_back=365)
    rows = FoodLogEntry.objects.filter(owner=token.owner, date=on).select_related("food").order_by("pk")
    order = {key: i for i, (key, _label) in enumerate(FoodLogEntry.SLOTS)}
    entries = sorted(rows, key=lambda e: (order.get(e.slot, 9), e.pk))
    totals = nutrition.totals(entries)
    return ({"date": on.isoformat(), "entries": [_entry_json(e) for e in entries],
             "totals": {n: f"{v:.1f}" for n, v in totals.items() if n in ("calories", "protein", "carbs", "fat", "fiber")}},
            200, f"read food diary for {on}")


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


# ---------------------------------------------------------------------------
# Programs
# ---------------------------------------------------------------------------

MAX_PROGRAM_DAYS = 7
DAY_NAMES = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


def _program_json(p):
    from albright_fitness import programs as fx_programs
    status = "draft" if p.is_draft else ("active" if p.active else "paused")
    return {"id": p.pk, "name": p.name, "status": status, "archived": p.archived_at is not None,
            "start_date": p.start_date.isoformat() if p.start_date else None,
            "weeks": p.weeks, "planner": p.on_planner, "on_planner_now": bool(p.scheduled_from),
            "time": p.planner_time.strftime("%H:%M") if p.planner_time else None,
            "days": [{"id": d.pk, "name": d.name, "weekdays": d.weekdays or [], "start_url": fx_programs.start_url(d),
                      "exercises": [{"exercise": pe.exercise.name, "target": pe.target_text, "sets": pe.target_sets,
                                     "reps": pe.target_reps, "minutes": float(pe.target_seconds / 60) if pe.target_seconds else None,
                                     "miles": f"{pe.target_distance:f}" if pe.target_distance is not None else None}
                                    for pe in d.exercises.all()]} for d in p.days.all()],
            "url": reverse("albright_fitness:program", args=[p.pk])}


def _weekdays(value):
    if not isinstance(value, list) or not value:
        raise ApiError(400, '"weekdays" must be a list like [0, 3] (0 = Monday) or ["mon", "thu"].', "rejected: bad weekdays")
    out = set()
    for d in value:
        n = DAY_NAMES.get(d.lower()[:3]) if isinstance(d, str) else (d if isinstance(d, int) and not isinstance(d, bool) else None)
        if n is None or not 0 <= n <= 6:
            raise ApiError(400, '"weekdays" must be 0-6 (0 = Monday) or day names.', "rejected: bad weekdays")
        out.add(n)
    return sorted(out)


def _program_days(user, days):
    """[{"name", "weekdays", "exercises": [{"exercise", "sets", "reps", "minutes", "miles"}]}] -> checked rows, and the
    exercise names that matched nothing (nothing is created then)."""
    from albright_fitness.models import Exercise
    if not isinstance(days, list) or not 1 <= len(days) <= MAX_PROGRAM_DAYS:
        raise ApiError(400, f'"days" must be a list of 1-{MAX_PROGRAM_DAYS} workouts.', "rejected: bad days")
    rows, unknown = [], []
    for d in days:
        if not isinstance(d, dict):
            raise ApiError(400, 'Each day must be {"name", "weekdays", "exercises"}.', "rejected: bad day")
        exercises = d.get("exercises") or []
        if not isinstance(exercises, list) or len(exercises) > MAX_EXERCISES:
            raise ApiError(400, f'"exercises" must be a list of up to {MAX_EXERCISES}.', "rejected: bad exercises")
        row = {"name": _str(d, "name", 60, required=True), "weekdays": _weekdays(d.get("weekdays")), "exercises": []}
        for e in exercises:
            if not isinstance(e, dict):
                raise ApiError(400, 'Each exercise must be {"exercise": name, "sets", "reps"}.', "rejected: bad exercise")
            name = _str(e, "exercise", 100, required=True)
            found = Exercise.objects.visible_to(user).filter(name__iexact=name).first()
            if found is None:
                unknown.append({"exercise": name, "suggestions": _suggest_exercises(user, name)})
                continue
            row["exercises"].append({"exercise": found, "sets": _number(e, "sets", 1, 20), "reps": _number(e, "reps", 1, 100),
                                     "minutes": _number(e, "minutes", Decimal("0.1"), 1440), "miles": _number(e, "miles", Decimal("0.01"), 500)})
        rows.append(row)
    return rows, unknown


@bot_endpoint("fitness:read")
def _programs_get(request, token):
    from albright_fitness.models import Program
    found = Program.objects.filter(owner=token.owner).prefetch_related("days__exercises__exercise")
    return {"programs": [_program_json(p) for p in found]}, 200, f"read {len(found)} programs"


@bot_endpoint("fitness:write", methods=("POST",))
def _programs_post(request, token):
    from datetime import datetime

    from django.db import transaction

    from albright_fitness import programs as fx_programs
    from albright_fitness.models import Program, ProgramDay, ProgramExercise
    user, data = token.owner, json_body(request)
    name = _str(data, "name", 100, required=True)
    rows, unknown = _program_days(user, data.get("days"))
    if unknown:
        raise ApiError(422, "Some exercises aren't in the library - nothing was created. Use the exact names (see "
                            "suggestions), or create them on the site first.", "rejected: unknown exercises",
                       extra={"unknown": unknown})
    today = timezone.localdate()
    start = today
    if data.get("start_date") not in (None, ""):
        try:
            start = date.fromisoformat(data["start_date"]) if isinstance(data["start_date"], str) else None
        except ValueError:
            start = None
        if start is None or not today - timedelta(days=365) <= start <= today + timedelta(days=365):
            raise ApiError(400, '"start_date" must be YYYY-MM-DD within a year of today.', "rejected: bad start_date")
    weeks = _number(data, "weeks", 1, 104)
    planner = data.get("planner", False)
    if not isinstance(planner, bool):
        raise ApiError(400, '"planner" must be true or false.', "rejected: bad planner")
    at = None
    if data.get("time") not in (None, ""):
        try:
            at = datetime.strptime(data["time"], "%H:%M").time() if isinstance(data["time"], str) else None
        except ValueError:
            at = None
        if at is None:
            raise ApiError(400, '"time" must be HH:MM (24-hour), or leave it out for all day.', "rejected: bad time")
    with transaction.atomic():
        p = Program.objects.create(owner=user, name=name, active=False)
        for n, row in enumerate(rows):
            day = ProgramDay.objects.create(program=p, name=row["name"], weekdays=row["weekdays"], order=n)
            for i, e in enumerate(row["exercises"]):
                pe = fx_programs.add_exercise(day, e["exercise"])
                if any(e[k] is not None for k in ("sets", "reps", "minutes", "miles")):
                    fx_programs.set_target(pe, sets=e["sets"], reps=e["reps"], minutes=e["minutes"], miles=e["miles"])
                ProgramExercise.objects.filter(pk=pe.pk).update(order=i)
        placed = fx_programs.finish_setup(p, start, weeks=int(weeks) if weeks else None, on_planner=planner, planner_time=at)
    p = Program.objects.prefetch_related("days__exercises__exercise").get(pk=p.pk)
    return ({**_program_json(p), "planner_tasks": placed}, 201, f"created program #{p.pk} ({len(rows)} days)")


@csrf_exempt
def programs(request):
    """GET (fitness:read) lists the owner's programs; POST (fitness:write) creates one."""
    return _programs_post(request) if request.method == "POST" else _programs_get(request)


def _set_program(token, pk, active):
    from albright_fitness import programs as fx_programs
    from albright_fitness.models import Program
    p = Program.objects.filter(owner=token.owner, pk=pk).first()
    if p is None:
        raise ApiError(404, "No program with that id.", "rejected: program not found")
    if p.is_draft:
        raise ApiError(409, "That program isn't set up yet - finish it on the site first.", "rejected: draft program")
    fx_programs.set_active(p, active)
    p = Program.objects.prefetch_related("days__exercises__exercise").get(pk=p.pk)
    return _program_json(p), 200, f"{'resumed' if active else 'paused'} program #{p.pk}"


@bot_endpoint("fitness:write", methods=("POST",))
def program_pause(request, token, item_id):
    """Off Today and the Planner after today; the Planner choice is kept for resume."""
    return _set_program(token, item_id, False)


@bot_endpoint("fitness:write", methods=("POST",))
def program_resume(request, token, item_id):
    return _set_program(token, item_id, True)


def _flag(request, data, key, default):
    """A true/false option from the JSON body or the query string ("true"/"false"/"1"/"0")."""
    value = data.get(key, request.GET.get(key))
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.lower() in ("true", "1", "false", "0"):
        return value.lower() in ("true", "1")
    raise ApiError(400, f'"{key}" must be true or false.', f"rejected: bad {key}")


@bot_endpoint("fitness:write", methods=("DELETE",))
def program_delete(request, token, item_id):
    """Delete one of the owner's programs. Logged workouts stay (with the program's names saved on them);
    remove_scheduled (default true) ends its upcoming Planner tasks, false keeps them as ordinary tasks."""
    from albright_fitness import programs as fx_programs
    from albright_fitness.models import Program
    p = Program.objects.filter(owner=token.owner, pk=item_id).first()
    if p is None:
        raise ApiError(404, "No program with that id.", "rejected: program not found")
    remove = _flag(request, json_body(request), "remove_scheduled", True)
    pk, name = p.pk, p.name
    done = fx_programs.delete_program(p, remove_scheduled=remove)
    return ({"deleted": pk, "name": name, "workouts_kept": done["workouts"], "planner_ended": done["planner_ended"],
             "planner_kept": done["planner_kept"]}, 200,
            f"deleted program #{pk} ({done['workouts']} workouts kept, planner {'ended' if remove else 'kept'})")


# ---------------------------------------------------------------------------
# Food log for a person's AI (fitness:food:read / fitness:food:write)
#
# Unlike POST fitness/food (which never guesses), these take the AI's own estimate: name, amount and macros per item,
# saved as source "ai" with its confidence, notes and the person's words. Several items in one POST, all or nothing;
# the same idempotency_key within 24 hours returns those entries again. The person's day and "now" are in their own
# time zone (their Planner setting). Writes are limited per key (FOOD_WRITES_PER_HOUR) and every one is in the key's
# activity log with the entry ids.
# ---------------------------------------------------------------------------

FOOD_ITEMS_MAX = 20
FOOD_WRITES_PER_HOUR = 60
IDEMPOTENCY_HOURS = 24
# field -> (low, high, required): the numbers an item can carry
ITEM_NUMBERS = {
    "calories": (0, 5000, True), "protein_g": (0, 1000, True), "carbs_g": (0, 1000, True), "fat_g": (0, 1000, True),
    "fiber_g": (0, 300, False), "sugar_g": (0, 1000, False), "sodium_mg": (0, 50000, False),
    "quantity": (0, 5000, False), "grams": (0, 5000, False),
}
# API name -> FoodLogEntry field
ITEM_FIELDS = {"calories": "calories", "protein_g": "protein", "carbs_g": "carbs", "fat_g": "fat", "fiber_g": "fiber",
               "sugar_g": "sugar", "sodium_mg": "sodium_mg"}
ITEM_KEYS = set(ITEM_NUMBERS) | {"name", "unit", "confidence", "notes"}
MEALS = ("breakfast", "lunch", "dinner", "snack")


def _user_now(user):
    from albright_planner import planner
    return timezone.localtime(timezone=planner.user_zone(user))


def meal_for(local_dt):
    """The meal it probably is, by the person's local time: breakfast before 10:30, lunch before 15:00, dinner
    before 21:00, else a snack."""
    from datetime import time
    t = local_dt.time()
    if t < time(10, 30):
        return "breakfast"
    if t < time(15, 0):
        return "lunch"
    return "dinner" if t < time(21, 0) else "snack"


def _user_date(user, value, field="date", days_back=60):
    """YYYY-MM-DD, not after the person's today (in their zone) nor more than days_back before; default today."""
    today = _user_now(user).date()
    if value in (None, ""):
        return today
    try:
        day = date.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        day = None
    if day is None or day > today or day < today - timedelta(days=days_back):
        message = f"must be YYYY-MM-DD, your today ({today}) or up to {days_back} days back"
        raise ApiError(400, f'"{field}" {message}.', f"rejected: bad {field}",
                       extra={"errors": [{"field": field, "message": message}]})
    return day


def _num(value, low, high):
    """(Decimal, None) or (None, why)."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None, "must be a number"
    try:
        number = Decimal(str(value).strip())
    except InvalidOperation:
        return None, "must be a number"
    if not number.is_finite():
        return None, "must be a number"
    if number < low:
        return None, "can't be negative" if low == 0 else f"must be at least {low}"
    if number > high:
        return None, f"must be at most {high}"
    return number, None


def _text(value, limit):
    """(text, None) or (None, why)."""
    if value in (None, ""):
        return "", None
    if not isinstance(value, str):
        return None, "must be a string"
    value = value.strip()
    return (value, None) if len(value) <= limit else (None, f"must be at most {limit} characters")


def _clean_item(item, where, partial=False):
    """(fields for _apply_item, [{"field", "message"}]). partial: a PATCH - only what's sent, nothing required."""
    errors, out = [], {}
    if not isinstance(item, dict):
        return {}, [{"field": where or "item", "message": "must be an object"}]

    def err(name, message):
        errors.append({"field": f"{where}.{name}" if where else name, "message": message})

    if "name" in item or not partial:
        name, why = _text(item.get("name"), 200)
        if why or not name:
            err("name", why or "is required")
        else:
            out["label"] = " ".join(name.split())
    for key, (low, high, required) in ITEM_NUMBERS.items():
        if key not in item and partial:
            continue
        value = item.get(key)
        if value in (None, ""):
            if required:
                err(key, "is required" if not partial else "can't be empty")
            elif key in item:
                out[key] = None
            continue
        number, why = _num(value, low, high)
        if why:
            err(key, why)
        elif key == "quantity" and number == 0:
            err(key, "must be more than 0")
        else:
            out[key] = number
    if "unit" in item:
        unit, why = _text(item.get("unit"), 40)
        if why:
            err("unit", why)
        else:
            out["unit"] = unit
    if "confidence" in item:
        value = item.get("confidence")
        if value in (None, ""):
            out["confidence"] = None
        elif value not in ("high", "medium", "low"):
            err("confidence", 'must be "high", "medium" or "low"')
        else:
            out["confidence"] = value
    if "notes" in item:
        notes, why = _text(item.get("notes"), 1000)
        if why:
            err("notes", why)
        else:
            out["notes"] = notes
    return out, errors


def _apply_item(entry, fields):
    """Put cleaned item fields on an entry (the amount: quantity + free-text unit, shown as "1 bowl")."""
    for key, attr in ITEM_FIELDS.items():
        if key in fields:
            value = fields[key]
            setattr(entry, attr, value.quantize(Decimal("0.01")) if value is not None else None)
    for attr in ("label", "confidence", "notes"):
        if attr in fields:
            setattr(entry, attr, fields[attr])
    if "grams" in fields:
        entry.grams = fields["grams"].quantize(Decimal("0.1")) if fields["grams"] is not None else None
    if "quantity" in fields:
        entry.quantity = fields["quantity"].quantize(Decimal("0.001")) if fields["quantity"] is not None else None
    if "unit" in fields:
        entry.unit = fields["unit"] or ""
    if {"quantity", "unit"} & set(fields):
        quantity = f"{entry.quantity.normalize():f}" if entry.quantity is not None else ""
        entry.portion_label = " ".join(p for p in (quantity, entry.unit) if p)[:60]


def _macro_warning(label, calories, protein, carbs, fat):
    """A note when the calories differ from 4×protein + 4×carbs + 9×fat by more than 25% and more than 50 kcal."""
    if calories is None:
        return None
    expected = 4 * (protein or 0) + 4 * (carbs or 0) + 9 * (fat or 0)
    gap = abs(calories - expected)
    if gap > 50 and (expected == 0 or gap > expected * Decimal("0.25")):
        return (f"{label}: {calories:.0f} kcal doesn't match its macros (4×protein + 4×carbs + 9×fat ≈ {expected:.0f} "
                "kcal). Saved as sent - PATCH it if it's wrong.")
    return None


def _num_out(value, places=1):
    return float(round(value, places)) if value is not None else None


def _ai_entry_json(entry):
    return {"id": entry.pk, "date": entry.date.isoformat(), "meal": entry.slot, "name": entry.label,
            "quantity": _num_out(entry.quantity, 3), "unit": entry.unit or None, "grams": _num_out(entry.grams),
            "calories": _num_out(entry.calories), "protein_g": _num_out(entry.protein), "carbs_g": _num_out(entry.carbs),
            "fat_g": _num_out(entry.fat), "fiber_g": _num_out(entry.fiber), "sugar_g": _num_out(entry.sugar),
            "sodium_mg": _num_out(entry.sodium_mg, 0), "source": entry.source, "confidence": entry.confidence,
            "notes": entry.notes or None, "edited": entry.edited}


def _day_summary(user, on):
    """{"totals", "targets"} for the person's day - targets null when none are set."""
    from albright_fitness import nutrition
    from albright_fitness.models import FoodLogEntry, NutritionProfile
    totals = nutrition.totals(FoodLogEntry.objects.filter(owner=user, date=on))
    profile = NutritionProfile.objects.filter(user=user).first()
    targets = None
    if profile is not None:
        values = {n: profile.target(n) for n in ("calories", "protein", "carbs", "fat")}
        if any(values.values()):
            targets = {"calories": values["calories"], "protein_g": values["protein"], "carbs_g": values["carbs"],
                       "fat_g": values["fat"]}
    return {"totals": {"calories": _num_out(totals["calories"], 0), "protein_g": _num_out(totals["protein"]),
                       "carbs_g": _num_out(totals["carbs"]), "fat_g": _num_out(totals["fat"])},
            "targets": targets}


def _food_write_limit(token):
    """At most FOOD_WRITES_PER_HOUR food log changes an hour for one key (on top of the per-minute limit)."""
    from .models import BotRequestLog
    recent = BotRequestLog.objects.filter(
        token=token, created_at__gte=timezone.now() - timedelta(hours=1), method__in=("POST", "PATCH", "DELETE"),
        endpoint__startswith=reverse("bot_api_v1:fitness_food_entries_v2")).exclude(status=429).count()
    if recent >= FOOD_WRITES_PER_HOUR:
        raise ApiError(429, f"Rate limit: {FOOD_WRITES_PER_HOUR} food log changes per hour per key.",
                       "rate limited: food writes")


def _day_response(user, on, entries, warnings=None, replayed=False):
    order = {key: i for i, key in enumerate(MEALS)}
    entries = sorted(entries, key=lambda e: (order.get(e.slot, 9), e.pk))
    data = {"date": on.isoformat(), "entries": [_ai_entry_json(e) for e in entries], **_day_summary(user, on)}
    if warnings is not None:
        data["warnings"] = warnings
    if replayed:
        data["replayed"] = True
    return data


def _ids(entries):
    return ", ".join(f"#{e.pk}" for e in entries)


@bot_endpoint("fitness:food:write", methods=("POST",))
def _food_entries_post(request, token):
    from django.db import transaction

    from albright_fitness.models import FoodLogEntry
    user = token.owner
    _food_write_limit(token)
    data = json_body(request)
    key, why = _text(data.get("idempotency_key"), 100)
    if why:
        raise ApiError(400, f'"idempotency_key" {why}.', "rejected: bad idempotency_key",
                       extra={"errors": [{"field": "idempotency_key", "message": why}]})
    if key:
        earlier = list(FoodLogEntry.objects.filter(owner=user, idempotency_key=key,
                                                   created_at__gte=timezone.now() - timedelta(hours=IDEMPOTENCY_HOURS)))
        if earlier:
            return (_day_response(user, earlier[0].date, earlier, warnings=[], replayed=True), 200,
                    f"replayed food {_ids(earlier)} (same idempotency key)")
    errors, on = [], None
    try:
        on = _user_date(user, data.get("date"))
    except ApiError as exc:
        errors += exc.extra.get("errors", [])
    meal = data.get("meal")
    if meal in (None, ""):
        meal = meal_for(_user_now(user))
    elif meal not in MEALS:
        errors.append({"field": "meal", "message": 'must be "breakfast", "lunch", "dinner" or "snack"'})
    source_text, why = _text(data.get("source_text"), 4000)
    if why:
        errors.append({"field": "source_text", "message": why})
    items, cleaned = data.get("items"), []
    if not isinstance(items, list) or not items:
        errors.append({"field": "items", "message": f"send a list of 1 to {FOOD_ITEMS_MAX} items"})
    elif len(items) > FOOD_ITEMS_MAX:
        errors.append({"field": "items", "message": f"at most {FOOD_ITEMS_MAX} items in one call"})
    else:
        for i, item in enumerate(items):
            fields, item_errors = _clean_item(item, f"items[{i}]")
            cleaned.append(fields)
            errors += item_errors
    if errors:
        raise ApiError(400, "Nothing was logged - fix these and send it again.", f"rejected: {len(errors)} field errors",
                       extra={"errors": errors})
    warnings, created = [], []
    with transaction.atomic():                         # all the items, or none
        for fields in cleaned:
            entry = FoodLogEntry(owner=user, date=on, slot=meal, source=FoodLogEntry.AI, source_text=source_text,
                                 bot_key=token, idempotency_key=key)
            _apply_item(entry, fields)
            entry.save()
            created.append(entry)
            note = _macro_warning(entry.label, entry.calories, entry.protein, entry.carbs, entry.fat)
            if note:
                warnings.append(note)
    return (_day_response(user, on, created, warnings=warnings), 201,
            f"logged food {_ids(created)} (ai, {meal} {on})")


@bot_endpoint("fitness:food:read")
def _food_entries_get(request, token):
    from albright_fitness.models import FoodLogEntry
    on = _user_date(token.owner, request.GET.get("date"), days_back=365)
    entries = list(FoodLogEntry.objects.filter(owner=token.owner, date=on))
    return _day_response(token.owner, on, entries), 200, f"read food log for {on}"


@csrf_exempt
def food_entries_v2(request):
    """GET (fitness:food:read) a day's entries, totals and targets; POST (fitness:food:write) logs an AI's items."""
    return _food_entries_post(request) if request.method == "POST" else _food_entries_get(request)


def _own_entry(token, entry_id):
    from albright_fitness.models import FoodLogEntry
    entry = FoodLogEntry.objects.filter(pk=entry_id, owner=token.owner).first()
    if entry is None:
        raise ApiError(404, "No food entry with that id in your log.", "rejected: entry not found")
    return entry


@bot_endpoint("fitness:food:write", methods=("PATCH",))
def _food_entry_patch(request, token, entry_id):
    _food_write_limit(token)
    entry = _own_entry(token, entry_id)
    data = json_body(request)
    fields, errors = _clean_item(data, "", partial=True)
    if "meal" in data:
        if data["meal"] not in MEALS:
            errors.append({"field": "meal", "message": 'must be "breakfast", "lunch", "dinner" or "snack"'})
        else:
            entry.slot = data["meal"]
    if "date" in data:
        try:
            entry.date = _user_date(token.owner, data["date"])
        except ApiError as exc:
            errors += exc.extra.get("errors", [])
    errors += [{"field": k, "message": "isn't a field you can change"} for k in sorted(set(data) - ITEM_KEYS - {"meal", "date"})]
    if errors:
        raise ApiError(400, "Nothing was changed - fix these and send it again.", f"rejected: {len(errors)} field errors",
                       extra={"errors": errors})
    _apply_item(entry, fields)
    entry.save()
    warning = _macro_warning(entry.label, entry.calories, entry.protein, entry.carbs, entry.fat)
    return ({"entry": _ai_entry_json(entry), **_day_summary(token.owner, entry.date), "warnings": [warning] if warning else []},
            200, f"changed food #{entry.pk}")


@bot_endpoint("fitness:food:write", methods=("DELETE",))
def _food_entry_delete(request, token, entry_id):
    _food_write_limit(token)
    entry = _own_entry(token, entry_id)
    on, pk = entry.date, entry.pk
    entry.delete()
    return {"deleted": pk, "date": on.isoformat(), **_day_summary(token.owner, on)}, 200, f"removed food #{pk}"


@csrf_exempt
def food_entry(request, entry_id):
    """PATCH fixes an entry, DELETE removes it (fitness:food:write; only the key owner's own entries)."""
    if request.method == "DELETE":
        return _food_entry_delete(request, entry_id=entry_id)
    return _food_entry_patch(request, entry_id=entry_id)


@bot_endpoint("fitness:food:read")
def food_recent(request, token):
    """The person's most-logged foods (last 60 days) with their numbers - send one back as an item to repeat it."""
    from albright_fitness import nutrition
    raw = request.GET.get("limit") or "20"
    limit = int(raw) if raw.isdigit() else 0
    if not 1 <= limit <= 50:
        raise ApiError(400, '"limit" must be 1 to 50.', "rejected: bad limit")
    out = []
    for e, times in nutrition.recent_foods(token.owner, limit=limit):
        item = {"name": e.label, "quantity": _num_out(e.quantity, 3), "unit": e.unit or None, "grams": _num_out(e.grams),
                "calories": _num_out(e.calories), "protein_g": _num_out(e.protein), "carbs_g": _num_out(e.carbs),
                "fat_g": _num_out(e.fat), "fiber_g": _num_out(e.fiber)}
        out.append({"times_logged": times, "last_logged": e.date.isoformat(), "meal": e.slot, "source": e.source,
                    "item": {k: v for k, v in item.items() if v is not None}})
    return {"foods": out}, 200, f"read recent foods ({len(out)})"


def openapi(request):
    """The machine-readable spec for the food log endpoints - no key needed to read it (it holds no data)."""
    from django.http import JsonResponse

    from . import food_ai
    return JsonResponse(food_ai.openapi_spec(food_ai.api_base(request)), json_dumps_params={"indent": 2})
