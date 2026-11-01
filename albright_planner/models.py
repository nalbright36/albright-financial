"""Albright Planner: each user's tasks and events, one-off or repeating.

A repeating item is ONE row with an RFC 5545 rule (ScheduleItem.recurrence,
e.g. "FREQ=WEEKLY;BYDAY=MO,WE,FR"); its occurrences are worked out on the fly
for the dates on screen (recurrence.occurrences) - never stored. What IS
stored per occurrence:
  - ItemCompletion: a checked-off occurrence (one row per date actually done -
    nothing is ever created ahead of time);
  - ItemException: a "this occurrence only" change - skipped, moved to another
    day/time, or retitled.
Items are private to their owner unless shared (ItemShare: view or edit);
everyone else gets a 404 (ScheduleItem.objects.visible_to)."""
from django.conf import settings
from django.db import models
from django.db.models import Q

DEFAULT_TIMEZONE = "America/New_York"
ME = "me"


class ItemQuerySet(models.QuerySet):
    def visible_to(self, user):
        if not (user and user.is_authenticated):
            return self.none()
        shared = ItemShare.objects.filter(user=user).values("item_id")
        return self.filter(Q(owner=user) | Q(pk__in=shared))

    def editable_by(self, user):
        if not (user and user.is_authenticated):
            return self.none()
        shared = ItemShare.objects.filter(user=user, permission=ItemShare.EDIT).values("item_id")
        return self.filter(Q(owner=user) | Q(pk__in=shared))


class ScheduleItem(models.Model):
    TASK, EVENT = "task", "event"
    KINDS = [(TASK, "Task"), (EVENT, "Event")]
    PRIORITIES = [("none", "None"), ("low", "Low"), ("medium", "Medium"), ("high", "High")]

    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="planner_items")
    title = models.CharField(max_length=200)
    notes = models.CharField(max_length=1000, blank=True)
    kind = models.CharField(max_length=5, choices=KINDS, default=TASK)
    date = models.DateField()                                     # the (first) day it happens
    start_time = models.TimeField(null=True, blank=True)          # empty = all day
    end_time = models.TimeField(null=True, blank=True)
    timezone = models.CharField(max_length=64, default=DEFAULT_TIMEZONE)
    checkable = models.BooleanField(default=True)                 # tasks always; events if you like
    priority = models.CharField(max_length=6, choices=PRIORITIES, default="none")
    color = models.CharField(max_length=7, blank=True)            # "#84CC16", or blank for the app's accent
    # An RFC 5545 RRULE without its end ("FREQ=MONTHLY;BYDAY=2TU"); empty = happens once. Its end lives in
    # recurrence_until or recurrence_count (at most one), so "this and following" can end a series cleanly.
    recurrence = models.CharField(max_length=500, blank=True)
    recurrence_until = models.DateField(null=True, blank=True)
    recurrence_count = models.PositiveIntegerField(null=True, blank=True)
    # Set when another app created it (e.g. a Meals plan); empty = added by hand.
    source_app = models.CharField(max_length=20, blank=True)
    source_id = models.CharField(max_length=64, blank=True)
    shared_with = models.ManyToManyField(settings.AUTH_USER_MODEL, through="ItemShare", related_name="planner_shared_items",
                                         blank=True)
    archived = models.BooleanField(default=False)
    created_by = models.CharField(max_length=150, default=ME)     # the username, or a bot's name
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = ItemQuerySet.as_manager()

    class Meta:
        ordering = ["date", "start_time", "pk"]
        indexes = [models.Index(fields=["owner", "archived", "date"], name="planner_owner_date")]
        constraints = [models.CheckConstraint(
            condition=Q(recurrence_until__isnull=True) | Q(recurrence_count__isnull=True), name="planner_until_or_count")]

    def __str__(self):
        return self.title

    @property
    def repeats(self):
        return bool(self.recurrence)

    @property
    def all_day(self):
        return self.start_time is None

    def can_edit(self, user):
        return self.owner_id == user.pk or self.shares.filter(user=user, permission=ItemShare.EDIT).exists()


class ItemShare(models.Model):
    VIEW, EDIT = "view", "edit"
    PERMISSIONS = [(VIEW, "Can view"), (EDIT, "Can edit")]
    item = models.ForeignKey(ScheduleItem, on_delete=models.CASCADE, related_name="shares")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="planner_shares")
    permission = models.CharField(max_length=4, choices=PERMISSIONS, default=VIEW)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["user__username"]
        constraints = [models.UniqueConstraint(fields=["item", "user"], name="planner_one_share_per_user")]


class ItemCompletion(models.Model):
    """One occurrence checked off (occurrence_date is the date it was scheduled for, before any move)."""
    item = models.ForeignKey(ScheduleItem, on_delete=models.CASCADE, related_name="completions")
    occurrence_date = models.DateField()
    completed_at = models.DateTimeField(auto_now_add=True)
    completed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                     related_name="+")

    class Meta:
        ordering = ["occurrence_date"]
        constraints = [models.UniqueConstraint(fields=["item", "occurrence_date"], name="planner_one_completion")]


class ItemException(models.Model):
    """A change to one occurrence only."""
    SKIP, MOVE, EDIT = "skip", "move", "edit"
    ACTIONS = [(SKIP, "Skip"), (MOVE, "Move"), (EDIT, "Edit")]
    item = models.ForeignKey(ScheduleItem, on_delete=models.CASCADE, related_name="exceptions")
    occurrence_date = models.DateField()                          # the date it was scheduled for
    action = models.CharField(max_length=4, choices=ACTIONS)
    new_date = models.DateField(null=True, blank=True)
    new_start_time = models.TimeField(null=True, blank=True)
    new_title = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["occurrence_date"]
        constraints = [models.UniqueConstraint(fields=["item", "occurrence_date"], name="planner_one_exception")]
