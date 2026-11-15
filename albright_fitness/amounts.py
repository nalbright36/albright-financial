"""Food amounts, the way MyFitnessPal does them: any quantity (2, 1.5, .3, 1/4, 1 1/2, ½) of any unit the food
supports, with the calories and macros worked out from it.

A food's units (basis()):
  - a USDA food: each household portion it lists ("1 container (170 g)", "1 cup, chopped (160 g)"), g and oz
    always (its nutrients are per 100 g), and - when one of its portions is a volume - ml, fl oz, cup, tbsp, tsp
    from that portion's weight;
  - one of My foods: its serving ("1 container"), plus g / oz when the serving's weight is given and ml / fl oz /
    cup / tbsp / tsp when its volume is;
  - a Meals recipe: servings;
  - an entry whose food is gone (deleted): "as logged", plus g / oz when its weight is known.
A diary entry keeps quantity + unit (the unit's key: "g", "oz", "serving", "p:<portion label>", ...) and grams when
known, so it can be changed later (FoodLogEntry.quantity / unit)."""
import re
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from .models import MACROS, FoodLogEntry
from .nutrition import HUNDRED, VOLUME_ML, WEIGHT_G, dec, portion_parts

MAX_QUANTITY = Decimal(5000)
MAX_GRAMS = Decimal(5000)
FRACTIONS = {"½": " 1/2", "¼": " 1/4", "¾": " 3/4", "⅓": " 1/3", "⅔": " 2/3", "⅛": " 1/8", "⅜": " 3/8", "⅝": " 5/8",
             "⅞": " 7/8", "⅕": " 1/5"}
_QUANTITY = re.compile(r"^(?:(\d+)(?:\s+|-))?(\d+)\s*/\s*(\d+)$|^(\d+(?:\.\d*)?|\.\d+)$")
SHOWN = {Decimal("0.25"): "¼", Decimal("0.5"): "½", Decimal("0.75"): "¾", Decimal("0.333"): "⅓", Decimal("0.667"): "⅔",
         Decimal("0.125"): "⅛"}
VOLUME_UNITS = [("ml", "ml"), ("floz", "fl oz"), ("cup", "1 cup"), ("tbsp", "1 tbsp"), ("tsp", "1 tsp")]


def parse_quantity(text):
    """"1/4" -> 0.25, "1 1/2" -> 1.5, ".3" -> 0.3, "1½" -> 1.5, "2,5" -> 2.5; anything else (or 0, or over 5,000) -> None."""
    text = str(text if text is not None else "").strip().lower()
    for char, words in FRACTIONS.items():
        text = text.replace(char, words)
    text = " ".join(text.replace(",", ".").split())
    match = _QUANTITY.match(text)
    if not match:
        return None
    try:
        if match.group(4) is not None:
            value = Decimal(match.group(4))
        else:
            bottom = Decimal(match.group(3))
            if not bottom:
                return None
            value = Decimal(match.group(1) or 0) + Decimal(match.group(2)) / bottom
    except InvalidOperation:
        return None
    value = value.quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)
    return value if 0 < value <= MAX_QUANTITY else None


def show_quantity(value):
    """0.25 -> "¼", 1.5 -> "1½", 2 -> "2", 0.3 -> "0.3"."""
    value = Decimal(value).quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)
    whole, part = divmod(value, 1)
    if part in SHOWN:
        return f"{int(whole) if whole else ''}{SHOWN[part]}"
    return f"{value.normalize():f}"


@dataclass
class Unit:
    key: str             # what an entry keeps: "g", "oz", "serving", "p:1 cup", ...
    label: str           # what the menu shows: "1 container (170 g)"
    base: Decimal        # how much of the basis one of these is (grams for a USDA food, servings otherwise)
    grams: Decimal = None   # grams in one, when known

    def describe(self, quantity):
        """2 g -> "2 g"; ¼ of "1 container (170 g)" -> "¼ container (170 g)"; 2 of "large" -> "2 × large"."""
        q = show_quantity(quantity)
        if self.key in ("g", "oz", "ml", "floz"):
            return f"{q} {self.label}"
        if self.label.startswith("1 "):
            return self.label if Decimal(quantity) == 1 else f"{q} {self.label[2:]}"
        return self.label if Decimal(quantity) == 1 else f"{q} × {self.label}"


@dataclass
class Basis:
    kind: str                         # "usda", "custom", "recipe", "entry"
    per: dict                         # nutrients for one base unit (None where unknown)
    units: list = field(default_factory=list)
    default: str = ""

    def unit(self, key_or_label):
        """A unit by its key, or by its label (any case) - None when this food doesn't have it."""
        text = (key_or_label or "").strip()
        for u in self.units:
            if u.key == text:
                return u
        for u in self.units:
            if u.label.lower() == text.lower() or u.key.lower() == text.lower() or (u.key.startswith("p:") and u.key[2:].lower() == text.lower()):
                return u
        return None

    def amount(self, quantity, key):
        """{"totals", "grams", "servings", "unit", "portion_label"} for this much, or None (unknown unit)."""
        u = self.unit(key)
        if u is None or quantity is None:
            return None
        base = Decimal(quantity) * u.base
        grams = dec(Decimal(quantity) * u.grams) if u.grams else None
        return {"totals": {n: dec(self.per[n] * base, "0.01") if self.per.get(n) is not None else None for n in MACROS},
                "grams": grams, "servings": dec(base, "0.01") if self.kind in ("custom", "recipe") else None,
                "unit": u, "portion_label": u.describe(quantity)[:60]}

    def menu(self):
        """For the amount panel: [{"key", "label", "per": nutrients for one of it}] - the page multiplies as you type."""
        return [{"key": u.key, "label": u.label,
                 "per": {n: float(self.per[n] * u.base) if self.per.get(n) is not None else None for n in MACROS}} for u in self.units]


