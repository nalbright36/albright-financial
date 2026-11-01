"""The shared grocery list's logic: reading a quick-add line ("2 lb chicken
thighs"), guessing its aisle, and adding to the list - merged into an
unchecked line for the same thing in the same unit ("1 onion" + "2 onions"
= "3 onion"), never into one someone already checked off. The same thing in a
different unit is added as its own line, linked as a possible duplicate
(GroceryItem.duplicate_of) for someone to Merge. "The same thing" is
name_key(): lowercase, singular, without words like "fresh" or "chopped".
Each line records every recipe its ingredients came from (GroceryItem.recipes).
Archived lines ("Clear checked items") are off the list and never matched.
"""
import re
from collections import OrderedDict
from decimal import Decimal, InvalidOperation

from django.db import models, transaction

from .models import AISLES, GroceryItem

MAX_NAME, MAX_UNIT, MAX_NOTE = 120, 20, 200

# Spelling -> the unit we store, so "lbs" and "pound" merge with "lb".
UNITS = {
    "lb": "lb", "lbs": "lb", "pound": "lb", "pounds": "lb",
    "oz": "oz", "ounce": "oz", "ounces": "oz",
    "g": "g", "gram": "g", "grams": "g", "kg": "kg", "kgs": "kg", "kilogram": "kg", "kilograms": "kg",
    "ml": "ml", "l": "l", "liter": "l", "liters": "l", "litre": "l", "litres": "l",
    "cup": "cup", "cups": "cup", "c": "cup",
    "tbsp": "tbsp", "tbs": "tbsp", "tablespoon": "tbsp", "tablespoons": "tbsp",
    "tsp": "tsp", "teaspoon": "tsp", "teaspoons": "tsp",
    "gal": "gal", "gallon": "gal", "gallons": "gal", "qt": "qt", "quart": "qt", "quarts": "qt",
    "pt": "pt", "pint": "pt", "pints": "pt",
    "can": "can", "cans": "can", "jar": "jar", "jars": "jar", "bag": "bag", "bags": "bag",
    "box": "box", "boxes": "box", "pack": "pack", "packs": "pack", "package": "pack", "packages": "pack",
    "pkg": "pack", "bunch": "bunch", "bunches": "bunch", "clove": "clove", "cloves": "clove",
    "dozen": "dozen", "head": "head", "heads": "head", "loaf": "loaf", "loaves": "loaf",
    "bottle": "bottle", "bottles": "bottle", "stick": "stick", "sticks": "stick",
    "slice": "slice", "slices": "slice", "piece": "piece", "pieces": "piece",
}
FRACTIONS = {"½": "1/2", "⅓": "1/3", "⅔": "2/3", "¼": "1/4", "¾": "3/4", "⅛": "1/8", "⅜": "3/8", "⅝": "5/8",
             "⅞": "7/8", "⅕": "1/5", "⅖": "2/5", "⅗": "3/5", "⅘": "4/5", "⅙": "1/6", "⅚": "5/6"}
_NUMBER = r"(?:\d+\s+\d+/\d+|\d+/\d+|\d+(?:\.\d+)?|\.\d+)"
_LEADING = re.compile(rf"^\s*({_NUMBER})\s*(?:x\s+)?(.*)$", re.I)

