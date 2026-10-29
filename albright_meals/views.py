"""Albright Meals pages (/meals/): This week, Groceries, Ideas, Recipes.
Everything is shared by everyone who can open Meals (Admin > Users -
albright_labs.app_access 404s it for anyone else)."""
from datetime import timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST

from . import groceries, plan
from .forms import EntryForm, IdeaForm, MealsSettingsForm, PlanSlotForm, RecipeForm
from .models import SLOTS, GroceryItem, MealIdea, MealPlanEntry, MealsSettings, Recipe


def _week_url(day):
    return f"{reverse('albright_meals:week')}?start={plan.week_start(day).isoformat()}"


def _wants_json(request):
    return request.headers.get("X-Requested-With") == "XMLHttpRequest" or "application/json" in \
        request.headers.get("Accept", "")


# ---------------------------------------------------------------------------
# This week
# ---------------------------------------------------------------------------

@login_required
def week(request):
    start = plan.week_start(plan.parse_day(request.GET.get("start"), timezone.localdate()))
    settings_form = None
    if request.user.is_superuser:
        settings_form = MealsSettingsForm(instance=MealsSettings.load())
    return render(request, "albright_meals/week.html", {
        "start": start, "end": start + timedelta(days=6), "slots": SLOTS,
        "days": [(day, [(slot, label, meals.get(slot)) for slot, label in SLOTS], bool(meals))
                 for day, meals in plan.week(start)],
        "today": timezone.localdate(), "this_week": plan.week_start(), "prev": start - timedelta(days=7), "next": start + timedelta(days=7),
        "recipes": Recipe.objects.only("pk", "title"), "settings_form": settings_form,
    })


@login_required
@require_POST
def plan_add(request):
    form = PlanSlotForm(request.POST)
    title = (request.POST.get("title") or "").strip()[:200]
    recipe = Recipe.objects.filter(pk=request.POST.get("recipe") or 0).first()
    if not form.is_valid() or not (recipe or title):
        messages.error(request, "Pick a day, a slot, and a recipe or a title.")
        return redirect("albright_meals:week")
    day, slot = form.cleaned_data["date"], form.cleaned_data["slot"]
    entry, _created = plan.set_slot(day, slot, recipe=recipe, title=title, user=request.user)
    messages.success(request, f"Planned {entry.display_title} for {day:%a %b} {day.day} ({entry.get_slot_display().lower()}).")
    return redirect(_week_url(day))


@login_required
def plan_entry(request, pk):
    entry = get_object_or_404(MealPlanEntry.objects.select_related("recipe", "created_by"), pk=pk)
    form = EntryForm(request.POST or None, instance=entry)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Saved.")
        return redirect(_week_url(entry.date))
    return render(request, "albright_meals/entry.html", {
        "entry": entry, "form": form, "move_form": PlanSlotForm(initial={"date": entry.date, "slot": entry.slot})})


@login_required
@require_POST
def plan_move(request, pk):
    entry = get_object_or_404(MealPlanEntry, pk=pk)
    form = PlanSlotForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Pick the day and slot to move it to.")
        return redirect(_week_url(entry.date))
    swapped = plan.move(entry, form.cleaned_data["date"], form.cleaned_data["slot"])
    day = form.cleaned_data["date"]
    messages.success(request, f"Moved to {day:%a %b} {day.day}." + (f" {swapped.display_title} took its place."
                                                                    if swapped else ""))
    return redirect(_week_url(day))


@login_required
@require_POST
def plan_status(request, pk):
    entry = get_object_or_404(MealPlanEntry, pk=pk)
    status = request.POST.get("status")
    if status in dict(MealPlanEntry.STATUSES):
        entry.status = status
        entry.save(update_fields=["status", "updated_at"])
    return redirect(_week_url(entry.date))


@login_required
@require_POST
def plan_delete(request, pk):
    entry = get_object_or_404(MealPlanEntry, pk=pk)
    day = entry.date
    entry.delete()
    messages.success(request, "Removed from the plan.")
    return redirect(_week_url(day))


