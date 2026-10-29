"""The shared grocery list's logic: reading a quick-add line ("2 lb chicken
thighs"), guessing its aisle, and adding to the list - merged into an
unchecked line for the same thing in the same unit ("1 onion" + "2 onions"
= "3 onion"), never into one someone already checked off.
"""
import re
from collections import OrderedDict
from decimal import Decimal, InvalidOperation

from django.db import transaction

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
FRACTIONS = {"½": "1/2", "⅓": "1/3", "⅔": "2/3", "¼": "1/4", "¾": "3/4", "⅛": "1/8"}
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


def name_key(name):
    """For matching the same thing: lowercase, single spaces, a simple singular ("onions" = "onion")."""
    words = re.sub(r"[^a-z0-9 ]+", " ", (name or "").lower()).split()
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


def add_item(name, quantity=None, unit="", aisle=None, note="", user=None, token=None, from_recipe=None):
    """Adds a line, or merges it into an unchecked line for the same thing and unit.
    Returns (item, merged)."""
    name = " ".join((name or "").split())[:MAX_NAME]
    unit = normalize_unit(unit)
    note = (note or "").strip()[:MAX_NOTE]
    key = name_key(name)
    with transaction.atomic():
        for existing in GroceryItem.objects.select_for_update().filter(checked=False, unit=unit):
            if name_key(existing.name) != key:
                continue
            if quantity is not None:
                existing.quantity = (existing.quantity or 0) + quantity if existing.quantity is not None else quantity
            if note and note.lower() not in existing.note.lower():
                existing.note = f"{existing.note}; {note}"[:MAX_NOTE] if existing.note else note
            existing.save()
            return existing, True
        item = GroceryItem.objects.create(
            name=name, quantity=quantity, unit=unit, aisle=aisle if aisle in AISLE_LABELS else guess_aisle(name),
            note=note, added_by=user if user and user.is_authenticated else None, from_bot=token is not None,
            bot_token=token, from_recipe=from_recipe)
        return item, False


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


def add_recipes(recipes, user=None):
    """Every ingredient of these recipes, duplicates combined first (same thing, same unit),
    then merged into the list. Returns (lines added, lines merged)."""
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
    added = merged = 0
    for row in combined.values():
        _item, was_merged = add_item(row["name"], row["quantity"], row["unit"], note=row["note"], user=user,
                                     from_recipe=row["recipe"] if len(row["recipes"]) == 1 else None)
        merged += was_merged
        added += not was_merged
    return added, merged


def grouped(items=None):
    """[(aisle key, label, [items, unchecked first then checked (most recently checked last)])], aisle order."""
    items = list(items if items is not None else GroceryItem.objects.select_related("added_by", "checked_by"))
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
    stats = GroceryItem.objects.aggregate(n=Count("pk"), last=Max("updated_at"))
    return f"{stats['n']}-{stats['last'].timestamp() if stats['last'] else 0}"
