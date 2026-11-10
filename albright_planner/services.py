"""How other apps put items on someone's Planner - one shared helper, so an
item made from another app's record is updated in place, never duplicated:
one ScheduleItem per (owner, source_app, source_id), enforced by the
planner_one_item_per_source constraint.

    upsert_source_item(user, "watchlist", lot.pk, "Auction ends: ...", day, time(18, 30), checkable=False, url=...)
    remove_source_items(user, "watchlist", keep=[...])     # what's no longer in the source

Whether a source may add items at all is the user's choice (Planner settings;
sources.SOURCES lists them, all off by default) - sources.sync() checks it before
calling these. An item the user deleted is never made again (DeletedSourceItem)."""
from datetime import timedelta

from django.db import IntegrityError, transaction

from .models import DeletedSourceItem, ScheduleItem, home_zone

SOURCE_TITLE_MAX = 200


@transaction.atomic
def upsert_source_item(user, source_app, source_id, title, date, time=None, checkable=True, url=""):
    """Create, or update in place, the item for one record of another app. A checkable item is a task, a view-only
    one an event. Coming back after its source was turned off un-archives it. Returns (item, created) - or
    (None, False) when the user deleted this one: deletes are permanent."""
    fields = {"title": (title or "").strip()[:SOURCE_TITLE_MAX] or "Untitled", "date": date, "start_time": time,
              "end_time": None, "recurrence": "", "recurrence_until": None, "recurrence_count": None,
              "checkable": bool(checkable), "kind": ScheduleItem.TASK if checkable else ScheduleItem.EVENT,
              "source_url": (url or "")[:1000], "archived": False}
    key = {"owner": user, "source_app": source_app, "source_id": str(source_id)[:64]}
    if DeletedSourceItem.objects.filter(**key).exists():
        return None, False
    item = ScheduleItem.objects.select_for_update().filter(**key).first()
    if item is None:
        try:
            with transaction.atomic():
                return ScheduleItem.objects.create(created_by=source_app, timezone=home_zone(user), **key,
                                                   **fields), True
        except IntegrityError:                     # made meanwhile by another request: update that one
            item = ScheduleItem.objects.select_for_update().get(**key)
    changed = [name for name, value in fields.items() if getattr(item, name) != value]
    for name in changed:
        setattr(item, name, fields[name])
    if changed:
        item.save(update_fields=[*changed, "updated_at"])
    return item, False


def remove_source_items(user, source_app, keep=()):
    """Delete this source's items whose records are gone, except ones already checked off (they stay as done)."""
    keep = {str(k) for k in keep}
    stale = ScheduleItem.objects.filter(owner=user, source_app=source_app).exclude(source_id__in=keep)
    return stale.exclude(completions__isnull=False).delete()[0]


def archive_source_items(user, source_app):
    """A source turned off: its items leave the Planner (kept, so turning it back on doesn't lose check-offs)."""
    return ScheduleItem.objects.filter(owner=user, source_app=source_app, archived=False).update(archived=True)


# ---------------------------------------------------------------------------
# Repeating items with a checklist (Fitness: "Schedule this program")
# ---------------------------------------------------------------------------

@transaction.atomic
def upsert_weekly_source_item(user, source_app, source_id, title, start, weekdays, until=None, checklist=(), url=""):
    """Create, or update in place, a private repeating task for one record of another app: every week on
    `weekdays` (0 = Monday) from the first of them on or after `start`, until `until` (None: no end), with
    `checklist` as its subtasks. Returns (item, created) - (None, False) if the user deleted it (permanent)."""
    from . import checklist as checklists
    from .recurrence import WEEKDAYS
    days = sorted({int(d) for d in weekdays if 0 <= int(d) <= 6})
    if not days:
        raise ValueError("A weekly item needs at least one weekday.")
    first = start
    while first.weekday() not in days:
        first += timedelta(days=1)
    fields = {"title": (title or "").strip()[:SOURCE_TITLE_MAX] or "Untitled", "date": first, "start_time": None,
              "end_time": None, "recurrence": "FREQ=WEEKLY;BYDAY=" + ",".join(WEEKDAYS[d] for d in days),
              "recurrence_until": until, "recurrence_count": None, "checkable": True, "kind": ScheduleItem.TASK,
              "source_url": (url or "")[:1000], "archived": False}
    key = {"owner": user, "source_app": source_app, "source_id": str(source_id)[:64]}
    if DeletedSourceItem.objects.filter(**key).exists():
        return None, False
    item = ScheduleItem.objects.select_for_update().filter(**key).first()
    created = item is None
    if created:
        item = ScheduleItem.objects.create(created_by=source_app, timezone=home_zone(user), **key, **fields)
        checklists.create_rows(item, checklist)
        return item, True
    if item.date < first and item.completions.exists():
        fields.pop("date")                          # keep its history: the series goes on from where it started
    changed = [name for name, value in fields.items() if getattr(item, name) != value]
    for name in changed:
        setattr(item, name, fields[name])
    if changed:
        item.save(update_fields=[*changed, "updated_at"])
    current = [r.text for r in checklists.current_rows(item)]
    wanted = checklists.clean_texts(checklist)
    if current != wanted:
        checklists.save_rows(item, [(r.pk, text) for r, text in zip(checklists.current_rows(item), wanted)]
                             + [(None, text) for text in wanted[len(current):]])
    return item, False


def end_source_items(user, source_app, source_ids, last_day):
    """These items stop after `last_day` - earlier occurrences and their check-offs stay. One that hadn't started
    yet leaves the Planner (archived)."""
    ended = 0
    for item in ScheduleItem.objects.filter(owner=user, source_app=source_app, source_id__in=[str(i) for i in source_ids],
                                            archived=False):
        if item.date > last_day:
            item.archived = True
            item.save(update_fields=["archived", "updated_at"])
        elif item.recurrence and (item.recurrence_until is None or item.recurrence_until > last_day):
            item.recurrence_until, item.recurrence_count = last_day, None
            item.save(update_fields=["recurrence_until", "recurrence_count", "updated_at"])
        ended += 1
    return ended


def source_item(user, source_app, source_id):
    """The user's (unarchived) item for one record of another app, or None."""
    return ScheduleItem.objects.filter(owner=user, source_app=source_app, source_id=str(source_id), archived=False).first()


def complete_occurrence(item, day, user):
    """Check off one occurrence (e.g. a workout logged for it). Returns True, or None if it isn't scheduled then."""
    from .planner import toggle
    return toggle(item, day, user, done=True)
