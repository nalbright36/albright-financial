"""Albright Meals pages (/meals/): This week, Groceries, Ideas, Recipes (mine, shared with me, the library), Eat out.
Everything belongs to a space - someone's Personal space or a circle's (scope.py, which asks albright_labs.circles).
Each page shows one space (the switcher; ?space=<id>, remembered); changing something works in any space you see, and
anything in a space you don't see is a 404 (Meals access itself is checked first - albright_labs.app_access). A
recipe in the library is read-only to everyone else; one shared with someone (albright_labs.sharing) is theirs to read
or edit as shared, and they can save a copy."""
import secrets
from types import SimpleNamespace
from datetime import timedelta
from urllib.parse import urlencode, urlsplit

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST

from albright_labs import circles, sharing
from albright_labs.models import ShareGrant

from . import eatout, groceries, importer, plan, recipe_nutrition, scope, signals
from .forms import (EntryForm, IdeaForm, ImportLinkForm, ImportPreviewForm, MealsSettingsForm, PlanSlotForm,
                    RecipeForm)
from .models import (GroceryItem, MealIdea, MealPlanEntry, MealsSettings, NUTRITION_FIELDS, Recipe, RecipeNutrition,
                     RecipeNutritionLine, RecipeRating, SLOTS, STANDARD_TAGS)

TABS = [("mine", "My recipes"), ("shared", "Shared with me"), ("library", "Recipe library")]


def _week_url(day):
    return f"{reverse('albright_meals:week')}?start={plan.week_start(day).isoformat()}"


def _wants_json(request):
    return request.headers.get("X-Requested-With") == "XMLHttpRequest" or "application/json" in \
        request.headers.get("Accept", "")


def _person(user):
    return (user.first_name or user.get_username()).strip() if user else "Someone"


def _notices(request):
    """"Nathan shared Family Meatballs with you" - recipes shared with me (as a person) I haven't seen yet."""
    grants = list(_recipe_grants().filter(user=request.user, seen_at__isnull=True).select_related("created_by")[:20])
    recipes = Recipe.objects.in_bulk([g.object_id for g in grants])
    mine = scope.space_ids(request.user)
    return [SimpleNamespace(shared_by=g.created_by, recipe=recipes[g.object_id]) for g in grants
            if g.object_id in recipes and recipes[g.object_id].space_id not in mine][:5]


def _recipe_grants():
    from django.contrib.contenttypes.models import ContentType
    return ShareGrant.objects.filter(content_type=ContentType.objects.get_for_model(Recipe))


@login_required
@scope.space_required
@require_POST
def notices_seen(request):
    _recipe_grants().filter(user=request.user, seen_at__isnull=True).update(seen_at=timezone.now())
    return JsonResponse({"ok": True}) if _wants_json(request) else redirect(request.POST.get("next") or "albright_meals:week")


# ---------------------------------------------------------------------------
# This week
# ---------------------------------------------------------------------------

@login_required
@scope.space_required
def week(request):
    space = request.space
    start = plan.week_start(plan.parse_day(request.GET.get("start"), timezone.localdate()))
    settings_form = MealsSettingsForm(instance=MealsSettings.load(space)) if request.user.is_superuser else None
    week = plan.week(space, start)
    macros = recipe_nutrition.planned_macros(
        MealPlanEntry.objects.filter(space=space, date__gte=start, date__lt=start + timedelta(days=7))
        .select_related("recipe__nutrition"))
    return render(request, "albright_meals/week.html", {
        "start": start, "end": start + timedelta(days=6), "slots": SLOTS,
        "days": [(day, [(slot, label, meals.get(slot)) for slot, label in SLOTS], bool(meals), macros.get(day))
                 for day, meals in week],
        "today": timezone.localdate(), "this_week": plan.week_start(), "prev": start - timedelta(days=7), "next": start + timedelta(days=7),
        "recipes": scope.own(space).only("pk", "title"), "settings_form": settings_form, "notices": _notices(request),
    })


@login_required
@scope.space_required
@require_POST
def plan_add(request):
    form = PlanSlotForm(request.POST)
    title = (request.POST.get("title") or "").strip()[:200]
    recipe = scope.own(request.space).filter(pk=request.POST.get("recipe") or 0).first()
    if not form.is_valid() or not (recipe or title):
        messages.error(request, "Pick a day, a slot, and a recipe or a title.")
        return redirect("albright_meals:week")
    day, slot = form.cleaned_data["date"], form.cleaned_data["slot"]
    entry, _created = plan.set_slot(request.space, day, slot, recipe=recipe, title=title, user=request.user)
    messages.success(request, f"Planned {entry.display_title} for {day:%a %b} {day.day} ({entry.get_slot_display().lower()}).")
    return redirect(_week_url(day))


