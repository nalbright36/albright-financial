"""Nutrition math for Fitness and Meals - no requests, no pages (usda.py fetches; views show).

- Amounts: a quantity and unit ("1.5 cup", "8 oz", "2" eggs) -> grams, from the food's household portions when they
  fit, else standard weight / volume conversions (volume at water's density when the food has no portion for it).
- Targets: Mifflin-St Jeor resting rate x activity, the goal (slow loss / gain, ~0.5 lb a week), protein per lb, then
  fat and carbs - never below the resting rate or more than a 20% deficit. Typed targets below that get a gentle
  note (check with a doctor or dietitian).
- The diary: a day's entries by meal, totals vs targets, what's left (over is just "over", never alarming).
- Recipes: each ingredient line matched to a USDA food and grams; totals and per serving.
"""
import re
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from django.db.models import Count, Max, Sum
from django.utils import timezone

from .models import KG_TO_LB, LB, MACROS, FoodLogEntry, NutritionProfile

ZERO = Decimal(0)
HUNDRED = Decimal(100)
LABELS = {"calories": "Calories", "protein": "Protein", "carbs": "Carbs", "fat": "Fat", "fiber": "Fiber"}
UNITS_OF = {"calories": "kcal", "protein": "g", "carbs": "g", "fat": "g", "fiber": "g"}

# ---------------------------------------------------------------------------
# Amounts -> grams
# ---------------------------------------------------------------------------

WEIGHT_G = {"g": Decimal(1), "kg": Decimal(1000), "oz": Decimal("28.3495"), "lb": Decimal("453.592"), "mg": Decimal("0.001")}
VOLUME_ML = {"ml": Decimal(1), "l": Decimal(1000), "tsp": Decimal("4.92892"), "tbsp": Decimal("14.7868"),
             "cup": Decimal("236.588"), "pt": Decimal("473.176"), "qt": Decimal("946.353"), "gal": Decimal("3785.41"),
             "floz": Decimal("29.5735")}
# How portion labels and ingredient units spell the same thing.
UNIT_WORDS = {"cup": ("cup", "cups", "c"), "tbsp": ("tbsp", "tablespoon", "tablespoons", "tbs", "t"),
              "tsp": ("tsp", "teaspoon", "teaspoons"), "oz": ("oz", "ounce", "ounces"), "floz": ("fl oz", "fluid ounce"),
              "lb": ("lb", "lbs", "pound", "pounds"), "g": ("g", "gram", "grams"), "kg": ("kg",), "ml": ("ml",),
              "l": ("l", "liter", "litre"), "pt": ("pt", "pint"), "qt": ("qt", "quart"), "gal": ("gal", "gallon")}
_SPELLING = {spelling: unit for unit, spellings in UNIT_WORDS.items() for spelling in spellings}
_LEADING_NUMBER = re.compile(r"^\s*(\d+\s+\d+/\d+|\d+/\d+|\d+(?:\.\d+)?)\s*(.*)$")


def dec(value, places="0.1"):
    try:
        return Decimal(str(value)).quantize(Decimal(places), rounding=ROUND_HALF_UP)
    except (InvalidOperation, TypeError, ValueError):
        return None


def unit_of(text):
    """"Cups" -> "cup", "fl oz" -> "floz", "large" -> "large" (a count word stays as it is)."""
    text = " ".join((text or "").lower().replace(".", "").split())
    return _SPELLING.get(text, text)


def _number(text):
    total = ZERO
    for part in (text or "").split():
        if "/" in part:
            top, bottom = part.split("/", 1)
            total += Decimal(top) / Decimal(bottom)
        else:
            total += Decimal(part)
    return total


def portion_parts(label):
    """"1 cup, chopped" -> (1, "cup", "chopped"); "2 large" -> (2, "large", ""); "1 serving (30 g)" -> (1, "serving", "")."""
    label = re.sub(r"\([^)]*\)", "", label or "").strip()
    match = _LEADING_NUMBER.match(label)
    amount, rest = (Decimal(1), label)
    if match:
        try:
            amount = _number(match.group(1)) or Decimal(1)
        except (InvalidOperation, ZeroDivisionError):
            amount = Decimal(1)
        rest = match.group(2)
    rest = rest.strip().lower()
    head, _, tail = rest.partition(",")
    words = head.split()
    if words[:2] == ["fl", "oz"]:
        return amount, "floz", " ".join(words[2:] + [tail.strip()]).strip()
    return amount, unit_of(words[0]) if words else "", " ".join(words[1:] + ([tail.strip()] if tail.strip() else []))


