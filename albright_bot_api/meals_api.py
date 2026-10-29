"""Bot API v1 - Meals (/api/bot/v1/meals/...), for the Grok "Recipe Bot".

Unlike the rest of the API, these endpoints write straight to the Meals app's
own tables (the household's plan, grocery list, ideas and recipes) - that's
what the bot is for, and the owner asked for it. Everything it creates is
marked from_bot (labeled "Recipe Bot" on the site), every input is checked
and length-limited, and every call is in the Activity log (auth.bot_endpoint).
meals:read lists; meals:write adds and changes - nothing here touches any
other app, users, or settings.
"""
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

from .auth import ApiError, bot_endpoint, json_body
from .links_check import is_http_url

MAX_RANGE_DAYS = 62
MAX_ITEMS = 100           # grocery items per request, ingredients and steps per recipe
MAX_RECIPES = 200
MAX_TAGS = 20


def _meals():
    from albright_meals import models
    return models


def _iso(value):
    return value.isoformat() if value else None


def _str(data, key, limit, required=False, default=""):
    value = data.get(key)
    if value in (None, ""):
        if required:
            raise ApiError(400, f'"{key}" is required.', f"rejected: missing {key}")
        return default
    if not isinstance(value, str):
        raise ApiError(400, f'"{key}" must be a string.', f"rejected: {key} not a string")
    value = value.strip()
    if len(value) > limit:
        raise ApiError(400, f'"{key}" is longer than {limit} characters.', f"rejected: {key} too long")
    if required and not value:
        raise ApiError(400, f'"{key}" is required.', f"rejected: missing {key}")
    return value


def _url(data, key):
    value = _str(data, key, 500)
    if value and not is_http_url(value):
        raise ApiError(400, f'"{key}" must be an http(s) URL.', f"rejected: bad {key}")
    return value


def _int(data, key, low, high):
    value = data.get(key)
    if value in (None, ""):
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ApiError(400, f'"{key}" must be a whole number from {low} to {high}.', f"rejected: bad {key}")
    return value


def _day(value, key):
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ApiError(400, f'"{key}" must be YYYY-MM-DD.', f"rejected: bad {key}") from exc


def _quantity(value, key="quantity"):
    from albright_meals.groceries import parse_quantity
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        raise ApiError(400, f'"{key}" must be a number.', f"rejected: bad {key}")
    if isinstance(value, (int, float)):
        try:
            quantity = Decimal(str(value))
        except InvalidOperation as exc:
            raise ApiError(400, f'"{key}" must be a number.', f"rejected: bad {key}") from exc
    elif isinstance(value, str) and len(value) <= 20:
        quantity = parse_quantity(value)
    else:
        quantity = None
    if quantity is None or not Decimal(0) < quantity <= Decimal(100000):
        raise ApiError(400, f'"{key}" must be a positive number (e.g. 2, 0.5 or "1 1/2").', f"rejected: bad {key}")
    return quantity.quantize(Decimal("0.001")).normalize()


def _choice(data, key, choices, default):
    value = data.get(key) or default
    if value not in dict(choices):
        raise ApiError(400, f'"{key}" must be one of: {", ".join(dict(choices))}.', f"rejected: bad {key}")
    return value


def _by(row):
    return row.author_label or None


# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------

def _entry_json(e):
    return {"id": e.pk, "date": e.date.isoformat(), "slot": e.slot, "title": e.display_title,
            "recipe_id": e.recipe_id, "status": e.status, "notes": e.notes, "added_by": _by(e)}