def _can_log(user):
    """Can open Fitness - where "Log to my diary" goes."""
    from albright_labs import app_access
    from albright_labs.models import UserPreference
    return app_access.can_use(user, UserPreference.for_user(user), "fitness")


def _diary_slots():
    from albright_fitness.models import FoodLogEntry
    return FoodLogEntry.SLOTS


def _entry(request, pk):
    return get_object_or_404(MealPlanEntry.objects.select_related("recipe", "created_by"), pk=pk,
                             space_id__in=scope.space_ids(request.user))


@login_required
@scope.space_required
def plan_entry(request, pk):
    entry = _entry(request, pk)
    form = EntryForm(request.POST or None, instance=entry)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Saved.")
        return redirect(_week_url(entry.date))
    row = getattr(entry.recipe, "nutrition", None) if entry.recipe_id else None
    return render(request, "albright_meals/entry.html", {
        "entry": entry, "form": form, "move_form": PlanSlotForm(initial={"date": entry.date, "slot": entry.slot}),
        "nutrition": row, "per_serving": recipe_nutrition.per_serving(row), "can_log": _can_log(request.user),
        "diary_slots": _diary_slots()})


@login_required
@scope.space_required
@require_POST
def plan_move(request, pk):
    entry = _entry(request, pk)
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
@scope.space_required
@require_POST
def plan_status(request, pk):
    entry = _entry(request, pk)
    status = request.POST.get("status")
    if status in dict(MealPlanEntry.STATUSES):
        entry.status = status
        entry.save(update_fields=["status", "updated_at"])
    return redirect(_week_url(entry.date))


@login_required
@scope.space_required
@require_POST
def plan_delete(request, pk):
    entry = _entry(request, pk)
    day = entry.date
    entry.delete()
    messages.success(request, "Removed from the plan.")
    return redirect(_week_url(day))


@login_required
@scope.space_required
@require_POST
def settings_save(request):
    """The owner's digest line (superusers only - it's their Telegram)."""
    if not request.user.is_superuser:
        return redirect("albright_meals:week")
    form = MealsSettingsForm(request.POST, instance=MealsSettings.load(request.space))
    if form.is_valid():
        form.save()
        messages.success(request, "Saved.")
    return redirect("albright_meals:week")


# ---------------------------------------------------------------------------
# Groceries
# ---------------------------------------------------------------------------

def _can_plan(user):
    """Whether this user can open the Planner - the grocery list's Select / "Create task" and "In task" chips."""
    from albright_labs import app_access
    from albright_labs.models import UserPreference
    return app_access.can_use(user, UserPreference.for_user(user), "planner")


def _list_payload(request):
    groups = groceries.grouped(request.space)
    can_plan = _can_plan(request.user)
    if can_plan:
        from albright_planner.checklist import tasks_for_lines
        tasks = tasks_for_lines([item for _k, _l, rows in groups for item in rows], request.user)
        for _key, _label, rows in groups:
            for item in rows:
                item.tasks = tasks.get(item.pk, [])
    html = render_to_string("albright_meals/_grocery_list.html", {"groups": groups, "can_plan": can_plan},
                            request=request)
    unchecked = sum(1 for _key, _label, rows in groups for item in rows if not item.checked)
    return {"version": groceries.list_version(request.space), "html": html, "unchecked": unchecked,
            "checked": sum(len(rows) for _k, _l, rows in groups) - unchecked}


def _item(request, pk, queryset=None):
    return get_object_or_404((queryset if queryset is not None else GroceryItem.objects.active()), pk=pk,
                             space_id__in=scope.space_ids(request.user))


@login_required
@scope.space_required
def grocery_page(request):
    if request.method == "POST":
        line = (request.POST.get("line") or "").strip()
        parsed = groceries.parse_line(line)
        if not parsed["name"]:
            messages.error(request, "Type what to add, e.g. \"2 lb chicken thighs\".")
        else:
            item, merged = groceries.add_item(request.space, parsed["name"], parsed["quantity"], parsed["unit"],
                                              parsed["aisle"], parsed["note"], user=request.user)
            if _wants_json(request):
                return JsonResponse({"ok": True, "merged": merged, **_list_payload(request)})
            messages.success(request, f"{'Updated' if merged else 'Added'} {item.name}.")
        return redirect("albright_meals:groceries")
    payload = _list_payload(request)
    return render(request, "albright_meals/groceries.html", {
        "list_html": payload["html"], "version": payload["version"], "unchecked": payload["unchecked"],
        "checked_count": payload["checked"], "week_start": plan.week_start(), "can_plan": _can_plan(request.user),
        "selecting": request.GET.get("select") == "1", "today": timezone.localdate(), "notices": _notices(request)})


