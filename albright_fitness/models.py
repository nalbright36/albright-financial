"""Albright Fitness: the exercise library, logged workouts (exercises and sets), personal records and programs.

Privacy: everything is its owner's. A workout - and nothing else - can be shared with a person or a circle, to view
or edit (albright_labs.sharing); never programs, records, the food diary, targets or body data. Anyone else gets a 404 (views look everything up
through the querysets below).

Built-in exercises have no owner (seeded by migration 0002); a person's own ones are theirs alone. Built-in
programs aren't rows: they're templates in programs.py that a person copies into their own Program.

Nutrition (Phase 2): Food is a shared cache of USDA FoodData Central foods (public data - usda.py fills it). Everything
else is private to its owner, even within a circle: NutritionProfile (body info, units, macro targets), CustomFood
("My foods") and FoodLogEntry (the diary). A recipe's nutrition lives with the recipe (albright_meals.RecipeNutrition).

Body metrics (Phase 3): WeightEntry and Measurement - private to their owner, never shared with anyone.
"""
from decimal import Decimal

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import Q

MUSCLES = [("chest", "Chest"), ("back", "Back"), ("shoulders", "Shoulders"), ("biceps", "Biceps"),
           ("triceps", "Triceps"), ("forearms", "Forearms"), ("core", "Core"), ("quads", "Quads"),
           ("hamstrings", "Hamstrings"), ("glutes", "Glutes"), ("calves", "Calves"), ("full_body", "Full body"),
           ("cardio", "Cardio")]
LOWER_BODY = {"quads", "hamstrings", "glutes", "calves"}
EQUIPMENT = [("barbell", "Barbell"), ("dumbbell", "Dumbbell"), ("machine", "Machine"), ("cable", "Cable"),
             ("kettlebell", "Kettlebell"), ("bodyweight", "Bodyweight"), ("band", "Band"),
             ("cardio_machine", "Cardio machine"), ("none", "None / outdoors"), ("other", "Other")]
LB, KG = "lb", "kg"
UNITS = [(LB, "lb"), (KG, "kg")]
MI, KM = "mi", "km"
DISTANCE_UNITS = [(MI, "mi"), (KM, "km")]
KG_TO_LB = Decimal("2.20462")
MONEYISH = {"max_digits": 7, "decimal_places": 2}


class ExerciseQuerySet(models.QuerySet):
    def visible_to(self, user):
        """The built-in library plus the user's own exercises."""
        return self.filter(Q(owner__isnull=True) | Q(owner=user))


class Exercise(models.Model):
    STRENGTH, CARDIO, BODYWEIGHT, MOBILITY = "strength", "cardio", "bodyweight", "mobility"
    KINDS = [(STRENGTH, "Strength"), (CARDIO, "Cardio"), (BODYWEIGHT, "Bodyweight"), (MOBILITY, "Mobility")]
    name = models.CharField(max_length=100)
    kind = models.CharField(max_length=10, choices=KINDS, default=STRENGTH)
    primary_muscles = models.JSONField(default=list, blank=True)     # MUSCLES keys
    secondary_muscles = models.JSONField(default=list, blank=True)
    equipment = models.CharField(max_length=20, choices=EQUIPMENT, default="other")
    instructions = models.TextField(blank=True, max_length=4000)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, null=True, blank=True,
                              related_name="fitness_exercises", help_text="Empty: built in, for everyone.")
    created_at = models.DateTimeField(auto_now_add=True)

    objects = ExerciseQuerySet.as_manager()

    class Meta:
        ordering = ["name", "pk"]
        constraints = [
            models.UniqueConstraint(fields=["name"], condition=Q(owner__isnull=True), name="fitness_builtin_name_unique"),
            models.UniqueConstraint(fields=["owner", "name"], condition=Q(owner__isnull=False), name="fitness_own_name_unique"),
        ]

    def __str__(self):
        return self.name

    @property
    def is_cardio(self):
        return self.kind == self.CARDIO

    @property
    def is_lower_body(self):
        """Progression steps are bigger for the legs (5 lb) than the upper body (2.5 lb)."""
        return bool(set(self.primary_muscles or []) & LOWER_BODY)


class Program(models.Model):
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="fitness_programs")
    name = models.CharField(max_length=100)
    weeks = models.PositiveSmallIntegerField(null=True, blank=True, validators=[MinValueValidator(1), MaxValueValidator(104)])
    active = models.BooleanField(default=True)
    # Set while it's on the Planner ("Schedule this program") - the first day it was scheduled from.
    scheduled_from = models.DateField(null=True, blank=True)
    template = models.CharField(max_length=40, blank=True)          # the built-in template it was copied from
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-active", "name", "pk"]

    def __str__(self):
        return self.name


