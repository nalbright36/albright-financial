"""Adding food the easy way (/fitness/food/find/ ...): ONE search box over everything, ONE amount panel.

  find       "What did you eat?" - Recent first, then My foods and Meals recipes, then all foods (USDA - left out,
             with a plain note, when the site has no key). Live as you type (fitness.js asks for ?format=json);
             without JS the form reloads with the same list.
  pick       the amount panel, MyFitnessPal-style: any quantity (1, 1.5, .3, 1/4, 1 1/2) of any unit the food
             has (amounts.py), the meal, and the calories / macros as you type. Opens in the bottom sheet.
  new        "Not here? Enter it yourself": name, what one serving is (and weighs), the numbers - saved to My foods
             and logged in one go (or just logged).
  edit       tap a logged entry: change its amount, unit or meal - or remove it.
  undo       right after adding, the diary offers Undo (the entry ids in the session, mine only).

Private: every lookup is through owner=request.user; recipes are only ones the user can read in Meals."""
import json
from datetime import time
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST

from . import amounts, nutrition, usda
from .food_views import SLOT_KEYS, _day, _diary_url, _macro, _number, _readable_recipes, _slot
from .models import MACROS, CustomFood, Food, FoodLogEntry

KINDS = ("usda", "custom", "recipe", "entry")
UNDO_KEY = "fx_food_undo"
LIMIT = 8


def slot_now(now=None, user=None):
    """The meal it probably is (by the person's own clock): breakfast until 10:30, lunch until 2:30, dinner 5-9, else
    a snack."""
    if now is None:
        from albright_planner import planner
        now = timezone.localtime(timezone=planner.user_zone(user)) if user is not None else timezone.localtime()
    t = now.time()
    if t < time(10, 30):
        return FoodLogEntry.BREAKFAST
    if t < time(14, 30):
        return FoodLogEntry.LUNCH
    if time(17, 0) <= t < time(21, 0):
        return FoodLogEntry.DINNER
    return FoodLogEntry.SNACK


def _kcal(value):
    return f"{value:.0f} kcal" if value is not None else ""


def _item(kind, pk, label, detail):
    return {"kind": kind, "id": pk, "label": label, "detail": detail,
            "url": reverse("albright_fitness:food_pick", args=[kind, pk])}


def _recent_item(entry):
    if entry.source == FoodLogEntry.USDA and entry.food_id:
        kind, pk = "usda", entry.food.fdc_id
    elif entry.source == FoodLogEntry.CUSTOM and entry.custom_food_id:
        kind, pk = "custom", entry.custom_food_id
    elif entry.source == FoodLogEntry.RECIPE and entry.recipe_id:
        kind, pk = "recipe", entry.recipe_id
    else:
        kind, pk = "entry", entry.pk
    detail = " · ".join(x for x in (entry.portion_label, _kcal(entry.calories)) if x)
    return _item(kind, pk, entry.label, detail)


def search(user, q):
    """[{"key", "title", "items"}] - Recent, My foods, Recipes, All foods (USDA) - and a note when USDA can't be searched."""
    q = usda.clean_query(q)
    words = q.lower().split()
    match = lambda text: all(w in (text or "").lower() for w in words)  # noqa: E731
    recent = [_recent_item(e) for e, _times in nutrition.recent_foods(user, limit=30) if match(e.label)][:LIMIT]
    seen = {(i["kind"], i["id"]) for i in recent}
    mine = [_item("custom", f.pk, f.name, " · ".join(x for x in (f.serving_label, _kcal(f.calories)) if x))
            for f in CustomFood.objects.filter(owner=user) if match(f.name) and ("custom", f.pk) not in seen][:LIMIT if q else 0]
    recipes = []
    readable = _readable_recipes(user) if q else None
    if readable is not None:
        for r in readable.order_by("title"):
            if match(r.title) and ("recipe", r.pk) not in seen:
                recipes.append(_item("recipe", r.pk, r.title, f"1 serving · {_kcal(r.nutrition.serving_calories)}".rstrip(" ·")))
            if len(recipes) >= LIMIT:
                break
    found, note = [], ""
    if q:
        if not usda.configured():
            note = "All-foods search isn't set up on this site yet - search Recent and My foods, or enter it yourself."
        else:
            try:
                found = [_item("usda", f.fdc_id, str(f), "per 100 g · " + _kcal(f.calories) if f.calories is not None else "")
                         for f in usda.search(q)[:12] if ("usda", f.fdc_id) not in seen]
            except usda.UsdaError as exc:
                note = exc.message
    groups = [("recent", "Recent", recent), ("mine", "My foods", mine), ("recipes", "Recipes", recipes), ("usda", "All foods", found)]
    return [{"key": k, "title": t, "items": items} for k, t, items in groups if items], note


def _context(request):
    on = _day(request.GET.get("date") or request.POST.get("date"), request.user)
    slot = request.GET.get("slot") or request.POST.get("slot")
    return on, (slot if slot in SLOT_KEYS else slot_now(user=request.user))