@login_required
@require_POST
def settings_save(request):
    """The owner's digest line (superusers only - it's their Telegram)."""
    if not request.user.is_superuser:
        return redirect("albright_meals:week")
    form = MealsSettingsForm(request.POST, instance=MealsSettings.load())
    if form.is_valid():
        form.save()
        messages.success(request, "Saved.")
    return redirect("albright_meals:week")


# ---------------------------------------------------------------------------
# Groceries
# ---------------------------------------------------------------------------

def _list_payload(request):
    groups = groceries.grouped()
    html = render_to_string("albright_meals/_grocery_list.html", {"groups": groups}, request=request)
    unchecked = sum(1 for _key, _label, rows in groups for item in rows if not item.checked)
    return {"version": groceries.list_version(), "html": html, "unchecked": unchecked,
            "checked": sum(len(rows) for _k, _l, rows in groups) - unchecked}


@login_required
def grocery_page(request):
    if request.method == "POST":
        line = (request.POST.get("line") or "").strip()
        parsed = groceries.parse_line(line)
        if not parsed["name"]:
            messages.error(request, "Type what to add, e.g. \"2 lb chicken thighs\".")
        else:
            item, merged = groceries.add_item(parsed["name"], parsed["quantity"], parsed["unit"], parsed["aisle"],
                                              parsed["note"], user=request.user)
            if _wants_json(request):
                return JsonResponse({"ok": True, "merged": merged, **_list_payload(request)})
            messages.success(request, f"{'Updated' if merged else 'Added'} {item.name}.")
        return redirect("albright_meals:groceries")
    payload = _list_payload(request)
    return render(request, "albright_meals/groceries.html", {
        "list_html": payload["html"], "version": payload["version"], "unchecked": payload["unchecked"],
        "checked_count": payload["checked"], "week_start": plan.week_start()})


@never_cache
@login_required
def grocery_list(request):
    """The list as HTML in JSON - what the page fetches every 20 seconds (and after each change).
    ?version=<the one it has>: just {"changed": false} when nothing changed."""
    version = groceries.list_version()
    if request.GET.get("version") == version:
        return JsonResponse({"changed": False, "version": version})
    return JsonResponse({"changed": True, **_list_payload(request)})


@login_required
@require_POST
def grocery_toggle(request, pk):
    item = get_object_or_404(GroceryItem, pk=pk)
    item.checked = not item.checked
    item.checked_by = request.user if item.checked else None
    item.checked_by_bot = False
    item.checked_at = timezone.now() if item.checked else None
    item.save()
    if _wants_json(request):
        return JsonResponse({"ok": True, **_list_payload(request)})
    return redirect("albright_meals:groceries")


@login_required
@require_POST
def grocery_delete(request, pk):
    GroceryItem.objects.filter(pk=pk).delete()
    if _wants_json(request):
        return JsonResponse({"ok": True, **_list_payload(request)})
    return redirect("albright_meals:groceries")


@login_required
@require_POST
def grocery_clear(request):
    count, _ = GroceryItem.objects.filter(checked=True).delete()
    messages.success(request, f"Cleared {count} checked item{'s' if count != 1 else ''}.")
    return redirect("albright_meals:groceries")


@login_required
@require_POST
def grocery_from_week(request):
    start = plan.week_start(plan.parse_day(request.POST.get("start"), timezone.localdate()))
    entries = MealPlanEntry.objects.filter(date__gte=start, date__lt=start + timedelta(days=7), status="planned") \
        .exclude(recipe=None).select_related("recipe")
    recipes = [entry.recipe for entry in entries]
    if not recipes:
        messages.error(request, "No planned meals with recipes this week.")
        return redirect("albright_meals:groceries")
    added, merged = groceries.add_recipes(recipes, user=request.user)
    messages.success(request, f"Added {added} item{'s' if added != 1 else ''} from {len(recipes)} meal"
                              f"{'s' if len(recipes) != 1 else ''}" + (f", {merged} combined with the list" if merged
                                                                       else "") + ".")
    return redirect("albright_meals:groceries")


# ---------------------------------------------------------------------------
# Ideas
# ---------------------------------------------------------------------------

