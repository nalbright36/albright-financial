"""Fitness food pages (/fitness/food/, /fitness/targets/): the diary, adding food (USDA search, My foods, Recent,
Quick add, a Meals recipe, today's meal plan), copying yesterday's meal, and macro targets.

Private: a diary, its targets, body info and My foods are their owner's alone - even within a circle; anything
else is a 404 (every lookup is through owner=request.user). Recipes and plan entries are only ever ones the user can
read in Meals (albright_meals.scope)."""
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST

from . import burn, nutrition, usda
from .models import MACROS, CustomFood, FoodLogEntry, NutritionProfile, WeightEntry

SLOT_KEYS = dict(FoodLogEntry.SLOTS)
TABS = [("search", "Search"), ("recent", "Recent"), ("mine", "My foods"), ("quick", "Quick add"), ("recipes", "Recipes")]
MAX_SERVINGS = Decimal(50)
MAX_GRAMS = Decimal(5000)
PICK_FIELD = {"custom": "food", "recent": "entry", "recipe": "recipe"}
PLAN_SLOT = {"breakfast": "breakfast", "lunch": "lunch", "dinner": "dinner", "snack": "snack"}


def _day(text, user):
    """A YYYY-MM-DD from a form, else the person's today (their own time zone - nutrition.today)."""
    try:
        return date.fromisoformat(text or "")
    except ValueError:
        return nutrition.today(user)


def _slot(text):
    return text if text in SLOT_KEYS else FoodLogEntry.SNACK


def _number(text, low=Decimal(0), high=None):
    """A positive amount from a form ("1.5") or None."""
    try:
        value = Decimal(str(text).strip())
    except (InvalidOperation, ValueError, TypeError):
        return None
    if not value.is_finite() or value <= low or (high is not None and value > high):
        return None
    return value


def _macro(text):
    """An optional macro from a form: blank -> None, else 0..10,000."""
    if text in (None, ""):
        return None
    try:
        value = Decimal(str(text).strip())
    except (InvalidOperation, ValueError):
        return None
    return value.quantize(Decimal("0.01")) if value.is_finite() and 0 <= value <= 10000 else None


def _diary_url(on, slot=None, user=None):
    url = reverse("albright_fitness:food_day", args=[on.isoformat()]) if on != nutrition.today(user) else reverse("albright_fitness:food")
    return url + (f"#slot-{slot}" if slot else "")


def _back(request, on, slot=None):
    nxt = request.POST.get("next") or ""
    if nxt and url_has_allowed_host_and_scheme(nxt, {request.get_host()}, require_https=request.is_secure()):
        return redirect(nxt)
    return redirect(_diary_url(on, slot, request.user))


def _meal_spaces(user):
    """Every Meals space this person sees (albright_labs.circles)."""
    from albright_meals import scope
    return scope.space_ids(user)


def _readable_recipes(user):
    """Recipes the user can read in Meals that have nutrition - in their spaces, shared with them, the library."""
    from albright_meals import scope
    if not _meal_spaces(user):
        return None
    return scope.readable(user).filter(nutrition__isnull=False).select_related("nutrition")


def _plan_today(user, on):
    """The planned meals that day, in every Meals space I see, with known nutrition - and whether I've logged each."""
    from albright_meals.models import MealPlanEntry
    from albright_meals.recipe_nutrition import per_serving
    spaces = _meal_spaces(user)
    if not spaces:
        return []
    entries = (MealPlanEntry.objects.filter(space_id__in=spaces, date=on, recipe__isnull=False).exclude(status="skipped")
               .select_related("recipe", "recipe__nutrition"))
    logged = set(FoodLogEntry.objects.filter(owner=user, date=on, plan_entry__in=entries).values_list("plan_entry_id", flat=True))
    order = {key: i for i, (key, _label) in enumerate(FoodLogEntry.SLOTS)}
    rows = []
    for entry in sorted(entries, key=lambda e: order.get(e.slot, 9)):
        amounts = per_serving(getattr(entry.recipe, "nutrition", None))
        if amounts:
            rows.append({"entry": entry, "amounts": amounts, "logged": entry.pk in logged})
    return rows


# ---------------------------------------------------------------------------
# The diary
# ---------------------------------------------------------------------------

