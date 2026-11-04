"""Albright Meals: each household's meal plan, recipes, meal ideas and grocery list (albright_labs.Household).
Everything here belongs to a household and only its members see it (scope.py - anything else is a 404); someone
with Meals access but no household is asked to set one up. Recipes can also be shared beyond the household:
read-only in the "Recipe library" (visibility), or with one person at a time (RecipeShare) - either way the
recipe's plan, grocery list, ideas and ratings stay the household's.

The Grok "Recipe Bot" writes here too, through the Bot API (meals:write).
Rows it creates have from_bot set (and the token that wrote them, while it
exists) and are labeled "Recipe Bot" on the site.

Recipes can also be imported from a link (importer.py): the page's own
recipe data, or - when it has none - the AI reading the page's text, which
marks the recipe "needs review".
"""
from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models

BOT_LABEL = "Recipe Bot"

AISLES = [("produce", "Produce"), ("meat", "Meat & seafood"), ("dairy", "Dairy"), ("bakery", "Bakery"),
          ("pantry", "Pantry"), ("frozen", "Frozen"), ("household", "Household"), ("other", "Other")]
SLOTS = [("breakfast", "Breakfast"), ("lunch", "Lunch"), ("dinner", "Dinner"), ("snack", "Snack")]


class _Authored(models.Model):
    """Who added a row: a signed-in user, or the Recipe Bot (Bot API)."""
    AUTHOR_FIELD = "created_by"
    from_bot = models.BooleanField(default=False)
    bot_token = models.ForeignKey("albright_bot_api.BotToken", on_delete=models.SET_NULL, null=True, blank=True,
                                  related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True

    @property
    def author_label(self):
        if self.from_bot:
            return BOT_LABEL
        user = getattr(self, self.AUTHOR_FIELD)
        return (user.first_name or user.get_username()) if user else ""


def _household_fk():
    return models.ForeignKey("albright_labs.Household", on_delete=models.CASCADE, related_name="+")


def _user_fk(related_name):
    return models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                             related_name=related_name)


# The shared recipe tags (Recipe.standard_tags) - a fixed set, separate from ratings and from the free-form `tags`
# that imports and the Recipe Bot write.
STANDARD_TAGS = [("quick_weeknight", "Quick weeknight"), ("good_leftovers", "Good leftovers"),
                 ("meal_prep", "Meal-prep friendly"), ("special_occasion", "Special occasion"),
                 ("company", "Company-worthy"), ("kid_friendly", "Kid-friendly"), ("budget", "Budget-friendly")]


