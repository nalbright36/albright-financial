"""A recipe's ingredients: one shape for every one, and its category.

Every saved ingredient (Recipe.save runs normalize_list - the recipe form, imports, the Bot API, the assistant) is a
dict: {"original": the line as written (never lost), "quantity": "1.5" (the most to buy - a range's upper end),
"quantity_min": "1" (a range's lower end, else absent), "unit": "cup", "name": "chicken broth", "note": "low-sodium",
"category": "pantry"} - or, for a section line ("For the sauce:"), {"original", "name", "section": true}.
Older rows that kept the whole line in "name" ("2 lb beef stew meat, trimmed") are parsed again from their text
(importer.parse_ingredient); a row that's already split keeps its parts.

The category groups the recipe page, the meal quick view, the public share page and the grocery list (its aisle -
GroceryItem.aisle uses the same keys, plus "household"). categorize() reads the name with a keyword map, longest
phrase first, whole words, so "black pepper" is a spice while "poblano peppers" is produce, and "chicken broth" is
pantry, not meat. Someone can change it on the recipe's edit page (normalize never overwrites a category there);
suggest_with_ai() asks the person's connected AI assistant about the ones the map leaves as Other."""
import json
import re

CATEGORIES = [("meat", "Meat & seafood"), ("produce", "Produce"), ("dairy", "Dairy & eggs"),
              ("pantry", "Pantry & canned"), ("spices", "Spices & seasonings"), ("sauces", "Sauces & condiments"),
              ("baking", "Baking"), ("frozen", "Frozen"), ("beverages", "Beverages & alcohol"), ("other", "Other")]
LABELS = dict(CATEGORIES)
KEYS = [key for key, _label in CATEGORIES]
ORDER = {key: n for n, key in enumerate(KEYS)}
# The grocery list's aisles: the same, plus what isn't food (GroceryItem.aisle).
AISLES = CATEGORIES[:-1] + [("household", "Household"), ("other", "Other")]
AISLE_ORDER = {key: n for n, (key, _label) in enumerate(AISLES)}

