"""The quick view's instant first paint (planner.js): each item link on the Day / Week / Month views and Home's Today
panel carries its occurrence's basics as JSON (data-qv) - title, date, time, repeat, sharing, linked note, checklist,
notes, attachment names - so tapping a task opens the quick view at once, drawn from that; the full panel
(views.view_item) is fetched in the background (already on pointerdown) and replaces it when it arrives.

attach(occurrences, user) adds occ.qv (the JSON) to every occurrence in a few bulk queries - never one per item:
the linked notes' titles, the attachments' names, who each item is shared with. Everything else is already on the
occurrence (planner.occurrences, checklist.attach)."""
import json

from django.contrib.contenttypes.models import ContentType
from django.urls import reverse
from django.utils.dateformat import format as date_format, time_format

from . import planner
from .models import ScheduleItem

NOTES_PREVIEW = 600          # characters of the notes in the first paint (the full panel brings them all, rendered)
CHECKLIST_MAX = 40
ATTACHMENTS_MAX = 12


def _names_by_item(items):
    """{item pk: ["Albright Family", "Kim"]} - who each item is shared with (owner's view), one query."""
    from albright_labs.models import ShareGrant
    ids = [i.pk for i in items]
    if not ids:
        return {}
    out = {}
    grants = ShareGrant.objects.filter(content_type=ContentType.objects.get_for_model(ScheduleItem), object_id__in=ids,
                                       hidden_at__isnull=True).select_related("user", "circle") \
        .order_by("circle__name", "user__username", "pk")
    for g in grants:
        out.setdefault(g.object_id, []).append(g.circle.name if g.circle_id else planner.person(g.user))
    return out


def _attachments_by_item(items):
    from albright_labs.models import Attachment
    ids = [i.pk for i in items]
    out = {}
    if ids:
        rows = Attachment.objects.filter(content_type=ContentType.objects.get_for_model(ScheduleItem), object_id__in=ids) \
            .order_by("pk").values_list("object_id", "original_name")
        for object_id, name in rows:
            out.setdefault(object_id, []).append(name)
    return out


def _note_titles(items, user):
    from albright_notes.models import Note
    ids = {i.note_id for i in items if i.note_id}
    if not ids:
        return {}
    return dict(Note.objects.visible_to(user).filter(pk__in=ids).values_list("pk", "title"))


def _editable(occ, item, user):
    """Whether this person gets "Open full editor" (no query for their own items)."""
    if item.is_weigh_in or item.locked:
        return False
    if item.owner_id == user.pk:
        return True
    if not hasattr(occ, "can_edit"):
        occ.can_edit = item.can_edit(user)
    return occ.can_edit


def attach(occurrences, user):
    """occ.qv for each occurrence (see the module docstring). Returns the list."""
    occs = [o for o in occurrences if getattr(o, "item", None) is not None]
    items = list({o.item.pk: o.item for o in occs}.values())
    if not items:
        return occurrences
    today = planner.today(user)
    shared = _names_by_item([i for i in items if i.owner_id == user.pk and getattr(i, "has_shares", True)])
    files = _attachments_by_item([i for i in items if getattr(i, "attachment_count", 1)])
    notes = _note_titles(items, user)
    for o in occs:
        item, day = o.item, o.date
        if getattr(o, "is_meal", False):
            continue
        if item.note_id:
            note = notes.get(item.note_id, "A note that isn't shared with you") or "Untitled note"
        else:
            note = "Note deleted" if item.note_deleted else "None"
        if o.shared_in:
            share_text = f"Shared by {planner.person(item.owner)}" + ("" if o.can_edit else " · view only") \
                if hasattr(o, "can_edit") else f"Shared by {planner.person(item.owner)}"
        else:
            share_text = ", ".join(shared.get(item.pk, [])) or "Only me"
        rows = getattr(o, "checklist", None) or []
        data = {
            "id": item.pk, "date": o.occurrence_date.isoformat(), "title": o.title, "done": bool(o.done),
            "skipped": bool(getattr(o, "skipped", False)), "checkable": item.checkable, "color": item.color,
            "dateText": date_format(day, "D, M j") + (date_format(day, ", Y") if day.year != today.year else ""),
            "timeText": (time_format(o.start_time, "g:i A") + ("–" + time_format(o.end_time, "g:i A") if o.end_time else ""))
            if o.start_time else "All day",
            "repeat": planner.plain_repeat(item) or "Doesn't repeat", "shared": share_text, "note": note,
            "checklist": [{"t": r.text, "c": bool(r.checked)} for r in rows[:CHECKLIST_MAX]],
            "notes": (item.notes or "")[:NOTES_PREVIEW], "more": len(item.notes or "") > NOTES_PREVIEW,
            "files": files.get(item.pk, [])[:ATTACHMENTS_MAX],
            "edit": reverse("albright_planner:edit", args=[item.pk]) if _editable(o, item, user) else "",
        }
        o.qv = json.dumps(data, separators=(",", ":"))
    return occurrences