@never_cache
@login_required
@scope.space_required
def grocery_list(request):
    """The list as HTML in JSON - what the page fetches every 20 seconds (and after each change).
    ?version=<the one it has>: just {"changed": false} when nothing changed."""
    version = groceries.list_version(request.space)
    if request.GET.get("version") == version:
        return JsonResponse({"changed": False, "version": version})
    return JsonResponse({"changed": True, **_list_payload(request)})


@login_required
@scope.space_required
@require_POST
def grocery_toggle(request, pk):
    item = _item(request, pk)
    item.checked = not item.checked
    item.checked_by = request.user if item.checked else None
    item.checked_by_bot = False
    item.checked_at = timezone.now() if item.checked else None
    item.save()
    if _wants_json(request):
        return JsonResponse({"ok": True, **_list_payload(request)})
    return redirect("albright_meals:groceries")


@login_required
@scope.space_required
@require_POST
def grocery_delete(request, pk):
    _item(request, pk, GroceryItem.objects.all()).delete()
    if _wants_json(request):
        return JsonResponse({"ok": True, **_list_payload(request)})
    return redirect("albright_meals:groceries")


@login_required
@scope.space_required
@require_POST
def grocery_merge(request, pk):
    """A "Possible duplicate" line's Merge: into the line it was linked to (groceries.merge)."""
    item = _item(request, pk, GroceryItem.objects.active().select_related("duplicate_of"))
    kept = groceries.merge(item)
    if _wants_json(request):
        return JsonResponse({"ok": kept is not None, **_list_payload(request)})
    if kept is None:
        messages.error(request, "Nothing to merge it into any more.")
    else:
        messages.success(request, f"Merged into {kept.name}.")
    return redirect("albright_meals:groceries")


@login_required
@scope.space_required
@require_POST
def grocery_clear(request):
    """Archives the checked lines (off the list, but a Planner task's checklist still shows them)."""
    now = timezone.now()
    count = GroceryItem.objects.active().filter(space=request.space, checked=True) \
        .update(archived_at=now, updated_at=now)
    signals.groceries_cleared(request.space.pk, count, request.user)
    messages.success(request, f"Cleared {count} checked item{'s' if count != 1 else ''}.")
    return redirect("albright_meals:groceries")


@login_required
@scope.space_required
@require_POST
def grocery_from_week(request):
    start = plan.week_start(plan.parse_day(request.POST.get("start"), timezone.localdate()))
    entries = MealPlanEntry.objects.filter(space=request.space, date__gte=start, date__lt=start + timedelta(days=7),
                                           status="planned").exclude(recipe=None).select_related("recipe")
    recipes = [entry.recipe for entry in entries]
    if not recipes:
        messages.error(request, "No planned meals with recipes this week.")
        return redirect("albright_meals:groceries")
    added, merged, _skipped = groceries.add_recipes(request.space, recipes, user=request.user)
    messages.success(request, f"Added {added} item{'s' if added != 1 else ''} from {len(recipes)} meal"
                              f"{'s' if len(recipes) != 1 else ''}" + (f", {merged} combined with the list" if merged
                                                                       else "") + ".")
    return redirect("albright_meals:groceries")


# ---------------------------------------------------------------------------
# Ideas
# ---------------------------------------------------------------------------

@login_required
@scope.space_required
def ideas(request):
    form = IdeaForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        idea = form.save(commit=False)
        idea.added_by, idea.space = request.user, request.space
        idea.save()
        messages.success(request, f"Saved {idea.title}.")
        return redirect("albright_meals:ideas")
    show = request.GET.get("show", "new")
    mine = MealIdea.objects.filter(space=request.space)
    qs = mine.select_related("added_by", "planned_entry")
    qs = qs.filter(status="new") if show == "new" else qs.exclude(status="new")
    return render(request, "albright_meals/ideas.html", {
        "form": form, "ideas": qs, "show": show, "slots": SLOTS, "plan_form": PlanSlotForm(initial={"date": timezone.localdate()}),
        "new_count": mine.filter(status="new").count(), "notices": _notices(request)})


@login_required
@scope.space_required
@require_POST
def idea_plan(request, pk):
    idea = get_object_or_404(MealIdea, pk=pk, space_id__in=scope.space_ids(request.user))
    form = PlanSlotForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Pick the day and slot.")
        return redirect("albright_meals:ideas")
    day, slot = form.cleaned_data["date"], form.cleaned_data["slot"]
    notes = "\n".join(x for x in (idea.notes, idea.link) if x)
    entry, _created = plan.set_slot(idea.space, day, slot, title=idea.title, notes=notes, user=request.user)
    idea.status, idea.planned_entry = "planned", entry
    idea.save(update_fields=["status", "planned_entry", "updated_at"])
    messages.success(request, f"Planned {idea.title} for {day:%a %b} {day.day}.")
    return redirect(_week_url(day))