@bot_endpoint("meals:read")
def plan_list(request, token):
    from albright_meals.plan import week_start
    models = _meals()
    start = _day(request.GET["start"], "start") if request.GET.get("start") else week_start()
    end = _day(request.GET["end"], "end") if request.GET.get("end") else start + timedelta(days=6)
    if end < start:
        raise ApiError(400, '"end" is before "start".', "rejected: end before start")
    if (end - start).days > MAX_RANGE_DAYS:
        raise ApiError(400, f"At most {MAX_RANGE_DAYS} days at a time.", "rejected: range too long")
    entries = models.MealPlanEntry.objects.filter(date__gte=start, date__lte=end).select_related("recipe", "created_by")
    rows = [_entry_json(e) for e in entries]
    return ({"start": start.isoformat(), "end": end.isoformat(), "entries": rows}, 200,
            f"listed {len(rows)} plan entries {start}..{end}")


@bot_endpoint("meals:write", methods=("POST",))
def plan_set(request, token):
    from albright_meals.plan import set_slot
    models = _meals()
    data = json_body(request)
    day = _day(data.get("date"), "date")
    slot = _choice(data, "slot", models.SLOTS, None) if data.get("slot") else None
    if slot is None:
        raise ApiError(400, '"slot" is required: breakfast, lunch, dinner or snack.', "rejected: missing slot")
    recipe = None
    if data.get("recipe_id") not in (None, ""):
        if isinstance(data["recipe_id"], bool) or not isinstance(data["recipe_id"], int):
            raise ApiError(400, '"recipe_id" must be a number.', "rejected: bad recipe_id")
        recipe = models.Recipe.objects.filter(pk=data["recipe_id"]).first()
        if recipe is None:
            raise ApiError(404, f"No recipe {data['recipe_id']}.", "rejected: recipe not found")
    title = _str(data, "title", 200)
    if not recipe and not title:
        raise ApiError(400, 'Send "recipe_id" or "title".', "rejected: no recipe or title")
    notes = _str(data, "notes", 5000) if "notes" in data else None
    entry, created = set_slot(day, slot, recipe=recipe, title=title, notes=notes, token=token)
    return ({"id": entry.pk, "created": created, **_entry_json(entry)}, 201 if created else 200,
            f"{'set' if created else 'replaced'} {day} {slot}: {entry.display_title[:80]}")


@bot_endpoint("meals:write", methods=("DELETE",))
def plan_delete(request, token, entry_id):
    models = _meals()
    entry = models.MealPlanEntry.objects.filter(pk=entry_id).first()
    if entry is None:
        raise ApiError(404, f"No plan entry {entry_id}.", "rejected: entry not found")
    if not entry.from_bot:
        raise ApiError(403, "You can only delete plan entries the Recipe Bot created.", "rejected: not the bot's entry")
    summary = f"deleted plan entry #{entry.pk} ({entry.date} {entry.slot})"
    entry.delete()
    return {"deleted": entry_id}, 200, summary


@csrf_exempt
def plan(request):
    """GET lists (meals:read); POST sets a slot (meals:write)."""
    return plan_set(request) if request.method == "POST" else plan_list(request)


# ---------------------------------------------------------------------------
# Groceries
# ---------------------------------------------------------------------------

def _item_json(i):
    from albright_meals.groceries import format_quantity
    return {"id": i.pk, "name": i.name, "quantity": format_quantity(i.quantity) or None, "unit": i.unit,
            "aisle": i.aisle, "note": i.note, "checked": i.checked, "added_by": _by(i)}


@bot_endpoint("meals:read")
def groceries_list(request, token):
    models = _meals()
    items = [_item_json(i) for i in models.GroceryItem.objects.select_related("added_by")]
    return {"items": items}, 200, f"listed {len(items)} grocery items"