def grams_per(portions, unit, name=""):
    """Grams in one `unit` of a food, from its household portions ("1 cup" = 128 g -> 128 per cup), or None."""
    unit = unit_of(unit)
    for p in portions or []:
        amount, p_unit, _rest = portion_parts(p.get("label"))
        if p_unit == unit and amount:
            return Decimal(str(p["grams"])) / amount
    return None


def to_grams(quantity, unit, portions=None, name=""):
    """An amount -> grams (Decimal, 0.1 g), or None when it can't be worked out.
    Weight units convert exactly; volume uses the food's portion for that unit, else other volume portions, else
    water's density; a count ("2 eggs", "1 large onion", "3 cloves") uses a matching portion, else the food's first
    portion."""
    quantity = Decimal(str(quantity)) if quantity not in (None, "") else Decimal(1)
    if quantity <= 0:
        return None
    unit = unit_of(unit)
    if unit in WEIGHT_G:
        return dec(quantity * WEIGHT_G[unit])
    per = grams_per(portions, unit)
    if per:
        return dec(quantity * per)
    if unit in VOLUME_ML:
        for p in portions or []:                          # another volume portion: scale by volume
            amount, p_unit, _rest = portion_parts(p.get("label"))
            if p_unit in VOLUME_ML and amount:
                per_ml = Decimal(str(p["grams"])) / (amount * VOLUME_ML[p_unit])
                return dec(quantity * VOLUME_ML[unit] * per_ml)
        return dec(quantity * VOLUME_ML[unit])            # water's density
    # A count: a portion named like the unit or the food ("large", "clove", "egg"), else the first portion.
    words = {unit} | set((name or "").lower().split())
    for p in portions or []:
        amount, p_unit, rest = portion_parts(p.get("label"))
        if amount and (p_unit in words or (unit and unit in rest.split())):
            return dec(quantity * Decimal(str(p["grams"])) / amount)
    for p in portions or []:
        amount, p_unit, _rest = portion_parts(p.get("label"))
        if amount and p_unit not in WEIGHT_G and p_unit not in VOLUME_ML:
            return dec(quantity * Decimal(str(p["grams"])) / amount)
    return None


def for_grams(food, grams):
    """A cached Food's macros for this many grams -> {calories, protein, carbs, fat, fiber} (None where unknown)."""
    grams = Decimal(str(grams or 0))
    return {n: dec(getattr(food, n) * grams / HUNDRED, "0.01") if getattr(food, n) is not None else None for n in MACROS}


def scaled(per_serving, servings):
    """{name: per-serving amount} x servings."""
    servings = Decimal(str(servings))
    return {n: dec(per_serving.get(n) * servings, "0.01") if per_serving.get(n) is not None else None for n in MACROS}


# ---------------------------------------------------------------------------
# Targets
# ---------------------------------------------------------------------------

ACTIVITY_FACTORS = {"sedentary": Decimal("1.2"), "light": Decimal("1.375"), "moderate": Decimal("1.55"),
                    "active": Decimal("1.725"), "very": Decimal("1.9")}
GOAL_KCAL = {"maintain": 0, "lose": -250, "gain": 250}      # ~0.5 lb a week (3,500 kcal a lb / 7 days)
MAX_DEFICIT = Decimal("0.20")
FAT_SHARE = Decimal("0.25")
FIBER_PER_1000 = 14
FALLBACK_FLOOR = 1200                                       # the note's floor when body info isn't filled in


def bmr(sex, age, height_cm, weight_kg):
    """Mifflin-St Jeor resting metabolic rate (kcal a day), or None without the body info."""
    if not (sex and age and height_cm and weight_kg):
        return None
    base = 10 * Decimal(str(weight_kg)) + Decimal("6.25") * Decimal(str(height_cm)) - 5 * Decimal(age)
    return base + (5 if sex == NutritionProfile.MALE else -161)