@login_required
@scope.space_required
@require_POST
def idea_decline(request, pk):
    idea = get_object_or_404(MealIdea, pk=pk, space_id__in=scope.space_ids(request.user))
    idea.status = "declined" if idea.status != "declined" else "new"
    idea.save(update_fields=["status", "updated_at"])
    return redirect("albright_meals:ideas")


# ---------------------------------------------------------------------------
# Recipes: mine, shared with me, the library - with ratings on the cards
# ---------------------------------------------------------------------------

def _filter_query(request, filters, tab, q, tag):
    """The current filters as a query string (bookmarkable) - without the page's own tab switch."""
    params = [("tab", tab)] if tab != "mine" else []
    if q:
        params.append(("q", q))
    if tag:
        params.append(("tag", tag))
    if scope.filters_active(filters):
        params.append(("rated_by", str(filters["rated_by"])))
        params += [("rating", str(level)) for level in filters["levels"]] + ([("rating", "none")] if filters["none"] else [])
        if filters["love"]:
            params.append(("love", "1"))
    return urlencode(params)


@login_required
@scope.space_required
def recipes(request):
    user, space = request.user, request.space
    tab = request.GET.get("tab", "mine")
    tab = tab if tab in dict(TABS) else "mine"
    q = (request.GET.get("q") or "").strip()[:100]
    tag = (request.GET.get("tag") or "").strip().lower()[:50]
    filters = scope.parse_filters(request.GET, user, space)
    base = {"mine": scope.own(space), "shared": scope.shared_with(user, space), "library": scope.library()}[tab]
    qs = base.select_related("created_by", "space__circle", "space__user", "nutrition")
    if q:
        qs = qs.filter(Q(title__icontains=q) | Q(description__icontains=q) | Q(notes__icontains=q))
    qs = scope.apply_filters(qs, filters, user, space).distinct()
    rows = [r for r in qs if not tag or tag in [t.lower() for t in (r.tags or [])]
            or tag in [label.lower() for key, label in STANDARD_TAGS if key in (r.standard_tags or [])]]
    stamps = scope.stamps(rows, space, user)
    mine_by_recipe, others = {}, {}
    for recipe_pk, ratings in stamps.items():
        for rating in ratings:
            if rating.user_id == user.pk:
                mine_by_recipe[recipe_pk] = rating
            else:
                others.setdefault(recipe_pk, []).append(rating)
    shares = {g.object_id: g for g in sharing.grants_reaching(user, Recipe)
              .filter(object_id__in=[r.pk for r in rows]).select_related("created_by")}
    visible = scope.space_ids(user)
    cards = [{"recipe": r, "mine": mine_by_recipe.get(r.pk), "others": others.get(r.pk, []), "own": r.space_id in visible,
              "nutrition": recipe_nutrition.per_serving(getattr(r, "nutrition", None)),
              "share": shares.get(r.pk)} for r in rows]
    tags = sorted({t.lower() for r in base.only("tags") for t in (r.tags or [])})
    return render(request, "albright_meals/recipes.html", {
        "cards": cards, "recipes": rows, "q": q, "tag": tag, "tags": tags, "tab": tab, "tabs": TABS,
        "filters": filters, "filtering": scope.filters_active(filters), "query": _filter_query(request, filters, tab, q, tag),
        "members": [m for m in circles.space_members(space, scope.APP) if m.pk != user.pk], "rating_levels": RecipeRating.choices(),
        "notices": _notices(request), "shared_count": scope.shared_with(user, space).count()})


def _target_space(request, recipe):
    """Where planning or shopping for a recipe goes: its own space when the user sees it (it's theirs), else the space
    they're looking at (a recipe shared with them)."""
    return recipe.space if recipe.space_id in scope.space_ids(request.user) else request.space


def _ingredients_for(space, obj):
    ingredients = []
    listed = groceries.on_list(space)
    for index, ingredient in enumerate(obj.ingredients or []):
        name, quantity, unit, note = groceries.ingredient_parts(ingredient)
        original = ingredient.get("original", "") if isinstance(ingredient, dict) else ""
        if name or original:
            on_list = [groceries.describe(item) for item in listed.get(groceries.name_key(name), [])] if name else []
            ingredients.append({"index": index, "name": name, "quantity": groceries.format_quantity(quantity),
                                "unit": unit, "note": note, "original": original, "on_list": on_list})
    return ingredients