class ProgramDay(models.Model):
    program = models.ForeignKey(Program, on_delete=models.CASCADE, related_name="days")
    order = models.PositiveSmallIntegerField(default=0)
    name = models.CharField(max_length=60)                          # "Upper A"
    weekdays = models.JSONField(default=list, blank=True)           # 0 = Monday ... 6 = Sunday

    class Meta:
        ordering = ["order", "pk"]

    def __str__(self):
        return f"{self.program.name}: {self.name}"


class ProgramExercise(models.Model):
    """One exercise on a program day, with its target: sets x reps, or a time / distance for cardio."""
    day = models.ForeignKey(ProgramDay, on_delete=models.CASCADE, related_name="exercises")
    order = models.PositiveSmallIntegerField(default=0)
    exercise = models.ForeignKey(Exercise, on_delete=models.CASCADE, related_name="+")
    target_sets = models.PositiveSmallIntegerField(default=3, validators=[MinValueValidator(1), MaxValueValidator(20)])
    target_reps = models.PositiveSmallIntegerField(null=True, blank=True, validators=[MaxValueValidator(100)])
    target_seconds = models.PositiveIntegerField(null=True, blank=True)
    target_distance = models.DecimalField(null=True, blank=True, max_digits=6, decimal_places=2)

    class Meta:
        ordering = ["order", "pk"]

    @property
    def target_text(self):
        """"3 × 5", "20 min", "3.1 mi"."""
        if self.target_distance:
            return f"{self.target_distance.normalize():f} {MI}"
        if self.target_seconds:
            minutes, seconds = divmod(self.target_seconds, 60)
            return f"{minutes} min" if not seconds else f"{minutes}:{seconds:02d}"
        return f"{self.target_sets} × {self.target_reps}" if self.target_reps else f"{self.target_sets} sets"


class WorkoutQuerySet(models.QuerySet):
    def visible_to(self, user):
        """Mine, and ones shared with me (a person or a circle - albright_labs.sharing)."""
        from albright_labs import sharing
        return sharing.visible_queryset(user, Workout, self)


class Workout(models.Model):
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="fitness_workouts")
    date = models.DateField()
    start_time = models.TimeField(null=True, blank=True)
    end_time = models.TimeField(null=True, blank=True)
    name = models.CharField(max_length=100, blank=True)
    notes = models.TextField(blank=True, max_length=4000)
    program_day = models.ForeignKey(ProgramDay, on_delete=models.SET_NULL, null=True, blank=True, related_name="workouts")
    # Started from a Planner task: finishing the workout checks off that occurrence (planner.toggle).
    planner_item = models.ForeignKey("albright_planner.ScheduleItem", on_delete=models.SET_NULL, null=True, blank=True,
                                     related_name="+")
    planner_date = models.DateField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = WorkoutQuerySet.as_manager()

    class Meta:
        ordering = ["-date", "-pk"]
        indexes = [models.Index(fields=["owner", "date"])]

    def __str__(self):
        return f"{self.date} {self.title}"

    @property
    def title(self):
        return self.name or (self.program_day.name if self.program_day_id else "Workout")

    @property
    def finished(self):
        return self.finished_at is not None




class WorkoutExercise(models.Model):
    workout = models.ForeignKey(Workout, on_delete=models.CASCADE, related_name="exercises")
    order = models.PositiveSmallIntegerField(default=0)
    exercise = models.ForeignKey(Exercise, on_delete=models.PROTECT, related_name="+")
    notes = models.CharField(max_length=500, blank=True)
    # The program's target, when it came from a program day ("last time" checks reps against it).
    target_reps = models.PositiveSmallIntegerField(null=True, blank=True)
    target_sets = models.PositiveSmallIntegerField(null=True, blank=True)

    class Meta:
        ordering = ["order", "pk"]