def floor_calories(profile):
    """The lowest calories the calculator would suggest (resting rate, and at most a 20% deficit), or None."""
    rest = bmr(profile.sex, profile.age, profile.height_cm, profile.weight_kg)
    if rest is None:
        return None
    tdee = rest * ACTIVITY_FACTORS.get(profile.activity, ACTIVITY_FACTORS["light"])
    return int(max(rest, tdee * (1 - MAX_DEFICIT)).to_integral_value(ROUND_HALF_UP))


def calculate(profile):
    """Suggested targets from the body info -> {"calories", "protein", "carbs", "fat", "fiber", "bmr", "tdee",
    "limited"} or None when the body info isn't complete. limited: the goal's deficit was held back by a guardrail."""
    rest = bmr(profile.sex, profile.age, profile.height_cm, profile.weight_kg)
    if rest is None:
        return None
    tdee = rest * ACTIVITY_FACTORS.get(profile.activity, ACTIVITY_FACTORS["light"])
    wanted = tdee + GOAL_KCAL.get(profile.goal, 0)
    floor = max(rest, tdee * (1 - MAX_DEFICIT))
    calories = max(wanted, floor)
    calories = int((calories / 10).to_integral_value(ROUND_HALF_UP) * 10)
    weight_lb = Decimal(str(profile.weight_kg)) * KG_TO_LB
    protein = int((weight_lb * Decimal(str(profile.protein_per_lb))).to_integral_value(ROUND_HALF_UP))
    fat = int((calories * FAT_SHARE / 9).to_integral_value(ROUND_HALF_UP))
    carbs = (calories - protein * 4 - fat * 9) / 4
    if carbs < 0:                                          # lots of protein on few calories: less fat first
        fat = int((calories * Decimal("0.20") / 9).to_integral_value(ROUND_HALF_UP))
        carbs = max(0, (calories - protein * 4 - fat * 9) / 4)
    return {"calories": calories, "protein": protein, "carbs": int(Decimal(carbs).to_integral_value(ROUND_HALF_UP)),
            "fat": fat, "fiber": round(calories / 1000 * FIBER_PER_1000), "bmr": int(rest), "tdee": int(tdee),
            "limited": wanted < floor}


def low_target_note(profile):
    """A gentle note when typed calories are below the calculator's floor (or 1,200 without body info), else ""."""
    if not profile.target_calories:
        return ""
    floor = floor_calories(profile)
    if floor is not None and profile.target_calories < floor:
        return (f"That's below about {floor:,} calories, the least we'd suggest from your body info (your resting rate, "
                "and no more than a 20% deficit). Going lower can be fine with guidance - it's worth checking with a "
                "doctor or registered dietitian.")
    if floor is None and profile.target_calories < FALLBACK_FLOOR:
        return ("That's a low calorie target. It's worth checking with a doctor or registered dietitian before eating "
                "this little.")
    return ""


# Units for the targets form: weight shown in lb or kg, height in in or cm (stored as kg and cm).
def weight_shown(profile):
    if profile.weight_kg is None:
        return None
    return dec(profile.weight_kg * KG_TO_LB) if profile.weight_unit == LB else profile.weight_kg


def weight_to_kg(value, unit):
    value = Decimal(str(value))
    return dec(value / KG_TO_LB) if unit == LB else dec(value)


def height_shown(profile):
    if profile.height_cm is None:
        return None
    return dec(profile.height_cm / Decimal("2.54")) if profile.height_unit == NutritionProfile.IN else profile.height_cm


def height_to_cm(value, unit):
    value = Decimal(str(value))
    return dec(value * Decimal("2.54")) if unit == NutritionProfile.IN else dec(value)


# ---------------------------------------------------------------------------
# The diary
# ---------------------------------------------------------------------------

def totals(entries):
    out = {n: ZERO for n in MACROS}
    for e in entries:
        for n in MACROS:
            out[n] += getattr(e, n) or ZERO
    return out