@login_required
@scope.space_required
def recipe(request, pk):
    obj, editable = scope.readable_recipe(request.user, request.space, pk)
    _recipe_grants().filter(object_id=obj.pk, user=request.user, seen_at__isnull=True).update(seen_at=timezone.now())
    ingredients = _ingredients_for(obj.space if obj.space_id in scope.space_ids(request.user) else request.space, obj)
    source_link = obj.source_url if importer.is_web_url(obj.source_url) else ""
    host = (urlsplit(source_link).hostname or "") if source_link else ""
    mine = RecipeRating.objects.filter(recipe=obj, user=request.user).first()
    others = [r for r in scope.stamps([obj], obj.space, request.user).get(obj.pk, []) if r.user_id != request.user.pk]
    labels = dict(STANDARD_TAGS)
    owned = obj.space_id in scope.space_ids(request.user)
    shared_grant = None if owned else sharing.grants_reaching(request.user, Recipe).filter(
        object_id=obj.pk).select_related("created_by").first()
    return render(request, "albright_meals/recipe.html", {
        "my_rating": mine, "others": others, "rating_levels": RecipeRating.choices(), "needs_tweaks": RecipeRating.NEEDS_TWEAKS,
        "standard_tags": [labels[key] for key in obj.standard_tags or [] if key in labels],
        "recipe": obj, "editable": editable, "owned": owned, "shared_grant": shared_grant, "ingredients": ingredients,
        "share": sharing.control(request.user, obj), "move_spaces": [s for s in request.spaces if s.pk != obj.space_id] if owned else [],
        "step_groups": importer.step_groups(obj.steps),
        "source_link": source_link, "source_host": host[4:] if host.startswith("www.") else host,
        "plan_form": PlanSlotForm(initial={"date": timezone.localdate()}),
        "nutrition": getattr(obj, "nutrition", None), "per_serving": recipe_nutrition.per_serving(getattr(obj, "nutrition", None)),
        "can_log": _can_log(request.user), "diary_slots": _diary_slots(), "today": timezone.localdate(),
        "picked_count": sum(1 for i in ingredients if i["name"] and not i["on_list"])})


@login_required
@scope.space_required
def recipe_edit(request, pk=None):
    obj = scope.editable_recipe(request.user, pk) if pk else None
    form = RecipeForm(request.POST or None, instance=obj)
    if request.method == "POST" and form.is_valid():
        saved = form.save(commit=False)
        new = not saved.pk
        if new:
            saved.created_by, saved.space = request.user, request.space
        saved.save()
        if new:
            try:
                sharing.apply_posted(request, saved)
            except sharing.ShareError as exc:
                messages.error(request, f"Not shared: {exc}")
        messages.success(request, f"Saved {saved.title}.")
        return redirect("albright_meals:recipe", pk=saved.pk)
    context = {"form": form, "recipe": obj, "share": sharing.control(request.user, obj, "meals")}
    if obj is None:
        # Add a recipe opens with Import from link; ?url=<link> (a bookmark, a bot) pre-fills it and starts it.
        link = (request.GET.get("url") or "").strip()[:importer.MAX_URL]
        context.update(import_url=link, import_autostart=link.lower().startswith(("http://", "https://")))
    return render(request, "albright_meals/recipe_form.html", context)


@login_required
@scope.space_required
@require_POST
def recipe_rate(request, pk):
    """My own stamp on a recipe I can read (blank removes it) and, optionally, a note - from the recipe page or a
    card. Only my space ever sees it."""
    obj, _editable = scope.readable_recipe(request.user, request.space, pk)
    raw = request.POST.get("level", "")
    level = int(raw) if raw.isdigit() and int(raw) in dict(RecipeRating.LEVELS) else None
    if raw and level is None:
        if _wants_json(request):
            return JsonResponse({"ok": False, "error": "Pick one of the ratings."}, status=400)
        messages.error(request, "Pick one of the ratings.")
        return redirect("albright_meals:recipe", pk=pk)
    if level is None:
        RecipeRating.objects.filter(recipe=obj, user=request.user).delete()
        rating = None
    else:
        defaults = {"level": level}
        if "note" in request.POST:
            defaults["note"] = request.POST.get("note", "").strip()[:500]
        rating, _ = RecipeRating.objects.update_or_create(recipe=obj, user=request.user, defaults=defaults)
    if _wants_json(request):
        return JsonResponse({"ok": True, "level": rating.level if rating else None,
                             "label": rating.get_level_display() if rating else "",
                             "meaning": rating.meaning if rating else ""})
    messages.success(request, f"Rated {rating.emoji} {rating.get_level_display()}." if rating else "Rating cleared.")
    return redirect(request.POST.get("next") or reverse("albright_meals:recipe", args=[pk]))


@login_required
@scope.space_required
@require_POST
def recipe_plan(request, pk):
    obj = scope.editable_recipe(request.user, pk)
    form = PlanSlotForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Pick the day and slot.")
        return redirect("albright_meals:recipe", pk=pk)
    day, slot = form.cleaned_data["date"], form.cleaned_data["slot"]
    plan.set_slot(_target_space(request, obj), day, slot, recipe=obj, user=request.user)
    messages.success(request, f"Planned {obj.title} for {day:%a %b} {day.day}.")
    return redirect(_week_url(day))