# (phrase, aisle) - checked longest phrase first, as whole words, so "black pepper" is
# pantry while "pepper" alone is produce, and "frozen ..." always wins.
_AISLE_WORDS = {
    "frozen": ["frozen", "ice cream", "popsicle", "frozen pizza", "tater tots", "frozen peas", "frozen corn"],
    "household": ["paper towel", "paper towels", "toilet paper", "tissue", "tissues", "soap", "dish soap",
                  "detergent", "laundry", "trash bag", "trash bags", "garbage bag", "foil", "aluminum foil",
                  "plastic wrap", "sponge", "sponges", "shampoo", "conditioner", "toothpaste", "napkins",
                  "bleach", "cleaner", "wipes", "batteries", "light bulb", "diapers", "deodorant"],
    "meat": ["chicken", "beef", "pork", "turkey", "bacon", "sausage", "ham", "salmon", "shrimp", "fish",
             "steak", "lamb", "ground beef", "ground turkey", "tilapia", "cod", "crab", "lobster", "scallops",
             "chicken thighs", "chicken breast", "ribs", "hot dogs", "brisket", "meatballs", "chorizo", "prosciutto"],
    "dairy": ["milk", "cheese", "butter", "yogurt", "cream", "egg", "eggs", "sour cream", "cream cheese",
              "cheddar", "mozzarella", "parmesan", "feta", "half and half", "heavy cream", "cottage cheese",
              "ricotta", "whipped cream", "oat milk", "almond milk"],
    "bakery": ["bread", "buns", "bun", "bagel", "bagels", "tortilla", "tortillas", "rolls", "croissant",
               "croissants", "muffin", "muffins", "pita", "baguette", "english muffins", "naan", "cake"],
    "pantry": ["rice", "pasta", "spaghetti", "noodles", "flour", "sugar", "brown sugar", "oil", "olive oil",
               "vegetable oil", "vinegar", "salt", "black pepper", "pepper flakes", "spice", "spices", "cumin",
               "paprika", "oregano", "cinnamon", "beans", "black beans", "chickpeas", "lentils", "sauce",
               "tomato sauce", "tomato paste", "canned tomatoes", "diced tomatoes", "broth", "stock",
               "chicken broth", "chicken stock", "cereal", "oats", "oatmeal", "honey", "peanut butter",
               "jam", "jelly", "ketchup", "mustard", "mayo", "mayonnaise", "soy sauce", "salsa", "coffee", "tea",
               "nuts", "almonds", "walnuts", "crackers", "chips", "baking soda", "baking powder", "vanilla",
               "syrup", "maple syrup", "cornstarch", "breadcrumbs", "tuna", "canned", "quinoa", "couscous"],
    "produce": ["apple", "apples", "banana", "bananas", "onion", "onions", "garlic", "potato", "potatoes",
                "sweet potato", "tomato", "tomatoes", "lettuce", "spinach", "carrot", "carrots", "celery",
                "pepper", "peppers", "bell pepper", "bell peppers", "jalapeno", "lemon", "lemons", "lime",
                "limes", "avocado", "avocados", "cilantro", "parsley", "basil", "mint", "thyme", "rosemary",
                "mushroom", "mushrooms", "broccoli", "cauliflower", "cucumber", "cucumbers", "zucchini",
                "squash", "berries", "strawberries", "blueberries", "raspberries", "grapes", "orange",
                "oranges", "ginger", "kale", "cabbage", "corn", "green beans", "peas", "asparagus", "scallions",
                "green onions", "shallot", "shallots", "salad", "arugula", "peach", "peaches", "pear",
                "pears", "melon", "watermelon", "pineapple", "mango", "eggplant", "radish", "beets", "herbs"],
}
_PHRASES = sorted(((phrase, aisle) for aisle, words in _AISLE_WORDS.items() for phrase in words),
                  key=lambda pair: -len(pair[0]))
AISLE_LABELS = dict(AISLES)


def parse_quantity(text):
    """"2", "1/2", "1 1/2", "1.5", "½" -> Decimal, or None."""
    text = (text or "").strip()
    for symbol, fraction in FRACTIONS.items():
        text = text.replace(symbol, f" {fraction}")
    text = text.strip()
    if not text or not re.fullmatch(_NUMBER, text):
        return None
    try:
        total = Decimal(0)
        for part in text.split():
            if "/" in part:
                top, bottom = part.split("/")
                total += Decimal(top) / Decimal(bottom)
            else:
                total += Decimal(part)
    except (InvalidOperation, ZeroDivisionError):
        return None
    return total.quantize(Decimal("0.001")).normalize() if total > 0 else None


def normalize_unit(unit):
    unit = (unit or "").strip().lower().rstrip(".")
    return UNITS.get(unit, unit[:MAX_UNIT])


def guess_aisle(name):
    lowered = f" {re.sub(r'[^a-z ]+', ' ', (name or '').lower())} "
    for phrase, aisle in _PHRASES:
        if f" {phrase} " in lowered:
            return aisle
    return "other"