def progress(profile, day_totals):
    """One row per tracked macro: total, target, left / over (over is just information), the bar's width."""
    rows = []
    for name in MACROS:
        if name not in (profile.tracked or []):
            continue
        total, target = day_totals.get(name, ZERO), profile.target(name)
        row = {"name": name, "label": LABELS[name], "unit": UNITS_OF[name], "total": int(total.to_integral_value(ROUND_HALF_UP)),
               "target": target, "left": None, "over": None, "pct": 0}
        if target:
            diff = int((Decimal(target) - total).to_integral_value(ROUND_HALF_UP))
            row["left"], row["over"] = (diff, None) if diff >= 0 else (None, -diff)
            row["pct"] = min(100, int(total * 100 / Decimal(target)))
        rows.append(row)
    return rows


def day(user, on):
    """A day's diary: {"slots": [(key, label, entries, totals)], "totals", "entries"}."""
    entries = list(FoodLogEntry.objects.filter(owner=user, date=on).order_by("pk"))
    slots = []
    for key, label in FoodLogEntry.SLOTS:
        rows = [e for e in entries if e.slot == key]
        slots.append((key, label, rows, totals(rows)))
    return {"slots": slots, "totals": totals(entries), "entries": entries}


def averages(user, today=None, days=7):
    """Average calories and protein a day over the last `days` days that have anything logged -> dict or None."""
    today = today or timezone.localdate()
    rows = (FoodLogEntry.objects.filter(owner=user, date__gt=today - timedelta(days=days), date__lte=today)
            .values("date").annotate(calories=Sum("calories"), protein=Sum("protein")))
    rows = list(rows)
    if not rows:
        return None
    n = len(rows)
    return {"days": n, "calories": int(sum((r["calories"] or ZERO) for r in rows) / n),
            "protein": int(sum((r["protein"] or ZERO) for r in rows) / n)}


def recent_foods(user, limit=12, days=60):
    """What I log most (then most recently) over the last couple of months - one tap re-logs it. Each is the
    latest entry of its kind (same source and food / my food / recipe / quick-add label)."""
    since = timezone.localdate() - timedelta(days=days)
    groups = (FoodLogEntry.objects.filter(owner=user, date__gte=since)
              .values("source", "label", "food_id", "custom_food_id", "recipe_id")
              .annotate(times=Count("pk"), last=Max("pk")).order_by("-times", "-last")[:limit])
    latest = FoodLogEntry.objects.in_bulk([g["last"] for g in groups])
    return [(latest[g["last"]], g["times"]) for g in groups if g["last"] in latest]


def status_line(user, today=None):
    """"Protein 92/150 g" for the launcher / Home on a day with no workout planned, or "" when there's nothing to say."""
    profile = NutritionProfile.objects.filter(user=user).first()
    if not profile:
        return ""
    today = today or timezone.localdate()
    eaten = totals(FoodLogEntry.objects.filter(owner=user, date=today))
    for name in ("protein", "calories", "carbs", "fat", "fiber"):
        if name in (profile.tracked or []) and profile.target(name):
            return f"{LABELS[name]} {int(eaten[name])}/{profile.target(name)} {UNITS_OF[name]}"
    return ""


# ---------------------------------------------------------------------------
# Recipes: ingredient lines -> USDA foods and grams
# ---------------------------------------------------------------------------

IGNORE_WORDS = {"salt", "pepper", "black pepper", "salt and pepper", "water", "ice", "cooking spray", "kosher salt",
                "sea salt"}


def suggest_ignore(ingredient):
    """"salt to taste", "water", "pepper" with no amount -> ignore by default (it can be un-ignored)."""
    text = " ".join((ingredient.get("original") or "").lower().split())
    name = " ".join((ingredient.get("name") or "").lower().split())
    return "to taste" in text or "as needed" in text or (not ingredient.get("quantity") and name in IGNORE_WORDS)


def search_words(name):
    """An ingredient's name for the USDA search, without preparation words ("2 cloves garlic, minced" -> "garlic")."""
    from albright_meals.groceries import DESCRIPTORS
    words = [w for w in re.sub(r"[^a-z0-9 ]+", " ", (name or "").lower()).split() if w not in DESCRIPTORS]
    return " ".join(words) or (name or "")


def line_amounts(food, grams):
    if food is None or grams is None:
        return {n: None for n in MACROS}
    return for_grams(food, grams)
