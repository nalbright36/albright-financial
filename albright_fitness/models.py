"""Albright Fitness: the exercise library, logged workouts (exercises and sets), personal records and programs.

Privacy: everything is its owner's. A workout can be shared view-only with specific people (WorkoutShare) -
never anything else (no programs, no records, no body data). Anyone else gets a 404 (views look everything up
through the querysets below).

Built-in exercises have no owner (seeded by migration 0002); a person's own ones are theirs alone. Built-in
programs aren't rows: they're templates in programs.py that a person copies into their own Program.
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
        """Mine, and ones shared with me (view only)."""
        return self.filter(Q(owner=user) | Q(shares__user=user)).distinct()


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


class WorkoutShare(models.Model):
    """A workout shown, view only, to one other person."""
    workout = models.ForeignKey(Workout, on_delete=models.CASCADE, related_name="shares")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="fitness_shared_workouts")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["workout", "user"], name="fitness_share_once")]


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