@login_required
@never_cache
def diary(request, day=None):
    today = nutrition.today(request.user)
    on = _day(day, request.user) if day else today
    if day and on.isoformat() != day:
        raise Http404
    profile = NutritionProfile.load(request.user)
    data = nutrition.day(request.user, on)
    exercise = burn.day(request.user, on)
    from .food_find_views import pop_undo, slot_now
    return render(request, "albright_fitness/food_diary.html", {
        "undo": pop_undo(request), "slot_now": slot_now(user=request.user),
        **data, "on": on, "today": today, "prev": on - timedelta(days=1), "next": on + timedelta(days=1),
        "profile": profile, "progress": nutrition.progress(profile, data["totals"], exercise["total"]),
        "plan": _plan_today(request.user, on), "exercise": exercise,
        "allowance": nutrition.allowance(profile, data["totals"], exercise["total"]),
        "has_calorie_target": bool(profile.target_calories), "missing": nutrition.tracked_missing(profile),
        "show_banner": not profile.target_calories and not profile.hide_targets_banner,
        "carb_extra": nutrition.exercise_extra(profile, exercise["total"])["carbs"], "tracked": profile.tracked or []})


@login_required
@require_POST
def hide_targets_banner(request):
    """"Don't show me again" on the diary's Set-targets banner - the header keeps a quiet "Set targets" link."""
    profile = NutritionProfile.load(request.user)
    profile.hide_targets_banner = True
    profile.save(update_fields=["hide_targets_banner", "updated_at"])
    return _back(request, nutrition.today(request.user))


@login_required
@require_POST
def workout_calories(request, pk):
    """A workout's calories burned, typed (a watch's number) - blank or "Use estimate" goes back to the estimate."""
    from .models import Workout
    workout = get_object_or_404(Workout, pk=pk, owner=request.user)
    text = (request.POST.get("calories") or "").strip()
    if request.POST.get("reset") or not text:
        workout.calories_burned = None
    elif text.isdigit() and 0 <= int(text) <= 5000:
        workout.calories_burned = int(text)
    else:
        messages.error(request, "Enter calories burned as a whole number from 0 to 5,000.")
        return _back(request, workout.date)
    workout.save(update_fields=["calories_burned"])
    messages.success(request, f"Updated calories burned for {workout.title}.")
    return _back(request, workout.date)


@login_required
@require_POST
def delete_entry(request, pk):
    entry = get_object_or_404(FoodLogEntry, pk=pk, owner=request.user)
    entry.delete()
    messages.success(request, f"Removed {entry.label}.")
    return _back(request, entry.date, entry.slot)


@login_required
@require_POST
def copy_yesterday(request):
    """Copies the day before's entries in one meal into this day's same meal."""
    on, slot = _day(request.POST.get("date"), request.user), _slot(request.POST.get("slot"))
    source = FoodLogEntry.objects.filter(owner=request.user, date=on - timedelta(days=1), slot=slot).order_by("pk")
    copies = []
    for e in source:
        e.pk, e.id, e.date, e.created_at = None, None, on, None
        e.plan_entry, e.idempotency_key = None, ""          # a copy is never the AI's original (fitness_api replay)
        copies.append(e)
    FoodLogEntry.objects.bulk_create(copies)
    if copies:
        messages.success(request, f"Copied {len(copies)} item{'s' if len(copies) != 1 else ''} from yesterday's {SLOT_KEYS[slot].lower()}.")
    else:
        messages.info(request, f"Nothing was logged for {SLOT_KEYS[slot].lower()} the day before.")
    return redirect(_diary_url(on, slot, request.user))


# ---------------------------------------------------------------------------
# Adding food
# ---------------------------------------------------------------------------

@login_required
@never_cache
def add(request):
    on, slot = _day(request.GET.get("date"), request.user), _slot(request.GET.get("slot"))
    tab = request.GET.get("tab") if request.GET.get("tab") in dict(TABS) else "search"
    q = usda.clean_query(request.GET.get("q", ""))
    context = {"on": on, "slot": slot, "slots": FoodLogEntry.SLOTS, "tab": tab, "tabs": TABS, "q": q,
               "configured": usda.configured(), "not_set": usda.NOT_SET}
    if tab == "search" and q:
        try:
            context["results"] = usda.search(q)
        except usda.UsdaError as exc:
            context["search_error"] = exc.message
    elif tab == "recent":
        context["recent"] = nutrition.recent_foods(request.user)
    elif tab == "mine":
        context["my_foods"] = CustomFood.objects.filter(owner=request.user)
    elif tab == "recipes":
        recipes = _readable_recipes(request.user)
        context["recipes"] = recipes.order_by("title")[:200] if recipes is not None else None
    return render(request, "albright_fitness/food_add.html", context)