@bot_endpoint("meals:write", methods=("POST",))
def groceries_add(request, token):
    from albright_meals.groceries import add_item
    models = _meals()
    data = json_body(request)
    raw = data["items"] if "items" in data else [data]
    if not isinstance(raw, list) or not raw:
        raise ApiError(400, '"items" must be a non-empty list.', "rejected: no items")
    if len(raw) > MAX_ITEMS:
        raise ApiError(400, f"At most {MAX_ITEMS} items per request.", "rejected: too many items")
    cleaned = []
    for n, item in enumerate(raw, 1):
        if not isinstance(item, dict):
            raise ApiError(400, f"Item {n} must be an object.", "rejected: bad item")
        try:
            cleaned.append({"name": _str(item, "name", 120, required=True), "quantity": _quantity(item.get("quantity")),
                            "unit": _str(item, "unit", 20), "note": _str(item, "note", 200),
                            "aisle": _choice(item, "aisle", models.AISLES, "other") if item.get("aisle") else None})
        except ApiError as exc:
            exc.message = f"Item {n}: {exc.message}"
            raise
    results = []
    with transaction.atomic():
        for row in cleaned:
            saved, merged = add_item(row["name"], row["quantity"], row["unit"], row["aisle"], row["note"], token=token)
            results.append({"id": saved.pk, "merged": merged, **_item_json(saved)})
    merged = sum(r["merged"] for r in results)
    return ({"items": results}, 201, f"added {len(results) - merged} grocery items, merged {merged}: "
            + ", ".join(r["name"] for r in results)[:200])


@bot_endpoint("meals:write", methods=("PATCH",))
def grocery_update(request, token, item_id):
    models = _meals()
    item = models.GroceryItem.objects.filter(pk=item_id).first()
    if item is None:
        raise ApiError(404, f"No grocery item {item_id}.", "rejected: item not found")
    data = json_body(request)
    if not {"quantity", "note", "checked"} & set(data):
        raise ApiError(400, 'Send at least one of "quantity", "note", "checked".', "rejected: nothing to change")
    changed = []
    if "quantity" in data:
        item.quantity = _quantity(data["quantity"])
        changed.append("quantity")
    if "note" in data:
        item.note = _str(data, "note", 200)
        changed.append("note")
    if "checked" in data:
        if not isinstance(data["checked"], bool):
            raise ApiError(400, '"checked" must be true or false.', "rejected: bad checked")
        item.checked = data["checked"]
        item.checked_by, item.checked_by_bot = None, data["checked"]
        item.checked_at = timezone.now() if data["checked"] else None
        changed.append("checked" if data["checked"] else "unchecked")
    item.save()
    return _item_json(item), 200, f"updated grocery item #{item.pk} {item.name[:60]}: {', '.join(changed)}"


@csrf_exempt
def groceries(request):
    """GET lists (meals:read); POST adds (meals:write)."""
    return groceries_add(request) if request.method == "POST" else groceries_list(request)


# ---------------------------------------------------------------------------
# Ideas
# ---------------------------------------------------------------------------

def _idea_json(i):
    return {"id": i.pk, "title": i.title, "notes": i.notes, "link": i.link, "status": i.status,
            "added_by": _by(i), "created_at": _iso(i.created_at)}


@bot_endpoint("meals:read")
def ideas_list(request, token):
    models = _meals()
    rows = [_idea_json(i) for i in models.MealIdea.objects.select_related("added_by")[:200]]
    return {"ideas": rows}, 200, f"listed {len(rows)} meal ideas"


@bot_endpoint("meals:write", methods=("POST",))
def ideas_add(request, token):
    models = _meals()
    data = json_body(request)
    idea = models.MealIdea.objects.create(title=_str(data, "title", 200, required=True), notes=_str(data, "notes", 5000),
                                          link=_url(data, "link"), from_bot=True, bot_token=token)
    return {"id": idea.pk, **_idea_json(idea)}, 201, f"meal idea #{idea.pk}: {idea.title[:80]}"


@csrf_exempt
def ideas(request):
    return ideas_add(request) if request.method == "POST" else ideas_list(request)


# ---------------------------------------------------------------------------
# Recipes
# ---------------------------------------------------------------------------

def _recipe_json(r):
    return {"id": r.pk, "title": r.title, "description": r.description, "ingredients": r.ingredients,
            "steps": r.steps, "prep_minutes": r.prep_minutes, "cook_minutes": r.cook_minutes, "servings": r.servings,
            "tags": r.tags, "source_url": r.source_url, "image_url": r.image_url, "notes": r.notes,
            "added_by": _by(r), "updated_at": _iso(r.updated_at)}