@login_required
def ideas(request):
    form = IdeaForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        idea = form.save(commit=False)
        idea.added_by = request.user
        idea.save()
        messages.success(request, f"Saved {idea.title}.")
        return redirect("albright_meals:ideas")
    show = request.GET.get("show", "new")
    qs = MealIdea.objects.select_related("added_by", "planned_entry")
    qs = qs.filter(status="new") if show == "new" else qs.exclude(status="new")
    return render(request, "albright_meals/ideas.html", {
        "form": form, "ideas": qs, "show": show, "slots": SLOTS, "plan_form": PlanSlotForm(initial={"date": timezone.localdate()}),
        "new_count": MealIdea.objects.filter(status="new").count()})


@login_required
@require_POST
def idea_plan(request, pk):
    idea = get_object_or_404(MealIdea, pk=pk)
    form = PlanSlotForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Pick the day and slot.")
        return redirect("albright_meals:ideas")
    day, slot = form.cleaned_data["date"], form.cleaned_data["slot"]
    notes = "\n".join(x for x in (idea.notes, idea.link) if x)
    entry, _created = plan.set_slot(day, slot, title=idea.title, notes=notes, user=request.user)
    idea.status, idea.planned_entry = "planned", entry
    idea.save(update_fields=["status", "planned_entry", "updated_at"])
    messages.success(request, f"Planned {idea.title} for {day:%a %b} {day.day}.")
    return redirect(_week_url(day))


@login_required
@require_POST
def idea_decline(request, pk):
    idea = get_object_or_404(MealIdea, pk=pk)
    idea.status = "declined" if idea.status != "declined" else "new"
    idea.save(update_fields=["status", "updated_at"])
    return redirect("albright_meals:ideas")


# ---------------------------------------------------------------------------
# Recipes
# ---------------------------------------------------------------------------

@login_required
def recipes(request):
    q = (request.GET.get("q") or "").strip()
    tag = (request.GET.get("tag") or "").strip().lower()
    qs = Recipe.objects.select_related("created_by")
    if q:
        qs = qs.filter(Q(title__icontains=q) | Q(description__icontains=q) | Q(notes__icontains=q))
    rows = [r for r in qs if not tag or tag in [t.lower() for t in r.tags]]
    tags = sorted({t.lower() for r in Recipe.objects.only("tags") for t in (r.tags or [])})
    return render(request, "albright_meals/recipes.html", {"recipes": rows, "q": q, "tag": tag, "tags": tags})


@login_required
def recipe(request, pk):
    obj = get_object_or_404(Recipe.objects.select_related("created_by"), pk=pk)
    ingredients = [groceries.ingredient_parts(i) for i in obj.ingredients or []]
    return render(request, "albright_meals/recipe.html", {
        "recipe": obj, "ingredients": [{"name": n, "quantity": groceries.format_quantity(q), "unit": u, "note": note}
                                       for n, q, u, note in ingredients if n],
        "plan_form": PlanSlotForm(initial={"date": timezone.localdate()})})


@login_required
def recipe_edit(request, pk=None):
    obj = get_object_or_404(Recipe, pk=pk) if pk else None
    form = RecipeForm(request.POST or None, instance=obj)
    if request.method == "POST" and form.is_valid():
        saved = form.save(commit=False)
        if not saved.pk:
            saved.created_by = request.user
        saved.save()
        messages.success(request, f"Saved {saved.title}.")
        return redirect("albright_meals:recipe", pk=saved.pk)
    return render(request, "albright_meals/recipe_form.html", {"form": form, "recipe": obj})


@login_required
@require_POST
def recipe_plan(request, pk):
    obj = get_object_or_404(Recipe, pk=pk)
    form = PlanSlotForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Pick the day and slot.")
        return redirect("albright_meals:recipe", pk=pk)
    day, slot = form.cleaned_data["date"], form.cleaned_data["slot"]
    plan.set_slot(day, slot, recipe=obj, user=request.user)
    messages.success(request, f"Planned {obj.title} for {day:%a %b} {day.day}.")
    return redirect(_week_url(day))


@login_required
@require_POST
def recipe_groceries(request, pk):
    obj = get_object_or_404(Recipe, pk=pk)
    added, merged = groceries.add_recipes([obj], user=request.user)
    messages.success(request, f"Added {added} item{'s' if added != 1 else ''} to the grocery list"
                              + (f", {merged} combined with what was there" if merged else "") + ".")
    return redirect("albright_meals:recipe", pk=pk)