class Recipe(_Authored):
    HOUSEHOLD, LIBRARY = "household", "library"
    VISIBILITY = [(HOUSEHOLD, "Household"), (LIBRARY, "Shared library")]
    household = _household_fk()
    # "Shared library": every Meals user can read it (and save a copy); only its household can change it.
    visibility = models.CharField(max_length=10, choices=VISIBILITY, default=HOUSEHOLD, db_index=True)
    # A copy saved from someone else's recipe ("Save to my recipes"): credited "Saved from <household name>".
    saved_from = models.ForeignKey("self", on_delete=models.SET_NULL, null=True, blank=True, related_name="copies")
    saved_from_name = models.CharField(max_length=100, blank=True)
    title = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    # [{"name": "onion", "quantity": "1", "unit": "", "note": "diced", "original": "1 onion, diced"}, ...]
    # ("original" - the line exactly as written - when it came from a link or the recipe form)
    ingredients = models.JSONField(default=list, blank=True)
    # ["Preheat the oven...", ...]; "## Sauce" starts a section headed "Sauce".
    steps = models.JSONField(default=list, blank=True)
    prep_minutes = models.PositiveIntegerField(null=True, blank=True)
    cook_minutes = models.PositiveIntegerField(null=True, blank=True)
    total_time_minutes = models.PositiveIntegerField(null=True, blank=True,
                                                     help_text="As the source states it; else prep + cook.")
    servings = models.PositiveSmallIntegerField(null=True, blank=True)
    tags = models.JSONField(default=list, blank=True)           # ["quick", "chicken"]
    standard_tags = models.JSONField(default=list, blank=True)  # keys of STANDARD_TAGS, any number
    source_url = models.URLField(max_length=500, blank=True)
    # source_url normalized (importer.normalize_url) - how a link that's already saved is recognized.
    source_key = models.CharField(max_length=500, blank=True, db_index=True, editable=False)
    source_name = models.CharField(max_length=120, blank=True)   # "Serious Eats" - the credit line
    author = models.CharField(max_length=200, blank=True)
    image_url = models.URLField(max_length=500, blank=True)
    notes = models.TextField(blank=True)
    needs_review = models.BooleanField(default=False, help_text="Read from the page's text by the AI - check it.")
    imported_at = models.DateTimeField(null=True, blank=True)
    created_by = _user_fk("meal_recipes")

    class Meta:
        ordering = ["title", "pk"]

    def __str__(self):
        return self.title

    def save(self, *args, **kwargs):
        from .importer import normalize_url
        self.source_key = normalize_url(self.source_url)[:500] if self.source_url else ""
        if kwargs.get("update_fields") is not None and "source_url" in kwargs["update_fields"]:
            kwargs["update_fields"] = {*kwargs["update_fields"], "source_key"}
        super().save(*args, **kwargs)

    @property
    def total_minutes(self):
        total = (self.prep_minutes or 0) + (self.cook_minutes or 0)
        return self.total_time_minutes or total or None


class RecipeRating(models.Model):
    """One person's "stamp" on a recipe - a text level, not a number. Stored as an ordered integer (1-6) so it
    sorts and filters; re-rating updates the same row (one per recipe and person)."""
    WONT_EAT, NOT_FOR_ME, NEEDS_TWEAKS, WILL_EAT, FAVORITE, STAPLE = 1, 2, 3, 4, 5, 6
    LEVELS = [(WONT_EAT, "Won't eat again"), (NOT_FOR_ME, "Not for me"), (NEEDS_TWEAKS, "Needs tweaks"),
              (WILL_EAT, "Will eat again"), (FAVORITE, "Favorite"), (STAPLE, "Staple")]
    # level -> (emoji, color token, what it means) - the badge and its tooltip / help text.
    INFO = {
        WONT_EAT: ("🚫", "red", "Didn't like it; skip it in future plans."),
        NOT_FOR_ME: ("😐", "muted", "Edible, but not worth making again."),
        NEEDS_TWEAKS: ("🔧", "amber", "Has potential; try again with changes."),
        WILL_EAT: ("👍", "neutral", "Good; happy to have it again sometime."),
        FAVORITE: ("❤️", "green", "Really loved it; make it often."),
        STAPLE: ("⭐", "gold", "Part of the regular rotation; our go-to."),
    }
    recipe = models.ForeignKey(Recipe, on_delete=models.CASCADE, related_name="ratings")
    # A person's own stamp - only their household's members ever see it (scope.household_ratings).
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="meal_ratings")
    level = models.PositiveSmallIntegerField(choices=LEVELS, validators=[MinValueValidator(1), MaxValueValidator(6)])
    # Optional - for "Needs tweaks" the form asks "What would you change?"
    note = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-level", "pk"]
        constraints = [
            models.UniqueConstraint(fields=["recipe", "user"], name="meals_one_rating_per_person"),
            models.CheckConstraint(condition=models.Q(level__gte=1, level__lte=6), name="meals_rating_level_1_to_6"),
        ]
        indexes = [models.Index(fields=["recipe", "level"])]

    def __str__(self):
        return f"{self.user} - {self.recipe}: {self.get_level_display()}"

    @classmethod
    def choices(cls):
        """[{"value", "label", "emoji", "color", "meaning"}] in order - the Rating drop-down."""
        return [{"value": value, "label": label, "emoji": cls.INFO[value][0], "color": cls.INFO[value][1],
                 "meaning": cls.INFO[value][2]} for value, label in cls.LEVELS]

    @property
    def emoji(self):
        return self.INFO[self.level][0]

    @property
    def color(self):
        return self.INFO[self.level][1]

    @property
    def meaning(self):
        return self.INFO[self.level][2]