@bot_endpoint("meals:read")
def recipes_list(request, token):
    models = _meals()
    q = (request.GET.get("q") or "").strip()[:100]
    qs = models.Recipe.objects.select_related("created_by").order_by("-updated_at", "-pk")
    if q:
        qs = qs.filter(title__icontains=q)
    rows = [_recipe_json(r) for r in qs[:MAX_RECIPES]]
    return {"recipes": rows}, 200, f"listed {len(rows)} recipes" + (f" matching {q!r}" if q else "")


def _ingredients(value):
    if not isinstance(value, list) or len(value) > MAX_ITEMS:
        raise ApiError(400, f'"ingredients" must be a list of at most {MAX_ITEMS}.', "rejected: bad ingredients")
    cleaned = []
    for n, item in enumerate(value, 1):
        if isinstance(item, str):
            item = {"name": item}
        if not isinstance(item, dict):
            raise ApiError(400, f"Ingredient {n} must be an object.", "rejected: bad ingredient")
        try:
            quantity = item.get("quantity")
            if isinstance(quantity, (int, float)) and not isinstance(quantity, bool):
                quantity = str(quantity)
            cleaned.append({"name": _str(item, "name", 120, required=True),
                            "quantity": _str({"quantity": quantity}, "quantity", 20),
                            "unit": _str(item, "unit", 20), "note": _str(item, "note", 200)})
        except ApiError as exc:
            exc.message = f"Ingredient {n}: {exc.message}"
            raise
    return cleaned


def _steps(value):
    if not isinstance(value, list) or len(value) > MAX_ITEMS:
        raise ApiError(400, f'"steps" must be a list of at most {MAX_ITEMS}.', "rejected: bad steps")
    steps = []
    for n, step in enumerate(value, 1):
        if not isinstance(step, str) or len(step.strip()) > 2000:
            raise ApiError(400, f"Step {n} must be text of at most 2000 characters.", "rejected: bad step")
        if step.strip():
            steps.append(step.strip())
    return steps


def _tags(value):
    if not isinstance(value, list):
        raise ApiError(400, '"tags" must be a list.', "rejected: tags not a list")
    tags = []
    for tag in value:
        if isinstance(tag, str) and tag.strip() and len(tag.strip()) <= 50 and tag.strip().lower() not in tags:
            tags.append(tag.strip().lower())
    return tags[:MAX_TAGS]


@bot_endpoint("meals:write", methods=("POST",))
def recipes_upsert(request, token):
    models = _meals()
    data = json_body(request)
    title = _str(data, "title", 200, required=True)
    fields = {}
    for key, limit in (("description", 5000), ("notes", 5000)):
        if key in data:
            fields[key] = _str(data, key, limit)
    for key in ("source_url", "image_url"):
        if key in data:
            fields[key] = _url(data, key)
    for key, low, high in (("prep_minutes", 0, 2880), ("cook_minutes", 0, 2880), ("servings", 1, 100)):
        if key in data:
            fields[key] = _int(data, key, low, high)
    if "ingredients" in data:
        fields["ingredients"] = _ingredients(data["ingredients"])
    if "steps" in data:
        fields["steps"] = _steps(data["steps"])
    if "tags" in data:
        fields["tags"] = _tags(data["tags"])
    with transaction.atomic():
        recipe = models.Recipe.objects.select_for_update().filter(title__iexact=title).order_by("pk").first()
        created = recipe is None
        if created:
            recipe = models.Recipe(title=title, from_bot=True, bot_token=token)
        for key, value in fields.items():
            setattr(recipe, key, value)
        recipe.save()
    return ({"id": recipe.pk, "created": created}, 201 if created else 200,
            f"{'created' if created else 'updated'} recipe #{recipe.pk}: {title[:80]}")


@csrf_exempt
def recipes(request):
    return recipes_upsert(request) if request.method == "POST" else recipes_list(request)
