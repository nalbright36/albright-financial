"""How other apps put items on someone's Planner - one shared helper, so an
item made from another app's record is updated in place, never duplicated:
one ScheduleItem per (owner, source_app, source_id), enforced by the
planner_one_item_per_source constraint.

    upsert_source_item(user, "watchlist", lot.pk, "Auction ends: ...", day, time(18, 30), checkable=False, url=...)
    remove_source_items(user, "watchlist", keep=[...])     # what's no longer in the source

Whether a source may add items at all is the user's choice (Planner settings;
sources.SOURCES lists them, all off by default) - sources.sync() checks it before
calling these. An item the user deleted is never made again (DeletedSourceItem)."""
from django.db import IntegrityError, transaction

from .models import DeletedSourceItem, ScheduleItem

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
                return ScheduleItem.objects.create(created_by=source_app, **key, **fields), True
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