class RecipeShare(models.Model):
    """One recipe shown, read-only, to one person outside its household (they can save a copy). seen_at: when they
    saw the "<name> shared <recipe> with you" notice."""
    recipe = models.ForeignKey(Recipe, on_delete=models.CASCADE, related_name="shares")
    shared_with = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="meals_shared_recipes")
    shared_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    seen_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-pk"]
        constraints = [models.UniqueConstraint(fields=["recipe", "shared_with"], name="meals_share_once")]


class MealPlanEntry(_Authored):
    """One meal in the plan: a day's breakfast/lunch/dinner/snack - a recipe, or just a title ("Leftovers")."""
    STATUSES = [("planned", "Planned"), ("cooked", "Cooked"), ("skipped", "Skipped")]
    household = _household_fk()
    date = models.DateField(db_index=True)
    slot = models.CharField(max_length=10, choices=SLOTS, default="dinner")
    recipe = models.ForeignKey(Recipe, on_delete=models.SET_NULL, null=True, blank=True, related_name="plan_entries")
    title = models.CharField(max_length=200, blank=True, help_text="Used when there's no recipe.")
    notes = models.TextField(blank=True)
    status = models.CharField(max_length=8, choices=STATUSES, default="planned")
    created_by = _user_fk("meal_plan_entries")

    class Meta:
        ordering = ["date", "slot", "pk"]
        constraints = [models.UniqueConstraint(fields=["household", "date", "slot"], name="meals_one_entry_per_slot")]

    def __str__(self):
        return f"{self.date} {self.slot}: {self.display_title}"

    @property
    def display_title(self):
        return self.recipe.title if self.recipe_id else (self.title or "Untitled meal")


class MealIdea(_Authored):
    """Something to try some day - moved into the plan, or declined."""
    AUTHOR_FIELD = "added_by"
    STATUSES = [("new", "New"), ("planned", "Planned"), ("declined", "Declined")]
    household = _household_fk()
    title = models.CharField(max_length=200)
    notes = models.TextField(blank=True)
    link = models.URLField(max_length=500, blank=True)
    status = models.CharField(max_length=8, choices=STATUSES, default="new", db_index=True)
    planned_entry = models.ForeignKey(MealPlanEntry, on_delete=models.SET_NULL, null=True, blank=True,
                                      related_name="+")
    added_by = _user_fk("meal_ideas")

    class Meta:
        ordering = ["-created_at", "-pk"]

    def __str__(self):
        return self.title


class ActiveGroceryQuerySet(models.QuerySet):
    def active(self):
        """On the list: not archived by "Clear checked items"."""
        return self.filter(archived_at__isnull=True)


