"""A recipe's nutrition (RecipeNutrition - shared exactly as the recipe is).

"Calculate nutrition" matches every ingredient line to its best USDA food (albright_fitness.usda - generic foods
first, branded ones when nothing generic matches) and converts the parsed quantity and unit to grams with the food's
space portions or standard conversions (albright_fitness.nutrition.to_grams). Lines like "salt to taste" start
ignored. It needs review until someone in the space confirms the matches; any line can be re-matched, given
other grams, or ignored. Or the per-serving numbers are typed in instead (MANUAL).

Totals are the matched, not-ignored lines; per serving divides by the recipe's servings (one serving when unset).
"""
from decimal import Decimal

from django.db import transaction

from albright_fitness import nutrition, usda

from . import groceries
from .models import NUTRITION_FIELDS, RecipeNutrition, RecipeNutritionLine

WEIGHT_UNITS = set(nutrition.WEIGHT_G)


def servings_of(recipe):
    return Decimal(recipe.servings or 1)


def _match(ingredient, session=None):
    """One ingredient -> (food or None, grams or None). Raises usda.UsdaError / NotConfigured."""
    words = nutrition.search_words(ingredient.get("name"))
    foods = usda.search(words, usda.GENERIC_TYPES, session=session) or usda.search(words, ["Branded"], session=session)
    if not foods:
        return None, None
    food = foods[0]
    unit = nutrition.unit_of(ingredient.get("unit"))
    if unit not in WEIGHT_UNITS and not food.detailed:
        food = usda.food(food.fdc_id, session=session)            # its space portions, for cups and counts
    quantity = groceries.parse_quantity(str(ingredient.get("quantity") or "")) or Decimal(1)
    return food, nutrition.to_grams(quantity, unit, food.portions, ingredient.get("name"))


def calculate(recipe, user, session=None):
    """Matches every line afresh -> the RecipeNutrition (needs review). Nothing changes if USDA can't be reached."""
    from .importer import ingredient_lines
    texts = ingredient_lines(recipe.ingredients)
    planned = []
    for i, ingredient in enumerate(recipe.ingredients or []):
        ingredient = ingredient if isinstance(ingredient, dict) else {"name": str(ingredient)}
        ignored = nutrition.suggest_ignore({**ingredient, "original": texts[i] if i < len(texts) else ""})
        food, grams = (None, None) if ignored else _match(ingredient, session=session)
        planned.append((texts[i] if i < len(texts) else ingredient.get("name", ""), food, grams, ignored))
    with transaction.atomic():
        row, _created = RecipeNutrition.objects.get_or_create(recipe=recipe)
        row.lines.all().delete()
        RecipeNutritionLine.objects.bulk_create([
            RecipeNutritionLine(nutrition=row, order=i, ingredient=text[:500], food=food, grams=grams, ignored=ignored,
                                **nutrition.line_amounts(food, grams))
            for i, (text, food, grams, ignored) in enumerate(planned)])
        row.status, row.updated_by = RecipeNutrition.NEEDS_REVIEW, user
        recompute(row)
    return row


def recompute(row):
    """Totals from the lines, per serving from the recipe's servings; saves."""
    totals = {n: Decimal(0) for n in NUTRITION_FIELDS}
    known = {n: False for n in NUTRITION_FIELDS}
    for line in row.lines.all():
        if line.ignored:
            continue
        for n in NUTRITION_FIELDS:
            if getattr(line, n) is not None:
                totals[n] += getattr(line, n)
                known[n] = True
    servings = servings_of(row.recipe)
    for n in NUTRITION_FIELDS:
        setattr(row, f"total_{n}", totals[n] if known[n] else None)
        setattr(row, f"serving_{n}", nutrition.dec(totals[n] / servings, "0.01") if known[n] else None)
    row.save()
    return row


def set_line(line, user, food=None, grams=None, ignored=None):
    """Re-match a line, change its grams, or (un)ignore it. Back to needs review."""
    if food is not None:
        line.food = food
        if grams is None:
            grams = line.grams
    if grams is not None:
        line.grams = grams if grams > 0 else None
    if ignored is not None:
        line.ignored = ignored
    for n, v in nutrition.line_amounts(line.food, line.grams).items():
        setattr(line, n, v)
    line.save()
    row = line.nutrition
    row.status, row.updated_by = RecipeNutrition.NEEDS_REVIEW, user
    return recompute(row)


def confirm(row, user):
    row.status, row.updated_by = RecipeNutrition.CONFIRMED, user
    row.save(update_fields=["status", "updated_by", "updated_at"])
    return row


def set_manual(recipe, per_serving, user):
    """Typed per-serving numbers instead of matching (the lines are kept, unused, for going back)."""
    row, _created = RecipeNutrition.objects.get_or_create(recipe=recipe)
    servings = servings_of(recipe)
    for n in NUTRITION_FIELDS:
        value = per_serving.get(n)
        setattr(row, f"serving_{n}", value)
        setattr(row, f"total_{n}", nutrition.dec(value * servings, "0.01") if value is not None else None)
    row.status, row.updated_by = RecipeNutrition.MANUAL, user
    row.save()
    return row


def per_serving(row):
    """{calories, protein, carbs, fat, fiber} per serving, or None when nothing's known."""
    if row is None or row.serving_calories is None and row.serving_protein is None:
        return None
    return {n: getattr(row, f"serving_{n}") for n in NUTRITION_FIELDS}


def unmatched(row):
    return [line for line in row.lines.all() if not line.ignored and (line.food_id is None or line.grams is None)]


def planned_macros(entries):
    """Plan entries -> {date: {"calories", "protein", "known", "count"}}: one serving of each planned recipe whose
    nutrition is known (what one person eats), summed per day."""
    days = {}
    for entry in entries:
        if entry.status == "skipped":
            continue
        day = days.setdefault(entry.date, {"calories": Decimal(0), "protein": Decimal(0), "known": 0, "count": 0})
        day["count"] += 1
        row = getattr(entry.recipe, "nutrition", None) if entry.recipe_id else None
        amounts = per_serving(row)
        if amounts:
            day["known"] += 1
            day["calories"] += amounts["calories"] or 0
            day["protein"] += amounts["protein"] or 0
    for day in days.values():
        day["calories"], day["protein"] = int(day["calories"]), int(day["protein"])
    return days
