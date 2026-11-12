"""Albright Planner: each user's tasks and events, one-off or repeating.

A repeating item is ONE row with an RFC 5545 rule (ScheduleItem.recurrence,
e.g. "FREQ=WEEKLY;BYDAY=MO,WE,FR"); its occurrences are worked out on the fly
for the dates on screen (recurrence.occurrences) - never stored. What IS
stored per occurrence:
  - ItemCompletion: a checked-off occurrence (one row per date actually done -
    nothing is ever created ahead of time);
  - ItemException: a "this occurrence only" change - skipped, moved to another
    day/time, or retitled.
A repeating task with no end (ScheduleItem.one_at_a_time) shows only its
current occurrence - the earliest not done or skipped - and the next appears
once that one is done (next_due / repeat_anchor; planner.current_occurrence).
Items are private to their owner unless shared (albright_labs.sharing - a person or a circle, view or edit);
everyone else gets a 404 (ScheduleItem.objects.visible_to).

Links to the other apps:
  - note: a Note the item points at (its id - the note itself is never copied).
    If the note is deleted the item stays, with note_deleted set ("Note deleted").
  - source_app / source_id / source_url: an item another app keeps up to date
    (services.upsert_source_item - one row per source record, updated in place).
    Only sources the user turned on add these (all are off by default), and
    one the user deleted is never added again (DeletedSourceItem).
  - from_bot / bot_token: added through the Bot API, labeled with the bot's name.
  - checklist (TaskChecklistItem): a task's subtasks. Each occurrence has its
    own ticks (ChecklistItemCompletion, per occurrence date), so every occurrence
    of a repeating task starts with all of them open. A row linked to a grocery
    line (a grocery run made from Meals' grocery list) has no ticks of its own -
    it reads and writes GroceryItem.checked (checklist.py)."""
from django.conf import settings
from django.db import models
from django.db.models import Q

DEFAULT_TIMEZONE = "America/New_York"
ME = "me"
# Sources whose items can't be edited, moved or checked off here (they mirror the source exactly).
LOCKED_SOURCES = {"watchlist"}


def note_gone(collector, field, sub_objs, using):
    """on_delete for ScheduleItem.note: unlink it and remember there was one (the item shows "Note deleted")."""
    collector.add_field_update(field, None, sub_objs)
    collector.add_field_update(field.model._meta.get_field("note_deleted"), True, sub_objs)


def idea_path(idea_id):
    """An Admin Idea's page (/control/ideas/<id>/) - the source_url of the Planner tasks made for it."""
    from django.urls import reverse
    return reverse("albright_control:idea", args=[idea_id])


class ItemQuerySet(models.QuerySet):
    def visible_to(self, user):
        if not (user and user.is_authenticated):
            return self.none()
        from albright_labs import sharing
        return sharing.visible_queryset(user, ScheduleItem, self)

    def editable_by(self, user):
        if not (user and user.is_authenticated):
            return self.none()
        from albright_labs import sharing
        return sharing.editable_queryset(user, ScheduleItem, self).exclude(source_app__in=LOCKED_SOURCES)


# An item's notes (Markdown): the model, the form and the Bot API all allow this many characters.
NOTES_MAX = 50_000