class WorkoutSet(models.Model):
    WARMUP, WORKING, DROP = "warmup", "working", "drop"
    TYPES = [(WARMUP, "Warm-up"), (WORKING, "Working"), (DROP, "Drop")]
    workout_exercise = models.ForeignKey(WorkoutExercise, on_delete=models.CASCADE, related_name="sets")
    order = models.PositiveSmallIntegerField(default=0)
    reps = models.PositiveSmallIntegerField(null=True, blank=True, validators=[MaxValueValidator(1000)])
    weight = models.DecimalField(null=True, blank=True, validators=[MinValueValidator(0)], **MONEYISH)
    unit = models.CharField(max_length=2, choices=UNITS, default=LB)
    duration_seconds = models.PositiveIntegerField(null=True, blank=True)
    distance = models.DecimalField(null=True, blank=True, max_digits=7, decimal_places=2, validators=[MinValueValidator(0)])
    distance_unit = models.CharField(max_length=2, choices=DISTANCE_UNITS, default=MI)
    rpe = models.DecimalField(null=True, blank=True, max_digits=3, decimal_places=1,
                              validators=[MinValueValidator(1), MaxValueValidator(10)])
    set_type = models.CharField(max_length=7, choices=TYPES, default=WORKING)
    done = models.BooleanField(default=False)

    class Meta:
        ordering = ["order", "pk"]

    @property
    def weight_lb(self):
        if self.weight is None:
            return None
        return self.weight if self.unit == LB else (self.weight * KG_TO_LB).quantize(Decimal("0.01"))

    @property
    def miles(self):
        if self.distance is None:
            return None
        return self.distance if self.distance_unit == MI else (self.distance / Decimal("1.609344")).quantize(Decimal("0.01"))


class PersonalRecord(models.Model):
    """A new best, as it happened (kept as history - the current record is the latest per kind)."""
    WEIGHT, E1RM, REPS, DISTANCE, DURATION, PACE = "weight", "e1rm", "reps", "distance", "duration", "pace"
    KINDS = [(WEIGHT, "Heaviest weight"), (E1RM, "Best estimated 1-rep max"), (REPS, "Most reps at a weight"),
             (DISTANCE, "Longest distance"), (DURATION, "Longest time"), (PACE, "Fastest pace")]
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="fitness_records")
    exercise = models.ForeignKey(Exercise, on_delete=models.CASCADE, related_name="+")
    kind = models.CharField(max_length=10, choices=KINDS)
    # weight / e1rm: pounds; reps: a count (at `at_weight` lb); distance: miles; duration: seconds; pace: seconds/mile.
    value = models.DecimalField(max_digits=10, decimal_places=2)
    previous = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    at_weight = models.DecimalField(null=True, blank=True, **MONEYISH)
    workout = models.ForeignKey(Workout, on_delete=models.CASCADE, related_name="records")
    achieved_on = models.DateField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-achieved_on", "-pk"]
        indexes = [models.Index(fields=["owner", "exercise", "kind"])]


# ---------------------------------------------------------------------------
# Nutrition: the USDA food cache, targets, My foods, the diary
# ---------------------------------------------------------------------------

NUTRIENTS = ("calories", "protein", "carbs", "fat", "fiber", "sugar", "sodium")    # sodium in mg, the rest g / kcal
MACROS = ("calories", "protein", "carbs", "fat", "fiber")
AMOUNT = {"max_digits": 9, "decimal_places": 2, "null": True, "blank": True}


class Food(models.Model):
    """A food from USDA FoodData Central, cached so a repeat lookup never calls the API. Nutrients are per 100 g
    (empty: USDA doesn't list it). portions: [{"label": "1 cup", "grams": 240.0}, ...]. detailed: the full record
    (with household portions) has been fetched - a search result alone has nutrients but often no portions."""
    fdc_id = models.PositiveIntegerField(unique=True)
    name = models.CharField(max_length=255)
    brand = models.CharField(max_length=120, blank=True)
    data_type = models.CharField(max_length=30, blank=True)
    calories = models.DecimalField(**AMOUNT)   # per 100 g (sodium mg)
    protein = models.DecimalField(**AMOUNT)
    carbs = models.DecimalField(**AMOUNT)
    fat = models.DecimalField(**AMOUNT)
    fiber = models.DecimalField(**AMOUNT)
    sugar = models.DecimalField(**AMOUNT)
    sodium = models.DecimalField(**AMOUNT)
    portions = models.JSONField(default=list, blank=True)
    detailed = models.BooleanField(default=False)
    fetched_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name", "pk"]

    def __str__(self):
        return f"{self.name} ({self.brand})" if self.brand else self.name