# Comma-separated phrases. A plural ("s"/"es") matches too. The longest phrase in a name wins, so put the tricky
# ones in full: "black pepper" (spices) beats "pepper" (produce); "chicken broth" (pantry) beats "chicken" (meat).
_WORDS = {
    "meat": """beef, pork, chicken, turkey, bacon, sausage, ham, salmon, shrimp, prawn, fish, steak, lamb, veal, tilapia,
        cod, crab, lobster, scallop, rib, brisket, meatball, chorizo, prosciutto, pancetta, pepperoni, salami,
        ground beef, ground pork, ground turkey, chicken breast, chicken thigh, chicken wing, drumstick, stew meat,
        chuck roast, pork shoulder, pork loin, pork chop, sirloin, flank steak, skirt steak, hot dog, clam, mussel,
        anchovy, anchovies, halibut, mahi mahi, trout, catfish, duck, venison, bratwurst, kielbasa, tuna steak,
        rotisserie chicken, ahi tuna""",
    "produce": """onion, garlic, potato, potatoes, sweet potato, tomato, tomatoes, lettuce, spinach, carrot, celery,
        bell pepper, pepper, poblano, poblano pepper, jalapeno, jalapeño, serrano, habanero, chile, chili pepper,
        anaheim pepper, lemon, lime, avocado, cilantro, parsley, basil, mint, thyme, rosemary, dill, sage, chives,
        mushroom, broccoli, cauliflower, cucumber, zucchini, squash, butternut squash, berries, strawberries,
        blueberries, raspberries, grape, orange, ginger, fresh ginger, kale, cabbage, corn, corn on the cob,
        green bean, pea, snap pea, asparagus, scallion, green onion, shallot, arugula, peach, peaches, pear, melon,
        watermelon, pineapple, mango, mangoes, eggplant, radish, radishes, beet, apple, banana, herbs, fresh herbs,
        leek, fennel, bok choy, tomatillo, lemon juice, lime juice, lemon zest, lime zest, coleslaw mix, romaine,
        spring mix, sprouts, cherry tomatoes, red onion, yellow onion, white onion, garlic clove, cloves garlic""",
    "dairy": """milk, whole milk, cheese, butter, unsalted butter, salted butter, yogurt, greek yogurt, cream,
        heavy cream, heavy whipping cream, whipping cream, egg, sour cream, cream cheese, cheddar, mozzarella,
        parmesan, parmigiano, feta, half and half, half & half, cottage cheese, ricotta, whipped cream, buttermilk,
        monterey jack, pepper jack, swiss, provolone, gouda, goat cheese, queso fresco, cotija, mascarpone, ghee,
        egg yolk, egg white, shredded cheese, sliced cheese, american cheese, colby jack, oat milk, almond milk""",
    "pantry": """rice, pasta, spaghetti, penne, noodle, egg noodle, macaroni, lasagna noodle, olive oil,
        extra virgin olive oil, vegetable oil, oil, canola oil, coconut oil, sesame oil, avocado oil, vinegar,
        apple cider vinegar, red wine vinegar, white wine vinegar, white vinegar, balsamic vinegar, rice vinegar,
        bean, black bean, kidney bean, pinto bean, white bean, cannellini bean, refried beans, chickpea, lentil,
        tomato paste, tomato sauce, canned tomatoes, diced tomatoes, crushed tomatoes, fire roasted tomatoes,
        whole tomatoes, broth, stock, chicken broth, chicken stock, beef broth, beef stock, vegetable broth,
        vegetable stock, bone broth, bouillon, chicken bouillon, cereal, oats, rolled oats, oatmeal, honey,
        maple syrup, peanut butter, almond butter, jam, jelly, nut, almond, walnut, pecan, peanut, cashew, cracker,
        chip, tortilla chip, tortilla strip, quinoa, couscous, breadcrumbs, bread crumbs, italian bread crumbs, panko,
        tortilla, flour tortilla, corn tortilla, bread, bun, hamburger bun, brioche, roll, pita, naan, bagel, crouton,
        coconut milk, tomato juice, cream of chicken soup, cream of mushroom soup, condensed soup, canned corn, raisin,
        dried cranberries, chex, rice chex, pretzel, popcorn, green chiles, diced green chiles, olive, caper, pickle,
        sun dried tomatoes, roasted red peppers, marshmallow, canned tuna, tuna, peanut butter chips, gnocchi,
        potato gnocchi, orzo, cornflakes, corn flakes, matzah meal, matzo meal, ramen, rice noodles, farro, barley,
        dried pasta, lasagna, tortellini, ravioli""",
    "spices": """salt, kosher salt, sea salt, table salt, flaky salt, black pepper, ground black pepper,
        freshly ground pepper, freshly ground black pepper, ground pepper, white pepper, red pepper flakes,
        crushed red pepper, pepper flakes, cayenne, cayenne pepper, cumin, ground cumin, chili powder, paprika,
        smoked paprika, oregano, dried oregano, cinnamon, ground cinnamon, nutmeg, ground cloves, whole cloves,
        allspice, turmeric, curry powder, garam masala, coriander, garlic powder, onion powder, garlic salt,
        onion salt, italian seasoning, taco seasoning, cajun seasoning, creole seasoning, bay leaf, bay leaves,
        dried thyme, dried basil, dried parsley, dried rosemary, seasoning, seasoned salt, everything bagel seasoning,
        ground ginger, chipotle powder, ancho chili powder, chipotle chili powder, mustard powder, dry mustard,
        celery seed, fennel seed, sesame seed, salt and pepper, salt & pepper, peppercorn, star anise, cardamom,
        steak seasoning, old bay, lemon pepper, pumpkin pie spice, msg""",
    "sauces": """salsa, adobo, adobo sauce, chipotle in adobo, chipotles in adobo, chipotle peppers in adobo,
        chipotle pepper in adobo sauce, hot sauce, soy sauce, tamari, worcestershire, worcestershire sauce, ketchup,
        mustard, dijon, dijon mustard, yellow mustard, whole grain mustard, mayo, mayonnaise, bbq sauce,
        barbecue sauce, sriracha, fish sauce, oyster sauce, hoisin, hoisin sauce, teriyaki, teriyaki sauce,
        enchilada sauce, pesto, marinara, marinara sauce, ranch, ranch dressing, italian dressing, dressing,
        vinaigrette, buffalo sauce, tahini, gochujang, chili garlic sauce, sweet chili sauce, relish, salsa verde,
        pasta sauce, pizza sauce, alfredo sauce, tabasco, sauce, steak sauce, cocktail sauce, tartar sauce,
        sambal, harissa, chimichurri, liquid smoke""",
    "baking": """flour, all purpose flour, all-purpose flour, bread flour, whole wheat flour, self rising flour,
        almond flour, sugar, brown sugar, light brown sugar, dark brown sugar, powdered sugar, granulated sugar,
        white sugar, confectioners sugar, baking soda, baking powder, vanilla, vanilla extract, pure vanilla extract,
        yeast, active dry yeast, instant yeast, cornstarch, cornmeal, cocoa, cocoa powder, chocolate chip,
        semisweet chocolate chips, chocolate, white chocolate chip, dark chocolate, caramel, caramel bit, sprinkle,
        shortening, molasses, corn syrup, food coloring, almond extract, gelatin, pie crust, cake mix,
        sweetened condensed milk, condensed milk, evaporated milk, graham cracker, butterscotch chip, cream of tartar, meringue powder""",
    "frozen": """frozen, ice cream, tater tot, ice""",
    "beverages": """beer, lager, stout, ale, wine, red wine, white wine, dry white wine, dry red wine, vodka, rum,
        tequila, bourbon, whiskey, brandy, sake, mirin, juice, orange juice, apple juice, cranberry juice,
        pineapple juice, soda, cola, club soda, sparkling water, seltzer, coffee, brewed coffee, espresso, tea,
        lemonade, cider, apple cider, apple cider mix, drink mix, triple sec, vermouth, sherry, champagne,
        prosecco, ginger ale, ginger beer""",
}