@login_required
@never_cache
def usda_food(request, fdc_id):
    """One USDA food: pick an amount (grams, or one of its household portions) and log it."""
    on, slot = _day(request.GET.get("date") or request.POST.get("date"), request.user), _slot(request.GET.get("slot") or request.POST.get("slot"))
    try:
        food = usda.food(fdc_id)
    except usda.UsdaError as exc:
        messages.error(request, exc.message)
        return redirect(f"{reverse('albright_fitness:food_add')}?date={on}&slot={slot}")
    if request.method == "POST":
        portion = request.POST.get("portion", "g")
        amount = _number(request.POST.get("amount"), high=MAX_GRAMS)
        grams, label, unit = None, "", "g"
        if amount is not None and portion == "g":
            grams = nutrition.dec(amount)
        elif amount is not None and portion.isdigit() and int(portion) < len(food.portions):
            p = food.portions[int(portion)]
            grams = nutrition.dec(amount * Decimal(str(p["grams"])))
            count = f"{amount.normalize():f}"
            label = p["label"] if amount == 1 else f"{count} × {p['label']}"
            unit = f"p:{p['label']}"[:64]
        if not grams or grams > MAX_GRAMS:
            messages.error(request, "Enter an amount - grams, or how many of a portion.")
        else:
            FoodLogEntry.objects.create(owner=request.user, date=on, slot=slot, source=FoodLogEntry.USDA, food=food,
                                        label=str(food)[:200], grams=grams, portion_label=label[:60],
                                        quantity=amount, unit=unit, **nutrition.for_grams(food, grams))
            messages.success(request, f"Logged {food.name} to {SLOT_KEYS[slot].lower()}.")
            return redirect(_diary_url(on, slot, request.user))
    return render(request, "albright_fitness/food_usda.html", {
        "food": food, "on": on, "slot": slot, "slots": FoodLogEntry.SLOTS,
        "per100": [(nutrition.LABELS[n], getattr(food, n), nutrition.UNITS_OF[n]) for n in MACROS]})


def _log_recipe(user, recipe, servings, on, slot, plan_entry=None):
    from albright_meals.recipe_nutrition import per_serving
    amounts = per_serving(getattr(recipe, "nutrition", None))
    if not amounts:
        return None
    count = f"{servings.normalize():f}"
    return FoodLogEntry.objects.create(owner=user, date=on, slot=slot, source=FoodLogEntry.RECIPE, recipe=recipe,
                                       plan_entry=plan_entry, label=recipe.title[:200], servings=servings,
                                       quantity=servings, unit="serving",
                                       portion_label=f"{count} serving{'s' if servings != 1 else ''}",
                                       **nutrition.scaled(amounts, servings))