@login_required
@never_cache
def find(request):
    on, slot = _context(request)
    q = usda.clean_query(request.GET.get("q", ""))
    groups, note = search(request.user, q)
    if request.GET.get("format") == "json":
        return JsonResponse({"q": q, "groups": groups, "note": note})
    return render(request, "albright_fitness/food_find.html", {
        "on": on, "slot": slot, "slots": FoodLogEntry.SLOTS, "q": q, "groups": groups, "note": note})


def _basis(user, kind, pk):
    """(basis, label, fields for the new entry, the last amount I used for it or None)."""
    last = FoodLogEntry.objects.filter(owner=user).order_by("-pk")
    if kind == "usda":
        try:
            food = usda.food(pk)
        except usda.UsdaError:
            food = Food.objects.filter(fdc_id=pk).first()
        if food is None:
            raise Http404
        return amounts.for_food(food), str(food)[:200], {"source": FoodLogEntry.USDA, "food": food}, last.filter(food=food).first()
    if kind == "custom":
        mine = get_object_or_404(CustomFood, pk=pk, owner=user)
        return amounts.for_custom(mine), mine.name, {"source": FoodLogEntry.CUSTOM, "custom_food": mine}, last.filter(custom_food=mine).first()
    if kind == "recipe":
        from albright_meals.recipe_nutrition import per_serving
        readable = _readable_recipes(user)
        recipe = get_object_or_404(readable, pk=pk) if readable is not None else None
        amounts_ = per_serving(getattr(recipe, "nutrition", None)) if recipe else None
        if not amounts_:
            raise Http404
        return amounts.for_recipe(amounts_), recipe.title[:200], {"source": FoodLogEntry.RECIPE, "recipe": recipe}, last.filter(recipe=recipe).first()
    if kind == "entry":
        earlier = get_object_or_404(FoodLogEntry, pk=pk, owner=user)
        fields = {"source": earlier.source, "food": earlier.food, "custom_food": earlier.custom_food, "recipe": earlier.recipe,
                  "grams": earlier.grams, "portion_label": earlier.portion_label}
        return amounts.for_entry(earlier), earlier.label, fields, None
    raise Http404


def _panel(request, template, context):
    worked = context["basis"].amount(amounts.parse_quantity(context["quantity"]), context["unit"])
    context["preview"] = [(n, nutrition.LABELS[n], nutrition.UNITS_OF[n], worked["totals"][n] if worked else None)
                          for n in ("calories", "protein", "carbs", "fat")]
    page = "albright_fitness/_food_amount.html" if request.headers.get("X-Requested-With") == "XMLHttpRequest" else template
    return render(request, page, context)


def _remember_undo(request, entry):
    request.session[UNDO_KEY] = {"ids": [entry.pk], "text": f"Added {entry.label} to {SLOT_KEYS[entry.slot].lower()}."}


@login_required
@never_cache
def pick(request, kind, pk):
    """The amount panel for one food; POST adds it."""
    if kind not in KINDS:
        raise Http404
    on, slot = _context(request)
    basis, label, fields, last = _basis(request.user, kind, pk)
    error = ""
    if request.method == "POST":
        quantity = amounts.parse_quantity(request.POST.get("quantity"))
        entry = FoodLogEntry(owner=request.user, date=on, slot=_slot(request.POST.get("slot")), label=label, **fields)
        if quantity is None:
            error = "Enter an amount like 1, 1.5, .3 or 1/4."
        elif not amounts.apply(entry, basis, quantity, request.POST.get("unit")):
            error = "Pick one of the units in the list (up to 5 kg)."
        else:
            entry.save()
            _remember_undo(request, entry)
            return redirect(_diary_url(on, entry.slot, request.user))
        slot = entry.slot
    quantity, unit = (_typed(last.quantity), last.unit) if last and last.quantity and basis.unit(last.unit) else (1, basis.default)
    if request.method == "POST":
        quantity, unit = request.POST.get("quantity", ""), request.POST.get("unit", unit)
    return _panel(request, "albright_fitness/food_pick.html", {
        "label": label, "basis": basis, "menu": _menu(basis), "quantity": quantity, "unit": unit,
        "on": on, "slot": slot, "slots": FoodLogEntry.SLOTS, "error": error, "tracked": _tracked(request.user),
        "action": reverse("albright_fitness:food_pick", args=[kind, pk])})


def _typed(value):
    """0.250 -> "1/4", 1.500 -> "1 1/2", 85.000 -> "85": what a person would type."""
    value = Decimal(value).quantize(Decimal("0.001"))
    whole, part = divmod(value, 1)
    shown = {"0.250": "1/4", "0.500": "1/2", "0.750": "3/4", "0.333": "1/3", "0.667": "2/3", "0.125": "1/8"}.get(f"{part:f}")
    if shown:
        return f"{int(whole)} {shown}" if whole else shown
    return f"{value.normalize():f}"


def _menu(basis):
    return json.dumps(basis.menu()).replace("</", "<\\/")            # safe inside <script type="application/json">


def _tracked(user):
    from .models import NutritionProfile
    return NutritionProfile.load(user).tracked or ["calories", "protein"]