# ---------------------------------------------------------------------------------------------------------------
# The full panel (views.view_item ?panel=1), cached briefly per task. The cache is per process, so instead of
# invalidating it the key carries a fingerprint of everything the panel shows - one query: the item's last change,
# its completions, moved / retitled occurrences, checklist rows and ticks, shares, attachments and linked note. Any
# change anywhere makes a new key, so a stale panel is never served; unchanged, the panel comes straight back.
# ---------------------------------------------------------------------------------------------------------------
PANEL_TTL = 30               # seconds

_FINGERPRINT_SQL = """
SELECT
  (SELECT updated_at::text FROM albright_planner_scheduleitem WHERE id = %(id)s),
  (SELECT COUNT(*)::text || ':' || COALESCE(MAX(id), 0)::text || ':' || COALESCE(MAX(skipped_at)::text, '')
     FROM albright_planner_itemcompletion WHERE item_id = %(id)s),
  (SELECT md5(COALESCE(string_agg(id::text || action || COALESCE(new_date::text, '') || COALESCE(new_start_time::text, '')
                                  || COALESCE(new_title, ''), ',' ORDER BY id), ''))
     FROM albright_planner_itemexception WHERE item_id = %(id)s),
  (SELECT md5(COALESCE(string_agg(id::text || '|' || text || '|' || "order"::text || '|' || COALESCE(starts_on::text, '')
                                  || '|' || COALESCE(ends_on::text, ''), ',' ORDER BY id), ''))
     FROM albright_planner_taskchecklistitem WHERE task_id = %(id)s),
  (SELECT COUNT(*)::text || ':' || COALESCE(MAX(id), 0)::text FROM albright_planner_checklistitemcompletion WHERE task_id = %(id)s),
  (SELECT md5(COALESCE(string_agg(id::text || permission || COALESCE(hidden_at::text, ''), ',' ORDER BY id), ''))
     FROM albright_labs_sharegrant WHERE content_type_id = %(ct)s AND object_id = %(id)s),
  (SELECT COUNT(*)::text || ':' || COALESCE(MAX(id), 0)::text FROM albright_labs_attachment
     WHERE content_type_id = %(ct)s AND object_id = %(id)s),
  (SELECT n.updated_at::text FROM albright_notes_note n JOIN albright_planner_scheduleitem i ON i.note_id = n.id
     WHERE i.id = %(id)s)
"""


def panel_key(request, item, occurrence):
    """The cache key for this user's panel of this occurrence as it is right now - or None (not cached: no CSRF cookie
    yet, the panel's forms carry this browser's token)."""
    import hashlib

    from django.conf import settings
    from django.db import connection
    csrf = request.COOKIES.get(settings.CSRF_COOKIE_NAME, "")
    if not csrf:
        return None
    with connection.cursor() as cursor:
        cursor.execute(_FINGERPRINT_SQL, {"id": item.pk, "ct": ContentType.objects.get_for_model(ScheduleItem).pk})
        fingerprint = "|".join(str(v) for v in cursor.fetchone())
    raw = f"{request.user.pk}|{csrf}|{item.pk}|{occurrence}|{planner.today(request.user)}|" \
          f"{planner.user_zone(request.user)}|{request.get_full_path()}|{fingerprint}"
    return "plqv:" + hashlib.sha1(raw.encode()).hexdigest()