@login_required
@require_POST
def log(request):
    """One tap logging: kind = recent (copy an earlier entry), custom (one of My foods x servings), quick (just the
    numbers), recipe (a Meals recipe x servings), plan (a planned meal x servings). The Today page's Quick log posts
    one form with a button per food instead: pick = "custom:<id>", "recent:<id>" or "recipe:<id>"."""
    post = request.POST.copy()
    pick_kind, _sep, pick_id = post.get("pick", "").partition(":")
    if pick_kind in PICK_FIELD and pick_id.isdigit():
        post["kind"], post[PICK_FIELD[pick_kind]] = pick_kind, pick_id
    request.POST = post
    user, kind = request.user, request.POST.get("kind")
    on, slot = _day(request.POST.get("date"), request.user), _slot(request.POST.get("slot"))
    servings = _number(request.POST.get("servings") or "1", high=MAX_SERVINGS)
    entry = None
    if kind == "recent":
        earlier = get_object_or_404(FoodLogEntry, pk=request.POST.get("entry") or 0, owner=user)
        earlier.pk, earlier.id, earlier.date, earlier.slot, earlier.created_at, earlier.plan_entry = None, None, on, slot, None, None
        earlier.idempotency_key = ""
        earlier.save()
        entry = earlier
    elif kind == "custom":
        mine = get_object_or_404(CustomFood, pk=request.POST.get("food") or 0, owner=user)
        if servings:
            count = f"{servings.normalize():f}"
            entry = FoodLogEntry.objects.create(
                owner=user, date=on, slot=slot, source=FoodLogEntry.CUSTOM, custom_food=mine, label=mine.name,
                servings=servings, quantity=servings, unit="serving", portion_label=(mine.serving_label if servings == 1 else f"{count} × {mine.serving_label}")[:60],
                grams=nutrition.dec(mine.serving_grams * servings) if mine.serving_grams else None,
                **nutrition.scaled({n: getattr(mine, n) for n in MACROS}, servings))
    elif kind == "quick":
        values = {n: _macro(request.POST.get(n)) for n in MACROS}
        label = " ".join(request.POST.get("label", "").split())[:200] or "Quick add"
        if any(v is not None for v in values.values()):
            entry = FoodLogEntry.objects.create(owner=user, date=on, slot=slot, source=FoodLogEntry.QUICK, label=label, **values)
    elif kind in ("recipe", "plan"):
        from albright_meals import scope
        from albright_meals.models import MealPlanEntry
        spaces = _meal_spaces(user)
        if not spaces:
            raise Http404
        plan_entry = None
        if kind == "plan":
            plan_entry = get_object_or_404(MealPlanEntry.objects.select_related("recipe"), pk=request.POST.get("entry") or 0,
                                           space_id__in=spaces, recipe__isnull=False)
            recipe = plan_entry.recipe
            if not request.POST.get("slot"):
                slot = PLAN_SLOT.get(plan_entry.slot, FoodLogEntry.SNACK)
        else:
            recipe, _editable = scope.readable_recipe(user, None, request.POST.get("recipe") or 0)
        if servings:
            entry = _log_recipe(user, recipe, servings, on, slot, plan_entry)
            if entry is None:
                messages.error(request, f"{recipe.title} doesn't have nutrition yet - calculate it on the recipe page first.")
                return _back(request, on, slot)
    else:
        raise Http404
    if entry is None:
        messages.error(request, "Enter an amount (servings above 0)." if kind != "quick" else "Enter calories or at least one macro.")
    else:
        messages.success(request, f"Logged {entry.label} to {SLOT_KEYS[entry.slot].lower()}.")
    return _back(request, on, slot)


# ---------------------------------------------------------------------------
# My foods
# ---------------------------------------------------------------------------

@login_required
def my_foods(request):
    if request.method == "POST":
        name = " ".join(request.POST.get("name", "").split())[:100]
        values = {n: _macro(request.POST.get(n)) for n in MACROS}
        if not name or values["calories"] is None:
            messages.error(request, "Give it a name and its calories per serving.")
        else:
            CustomFood.objects.create(owner=request.user, name=name,
                                      serving_label=" ".join(request.POST.get("serving_label", "").split())[:40] or "1 serving",
                                      serving_grams=_number(request.POST.get("serving_grams") or "", high=MAX_GRAMS),
                                      serving_ml=_number(request.POST.get("serving_ml") or "", high=MAX_GRAMS), **values)
            messages.success(request, f"Added {name} to My foods.")
            return redirect("albright_fitness:my_foods")
    return render(request, "albright_fitness/my_foods.html", {"foods": CustomFood.objects.filter(owner=request.user)})


@login_required
@require_POST
def delete_my_food(request, pk):
    food = get_object_or_404(CustomFood, pk=pk, owner=request.user)
    food.delete()
    messages.success(request, f"Removed {food.name} from My foods (your diary keeps what you logged).")
    return redirect("albright_fitness:my_foods")


# ---------------------------------------------------------------------------
# Targets
# ---------------------------------------------------------------------------

def _int_or_none(text, high):
    text = (text or "").strip()
    return int(text) if text.isdigit() and 0 < int(text) <= high else None


TARGET_LIMITS = (("calories", 10000), ("protein", 1000), ("carbs", 2000), ("fat", 1000), ("fiber", 300))


def _target_errors(post):
    """Typed targets that can't be saved (not a whole number, or out of range) - shown, never silently cleared."""
    errors = []
    for n, high in TARGET_LIMITS:
        text = (post.get(n) or "").strip().replace(",", "")
        if text and _int_or_none(text, high) is None:
            errors.append(f"{nutrition.LABELS[n]}: enter a whole number from 1 to {high:,} {nutrition.UNITS_OF[n]} (or leave it empty).")
    return errors


