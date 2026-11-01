"""Planner logic shared by the views, Home's Today panel and the launcher:
what's on in a date range, overdue tasks, check-off, and changing a repeating
item for "this occurrence only", "this and following" or "all occurrences"."""
from datetime import time, timedelta

from django.db import transaction
from django.db.models import F, Q
from django.utils import timezone

from . import recurrence
from .models import DeletedSourceItem, ItemCompletion, ItemException, ItemShare, ScheduleItem

THIS, FOLLOWING, ALL = "this", "following", "all"
SCOPES = [(THIS, "This occurrence only"), (FOLLOWING, "This and following"), (ALL, "All occurrences")]
SERIES_FIELDS = ("notes", "kind", "checkable", "priority", "color", "end_time")


def today():
    return timezone.localdate()


def week_start(day):
    return day - timedelta(days=day.weekday())


# ---------------------------------------------------------------------------
# What's on
# ---------------------------------------------------------------------------

def _candidates(user, start, end):
    """Items that could have an occurrence between start and end."""
    one_off = Q(recurrence="", date__gte=start, date__lte=end)
    repeating = ~Q(recurrence="") & Q(date__lte=end) & (Q(recurrence_until__isnull=True) | Q(recurrence_until__gte=start))
    moved_in = Q(exceptions__action=ItemException.MOVE, exceptions__new_date__gte=start, exceptions__new_date__lte=end)
    ids = ScheduleItem.objects.visible_to(user).filter(archived=False).filter(one_off | repeating | moved_in) \
        .values_list("pk", flat=True).distinct()
    return ScheduleItem.objects.filter(pk__in=list(ids)).select_related("owner").prefetch_related("exceptions", "completions")


def occurrences(user, start, end, kind="", show_done=True, shared_only=False):
    """Every occurrence the user can see between start and end (inclusive), sorted by day and time."""
    items = list(_candidates(user, start, end))
    if kind in (ScheduleItem.TASK, ScheduleItem.EVENT):
        items = [i for i in items if i.kind == kind]
    if shared_only:
        items = [i for i in items if i.owner_id != user.pk]
    editable = set(ScheduleItem.objects.editable_by(user).filter(pk__in=[i.pk for i in items]).values_list("pk", flat=True))
    out = []
    for item in items:
        done = {c.occurrence_date for c in item.completions.all()}
        exceptions = {e.occurrence_date: e for e in item.exceptions.all()}
        for occ in recurrence.occurrences(item, start, end, done, exceptions):
            occ.can_edit = item.pk in editable
            if show_done or not occ.done:
                out.append(occ)
    out.sort(key=lambda o: (o.date, o.start_time is not None, o.start_time or time.min, o.title.lower()))
    return out


def overdue(user, day=None, kind="", shared_only=False):
    """One-off tasks from before `day` that aren't checked off - they roll into Today until done or moved."""
    day = day or today()
    if kind == ScheduleItem.EVENT:
        return []
    qs = ScheduleItem.objects.visible_to(user).filter(archived=False, recurrence="", kind=ScheduleItem.TASK,
                                                      checkable=True, date__lt=day) \
        .exclude(completions__occurrence_date=F("date")).select_related("owner").order_by("date", "start_time", "pk")
    if shared_only:
        qs = qs.exclude(owner=user)
    editable = set(ScheduleItem.objects.editable_by(user).filter(pk__in=[i.pk for i in qs]).values_list("pk", flat=True))
    out = []
    for item in qs:
        occ = recurrence.Occurrence(item, item.date, item.date, item.title, item.start_time, item.end_time)
        occ.overdue, occ.can_edit = True, item.pk in editable
        out.append(occ)
    return out


def open_tasks_today(user):
    """Today's unchecked tasks (and overdue ones) - the launcher's and Home's number."""
    day = today()
    todays = [o for o in occurrences(user, day, day, kind=ScheduleItem.TASK) if o.item.checkable and not o.done]
    return todays, overdue(user, day)