@login_required
@scope.space_required
@require_POST
def recipe_groceries(request, pk):
    """The recipe page's "Add selected to grocery list" (picked=1, ingredient=<position>...): the ticked ones,
    combined with a same-unit line already there, else linked to it as a possible duplicate. Without picked:
    every ingredient, except any already on the list from this recipe (unchecked)."""
    obj = scope.editable_recipe(request.user, pk)
    if request.POST.get("picked") == "1":
        indexes = [int(v) for v in request.POST.getlist("ingredient") if v.isdigit()]
        if not indexes:
            messages.error(request, "Tick the ingredients to add first.")
        else:
            counts = groceries.add_selected(_target_space(request, obj), obj, indexes, user=request.user)
            messages.success(request, groceries.summary(counts) + ".")
        return redirect(reverse("albright_meals:recipe", args=[pk]) + "#ingredients")
    added, merged, skipped = groceries.add_recipes(_target_space(request, obj), [obj], user=request.user, skip_listed=True)
    if not added and not merged and skipped:
        messages.info(request, "Everything from this recipe is already on the grocery list.")
    else:
        messages.success(request, f"Added {added} item{'s' if added != 1 else ''} to the grocery list"
                                  + (f", {merged} combined with what was there" if merged else "")
                                  + (f" ({skipped} already on it from this recipe)" if skipped else "") + ".")
    return redirect("albright_meals:recipe", pk=pk)


@login_required
@scope.space_required
@require_POST
def recipe_save_copy(request, pk):
    """"Save to my recipes": a copy in my space - credited "Saved from <their space>", with the original
    source link - that we can edit, rate and plan. The original (and its ratings) stay theirs."""
    obj, editable = scope.readable_recipe(request.user, request.space, pk)
    if editable:
        return redirect("albright_meals:recipe", pk=pk)
    fields = ("title", "description", "ingredients", "steps", "prep_minutes", "cook_minutes", "total_time_minutes",
              "servings", "tags", "standard_tags", "source_url", "source_name", "author", "image_url", "notes")
    copy = Recipe(**{name: getattr(obj, name) for name in fields}, space=request.space, created_by=request.user,
                  saved_from=obj, saved_from_name=scope.title(obj.space)[:100], visibility=Recipe.HOUSEHOLD)
    copy.save()
    messages.success(request, f"Saved {copy.title} to your recipes.")
    return redirect("albright_meals:recipe", pk=copy.pk)


@login_required
@scope.space_required
@require_POST
def recipe_visibility(request, pk):
    """Put one of our recipes in the shared library (read-only for every Meals user), or take it out. Copies others
    saved stay theirs."""
    obj = scope.owned_recipe(request.user, pk)
    obj.visibility = Recipe.LIBRARY if request.POST.get("visibility") == Recipe.LIBRARY else Recipe.HOUSEHOLD
    obj.save(update_fields=["visibility", "updated_at"])
    messages.success(request, "In the shared library - everyone with Meals can read it and save a copy."
                     if obj.visibility == Recipe.LIBRARY else "Back to just your space. Copies others saved stay theirs.")
    return redirect("albright_meals:recipe", pk=pk)


@login_required
@scope.space_required
@require_POST
def recipe_move(request, pk):
    """Move to space: the recipe itself goes into another of my spaces (Personal or a circle) - unlike sharing, which
    only shows it to others. Its ratings then show to that space's people; copies others saved stay theirs."""
    obj = scope.owned_recipe(request.user, pk)
    target = next((s for s in request.spaces if str(s.pk) == request.POST.get("to")), None)
    if target is None:
        messages.error(request, "Pick one of your spaces.")
    elif target.pk != obj.space_id:
        obj.space = target
        obj.save(update_fields=["space", "updated_at"])
        messages.success(request, f"Moved {obj.title} to {target.name}.")
    return redirect("albright_meals:recipe", pk=pk)