class NutritionProfile(models.Model):
    """One person's units, body info (for the calculator) and macro targets - theirs alone. tracked: which of
    MACROS the diary and dashboard show (e.g. protein only, calories hidden)."""
    MALE, FEMALE = "male", "female"
    SEXES = [(MALE, "Male"), (FEMALE, "Female")]
    ACTIVITY = [("sedentary", "Sedentary (desk job, little exercise)"), ("light", "Light (exercise 1-3 days a week)"),
                ("moderate", "Moderate (exercise 3-5 days a week)"), ("active", "Active (hard exercise 6-7 days a week)"),
                ("very", "Very active (physical job plus training)")]
    MAINTAIN, LOSE, GAIN = "maintain", "lose", "gain"
    GOALS = [(MAINTAIN, "Maintain"), (LOSE, "Slow loss (~0.5 lb a week)"), (GAIN, "Slow gain (~0.5 lb a week)")]
    IN, CM = "in", "cm"
    HEIGHT_UNITS = [(IN, "in"), (CM, "cm")]
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="fitness_nutrition")
    weight_unit = models.CharField(max_length=2, choices=UNITS, default=LB)
    height_unit = models.CharField(max_length=2, choices=HEIGHT_UNITS, default=IN)
    sex = models.CharField(max_length=6, choices=SEXES, blank=True)
    age = models.PositiveSmallIntegerField(null=True, blank=True, validators=[MinValueValidator(13), MaxValueValidator(110)])
    height_cm = models.DecimalField(null=True, blank=True, max_digits=5, decimal_places=1,
                                    validators=[MinValueValidator(90), MaxValueValidator(250)])
    weight_kg = models.DecimalField(null=True, blank=True, max_digits=5, decimal_places=1,
                                    validators=[MinValueValidator(30), MaxValueValidator(350)])
    activity = models.CharField(max_length=10, choices=ACTIVITY, default="light")
    goal = models.CharField(max_length=8, choices=GOALS, default=MAINTAIN)
    protein_per_lb = models.DecimalField(max_digits=3, decimal_places=2, default=Decimal("0.8"),
                                         validators=[MinValueValidator(Decimal("0.7")), MaxValueValidator(Decimal("1.0"))])
    target_calories = models.PositiveIntegerField(null=True, blank=True, validators=[MaxValueValidator(10000)])
    target_protein = models.PositiveIntegerField(null=True, blank=True, validators=[MaxValueValidator(1000)])
    target_carbs = models.PositiveIntegerField(null=True, blank=True, validators=[MaxValueValidator(2000)])
    target_fat = models.PositiveIntegerField(null=True, blank=True, validators=[MaxValueValidator(1000)])
    target_fiber = models.PositiveIntegerField(null=True, blank=True, validators=[MaxValueValidator(300)])
    tracked = models.JSONField(default=list, blank=True)
    # Body metrics (Phase 3): a goal weight (for a projected date), hiding weight from the dashboard, and an
    # optional weekly weigh-in reminder on the Planner (off by default) on weigh_in_weekday (0 = Monday).
    goal_weight_kg = models.DecimalField(null=True, blank=True, max_digits=5, decimal_places=1,
                                         validators=[MinValueValidator(30), MaxValueValidator(350)])
    hide_weight = models.BooleanField(default=False)
    weigh_in_reminder = models.BooleanField(default=False)
    weigh_in_weekday = models.PositiveSmallIntegerField(default=0, validators=[MaxValueValidator(6)])
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Nutrition for {self.user}"

    @classmethod
    def load(cls, user):
        return cls.objects.get_or_create(user=user, defaults={"tracked": ["calories", "protein", "carbs", "fat"]})[0]

    def target(self, name):
        return getattr(self, f"target_{name}")


class CustomFood(models.Model):
    """One of "My foods": the person's own food with macros per serving. Theirs alone."""
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="fitness_foods")
    name = models.CharField(max_length=100)
    serving_label = models.CharField(max_length=40, default="1 serving")
    serving_grams = models.DecimalField(null=True, blank=True, max_digits=7, decimal_places=1, validators=[MinValueValidator(0)])
    calories = models.DecimalField(**AMOUNT)   # per serving
    protein = models.DecimalField(**AMOUNT)
    carbs = models.DecimalField(**AMOUNT)
    fat = models.DecimalField(**AMOUNT)
    fiber = models.DecimalField(**AMOUNT)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name", "pk"]

    def __str__(self):
        return self.name