def today_panel(user):
    """Home's Today panel: today's tasks (checkable) and events, and how many tasks are overdue."""
    day = today()
    occs = occurrences(user, day, day)
    return {"date": day, "tasks": [o for o in occs if o.item.kind == ScheduleItem.TASK],
            "events": [o for o in occs if o.item.kind == ScheduleItem.EVENT], "overdue": len(overdue(user, day))}


# ---------------------------------------------------------------------------
# Check-off
# ---------------------------------------------------------------------------

def toggle(item, occurrence_date, user, done=None):
    """Check off (or undo) one occurrence. Returns whether it's now done, or None if it isn't scheduled that day."""
    if not recurrence.is_scheduled(item, occurrence_date):
        return None
    existing = ItemCompletion.objects.filter(item=item, occurrence_date=occurrence_date)
    want = (not existing.exists()) if done is None else done
    if want:
        ItemCompletion.objects.get_or_create(item=item, occurrence_date=occurrence_date,
                                             defaults={"completed_by": user})
    else:
        existing.delete()
    return want


# ---------------------------------------------------------------------------
# Changing items
# ---------------------------------------------------------------------------

def _apply(item, changes):
    for name in ("title", "notes", "kind", "start_time", "end_time", "checkable", "priority", "color"):
        if name in changes:
            setattr(item, name, changes[name])
    if item.kind == ScheduleItem.TASK:
        item.checkable = True


def _set_rule(item, changes, moved_from=None):
    """The repeat settings from the form (if sent), else the old rule moved along with the series."""
    if "recurrence" in changes:
        item.recurrence = changes["recurrence"]
        item.recurrence_until = changes.get("recurrence_until")
        item.recurrence_count = None if item.recurrence_until else changes.get("recurrence_count")
    elif moved_from is not None and moved_from != item.date and item.recurrence:
        item.recurrence = recurrence.shift(item.recurrence, moved_from, item.date)
    if not item.recurrence:
        item.recurrence_until = item.recurrence_count = None


@transaction.atomic
def change(item, occurrence_date, scope, changes):
    """Edit or move an item. changes: any of title, notes, kind, date (the occurrence's new day), start_time,
    end_time, checkable, priority, color, and (for a series) recurrence / recurrence_until / recurrence_count.
    Returns the item now holding that occurrence."""
    new_date = changes.get("date") or occurrence_date
    if not item.recurrence:
        _apply(item, changes)
        item.date = new_date
        _set_rule(item, changes)
        item.save()
        return item

    if scope == THIS:
        exc, _ = ItemException.objects.get_or_create(item=item, occurrence_date=occurrence_date,
                                                     defaults={"action": ItemException.EDIT})
        title = changes.get("title", exc.new_title or item.title)
        exc.new_title = "" if title == item.title else title[:200]
        start = changes.get("start_time", exc.new_start_time or item.start_time)
        exc.new_start_time = None if start == item.start_time else start
        exc.new_date = None if new_date == occurrence_date else new_date
        exc.action = ItemException.MOVE if exc.new_date or exc.new_start_time else ItemException.EDIT
        if exc.action == ItemException.EDIT and not exc.new_title:
            exc.delete()                                  # back to the plain occurrence
        else:
            exc.save()
        return item

    delta = new_date - occurrence_date
    if scope == FOLLOWING and occurrence_date > item.date:
        before = recurrence.count_before(item, occurrence_date)
        new = ScheduleItem.objects.get(pk=item.pk)
        new.pk = new.id = None
        new.source_app = new.source_id = ""          # one item per source record: the copy isn't it
        new.date = occurrence_date + delta
        if item.recurrence_count:
            new.recurrence_count = max(1, item.recurrence_count - before)
        item.recurrence_until, item.recurrence_count = occurrence_date - timedelta(days=1), None
        item.save()
        _apply(new, changes)
        _set_rule(new, changes, moved_from=occurrence_date)
        new.save()
        for share in item.shares.all():
            ItemShare.objects.create(item=new, user=share.user, permission=share.permission)
        for row in list(item.completions.filter(occurrence_date__gte=occurrence_date)) + \
                list(item.exceptions.filter(occurrence_date__gte=occurrence_date)):
            row.item, row.occurrence_date = new, row.occurrence_date + delta
            row.save()
        return new

    # ALL (or "following" from the first occurrence): the whole series, its start moved by the same amount.
    old_start = item.date
    _apply(item, changes)
    item.date = item.date + delta
    _set_rule(item, changes, moved_from=old_start)
    item.save()
    if delta:
        for row in list(item.completions.all()) + list(item.exceptions.all()):
            row.occurrence_date = row.occurrence_date + delta
            row.save()
    return item