# ---------------------------------------------------------------------------
# Import from link (top of Add a recipe) - importer.py does the reading
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
@scope.space_required
@require_POST
def recipe_import(request):
    form = ImportLinkForm(request.POST)
    context = {"import_url": (request.POST.get("url") or "")[:importer.MAX_URL]}
    if not form.is_valid():
        context.update(import_status="error", import_error="Paste a recipe's web link (https://...).")
        return _import_response(request, context)
    url = form.cleaned_data["url"]
    try:
        existing = importer.find_saved(request.space, url)
        data = None
        if not existing:
            data = importer.read_page(url)
            existing = importer.find_saved(request.space, data["source_url"], data["page_url"])
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
@scope.space_required
@require_POST
def recipe_import_save(request):
    form = ImportPreviewForm(request.POST)
    imports = dict(request.session.get(IMPORTS_SESSION_KEY) or {})
    import_id = request.POST.get("import_id") or ""
    data = imports.get(import_id)
    if data is None:
        messages.error(request, "That preview has expired - import the link again.")
        return redirect("albright_meals:recipe_add")
    if not form.is_valid():
        return render(request, "albright_meals/recipe_import.html", {
            "import_status": "needs_review" if data["needs_review"] else "preview", "import_data": data,
            "import_form": form, "import_url": data.get("page_url", "")})
    existing = importer.find_saved(request.space, data["source_url"], data["page_url"])
    if existing:
        messages.info(request, f"Already saved: {existing.title}.")
        return redirect("albright_meals:recipe", pk=existing.pk)
    with transaction.atomic():
        saved = importer.save_new({**data, **form.recipe_fields(data)}, request.space, user=request.user)
    imports.pop(import_id, None)
    request.session[IMPORTS_SESSION_KEY] = imports
    messages.success(request, f"Saved {saved.title}." + (" It's marked \"Needs review\" - check it against the "
                                                          "original." if saved.needs_review else ""))
    return redirect("albright_meals:recipe", pk=saved.pk)


@login_required
@scope.space_required
@require_POST
def recipe_reimport(request, pk):
    obj = scope.editable_recipe(request.user, pk)
    try:
        importer.reimport(obj)
    except importer.RecipeImportError as exc:
        messages.error(request, f"Couldn't re-import: {exc.message} Nothing was changed.")
    else:
        messages.success(request, f"Re-imported {obj.title} from {obj.source_name or 'its source'}."
                                  + (" It's marked \"Needs review\"." if obj.needs_review else ""))
    return redirect("albright_meals:recipe", pk=pk)


# ---------------------------------------------------------------------------
# Recipe nutrition (recipe_nutrition.py; USDA via albright_fitness.usda) - the owning space works it out and
# reviews it; it's shown wherever the recipe can be read.
# ---------------------------------------------------------------------------

def _nutrition_row(obj):
    return RecipeNutrition.objects.filter(recipe=obj).prefetch_related("lines__food").first()


@login_required
@scope.space_required
@never_cache
def recipe_nutrition_page(request, pk):
    """The review table: each ingredient line, its USDA match, grams and macros - change the match or grams, ignore
    a line, confirm; or type per-serving numbers instead. ?line=<id>&q=<words> lists other matches for a line."""
    from albright_fitness import usda
    obj = scope.editable_recipe(request.user, pk)
    row = _nutrition_row(obj)
    candidates, picking, search_error = [], None, ""
    if row and request.GET.get("line"):
        picking = row.lines.filter(pk=request.GET.get("line") if request.GET.get("line").isdigit() else 0).first()
        q = usda.clean_query(request.GET.get("q", ""))
        if picking and q:
            try:
                candidates = usda.search(q)[:8]
            except usda.UsdaError as exc:
                search_error = exc.message
    return render(request, "albright_meals/recipe_nutrition.html", {
        "recipe": obj, "row": row, "lines": list(row.lines.all()) if row else [], "candidates": candidates,
        "picking": picking, "q": request.GET.get("q", ""), "search_error": search_error,
        "unmatched": recipe_nutrition.unmatched(row) if row else [], "configured": usda.configured(), "not_set": usda.NOT_SET,
        "servings": recipe_nutrition.servings_of(obj)})


@login_required
@scope.space_required
@require_POST
def recipe_nutrition_calculate(request, pk):
    from albright_fitness import usda
    obj = scope.editable_recipe(request.user, pk)
    if not obj.ingredients:
        messages.error(request, "This recipe has no ingredients to match - add them, or type the nutrition in.")
        return redirect("albright_meals:recipe_nutrition", pk=pk)
    try:
        recipe_nutrition.calculate(obj, request.user)
    except usda.UsdaError as exc:
        messages.error(request, exc.message)
    else:
        messages.success(request, "Matched each ingredient to a USDA food - check the matches, then confirm.")
    return redirect("albright_meals:recipe_nutrition", pk=pk)


@login_required
@scope.space_required
@require_POST
def recipe_nutrition_line(request, pk, line_id):
    """One line: a new match (fdc_id), new grams, or ignore / count it."""
    from albright_fitness import nutrition, usda
    obj = scope.editable_recipe(request.user, pk)
    line = get_object_or_404(RecipeNutritionLine.objects.select_related("nutrition", "food"), pk=line_id, nutrition__recipe=obj)
    food = grams = ignored = None
    if request.POST.get("fdc_id", "").isdigit():
        try:
            food = usda.food(int(request.POST["fdc_id"]))
        except usda.UsdaError as exc:
            messages.error(request, exc.message)
            return redirect("albright_meals:recipe_nutrition", pk=pk)
        if line.grams is None:
            quantity_unit = groceries.parse_line(line.ingredient)
            grams = nutrition.to_grams(quantity_unit["quantity"] or 1, quantity_unit["unit"], food.portions, quantity_unit["name"])
    if request.POST.get("grams", "") != "":
        grams = nutrition.dec(request.POST["grams"])
        if grams is None or grams < 0 or grams > 20000:
            messages.error(request, "Grams should be a number from 0 to 20,000.")
            return redirect("albright_meals:recipe_nutrition", pk=pk)
    if "ignored" in request.POST:
        ignored = request.POST.get("ignored") == "1"
    recipe_nutrition.set_line(line, request.user, food=food, grams=grams, ignored=ignored)
    return redirect(reverse("albright_meals:recipe_nutrition", args=[pk]) + f"#line-{line.pk}")