class FoodLogEntry(models.Model):
    """One line in a person's food diary - theirs alone. The amounts are worked out when it's logged and kept
    (editing a food or recipe later doesn't rewrite the diary). Where it came from: a USDA food (grams, maybe a
    household portion), one of My foods (servings), a quick add (just the numbers), or a Meals recipe (servings -
    from the meal plan when plan_entry is set)."""
    BREAKFAST, LUNCH, DINNER, SNACK = "breakfast", "lunch", "dinner", "snack"
    SLOTS = [(BREAKFAST, "Breakfast"), (LUNCH, "Lunch"), (DINNER, "Dinner"), (SNACK, "Snacks")]
    USDA, CUSTOM, QUICK, RECIPE = "usda", "custom", "quick", "recipe"
    SOURCES = [(USDA, "USDA food"), (CUSTOM, "My food"), (QUICK, "Quick add"), (RECIPE, "Recipe")]
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="fitness_food_log")
    date = models.DateField()
    slot = models.CharField(max_length=9, choices=SLOTS, default=SNACK)
    source = models.CharField(max_length=6, choices=SOURCES)
    label = models.CharField(max_length=200)
    food = models.ForeignKey(Food, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    custom_food = models.ForeignKey(CustomFood, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    recipe = models.ForeignKey("albright_meals.Recipe", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    plan_entry = models.ForeignKey("albright_meals.MealPlanEntry", on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name="+")
    grams = models.DecimalField(null=True, blank=True, max_digits=8, decimal_places=1)
    portion_label = models.CharField(max_length=60, blank=True)      # "1 cup" when logged by a household portion
    servings = models.DecimalField(null=True, blank=True, max_digits=6, decimal_places=2)
    calories = models.DecimalField(**AMOUNT)   # this entry's total
    protein = models.DecimalField(**AMOUNT)
    carbs = models.DecimalField(**AMOUNT)
    fat = models.DecimalField(**AMOUNT)
    fiber = models.DecimalField(**AMOUNT)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["date", "pk"]
        indexes = [models.Index(fields=["owner", "date"])]

    def __str__(self):
        return f"{self.date} {self.slot}: {self.label}"


# ---------------------------------------------------------------------------
# Body metrics - private to their owner, never shared
# ---------------------------------------------------------------------------

class WeightEntry(models.Model):
    """One weigh-in a day (a second one that day replaces it). Stored in kg; shown in the person's unit."""
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="fitness_weights")
    date = models.DateField()
    weight_kg = models.DecimalField(max_digits=6, decimal_places=2, validators=[MinValueValidator(20), MaxValueValidator(400)])
    note = models.CharField(max_length=200, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["date", "pk"]
        constraints = [models.UniqueConstraint(fields=["owner", "date"], name="fitness_one_weight_a_day")]

    def __str__(self):
        return f"{self.date}: {self.weight_kg} kg"


class Measurement(models.Model):
    """Body measurements on one day, in cm (shown in the person's height unit), and body fat %. Any can be empty."""
    PARTS = [("waist", "Waist"), ("chest", "Chest"), ("hips", "Hips"), ("arms", "Arms"), ("thighs", "Thighs"), ("neck", "Neck")]
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="fitness_measurements")
    date = models.DateField()
    waist = models.DecimalField(null=True, blank=True, max_digits=5, decimal_places=1, validators=[MinValueValidator(5), MaxValueValidator(300)])
    chest = models.DecimalField(null=True, blank=True, max_digits=5, decimal_places=1, validators=[MinValueValidator(5), MaxValueValidator(300)])
    hips = models.DecimalField(null=True, blank=True, max_digits=5, decimal_places=1, validators=[MinValueValidator(5), MaxValueValidator(300)])
    arms = models.DecimalField(null=True, blank=True, max_digits=5, decimal_places=1, validators=[MinValueValidator(5), MaxValueValidator(300)])
    thighs = models.DecimalField(null=True, blank=True, max_digits=5, decimal_places=1, validators=[MinValueValidator(5), MaxValueValidator(300)])
    neck = models.DecimalField(null=True, blank=True, max_digits=5, decimal_places=1, validators=[MinValueValidator(5), MaxValueValidator(300)])
    body_fat = models.DecimalField(null=True, blank=True, max_digits=4, decimal_places=1, validators=[MinValueValidator(2), MaxValueValidator(70)])
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["date", "pk"]
        constraints = [models.UniqueConstraint(fields=["owner", "date"], name="fitness_one_measurement_a_day")]

    def __str__(self):
        return f"Measurements {self.date}"


# Challenge models live in challenges.py (imported here so migrations see them).
from .challenges import Challenge, ChallengeRule, ChallengeEntry, EntryComment  # noqa: F401