class ScheduleItem(models.Model):
    TASK, EVENT = "task", "event"
    KINDS = [(TASK, "Task"), (EVENT, "Event")]
    PRIORITIES = [("none", "None"), ("low", "Low"), ("medium", "Medium"), ("high", "High")]

    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="planner_items")
    title = models.CharField(max_length=200)
    notes = models.TextField(max_length=NOTES_MAX, blank=True)  # Markdown, rendered like a note (albright_notes.render)
    kind = models.CharField(max_length=5, choices=KINDS, default=TASK)
    date = models.DateField()                                     # the (first) day it happens
    start_time = models.TimeField(null=True, blank=True)          # empty = all day
    end_time = models.TimeField(null=True, blank=True)
    timezone = models.CharField(max_length=64, default=DEFAULT_TIMEZONE)
    # Optional place this item happens when it isn't at the viewer's home (PlannerSettings.weather_*).
    # Empty: Day and Week use the viewer's home forecast. The label is the geocoded name.
    location_label = models.CharField(max_length=200, blank=True)
    location_lat = models.DecimalField(max_digits=8, decimal_places=5, null=True, blank=True)
    location_lon = models.DecimalField(max_digits=8, decimal_places=5, null=True, blank=True)
    checkable = models.BooleanField(default=True)                 # tasks always; events if you like
    priority = models.CharField(max_length=6, choices=PRIORITIES, default="none")
    color = models.CharField(max_length=7, blank=True)            # "#84CC16", or blank for the app's accent
    # An RFC 5545 RRULE without its end ("FREQ=MONTHLY;BYDAY=2TU"); empty = happens once. Its end lives in
    # recurrence_until or recurrence_count (at most one), so "this and following" can end a series cleanly.
    recurrence = models.CharField(max_length=500, blank=True)
    recurrence_until = models.DateField(null=True, blank=True)
    recurrence_count = models.PositiveIntegerField(null=True, blank=True)
    # A repeating TASK with no end shows one occurrence at a time (planner.current_occurrence): the earliest one
    # not done or skipped. next_due: where it stands after the last check-off or skip (empty = never advanced).
    # repeat_from_completion: the next one is the check-off day plus the repeat interval (repeat_anchor = that
    # day) instead of the next scheduled date.
    repeat_from_completion = models.BooleanField(default=False)
    next_due = models.DateField(null=True, blank=True)
    repeat_anchor = models.DateField(null=True, blank=True)
    # Set when another app keeps it up to date (sources.SOURCES key, e.g. "watchlist"); empty = added by hand.
    source_app = models.CharField(max_length=20, blank=True)
    source_id = models.CharField(max_length=64, blank=True)
    source_url = models.CharField(max_length=1000, blank=True)    # the record's page (a site path, or an external URL)
    # Admin > Ideas: one Idea can have several tasks (each its own item and date), so this is a plain FK - not
    # source_app/source_id, whose one-item-per-source constraint would allow only one. Not locked.
    idea = models.ForeignKey("albright_control.Idea", null=True, blank=True, on_delete=models.SET_NULL,
                             related_name="planner_tasks")
    # An Idea's subtask: the Idea's main task (the first one, parent empty) is its parent. Deleting the main task
    # leaves the subtasks as plain tasks.
    parent = models.ForeignKey("self", null=True, blank=True, on_delete=models.SET_NULL, related_name="subtasks")
    note =models.ForeignKey("albright_notes.Note", on_delete=note_gone, null=True, blank=True,
                             related_name="planner_items")
    note_deleted = models.BooleanField(default=False)
    from_bot = models.BooleanField(default=False)
    bot_token = models.ForeignKey("albright_bot_api.BotToken", on_delete=models.SET_NULL, null=True, blank=True,
                                  related_name="+")
    archived = models.BooleanField(default=False)
    created_by = models.CharField(max_length=150, default=ME)     # the username, or a bot's name
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = ItemQuerySet.as_manager()

    class Meta:
        ordering = ["date", "start_time", "pk"]
        indexes = [models.Index(fields=["owner", "archived", "date"], name="planner_owner_date")]
        constraints = [
            models.CheckConstraint(condition=Q(recurrence_until__isnull=True) | Q(recurrence_count__isnull=True),
                                   name="planner_until_or_count"),
            models.UniqueConstraint(fields=["owner", "source_app", "source_id"], condition=~Q(source_app=""),
                                    name="planner_one_item_per_source"),
        ]

    def __str__(self):
        return self.title

    @property
    def repeats(self):
        return bool(self.recurrence)

    @property
    def one_at_a_time(self):
        """A repeating task that never ends ("Ends: Never"): only its current occurrence shows."""
        return (self.kind == self.TASK and self.checkable and bool(self.recurrence)
                and self.recurrence_until is None and self.recurrence_count is None)

    @property
    def all_day(self):
        return self.start_time is None

    @property
    def locked(self):
        return self.source_app in LOCKED_SOURCES

    @property
    def source_href(self):
        """Where the item's source link points: a site page as is; an external URL through the link check."""
        url = self.source_url
        if not url:
            return ""
        if url.startswith("/") and not url.startswith("//"):
            return url
        from albright_crypto_app.links import outbound_href
        return outbound_href(url)

    def can_edit(self, user):
        if self.locked:
            return False
        from albright_labs import sharing
        return sharing.can_edit(user, self)




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