def _grams_text(grams):
    return f"{dec(grams, '1').normalize():f}"


def _weights(per_gram_base, units, grams_per_base=None):
    """g and oz: per_gram_base = how much of the basis one gram is."""
    units.append(Unit("g", "g", per_gram_base, Decimal(1)))
    units.append(Unit("oz", "oz", per_gram_base * WEIGHT_G["oz"], WEIGHT_G["oz"]))


def _volumes(base_per_ml, units, grams_per_ml=None, skip=()):
    for key, label in VOLUME_UNITS:
        if key in skip:
            continue
        ml = VOLUME_ML[key]
        grams = grams_per_ml * ml if grams_per_ml else None
        shown = label if not grams or key in ("ml", "floz") else f"{label} ({_grams_text(grams)} g)"
        units.append(Unit(key, shown, base_per_ml * ml, grams))


def for_food(food):
    """A cached USDA Food: nutrients per gram."""
    per = {n: getattr(food, n) / HUNDRED if getattr(food, n) is not None else None for n in MACROS}
    units, volume_units, grams_per_ml = [], set(), None
    for p in food.portions or []:
        grams = Decimal(str(p["grams"]))
        label = p["label"] if re.search(r"\d\s*g\)", p["label"]) else f"{p['label']} ({_grams_text(grams)} g)"
        units.append(Unit(f"p:{p['label']}"[:64], label, grams, grams))
        amount, unit, _rest = portion_parts(p["label"])
        if unit in VOLUME_ML and amount:
            volume_units.add(unit)
            grams_per_ml = grams_per_ml or grams / (amount * VOLUME_ML[unit])
    _weights(Decimal(1), units)
    if grams_per_ml:
        _volumes(grams_per_ml, units, grams_per_ml, skip=volume_units - {"ml", "floz"})
    default = units[0].key if food.portions else "g"
    return Basis("usda", per, units, default)


def for_custom(mine):
    """One of My foods: nutrients per serving; g / oz from serving_grams, volumes from serving_ml."""
    per = {n: getattr(mine, n) for n in MACROS}
    grams = mine.serving_grams if mine.serving_grams else None
    ml = getattr(mine, "serving_ml", None) or None
    extra = [f"{_grams_text(grams)} g" if grams else "", f"{_grams_text(ml)} ml" if ml else ""]
    label = mine.serving_label or "1 serving"
    detail = ", ".join(x for x in extra if x)
    units = [Unit("serving", f"{label} ({detail})" if detail else label, Decimal(1), grams)]
    if grams:
        _weights(Decimal(1) / grams, units)
    if ml:
        _volumes(Decimal(1) / ml, units, grams / ml if grams else None)
    return Basis("custom", per, units, "serving")


def for_recipe(per_serving):
    return Basis("recipe", dict(per_serving), [Unit("serving", "1 serving", Decimal(1))], "serving")


def for_entry(entry):
    """An entry whose food is gone: scale what was logged."""
    per = {n: getattr(entry, n) for n in MACROS}
    units = [Unit("entry", f"as logged ({entry.portion_label})" if entry.portion_label else "as logged", Decimal(1), entry.grams)]
    if entry.grams:
        _weights(Decimal(1) / entry.grams, units)
    return Basis("entry", per, units, "entry")


def basis_for_entry(entry):
    """The units an entry can be changed to - its food's, or (food gone, quick add) scaling what was logged."""
    if entry.source == FoodLogEntry.USDA and entry.food_id:
        return for_food(entry.food)
    if entry.source == FoodLogEntry.CUSTOM and entry.custom_food_id:
        return for_custom(entry.custom_food)
    if entry.source == FoodLogEntry.RECIPE and entry.recipe_id:
        from albright_meals.recipe_nutrition import per_serving
        amounts = per_serving(getattr(entry.recipe, "nutrition", None))
        if amounts:
            return for_recipe(amounts)
    return for_entry(entry)


def current(entry, basis):
    """(quantity, unit key) to start the panel at: what the entry has, else "1 as logged"."""
    if entry.quantity and entry.unit and basis.unit(entry.unit):
        return entry.quantity, basis.unit(entry.unit).key
    return Decimal(1), basis.units[0].key


def apply(entry, basis, quantity, key):
    """Sets the entry's amount (not saved) -> True, or False when the unit isn't one of the food's."""
    worked = basis.amount(quantity, key)
    if worked is None or (worked["grams"] and worked["grams"] > MAX_GRAMS):
        return False
    for n, v in worked["totals"].items():
        setattr(entry, n, v)
    if basis.kind == "entry":                                 # scaling what was logged: it becomes the new "as logged"
        entry.quantity, entry.unit = None, ""
    else:
        entry.quantity, entry.unit = quantity, worked["unit"].key
    entry.grams = worked["grams"] if worked["grams"] else (None if basis.kind != "entry" else entry.grams)
    entry.servings = worked["servings"]
    if basis.kind != "entry" or worked["unit"].key != "entry":
        entry.portion_label = worked["portion_label"]
    elif Decimal(quantity) != 1:                              # "½ × 1 cup" of what was logged as "1 cup"
        entry.portion_label = f"{show_quantity(quantity)} × {entry.portion_label or 'serving'}"[:60]
    return True