def _build():
    rows = []
    for key, text in _WORDS.items():
        for phrase in text.split(","):
            phrase = " ".join(phrase.lower().split())
            if phrase:
                rows.append((phrase, key))
    rows.sort(key=lambda pair: -len(pair[0]))
    return [(re.compile(rf"(?<![a-z]){re.escape(phrase)}(?:e?s)?(?![a-z])"), key) for phrase, key in rows]


_TABLE = _build()


def _clean(name):
    return " ".join(re.sub(r"[^a-z0-9&ñé ]+", " ", (name or "").lower().replace("-", " ")).split())


def categorize(name):
    """The category key for an ingredient's name ("other" when nothing matches): the longest phrase found anywhere in
    the name wins (the table is longest first), so "chicken broth" is pantry and "black pepper" a spice."""
    text = _clean(name)
    text = re.sub(r"^(?:juice|zest|zest and juice) of (?:half |1/2 )?(?:an? |one |the )?", "", text)   # "Juice of half a lime"
    if not text:
        return "other"
    for pattern, key in _TABLE:
        if pattern.search(text):
            return key
    return "other"


def aisle_for(name):
    """The grocery list's aisle for a line: its category - or Household for what isn't food."""
    from .groceries import household
    return "household" if household(name) else categorize(name)


# ---------------------------------------------------------------------------
# One shape for every ingredient
# ---------------------------------------------------------------------------

_LEADS_WITH_AMOUNT = re.compile(r"^\s*(?:\d|[½⅓⅔¼¾⅛⅜⅝⅞⅕⅖⅗⅘⅙⅚]|\.\d)")


def _line_of(ingredient):
    """The text an ingredient was written as."""
    if isinstance(ingredient, str):
        return ingredient.strip()
    if ingredient.get("original"):
        return str(ingredient["original"]).strip()
    text = " ".join(str(ingredient.get(k) or "").strip() for k in ("quantity", "unit", "name")).strip()
    text = " ".join(text.split())
    note = str(ingredient.get("note") or "").strip()
    return f"{text}, {note}" if note else text