@login_required
@never_cache
def targets(request):
    profile = NutritionProfile.load(request.user)
    suggestion = None
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "units":
            profile.weight_unit = request.POST.get("weight_unit") if request.POST.get("weight_unit") in ("lb", "kg") else profile.weight_unit
            profile.height_unit = request.POST.get("height_unit") if request.POST.get("height_unit") in ("in", "cm") else profile.height_unit
            profile.save()
            return redirect("albright_fitness:targets")
        if action in ("calculate", "use"):
            errors = _body_from_post(profile, request.POST)
            if errors:
                for e in errors:
                    messages.error(request, e)
            else:
                profile.save()
                suggestion = nutrition.calculate(profile)
                if action == "use" and suggestion:
                    for n in MACROS:
                        setattr(profile, f"target_{n}", suggestion[n])
                    profile.save()
                    messages.success(request, "Saved your targets.")
                    return redirect("albright_fitness:targets")
        elif action == "targets":
            errors = _target_errors(request.POST)
            if errors:
                for e in errors:
                    messages.error(request, e)
            else:
                for n, high in TARGET_LIMITS:
                    setattr(profile, f"target_{n}", _int_or_none((request.POST.get(n) or "").replace(",", ""), high))
                profile.tracked = [n for n in MACROS if request.POST.get(f"track_{n}")] or ["protein"]
                if request.POST.get("exercise_shown"):                 # the checkbox was on the form
                    profile.add_exercise = bool(request.POST.get("add_exercise"))
                profile.save()
                messages.success(request, "Saved your targets.")
                return redirect("albright_fitness:targets")
    latest = WeightEntry.objects.filter(owner=request.user).order_by("-date").first()
    weigh_in = None                         # a newer weigh-in than the calculator's weight (2 lb / 1 kg or more apart)
    if latest is not None and (profile.weight_kg is None or abs(Decimal(latest.weight_kg) - profile.weight_kg) >= 1):
        weigh_in = {"date": latest.date, "shown": nutrition.weight_shown(NutritionProfile(weight_kg=Decimal(latest.weight_kg),
                                                                                         weight_unit=profile.weight_unit))}
    form_errors = request.method == "POST" and request.POST.get("action") == "targets"
    return render(request, "albright_fitness/targets.html", {
        "profile": profile, "suggestion": suggestion, "note": nutrition.low_target_note(profile),
        "macro_note": nutrition.macro_note(profile), "weigh_in": weigh_in,
        "rerun_nudge": profile.add_exercise and profile.activity in ("moderate", "active", "very") and bool(profile.target_calories),
        "weight": weigh_in["shown"] if weigh_in else nutrition.weight_shown(profile), "height": nutrition.height_shown(profile),
        "macros": [(n, nutrition.LABELS[n], nutrition.UNITS_OF[n], request.POST.get(n) if form_errors else profile.target(n),
                    bool(request.POST.get(f"track_{n}")) if form_errors else n in (profile.tracked or []), high)
                   for n, high in TARGET_LIMITS],
        "activity": NutritionProfile.ACTIVITY, "goals": NutritionProfile.GOALS, "sexes": NutritionProfile.SEXES})


def _body_from_post(profile, post):
    """The calculator's inputs into the profile (not saved) -> a list of problems."""
    errors = []
    profile.sex = post.get("sex") if post.get("sex") in dict(NutritionProfile.SEXES) else ""
    profile.age = _int_or_none(post.get("age"), 110)
    if profile.age is not None and profile.age < 13:
        profile.age = None
    weight = _number(post.get("weight") or "", high=Decimal(800))
    height = _number(post.get("height") or "", high=Decimal(300))
    profile.weight_kg = nutrition.weight_to_kg(weight, profile.weight_unit) if weight else None
    profile.height_cm = nutrition.height_to_cm(height, profile.height_unit) if height else None
    if post.get("activity") in dict(NutritionProfile.ACTIVITY):
        profile.activity = post.get("activity")
    if post.get("goal") in dict(NutritionProfile.GOALS):
        profile.goal = post.get("goal")
    per_lb = _number(post.get("protein_per_lb") or "", high=Decimal("1.0"))
    if per_lb is not None and per_lb >= Decimal("0.7"):
        profile.protein_per_lb = per_lb.quantize(Decimal("0.01"))
    if not (profile.sex and profile.age and profile.weight_kg and profile.height_cm):
        errors.append("Fill in sex, age (13 or over), height and weight to use the calculator.")
    elif not (30 <= profile.weight_kg <= 350 and 90 <= profile.height_cm <= 250):
        errors.append("Check your height and weight - they look out of range.")
    return errors