@login_required
@scope.space_required
@require_POST
def recipe_nutrition_confirm(request, pk):
    obj = scope.editable_recipe(request.user, pk)
    row = get_object_or_404(RecipeNutrition, recipe=obj)
    recipe_nutrition.confirm(row, request.user)
    messages.success(request, "Nutrition confirmed.")
    return redirect("albright_meals:recipe", pk=pk)


@login_required
@scope.space_required
@require_POST
def recipe_nutrition_manual(request, pk):
    """Per-serving numbers typed in instead of matched (blank leaves that one unknown)."""
    from albright_fitness import nutrition
    obj = scope.editable_recipe(request.user, pk)
    values = {}
    for name in NUTRITION_FIELDS:
        raw = (request.POST.get(name) or "").strip()
        value = nutrition.dec(raw, "0.01") if raw else None
        if raw and (value is None or value < 0 or value > 10000):
            messages.error(request, "Each number should be from 0 to 10,000.")
            return redirect("albright_meals:recipe_nutrition", pk=pk)
        values[name] = value
    if values["calories"] is None and values["protein"] is None:
        messages.error(request, "Type at least the calories or protein per serving.")
        return redirect("albright_meals:recipe_nutrition", pk=pk)
    recipe_nutrition.set_manual(obj, values, request.user)
    messages.success(request, "Saved the nutrition per serving.")
    return redirect("albright_meals:recipe", pk=pk)


# ---------------------------------------------------------------------------
# Eat out (idea #78)
# ---------------------------------------------------------------------------

def _eatout_json(request):
    """Body of a JSON POST, or an empty dict if it isn't JSON."""
    import json
    try:
        data = json.loads(request.body.decode() or "{}")
    except (ValueError, UnicodeDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


@login_required
@scope.space_required
def eat_out(request):
    """Guided questions and a ranked shortlist of nearby places (F1). A GET
    form; a down Overpass or geocode API never fails the page."""
    filters = eatout.filters_from_get(request.GET)
    loc, loc_error, home, current, redirect_to = eatout.resolve_location(request.user, request.GET)
    if redirect_to:
        params = eatout.filter_params(
            filters,
            eatout.Location(redirect_to["label"], redirect_to["lat"], redirect_to["lon"], "typed"),
        )
        return redirect(f"{reverse('albright_meals:eat_out')}?{urlencode(params)}")
    result = eatout.search(loc, filters, request.user)
    pick = eatout.pick_index(result.places, request.GET.get("pick"))
    next_pick = (pick % len(result.places) + 1) if result.places else 0
    return render(request, "albright_meals/eatout.html", {
        "filters": filters,
        "loc": loc,
        "loc_error": loc_error,
        "loc_caption": eatout.source_caption(loc.source) if loc else "",
        "home": home,
        "current": current,
        "places": result.places,
        "offline": result.offline,
        "offline_message": result.offline_message,
        "empty_message": result.empty_message,
        "attribution": result.attribution,
        "steps": eatout.steps_done(filters),
        "summary": eatout.summary_text(filters),
        "pick": pick,
        "next_pick": next_pick,
        "qs": eatout.query_string(filters, loc),
        "pick_qs": eatout.query_string(filters, loc, pick or 1),
        "next_pick_qs": eatout.query_string(filters, loc, next_pick),
        "budget_note": bool(filters.budget),
        "cravings": eatout.CRAVINGS,
        "budgets": eatout.BUDGETS,
        "distances": eatout.DISTANCES,
        "modes": eatout.MODES,
        "weather_auto": eatout.weather_follows_device(request.user),
        "location_url": reverse("albright_meals:eat_out_location"),
    })


@login_required
@require_POST
def eat_out_location(request):
    """The browser's rough coordinates, saved onto PlannerSettings.weather_auto_*
    the same way Planner weather already does. Lives under /meals/ so a
    Meals-only account can reach it."""
    body = _eatout_json(request)
    payload, status = eatout.set_current_location(request.user, body.get("lat"), body.get("lon"))
    return JsonResponse(payload, status=status)
