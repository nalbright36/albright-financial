"""Albright Meals pages (/meals/): This week, Groceries, Ideas, Recipes.
Everything is shared by everyone who can open Meals (Admin > Users -
albright_labs.app_access 404s it for anyone else)."""
import secrets
from datetime import timedelta
from urllib.parse import urlsplit

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

from . import groceries, importer, plan
from .forms import (EntryForm, IdeaForm, ImportLinkForm, ImportPreviewForm, MealsSettingsForm, PlanSlotForm,
                    RecipeForm)
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
    added, merged, _skipped = groceries.add_recipes(recipes, user=request.user)
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
    ingredients = []
    for ingredient in obj.ingredients or []:
        name, quantity, unit, note = groceries.ingredient_parts(ingredient)
        original = ingredient.get("original", "") if isinstance(ingredient, dict) else ""
        if name or original:
            ingredients.append({"name": name, "quantity": groceries.format_quantity(quantity), "unit": unit,
                                "note": note, "original": original})
    source_link = obj.source_url if importer.is_web_url(obj.source_url) else ""
    host = (urlsplit(source_link).hostname or "") if source_link else ""
    return render(request, "albright_meals/recipe.html", {
        "recipe": obj, "ingredients": ingredients, "step_groups": importer.step_groups(obj.steps),
        "source_link": source_link, "source_host": host[4:] if host.startswith("www.") else host,
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
    """Adds the recipe's ingredients - except any already on the list from this recipe (unchecked)."""
    obj = get_object_or_404(Recipe, pk=pk)
    added, merged, skipped = groceries.add_recipes([obj], user=request.user, skip_listed=True)
    if not added and not merged and skipped:
        messages.info(request, "Everything from this recipe is already on the grocery list.")
    else:
        messages.success(request, f"Added {added} item{'s' if added != 1 else ''} to the grocery list"
                                  + (f", {merged} combined with what was there" if merged else "")
                                  + (f" ({skipped} already on it from this recipe)" if skipped else "") + ".")
    return redirect("albright_meals:recipe", pk=pk)


# ---------------------------------------------------------------------------
# Import from link (Recipes) - importer.py does the reading
# ---------------------------------------------------------------------------

IMPORTS_SESSION_KEY = "meals_imports"
MAX_PREVIEWS = 3          # previews kept in the session at once


def _remember_import(request, data):
    """Keeps what was read until Save (the preview form sends only what can be edited)."""
    imports = dict(request.session.get(IMPORTS_SESSION_KEY) or {})
    import_id = secrets.token_urlsafe(12)
    imports[import_id] = data
    request.session[IMPORTS_SESSION_KEY] = dict(list(imports.items())[-MAX_PREVIEWS:])
    return import_id


def _import_response(request, context):
    """The preview (or the reason there isn't one): HTML for recipe_import.js, or a page without JS."""
    if _wants_json(request):
        html = render_to_string("albright_meals/_import_result.html", context, request=request)
        return JsonResponse({"status": context["import_status"], "html": html})
    return render(request, "albright_meals/recipe_import.html", context)


@login_required
@require_POST
def recipe_import(request):
    form = ImportLinkForm(request.POST)
    context = {"import_url": (request.POST.get("url") or "")[:importer.MAX_URL]}
    if not form.is_valid():
        context.update(import_status="error", import_error="Paste a recipe's web link (https://...).")
        return _import_response(request, context)
    url = form.cleaned_data["url"]
    try:
        existing = importer.find_saved(url)
        data = None
        if not existing:
            data = importer.read_page(url)
            existing = importer.find_saved(data["source_url"], data["page_url"])
    except importer.RecipeImportError as exc:
        context.update(import_status="error", import_error=exc.message)
        return _import_response(request, context)
    if existing:
        context.update(import_status="already_saved", import_existing=existing)
    else:
        context.update(import_status="needs_review" if data["needs_review"] else "preview", import_data=data,
                       import_form=ImportPreviewForm.for_import(data, _remember_import(request, data)))
    return _import_response(request, context)


@login_required
@require_POST
def recipe_import_save(request):
    form = ImportPreviewForm(request.POST)
    imports = dict(request.session.get(IMPORTS_SESSION_KEY) or {})
    import_id = request.POST.get("import_id") or ""
    data = imports.get(import_id)
    if data is None:
        messages.error(request, "That preview has expired - import the link again.")
        return redirect("albright_meals:recipes")
    if not form.is_valid():
        return render(request, "albright_meals/recipe_import.html", {
            "import_status": "needs_review" if data["needs_review"] else "preview", "import_data": data,
            "import_form": form, "import_url": data.get("page_url", "")})
    existing = importer.find_saved(data["source_url"], data["page_url"])
    if existing:
        messages.info(request, f"Already saved: {existing.title}.")
        return redirect("albright_meals:recipe", pk=existing.pk)
    saved = importer.save_new({**data, **form.recipe_fields(data)}, user=request.user)
    imports.pop(import_id, None)
    request.session[IMPORTS_SESSION_KEY] = imports
    messages.success(request, f"Saved {saved.title}." + (" It's marked \"Needs review\" - check it against the "
                                                          "original." if saved.needs_review else ""))
    return redirect("albright_meals:recipe", pk=saved.pk)


@login_required
@require_POST
def recipe_reimport(request, pk):
    obj = get_object_or_404(Recipe, pk=pk)
    try:
        importer.reimport(obj)
    except importer.RecipeImportError as exc:
        messages.error(request, f"Couldn't re-import: {exc.message} Nothing was changed.")
    else:
        messages.success(request, f"Re-imported {obj.title} from {obj.source_name or 'its source'}."
                                  + (" It's marked \"Needs review\"." if obj.needs_review else ""))
    return redirect("albright_meals:recipe", pk=pk)