def parse_line(line):
    """A quick-add line -> {"name", "quantity", "unit", "note", "aisle"}.
    "2 lb chicken thighs" -> 2, lb, chicken thighs, meat. "milk (whole)" -> note "whole"."""
    text = " ".join((line or "").split())
    for symbol, fraction in FRACTIONS.items():
        text = text.replace(symbol, f" {fraction} ").strip()
    text = " ".join(text.split())
    note = ""
    paren = re.search(r"\(([^)]*)\)\s*$", text)
    if paren:
        note, text = paren.group(1).strip(), text[:paren.start()].strip()
    elif "," in text:
        text, note = (part.strip() for part in text.split(",", 1))
    quantity, unit, name = None, "", text
    match = _LEADING.match(text)
    if match:
        quantity = parse_quantity(match.group(1))
        rest = match.group(2).strip()
        first, _, remainder = rest.partition(" ")
        if first.lower().rstrip(".") in UNITS and remainder:
            unit, name = normalize_unit(first), remainder.strip()
        else:
            name = rest
        if name.lower().startswith("of "):
            name = name[3:]
    name = name.strip()[:MAX_NAME]
    return {"name": name, "quantity": quantity, "unit": unit, "note": note[:MAX_NOTE], "aisle": guess_aisle(name)}


# Words that describe how an ingredient is prepared or sized, not what it is - left out when matching, so
# "2 cloves garlic, minced", "fresh garlic" and "garlic" are the same thing.
DESCRIPTORS = frozenset("""
    fresh freshly chopped diced minced sliced finely roughly coarsely thinly large small medium peeled grated
    shredded crushed ripe organic raw boneless skinless optional packed softened melted cold warm divided
    of a an the to taste for serving garnish about plus extra
""".split())


def name_key(name):
    """For matching the same thing: lowercase, single spaces, without preparation words ("fresh", "chopped" -
    DESCRIPTORS), and a simple singular ("onions" = "onion", "tomatoes" = "tomato")."""
    words = re.sub(r"[^a-z0-9 ]+", " ", (name or "").lower()).split()
    words = [w for w in words if w not in DESCRIPTORS] or words
    if words:
        last = words[-1]
        if last.endswith("oes") and len(last) > 4:
            last = last[:-2]
        elif last.endswith("ies") and len(last) > 4:
            last = last[:-3] + "y"
        elif last.endswith("s") and not last.endswith("ss") and len(last) > 3:
            last = last[:-1]
        words[-1] = last
    return " ".join(words)