class PlannerSettings(models.Model):
    """Per-user Planner preferences: the Meals overlay, which apps may add items (sources.SOURCES), the views'
    saved filters (View options - the same on every device; a link's ?kind= / ?done= / ?who= still overrides),
    and the home location used for weather (empty: weather is hidden unless an item has its own place)."""
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="planner_settings")
    show_meals = models.BooleanField(default=True)
    filter_kind = models.CharField(max_length=10, blank=True, default="")     # "" | "task" | "event"
    hide_done = models.BooleanField(default=False)
    filter_who = models.CharField(max_length=10, blank=True, default="")      # planner.WHO: "" | "mine" | "shared"
    sources_on = models.JSONField(default=list, blank=True)      # source keys turned ON; everything else is off
    # A task with a checklist checks itself off when every row is checked (and reopens when one is unchecked).
    complete_when_checklist_done = models.BooleanField(default=True,
                                                       verbose_name="Complete task when all items are checked")
    synced_at = models.DateTimeField(null=True, blank=True)      # sources last brought up to date
    # The zone this person sees times in (planner.user_zone) and new items are made in (home_zone). auto_timezone:
    # it follows the browser (base.html posts the browser's zone to planner/timezone/); off: the one set here.
    timezone = models.CharField(max_length=64, default=DEFAULT_TIMEZONE)
    auto_timezone = models.BooleanField(default=True)
    # Home location for Day/Week forecasts. Empty (no lat/lon): no home weather; an item with its
    # own location still shows one. The label is the geocoded name; lat/lon are what we fetch with.
    weather_label = models.CharField(max_length=200, blank=True)
    weather_lat = models.DecimalField(max_digits=8, decimal_places=5, null=True, blank=True)
    weather_lon = models.DecimalField(max_digits=8, decimal_places=5, null=True, blank=True)

    @classmethod
    def for_user(cls, user):
        return cls.objects.get_or_create(user=user)[0]

    def source_on(self, key):
        return key in (self.sources_on or [])


def home_zone(user):
    """The zone a new item of `user`'s is made in (ScheduleItem.timezone): their Planner zone."""
    return PlannerSettings.for_user(user).timezone


class TaskChecklistItem(models.Model):
    """One subtask of a task (the definition - whether it's ticked is per occurrence: ChecklistItemCompletion). A
    row linked to a grocery line (grocery_item) is ticked exactly when that line is checked on the grocery list.
    Rows keep their text, so a cleared (archived) grocery line still shows here.
    starts_on / ends_on: the occurrences it belongs to (empty = from the start / still there). A repeating task's
    checklist edits apply from its current occurrence on, so earlier occurrences keep the list they had."""
    task = models.ForeignKey(ScheduleItem, on_delete=models.CASCADE, related_name="checklist")
    order = models.PositiveIntegerField(default=0)
    text = models.CharField(max_length=200)
    grocery_item = models.ForeignKey("albright_meals.GroceryItem", on_delete=models.SET_NULL, null=True, blank=True,
                                     related_name="checklist_rows")
    starts_on = models.DateField(null=True, blank=True)
    ends_on = models.DateField(null=True, blank=True)          # removed from this occurrence on

    class Meta:
        ordering = ["order", "pk"]

    def __str__(self):
        return self.text

    def applies_to(self, day):
        return (self.starts_on is None or self.starts_on <= day) and (self.ends_on is None or day < self.ends_on)


class ChecklistItemCompletion(models.Model):
    """A subtask ticked on one occurrence of its task. `text` is the subtask as it read then, so the history stays
    right after it's renamed or deleted (checklist_item is emptied then; task keeps the row with its task)."""
    task = models.ForeignKey(ScheduleItem, on_delete=models.CASCADE, related_name="checklist_completions")
    checklist_item = models.ForeignKey(TaskChecklistItem, on_delete=models.SET_NULL, null=True, blank=True,
                                       related_name="completions")
    occurrence_date = models.DateField()
    text = models.CharField(max_length=200)
    done_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                related_name="+")
    done_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["occurrence_date", "pk"]
        constraints = [models.UniqueConstraint(fields=["checklist_item", "occurrence_date"],
                                               name="planner_one_tick_per_subtask_occurrence")]

    def __str__(self):
        return f"{self.text} ({self.occurrence_date})"


class DeletedSourceItem(models.Model):
    """A source item its owner deleted: services.upsert_source_item never makes it again, even with the source on."""
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")
    source_app = models.CharField(max_length=40)
    source_id = models.CharField(max_length=64)
    deleted_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["owner", "source_app", "source_id"],
                                               name="planner_deleted_source_once")]

    @classmethod
    def record(cls, item):
        if item.source_app:
            cls.objects.get_or_create(owner_id=item.owner_id, source_app=item.source_app, source_id=item.source_id)