@login_required
@never_cache
def edit_entry(request, pk):
    """Tap a logged entry: change its amount, unit or meal, or remove it. A quick add or an AI's estimate changes its
    numbers (and name) instead; an AI entry stays source "ai" but is marked edited ("AI · edited")."""
    entry = get_object_or_404(FoodLogEntry.objects.select_related("food", "custom_food", "recipe"), pk=pk, owner=request.user)
    basis = amounts.basis_for_entry(entry)
    error = ""
    if request.method == "POST":
        if request.POST.get("action") == "remove":
            entry.delete()
            messages.success(request, f"Removed {entry.label}.")
            return redirect(_diary_url(entry.date, entry.slot, request.user))
        entry.slot = _slot(request.POST.get("slot"))
        if entry.source in (FoodLogEntry.QUICK, FoodLogEntry.AI):
            for n in MACROS:
                setattr(entry, n, _macro(request.POST.get(n)))
            entry.label = " ".join(request.POST.get("label", "").split())[:200] or entry.label
            if entry.source == FoodLogEntry.AI:
                entry.edited = True
            entry.save()
            return redirect(_diary_url(entry.date, entry.slot, request.user))
        quantity = amounts.parse_quantity(request.POST.get("quantity"))
        if quantity is None:
            error = "Enter an amount like 1, 1.5, .3 or 1/4."
        elif not amounts.apply(entry, basis, quantity, request.POST.get("unit")):
            error = "Pick one of the units in the list (up to 5 kg)."
        else:
            entry.save()
            messages.success(request, f"Changed {entry.label}.")
            return redirect(_diary_url(entry.date, entry.slot, request.user))
    quantity, unit = amounts.current(entry, basis)
    quantity = _typed(quantity)
    if request.method == "POST":
        quantity, unit = request.POST.get("quantity", ""), request.POST.get("unit", unit)
    return _panel(request, "albright_fitness/food_pick.html", {
        "label": entry.label, "entry": entry, "basis": basis, "menu": _menu(basis), "quantity": quantity,
        "unit": unit, "on": entry.date, "slot": entry.slot, "slots": FoodLogEntry.SLOTS, "error": error,
        "tracked": _tracked(request.user), "quick": entry.source in (FoodLogEntry.QUICK, FoodLogEntry.AI), "macros": [
            (n, nutrition.LABELS[n], getattr(entry, n)) for n in MACROS],
        "action": reverse("albright_fitness:food_edit", args=[entry.pk])})


@login_required
@never_cache
def new_food(request):
    """Enter it yourself: what one serving is (and weighs / holds) and its numbers - saved to My foods and logged."""
    on, slot = _context(request)
    errors, values = [], {}
    if request.method == "POST":
        name = " ".join(request.POST.get("name", "").split())[:100]
        values = {n: _macro(request.POST.get(n)) for n in MACROS}
        quantity = amounts.parse_quantity(request.POST.get("quantity") or "1")
        if not name:
            errors.append("Give it a name.")
        if values["calories"] is None:
            errors.append("Enter its calories for one serving.")
        if quantity is None:
            errors.append("Enter how much you had, like 1, 1.5 or 1/4.")
        slot = _slot(request.POST.get("slot"))
        if not errors:
            label = " ".join(request.POST.get("serving_label", "").split())[:40] or "1 serving"
            if request.POST.get("save") == "1":
                mine = CustomFood.objects.create(
                    owner=request.user, name=name, serving_label=label,
                    serving_grams=_number(request.POST.get("serving_grams") or "", high=amounts.MAX_GRAMS),
                    serving_ml=_number(request.POST.get("serving_ml") or "", high=amounts.MAX_GRAMS), **values)
                entry = FoodLogEntry(owner=request.user, date=on, slot=slot, label=name, source=FoodLogEntry.CUSTOM, custom_food=mine)
                amounts.apply(entry, amounts.for_custom(mine), quantity, "serving")
            else:
                entry = FoodLogEntry(owner=request.user, date=on, slot=slot, label=name, source=FoodLogEntry.QUICK,
                                     portion_label=amounts.Unit("serving", label, 1).describe(quantity)[:60],
                                     **nutrition.scaled(values, quantity))
            entry.save()
            _remember_undo(request, entry)
            return redirect(_diary_url(on, slot, request.user))
    return render(request, "albright_fitness/food_new.html", {
        "on": on, "slot": slot, "slots": FoodLogEntry.SLOTS, "errors": errors, "post": request.POST,
        "name": request.POST.get("name") or usda.clean_query(request.GET.get("q", ""))})


@login_required
@require_POST
def undo(request):
    """Undo the last add - only entries this session just added, and mine."""
    saved = request.session.pop(UNDO_KEY, None) or {}
    gone = FoodLogEntry.objects.filter(owner=request.user, pk__in=saved.get("ids") or [])
    first = gone.first()
    if first:
        on, slot = first.date, first.slot
        gone.delete()
        messages.success(request, "Undone.")
        return redirect(_diary_url(on, slot, request.user))
    return redirect("albright_fitness:food")


def pop_undo(request):
    """For the diary: the "Added ... Undo" note, once."""
    return request.session.pop(UNDO_KEY, None)
