"""Albright Meals: the household's meal plan, recipes, meal ideas and one
shared grocery list. Everything here is shared by everyone who can open the
Meals app (Admin > Users) - there's no per-user copy.

The Grok "Recipe Bot" writes here too, through the Bot API (meals:write).
Rows it creates have from_bot set (and the token that wrote them, while it
exists) and are labeled "Recipe Bot" on the site.
"""
from django.conf import settings
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


def _user_fk(related_name):
    return models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                             related_name=related_name)


class Recipe(_Authored):
    title = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    # [{"name": "onion", "quantity": "1", "unit": "", "note": "diced"}, ...]
    ingredients = models.JSONField(default=list, blank=True)
    steps = models.JSONField(default=list, blank=True)          # ["Preheat the oven...", ...]
    prep_minutes = models.PositiveIntegerField(null=True, blank=True)
    cook_minutes = models.PositiveIntegerField(null=True, blank=True)
    servings = models.PositiveSmallIntegerField(null=True, blank=True)
    tags = models.JSONField(default=list, blank=True)           # ["quick", "chicken"]
    source_url = models.URLField(max_length=500, blank=True)
    image_url = models.URLField(max_length=500, blank=True)
    notes = models.TextField(blank=True)
    created_by = _user_fk("meal_recipes")

    class Meta:
        ordering = ["title", "pk"]

    def __str__(self):
        return self.title

    @property
    def total_minutes(self):
        total = (self.prep_minutes or 0) + (self.cook_minutes or 0)
        return total or None


class MealPlanEntry(_Authored):
    """One meal in the plan: a day's breakfast/lunch/dinner/snack - a recipe, or just a title ("Leftovers")."""
    STATUSES = [("planned", "Planned"), ("cooked", "Cooked"), ("skipped", "Skipped")]
    date = models.DateField(db_index=True)
    slot = models.CharField(max_length=10, choices=SLOTS, default="dinner")
    recipe = models.ForeignKey(Recipe, on_delete=models.SET_NULL, null=True, blank=True, related_name="plan_entries")
    title = models.CharField(max_length=200, blank=True, help_text="Used when there's no recipe.")
    notes = models.TextField(blank=True)
    status = models.CharField(max_length=8, choices=STATUSES, default="planned")
    created_by = _user_fk("meal_plan_entries")

    class Meta:
        ordering = ["date", "slot", "pk"]
        constraints = [models.UniqueConstraint(fields=["date", "slot"], name="meals_one_entry_per_slot")]

    def __str__(self):
        return f"{self.date} {self.slot}: {self.display_title}"

    @property
    def display_title(self):
        return self.recipe.title if self.recipe_id else (self.title or "Untitled meal")


class MealIdea(_Authored):
    """Something to try some day - moved into the plan, or declined."""
    AUTHOR_FIELD = "added_by"
    STATUSES = [("new", "New"), ("planned", "Planned"), ("declined", "Declined")]
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


class GroceryItem(_Authored):
    """One line on the shared grocery list."""
    AUTHOR_FIELD = "added_by"
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

    class Meta:
        ordering = ["aisle", "checked", "checked_at", "name", "pk"]

    def __str__(self):
        return self.name


class MealsSettings(models.Model):
    """Meals' own settings (one row): the "Tonight: <dinner>" line in the owner's daily Telegram digest."""
    SINGLETON_PK = 1
    digest_tonight = models.BooleanField(default=True, verbose_name='"Tonight: <dinner>" in my daily digest')
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return "Meals settings"

    @classmethod
    def load(cls):
        return cls.objects.get_or_create(pk=cls.SINGLETON_PK)[0]
