"""A recipe's ingredients as display rows, and the Planner's meal quick view.

ingredient_rows(recipe.ingredients): one row per ingredient - {"amount": "1 1/2 cups", "name": "flour", "note": "sifted"}
(the amount as written when there is one) - or {"section": "For the sauce"} for a heading line ("## Sauce", "For the
sauce:", a short "For the ..." with no amount). Every list of ingredients draws them the same way: the recipe page
(recipe.html, two columns: the amount, then the name wrapping under itself with the note muted after it) and the
quick view (planner.js, from this JSON) - the .ing-* styles in style.css.

payload(entry, user): the meal quick view's data - the meal (title, slot, date), the move / remove / groceries URLs and,
with full=True, the whole recipe (time, servings, my rating, ingredient rows, step groups), so the popup opens at once
with nothing to fetch. attach(meals, user, full) puts it on the Planner's sources.Meal rows (data-mqv); the Month view
sends full=False (it can hold many meals) and planner.js fetches plan_quick's JSON on pointerdown instead."""
import json
from decimal import Decimal, InvalidOperation

from django.urls import reverse
from django.utils import timezone
from django.utils.dateformat import format as date_format

from . import groceries, importer
from .models import SLOTS, RecipeRating

_FRACTIONS = {Decimal("0.125"): "1/8", Decimal("0.25"): "1/4", Decimal("0.333"): "1/3", Decimal("0.5"): "1/2",
              Decimal("0.667"): "2/3", Decimal("0.75"): "3/4"}


def _quantity_text(quantity):
    """Decimal("1.5") -> "1 1/2"; anything else as groceries.format_quantity writes it."""
    if quantity is None:
        return ""
    try:
        value = Decimal(quantity)
    except (InvalidOperation, TypeError, ValueError):
        return str(quantity)
    whole = int(value)
    rest = (value - whole).quantize(Decimal("0.001"))
    for frac, text in _FRACTIONS.items():
        if abs(rest - frac) < Decimal("0.01"):
            return f"{whole} {text}" if whole else text
    return groceries.format_quantity(value)


_UNIT_PLURALS = {"cup": "cups", "clove": "cloves", "can": "cans", "pinch": "pinches", "slice": "slices", "stick": "sticks",
                 "bunch": "bunches", "head": "heads", "sprig": "sprigs", "package": "packages", "jar": "jars", "piece": "pieces",
                 "pound": "pounds", "ounce": "ounces", "tablespoon": "tablespoons", "teaspoon": "teaspoons", "quart": "quarts",
                 "pint": "pints", "gallon": "gallons", "handful": "handfuls", "dash": "dashes", "bag": "bags", "bottle": "bottles"}


def _unit_text(unit, quantity):
    """"cup" -> "cups" for more than one (abbreviations like tbsp, oz, g stay as they are)."""
    try:
        many = quantity is not None and Decimal(quantity) > 1
    except (InvalidOperation, TypeError, ValueError):
        many = False
    return _UNIT_PLURALS.get(unit, unit) if many else unit


def ingredient_rows(ingredients):
    rows = []
    for index, ingredient in enumerate(ingredients or []):
        name, quantity, unit, note = groceries.ingredient_parts(ingredient)
        original = (ingredient.get("original") or "") if isinstance(ingredient, dict) else \
            (ingredient if isinstance(ingredient, str) else "")
        # As written when it's a fraction ("1 1/2", "1/2"); a plain decimal reads better as one ("1.5" -> "1 1/2").
        written = str(ingredient.get("quantity") or "").strip() if isinstance(ingredient, dict) else ""
        amount_qty = written if "/" in written and not any(c.isalpha() for c in written) else _quantity_text(quantity)
        amount = " ".join(p for p in (amount_qty, _unit_text(unit, quantity)) if p)
        text = (name or original).strip()
        if not text and not original:
            continue
        if not amount and groceries.is_heading(original or text):
            rows.append({"index": index, "section": (original or text).strip().lstrip("#").strip().rstrip(":").strip()})
            continue
        rows.append({"index": index, "amount": amount, "name": text or original, "note": note, "original": original})
    return rows


def _times(recipe):
    parts = []
    if recipe.prep_minutes:
        parts.append(f"Prep {recipe.prep_minutes} min")
    if recipe.cook_minutes:
        parts.append(f"Cook {recipe.cook_minutes} min")
    total = recipe.total_time_minutes or ((recipe.prep_minutes or 0) + (recipe.cook_minutes or 0))
    if total and len(parts) != 1:
        parts.append(f"Total {total} min")
    return " · ".join(parts)


def recipe_json(recipe, rating=None):
    return {
        "title": recipe.title, "description": recipe.description or "", "times": _times(recipe),
        "servings": recipe.servings or None,
        "rating": {"emoji": rating.emoji, "label": rating.get_level_display()} if rating else None,
        "ingredients": [{k: v for k, v in row.items() if k not in ("index", "original")} for row in ingredient_rows(recipe.ingredients)],
        "steps": importer.step_groups(recipe.steps),
        "url": reverse("albright_meals:recipe", args=[recipe.pk]),
    }


def _date_text(day):
    today = timezone.localdate()
    return date_format(day, "D, M j") + (date_format(day, ", Y") if day.year != today.year else "")


def payload(entry, user, ratings=None, full=True):
    recipe = entry.recipe if entry.recipe_id else None
    if ratings is None and recipe is not None:
        ratings = {r.recipe_id: r for r in RecipeRating.objects.filter(recipe=recipe, user=user)}
    data = {
        "id": entry.pk, "title": entry.display_title, "slot": entry.slot, "slotLabel": entry.get_slot_display(),
        "date": entry.date.isoformat(), "dateText": _date_text(entry.date), "slots": SLOTS,
        "move": reverse("albright_meals:plan_move", args=[entry.pk]),
        "remove": reverse("albright_meals:plan_delete", args=[entry.pk]),
        "groceries": reverse("albright_meals:plan_groceries", args=[entry.pk]) if recipe else "",
        "page": reverse("albright_meals:recipe", args=[recipe.pk]) if recipe else reverse("albright_meals:plan_entry", args=[entry.pk]),
        "notes": entry.notes or "",
        "hasRecipe": recipe is not None,
    }
    if recipe is not None:
        if full:
            data["recipe"] = recipe_json(recipe, (ratings or {}).get(recipe.pk))
        else:
            data["recipeUrl"] = reverse("albright_meals:plan_quick", args=[entry.pk])
    return data


def attach(meals, user, full=True):
    """meal.qv (JSON) on each sources.Meal - the ratings in one query."""
    recipe_ids = {m.entry.recipe_id for m in meals if m.entry.recipe_id}
    ratings = {r.recipe_id: r for r in RecipeRating.objects.filter(recipe_id__in=recipe_ids, user=user)} if recipe_ids and full else {}
    for m in meals:
        m.qv = json.dumps(payload(m.entry, user, ratings, full), separators=(",", ":"))
    return meals