def format_quantity(quantity):
    if quantity is None:
        return ""
    text = format(Decimal(quantity).normalize(), "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


ADDED, COMBINED, DUPLICATE = "added", "combined", "duplicate"


def add_item_detail(name, quantity=None, unit="", aisle=None, note="", user=None, token=None, from_recipe=None,
                    recipes=()):
    """Adds a line, or combines it into an unchecked line for the same thing and unit; the same thing in another
    unit is added as its own line linked to that one (a possible duplicate). Either way the recipes it came from
    are recorded on the line. Returns (item, ADDED | COMBINED | DUPLICATE)."""
    recipes = [r for r in (recipes or ([from_recipe] if from_recipe else [])) if r is not None]
    name = " ".join((name or "").split())[:MAX_NAME]
    unit = normalize_unit(unit)
    note = (note or "").strip()[:MAX_NOTE]
    key = name_key(name)
    with transaction.atomic():
        same_thing = [i for i in GroceryItem.objects.active().select_for_update().filter(checked=False).order_by("pk")
                      if name_key(i.name) == key]
        for existing in same_thing:
            if existing.unit != unit:
                continue
            if quantity is not None:
                existing.quantity = (existing.quantity or 0) + quantity if existing.quantity is not None else quantity
            if note and note.lower() not in existing.note.lower():
                existing.note = f"{existing.note}; {note}"[:MAX_NOTE] if existing.note else note
            existing.save()
            if recipes:
                existing.recipes.add(*recipes)
            return existing, COMBINED
        item = GroceryItem.objects.create(
            name=name, quantity=quantity, unit=unit, aisle=aisle if aisle in AISLE_LABELS else guess_aisle(name),
            note=note, added_by=user if user and user.is_authenticated else None, from_bot=token is not None,
            bot_token=token, from_recipe=from_recipe or (recipes[0] if len(recipes) == 1 else None),
            duplicate_of=same_thing[0] if same_thing else None)
        if recipes:
            item.recipes.set(recipes)
        return item, DUPLICATE if same_thing else ADDED


def add_item(name, quantity=None, unit="", aisle=None, note="", user=None, token=None, from_recipe=None,
             recipes=()):
    """add_item_detail, answering (item, merged) - merged when it was combined into an existing line."""
    item, status = add_item_detail(name, quantity, unit, aisle, note, user=user, token=token,
                                   from_recipe=from_recipe, recipes=recipes)
    return item, status == COMBINED


def ingredient_parts(ingredient):
    """A recipe ingredient (a dict, or a plain "2 lb chicken" string) -> (name, quantity, unit, note)."""
    if isinstance(ingredient, str):
        parsed = parse_line(ingredient)
        return parsed["name"], parsed["quantity"], parsed["unit"], parsed["note"]
    if not isinstance(ingredient, dict):
        return "", None, "", ""
    name = str(ingredient.get("name") or "").strip()
    quantity_text = str(ingredient.get("quantity") or "").strip()
    unit = str(ingredient.get("unit") or "").strip()
    quantity = parse_quantity(quantity_text)
    if quantity is None and quantity_text and not unit:
        # "1 cup" in the quantity field
        parsed = parse_line(f"{quantity_text} {name}")
        quantity, unit = parsed["quantity"], parsed["unit"]
    return name, quantity, normalize_unit(unit), str(ingredient.get("note") or "").strip()


def add_recipes(recipes, user=None, token=None, skip_listed=False):
    """Every ingredient of these recipes, duplicates combined first (same thing, same unit),
    then merged into the list. skip_listed: leave out an ingredient whose unchecked line already
    came from one of these recipes (so pressing "Add ingredients" twice doesn't double it).
    Returns (lines added, lines merged, ingredients skipped)."""
    listed = set()
    if skip_listed:
        for name, unit, recipe_pk in GroceryItem.objects.active().filter(checked=False, recipes__in=[r.pk for r in recipes]) \
                .values_list("name", "unit", "recipes"):
            listed.add((name_key(name), unit, recipe_pk))
    combined = OrderedDict()
    for recipe in recipes:
        for ingredient in recipe.ingredients or []:
            name, quantity, unit, note = ingredient_parts(ingredient)
            if not name:
                continue
            key = (name_key(name), unit)
            if key in combined:
                row = combined[key]
                if quantity is not None:
                    row["quantity"] = (row["quantity"] or 0) + quantity if row["quantity"] is not None else quantity
                if note and note.lower() not in row["note"].lower():
                    row["note"] = f"{row['note']}; {note}" if row["note"] else note
                row["recipes"].add(recipe.pk)
            else:
                combined[key] = {"name": name, "quantity": quantity, "unit": unit, "note": note,
                                 "recipe": recipe, "recipes": {recipe.pk}}
    added = merged = skipped = 0
    by_pk = {r.pk: r for r in recipes}
    for (key, unit), row in combined.items():
        if any((key, unit, pk) in listed for pk in row["recipes"]):
            skipped += 1
            continue
        _item, was_merged = add_item(row["name"], row["quantity"], row["unit"], note=row["note"], user=user,
                                     token=token, recipes=[by_pk[pk] for pk in sorted(row["recipes"])])
        merged += was_merged
        added += not was_merged
    return added, merged, skipped


# ---------------------------------------------------------------------------
# A recipe's ingredients: what's already on the list, and adding the ones picked
# ---------------------------------------------------------------------------

def describe(item):
    """"1 lb (from Chili)" - an unchecked line as the recipe page's "On list" badge shows it."""
    amount = " ".join(part for part in (format_quantity(item.quantity), item.unit) if part) or item.name
    titles = [r.title for r in item.recipes.all()]
    return amount + (f" (from {', '.join(titles)})" if titles else "")


def on_list():
    """{name_key: [unchecked lines on the list]} - for the recipe page's "On list" badges."""
    index = {}
    for item in GroceryItem.objects.active().filter(checked=False).prefetch_related("recipes").order_by("pk"):
        index.setdefault(name_key(item.name), []).append(item)
    return index


def add_selected(recipe, indexes, user=None):
    """The picked ingredients of one recipe (by position in recipe.ingredients), the same thing in the same unit
    combined first, then added to the list. Returns {"added", "combined", "duplicates"} counts."""
    picked = OrderedDict()
    ingredients = recipe.ingredients or []
    for index in sorted(set(indexes)):
        if not 0 <= index < len(ingredients):
            continue
        name, quantity, unit, note = ingredient_parts(ingredients[index])
        if not name:
            continue
        key = (name_key(name), unit)
        row = picked.setdefault(key, {"name": name, "quantity": None, "unit": unit, "note": ""})
        if quantity is not None:
            row["quantity"] = (row["quantity"] or 0) + quantity
        if note and note.lower() not in row["note"].lower():
            row["note"] = f"{row['note']}; {note}" if row["note"] else note
    counts = {"added": 0, "combined": 0, "duplicates": 0}
    for row in picked.values():
        _item, status = add_item_detail(row["name"], row["quantity"], row["unit"], note=row["note"], user=user,
                                        recipes=[recipe])
        counts[{ADDED: "added", COMBINED: "combined", DUPLICATE: "duplicates"}[status]] += 1
    return counts


def summary(counts):
    """"Added 4 · Combined 2 · 1 possible duplicate" (only the parts that happened)."""
    parts = []
    if counts.get("added"):
        parts.append(f"Added {counts['added']}")
    if counts.get("combined"):
        parts.append(f"Combined {counts['combined']}")
    if counts.get("duplicates"):
        n = counts["duplicates"]
        parts.append(f"{n} possible duplicate{'s' if n != 1 else ''}")
    return " · ".join(parts) or "Nothing added"


@transaction.atomic
def merge(item):
    """Merge a possible duplicate into the line it was linked to: one line, the same unit's quantities added (else
    the other amount kept in its note), every recipe kept, and any Planner checklist row moved over. Returns the
    line kept, or None if there's nothing to merge into (it was checked off or cleared meanwhile)."""
    target = item.duplicate_of
    if target is None or target.checked or target.archived_at or item.checked or item.archived_at:
        return None
    if item.unit == target.unit:
        if item.quantity is not None:
            target.quantity = (target.quantity or 0) + item.quantity
    else:
        amount = " ".join(part for part in (format_quantity(item.quantity), item.unit) if part)
        extra = f"+ {amount}" if amount else ""
        if extra and extra not in target.note:
            target.note = f"{target.note}; {extra}"[:MAX_NOTE] if target.note else extra
    if item.note and item.note.lower() not in target.note.lower():
        target.note = f"{target.note}; {item.note}"[:MAX_NOTE] if target.note else item.note[:MAX_NOTE]
    target.save()
    target.recipes.add(*item.recipes.all())
    GroceryItem.objects.filter(duplicate_of=item).update(duplicate_of=target)
    from albright_planner.models import TaskChecklistItem
    TaskChecklistItem.objects.filter(grocery_item=item).update(grocery_item=target)
    item.delete()
    return target


def possible_duplicate(item):
    """Whether a line is flagged "Possible duplicate": linked to (or from) another unchecked line on the list."""
    other = item.duplicate_of
    if other is not None and not other.checked and other.archived_at is None and not item.checked:
        return True
    return not item.checked and any(not d.checked and d.archived_at is None for d in item.duplicates.all())


def grouped(items=None):
    """[(aisle key, label, [items, unchecked first then checked (most recently checked last)])], aisle order.
    Each item gets .possible_duplicate and .can_merge."""
    items = list(items if items is not None else GroceryItem.objects.active()
                 .select_related("added_by", "checked_by", "duplicate_of").prefetch_related("recipes", "duplicates"))
    for item in items:
        item.possible_duplicate = possible_duplicate(item)
        item.can_merge = item.possible_duplicate and item.duplicate_of_id is not None \
            and not item.duplicate_of.checked and item.duplicate_of.archived_at is None
    groups = []
    for key, label in AISLES:
        rows = [i for i in items if i.aisle == key]
        if rows:
            rows.sort(key=lambda i: (i.checked, i.checked_at.timestamp() if i.checked_at else 0, i.name.lower()))
            groups.append((key, label, rows))
    return groups


def list_version():
    """Changes whenever anything on the list changes - the 20-second refresh skips an unchanged list."""
    from django.db.models import Count, Max
    stats = GroceryItem.objects.aggregate(n=Count("pk", filter=models.Q(archived_at__isnull=True)), last=Max("updated_at"))
    return f"{stats['n']}-{stats['last'].timestamp() if stats['last'] else 0}"