class GroceryItem(_Authored):
    """One line on the shared grocery list. "Clear checked items" archives checked lines (archived_at) rather than
    deleting them, so a Planner task's checklist keeps showing what was bought; archived lines are off the list.
    duplicate_of links a line to an unchecked line for the same ingredient in a different unit ("1 lb" vs "2 cups")
    - shown as "Possible duplicate" with a Merge button."""
    AUTHOR_FIELD = "added_by"
    household = _household_fk()
    name = models.CharField(max_length=120)
    quantity = models.DecimalField(max_digits=10, decimal_places=3, null=True, blank=True)
    unit = models.CharField(max_length=20, blank=True)
    aisle = models.CharField(max_length=10, choices=AISLES, default="other")
    note = models.CharField(max_length=200, blank=True)
    checked = models.BooleanField(default=False, db_index=True)
    checked_by = _user_fk("+")
    checked_by_bot = models.BooleanField(default=False)
    checked_at = models.DateTimeField(null=True, blank=True)
    added_by = _user_fk("meal_grocery_items")
    from_recipe = models.ForeignKey(Recipe, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    # Every recipe this line's ingredients came from (merging adds to it).
    recipes = models.ManyToManyField(Recipe, blank=True, related_name="grocery_items")
    duplicate_of = models.ForeignKey("self", on_delete=models.SET_NULL, null=True, blank=True, related_name="duplicates")
    archived_at = models.DateTimeField(null=True, blank=True, db_index=True)

    objects = ActiveGroceryQuerySet.as_manager()

    class Meta:
        ordering = ["aisle", "checked", "checked_at", "name", "pk"]

    def __str__(self):
        return self.name


class MealsSettings(models.Model):
    """A household's Meals settings: the "Tonight: <dinner>" line in the owner's daily Telegram digest."""
    household = models.OneToOneField("albright_labs.Household", on_delete=models.CASCADE, related_name="meals_settings")
    digest_tonight = models.BooleanField(default=True, verbose_name='"Tonight: <dinner>" in my daily digest')
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return "Meals settings"

    @classmethod
    def load(cls, household):
        return cls.objects.get_or_create(household=household)[0]


NUTRITION_FIELDS = ("calories", "protein", "carbs", "fat", "fiber")
_AMOUNT = {"max_digits": 9, "decimal_places": 2, "null": True, "blank": True}


class RecipeNutrition(models.Model):
    """A recipe's nutrition - it belongs to the recipe, so it's shared exactly as the recipe is (its household; the
    library or a share when the recipe is). Worked out by matching each ingredient line to a USDA food
    (albright_fitness.nutrition - the lines below), or typed in per serving (MANUAL). NEEDS_REVIEW until someone in
    the household confirms the matches. total_*: the whole recipe; serving_*: per serving (the recipe's servings, or
    the whole recipe as one serving when it has none)."""
    NEEDS_REVIEW, CONFIRMED, MANUAL = "review", "confirmed", "manual"
    STATUSES = [(NEEDS_REVIEW, "Needs review"), (CONFIRMED, "Confirmed"), (MANUAL, "Typed in")]
    recipe = models.OneToOneField(Recipe, on_delete=models.CASCADE, related_name="nutrition")
    status = models.CharField(max_length=9, choices=STATUSES, default=NEEDS_REVIEW)
    total_calories = models.DecimalField(**_AMOUNT)
    total_protein = models.DecimalField(**_AMOUNT)
    total_carbs = models.DecimalField(**_AMOUNT)
    total_fat = models.DecimalField(**_AMOUNT)
    total_fiber = models.DecimalField(**_AMOUNT)
    serving_calories = models.DecimalField(**_AMOUNT)
    serving_protein = models.DecimalField(**_AMOUNT)
    serving_carbs = models.DecimalField(**_AMOUNT)
    serving_fat = models.DecimalField(**_AMOUNT)
    serving_fiber = models.DecimalField(**_AMOUNT)
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Nutrition: {self.recipe}"

    @property
    def needs_review(self):
        return self.status == self.NEEDS_REVIEW


class RecipeNutritionLine(models.Model):
    """One ingredient line, matched to a USDA food and an amount in grams (or ignored - "salt to taste")."""
    nutrition = models.ForeignKey(RecipeNutrition, on_delete=models.CASCADE, related_name="lines")
    order = models.PositiveSmallIntegerField(default=0)
    ingredient = models.CharField(max_length=500)
    food = models.ForeignKey("albright_fitness.Food", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    grams = models.DecimalField(null=True, blank=True, max_digits=8, decimal_places=1, validators=[MinValueValidator(0)])
    ignored = models.BooleanField(default=False)
    calories = models.DecimalField(**_AMOUNT)
    protein = models.DecimalField(**_AMOUNT)
    carbs = models.DecimalField(**_AMOUNT)
    fat = models.DecimalField(**_AMOUNT)
    fiber = models.DecimalField(**_AMOUNT)

    class Meta:
        ordering = ["order", "pk"]

    def __str__(self):
        return self.ingredient