def normalize(ingredient):
    """One ingredient (a dict or a plain line) -> the stored shape. Idempotent; keeps a category someone chose."""
    from . import importer
    from .groceries import is_heading
    if isinstance(ingredient, str) or not isinstance(ingredient, dict):
        line = str(ingredient or "").strip()
        out = {"original": line}
        given = {}
    else:
        given = ingredient
        line = _line_of(ingredient)
        out = {k: v for k, v in ingredient.items()}
        out.setdefault("original", line)
    if not line:
        return None
    no_amount = not str(out.get("quantity") or "").strip() and not str(out.get("unit") or "").strip()
    if is_heading(line) and no_amount:
        return {"original": line, "name": line.lstrip("#").strip().rstrip(":").strip(), "section": True}
    name = str(out.get("name") or "").strip()
    # Never split (a plain line), or the whole line ended up in the name ("1 tsp cinnamon", "Salt and pepper to taste").
    if not given or (no_amount and (not name or _LEADS_WITH_AMOUNT.match(name) or _TASTE.search(name)
                                    or ("," in name and not out.get("note")))):
        parsed = importer.parse_ingredient(out["original"])
        for key in ("name", "quantity", "quantity_min", "unit", "note"):
            if parsed.get(key) not in (None, ""):
                out[key] = parsed[key]
            elif key == "quantity_min":
                out.pop(key, None)
    for key in ("name", "quantity", "unit", "note"):
        out[key] = str(out.get(key) or "").strip()
    out.pop("section", None)
    if out.get("category") not in ORDER:
        out["category"] = categorize(out["name"])
    return out


_TASTE = re.compile(r"(?i)[ ,]+to taste$")


def normalize_list(ingredients):
    out = []
    for ingredient in ingredients or []:
        row = normalize(ingredient)
        if row is not None:
            out.append(row)
    return out


def has_sections(ingredients):
    return any(isinstance(i, dict) and i.get("section") for i in ingredients or [])


# ---------------------------------------------------------------------------
# Grouping for display
# ---------------------------------------------------------------------------

BY_CATEGORY, BY_RECIPE = "category", "recipe"


def group_rows(rows, mode):
    """Display rows (meal_view.ingredient_rows) -> [{"label": "Produce" or a section's name or "", "category": key,
    "rows": [...]}]. By category: the categories in order, the recipe's order kept inside each; section lines
    dropped. By recipe: one group per section (the first one unlabeled)."""
    if mode == BY_CATEGORY:
        buckets = {}
        for row in rows:
            if row.get("section"):
                continue
            buckets.setdefault(row.get("category") or "other", []).append(row)
        return [{"label": LABELS[key], "category": key, "rows": buckets[key]} for key in KEYS if key in buckets]
    groups = [{"label": "", "category": "", "rows": []}]
    for row in rows:
        if row.get("section"):
            groups.append({"label": row["section"], "category": "", "rows": []})
        else:
            groups[-1]["rows"].append(row)
    return [g for g in groups if g["rows"] or g["label"]]


def mode_for(user, ingredients):
    """The person's "Group by" choice (UserPreference.ingredient_grouping); never chosen: by category - except a recipe
    that has its own sections ("For the sauce"), which keeps them."""
    choice = ""
    if user is not None and getattr(user, "is_authenticated", False):
        from albright_labs.models import UserPreference
        choice = UserPreference.for_user(user).ingredient_grouping or ""
    if choice in (BY_CATEGORY, BY_RECIPE):
        return choice
    return BY_RECIPE if has_sections(ingredients) else BY_CATEGORY


# ---------------------------------------------------------------------------
# The person's AI assistant, for what the map can't place
# ---------------------------------------------------------------------------

AI_LIMIT = 40


def ai_available(user):
    from albright_assistant import access
    conn = access.connection(user) if access.allowed(user) else None
    return conn if conn is not None and not conn.disabled and conn.model else None


def suggest_with_ai(user, names):
    """{name: category key} for these names, from the person's connected AI assistant (their key, their model) - {}
    when it isn't connected or doesn't answer. Only keys from CATEGORIES come back."""
    conn = ai_available(user)
    names = [n for n in dict.fromkeys(str(n).strip() for n in names) if n][:AI_LIMIT]
    if conn is None or not names:
        return {}
    from albright_assistant import access, providers
    system = ("You sort grocery ingredients into store categories. Answer with JSON only: an object mapping each "
              "ingredient exactly as given to one of these keys: " + ", ".join(KEYS) + ".")
    text = "Ingredients:\n" + "\n".join(f"- {n}" for n in names)
    try:
        reply = providers.get(conn.provider).chat(access.decrypt(conn.key_encrypted), conn.model, system,
                                                  [{"role": "user", "text": text}], [], timeout=20)
    except Exception:
        return {}
    match = re.search(r"\{.*\}", reply.text or "", re.S)
    try:
        data = json.loads(match.group(0)) if match else {}
    except ValueError:
        return {}
    return {name: data[name] for name in names if isinstance(data.get(name), str) and data[name] in ORDER}