@transaction.atomic
def remove(item, occurrence_date, scope):
    """Delete a one-off item; for a series skip this occurrence, end it before this one, or delete it all."""
    if not item.recurrence or scope == ALL or (scope == FOLLOWING and occurrence_date <= item.date):
        DeletedSourceItem.record(item)                    # a source item never comes back once deleted
        item.delete()
        return
    if scope == THIS:
        ItemException.objects.update_or_create(item=item, occurrence_date=occurrence_date, defaults={
            "action": ItemException.SKIP, "new_date": None, "new_start_time": None, "new_title": ""})
        ItemCompletion.objects.filter(item=item, occurrence_date=occurrence_date).delete()
        return
    item.recurrence_until, item.recurrence_count = occurrence_date - timedelta(days=1), None
    item.save()
    item.completions.filter(occurrence_date__gte=occurrence_date).delete()
    item.exceptions.filter(occurrence_date__gte=occurrence_date).delete()


def planner_users():
    """Everyone who can open the Planner (Admin > Users) - who an item can be shared with."""
    from django.contrib.auth import get_user_model
    return get_user_model().objects.filter(is_active=True).filter(
        Q(is_superuser=True) | Q(labs_preference__allowed_apps__contains=["planner"])).order_by("username")


# ---------------------------------------------------------------------------
# Linked notes (ScheduleItem.note - the note's id; the note lives in Notes)
# ---------------------------------------------------------------------------

def linked_note(item, user):
    """What an item page shows about its note: {"title", "preview", "url"}; {"hidden": True} for a note this user
    can't see; {"deleted": True} once the note was deleted; None when there's none."""
    if item.note_id is None:
        return {"deleted": True} if item.note_deleted else None
    from albright_notes.models import Note
    from albright_notes.render import preview
    from django.urls import reverse
    note = Note.objects.visible_to(user).filter(pk=item.note_id).first()
    if note is None:
        return {"hidden": True}
    return {"title": note.title, "preview": preview(note.body), "url": reverse("albright_notes:note", args=[note.pk]),
            "id": note.pk}


def attachable_notes(user, limit=200):
    """The notes this user may attach: theirs and ones shared with them, most recently changed first."""
    from albright_notes.models import Note
    return list(Note.objects.visible_to(user).filter(archived=False).order_by("-updated_at", "-pk")
                .only("pk", "title")[:limit])


def scheduled_for_note(note, user):
    """Notes' "Scheduled: <date>" chips: this user's visible items linked to the note, each with its next date
    (a repeating one's next occurrence from today, else its first) and a link to that day in the Planner."""
    from django.urls import reverse
    day = today()
    out = []
    for item in ScheduleItem.objects.visible_to(user).filter(note=note, archived=False).order_by("date", "pk"):
        when = item.date
        if item.recurrence:
            upcoming = recurrence.scheduled_dates(item, day, day + timedelta(days=400))
            when = upcoming[0] if upcoming else item.date
        out.append({"item": item, "date": when, "repeats": item.repeats,
                    "url": reverse("albright_planner:day") + f"?date={when.isoformat()}"})
    return out
