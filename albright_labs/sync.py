"""Offline editing, the server's half: POST /sync/ (labs/sync.js's outbox, labs/sync_core.js sends it).

The browser sends the changes made offline, in order: {"sent_at": <ms>, "changes": [{"id": <uuid>, "user": <id>,
"action": "grocery.check", "target": <id or the uuid of something made offline>, "client_id": <uuid, for a new
item>, "payload": {...}, "base": <version seen>, "at": <ms, when it was made>}, ...]}. Each one comes back with a
result: {"id", "status", "message"?, "object_id"?, "copy_id"?}.

  applied    saved
  skipped    another device changed it later (latest change wins, by when each change was made) - nothing saved
  conflict   a note changed on the server since the offline edit's version: the edit is saved as its own note,
             "<title> (offline copy)" (copy_id), for the person to compare and keep one, the other or both
  deleted    it was deleted on another device (the browser offers Restore / Discard)
  forbidden  it's no longer theirs to change (unshared, or the app was taken away)
  rejected   it didn't pass the same checks the page's own form has (message says why)
  error      something broke here; the browser tries again later (not recorded)

Every change goes through the app's own save view - the one its page posts to - as a request of its own (call()):
the same permission checks (the view's lookups and AppAccessMiddleware), the same validation, the same save code and
signals. Nothing here writes app data itself. SyncChange records each result by its id, so a change sent twice (a
retry after a lost reply) is applied once; SyncObject maps the uuid of something made offline to the row it became.
"""
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone as dt_timezone

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, transaction
from django.http import Http404, HttpRequest, HttpResponse, JsonResponse, QueryDict
from django.urls import reverse, resolve
from django.utils import timezone
from django.views.decorators.http import require_POST

from .models import SyncChange, SyncObject

log = logging.getLogger(__name__)

MAX_BATCH = 100
DELETED = "This was deleted on another device."
FORBIDDEN = "You no longer have access to this."
NEWER = "A newer change from another device was kept."
# The outer request's headers a view may look at (host checks, "next" links, activity).
_META = ("HTTP_HOST", "SERVER_NAME", "SERVER_PORT", "REMOTE_ADDR", "HTTP_USER_AGENT", "HTTP_X_FORWARDED_FOR",
         "HTTP_X_FORWARDED_PROTO", "HTTP_X_FORWARDED_HOST", "HTTP_ACCEPT_LANGUAGE")


class Missing(Exception):
    """A change names something made offline whose own change never saved."""


class _Messages(list):
    """Stands in for django.contrib.messages on a call(): the view's flash messages, kept to read its answer."""
    used = False

    def add(self, level, message, extra_tags=""):
        self.append((level, str(message)))


def call(outer, url, data=None, body=None, quick=False):
    """POST to the view at `url` as the person signed in on `outer`: form `data`, or a JSON `body`. Returns
    (status, JSON reply or {}, error messages it flashed)."""
    from .app_access import AppAccessMiddleware
    request = HttpRequest()
    request.method = "POST"
    request.path = request.path_info = url
    request.META = {key: outer.META[key] for key in _META if key in outer.META}
    request.META.update(REQUEST_METHOD="POST", PATH_INFO=url, HTTP_X_REQUESTED_WITH="XMLHttpRequest")
    if quick:
        request.META["HTTP_X_QUICK_ADD"] = "1"
    if body is not None:
        request._body = json.dumps(body).encode()
        request.META["CONTENT_TYPE"] = "application/json"
        request.POST = QueryDict()
    else:
        request._body = b""
        post = QueryDict(mutable=True)
        for key, value in (data or {}).items():
            if value is not None:
                post.setlist(key, [str(v) for v in value] if isinstance(value, (list, tuple)) else [str(value)])
        request.POST = post
    request.COOKIES, request.user = outer.COOKIES, outer.user
    if hasattr(outer, "session"):
        request.session = outer.session
    request._messages = _Messages()
    request._dont_enforce_csrf_checks = True
    match = resolve(url)
    try:
        response = AppAccessMiddleware(lambda r: None).process_view(request, match.func, match.args, match.kwargs) \
            or match.func(request, *match.args, **match.kwargs)
    except Http404:
        response = HttpResponse(status=404)
    except PermissionDenied:
        response = HttpResponse(status=403)
    reply = {}
    if response.get("Content-Type", "").startswith("application/json"):
        try:
            reply = json.loads(response.content or b"{}")
        except ValueError:
            reply = {}
    errors = [text for level, text in request._messages if level >= 40]
    return response.status_code, reply if isinstance(reply, dict) else {}, errors


def outcome(status, reply, errors):
    """A call()'s answer as a result: applied, forbidden (403 / 404), conflict (409) or rejected with its reason."""
    if status in (403, 404):
        return {"status": "forbidden", "message": FORBIDDEN}
    if status == 409:
        return {"status": "conflict", "reply": reply}
    reason = reply.get("error") or (reply.get("errors") or [None])[0] or (errors[0] if errors else "")
    if status >= 400 or reply.get("ok") is False or errors:
        return {"status": "rejected", "message": reason or "That change couldn't be saved."}
    return {"status": "applied", "reply": reply}


class Context:
    def __init__(self, request, offset):
        self.request, self.user, self.offset = request, request.user, offset
        self.touched = ""                               # "grocery:12": what the change being applied names

    def run(self, url, data=None, body=None, quick=False):
        return outcome(*call(self.request, url, data, body, quick))

    def when(self, change):
        """When the change was made, on this server's clock (the browser's clock corrected by `offset`)."""
        at = change.get("at")
        if not isinstance(at, (int, float)):
            return None
        moment = datetime.fromtimestamp(at / 1000, tz=dt_timezone.utc) + self.offset
        return min(moment, timezone.now())

    def newer(self, stamp, change):
        """True when the server's value (changed at `stamp`) is later than this change - the server's stays. A
        change this person's own offline changes made here (an earlier one in the outbox) never counts."""
        moment = self.when(change)
        if not (stamp and moment and stamp > moment):
            return False
        if self.touched:
            mine = SyncChange.objects.filter(user=self.user, target=self.touched).order_by("-created_at")                 .values_list("created_at", flat=True).first()
            if mine and stamp <= mine + timedelta(seconds=1):
                return False
        return True

    def target(self, change, kind):
        value = change.get("target")
        if isinstance(value, int) and not isinstance(value, bool):
            pk = value
        elif str(value or "").isdigit():
            pk = int(value)
        else:
            try:
                client_id = uuid.UUID(str(value or ""))
            except ValueError:
                raise Missing() from None
            row = SyncObject.objects.filter(user=self.user, client_id=client_id, kind=kind).first()
            if row is None:
                raise Missing()
            pk = row.object_id
        self.touched = f"{kind}:{pk}"
        return pk

    def made(self, change, kind, object_id):
        """Remember what a new item's uuid became."""
        try:
            client_id = uuid.UUID(str(change.get("client_id") or ""))
        except ValueError:
            return
        if object_id:
            self.touched = f"{kind}:{object_id}"
            SyncObject.objects.get_or_create(user=self.user, client_id=client_id,
                                             defaults={"kind": kind, "object_id": object_id})


def _deleted():
    return {"status": "deleted", "message": DELETED}


def _skipped():
    return {"status": "skipped", "message": NEWER}


def _created(ctx, change, kind, result, object_id):
    if result["status"] == "applied":
        if not object_id:
            return {"status": "rejected", "message": "That couldn't be saved."}
        ctx.made(change, kind, object_id)
        result["object_id"] = object_id
    return result


def _newest(model, **filters):
    return model.objects.filter(**filters).order_by("-pk").values_list("pk", flat=True).first() or 0


def _text(value, limit):
    return str(value or "").strip()[:limit]


# ---------------------------------------------------------------------------
# Planner
# ---------------------------------------------------------------------------

def _schedule_item(pk):
    from albright_planner.models import ScheduleItem
    return ScheduleItem.objects.filter(pk=pk).first()


def planner_add(ctx, change):
    p = change["payload"]
    result = ctx.run(reverse("albright_planner:new"), {
        "title": _text(p.get("title"), 200), "kind": "task", "date": p.get("date") or "",
        "start_time": p.get("time") or "", "notes": p.get("notes") or "", "checkable": "on", "priority": "none",
        "repeat": "none", "ends": "never"}, quick=True)
    return _created(ctx, change, "planner", result, result.get("reply", {}).get("id"))


def planner_check(ctx, change):
    from albright_planner.models import ItemCompletion
    pk = ctx.target(change, "planner")
    if _schedule_item(pk) is None:
        return _deleted()
    p = change["payload"]
    done = bool(p.get("done"))
    completion = ItemCompletion.objects.filter(item_id=pk, occurrence_date=p.get("date")).first()
    if (completion is not None) != done and completion is not None and \
            ctx.newer(completion.skipped_at or completion.completed_at, change):
        return _skipped()
    return ctx.run(reverse("albright_planner:toggle", args=[pk]), {"date": p.get("date"), "checked": "1" if done else "0"})


def planner_skip(ctx, change):
    pk = ctx.target(change, "planner")
    item = _schedule_item(pk)
    if item is None:
        return _deleted()
    p = change["payload"]
    if item.is_workout or item.is_weigh_in:
        return ctx.run(reverse("albright_planner:skip", args=[pk]), {"date": p.get("date"), "reason": p.get("reason") or ""})
    if item.repeats:
        return ctx.run(reverse("albright_planner:remove", args=[pk]), {"date": p.get("date"), "scope": "this"})
    return {"status": "rejected", "message": "Only a repeating item or a planned workout can be skipped."}


def planner_edit(ctx, change):
    pk = ctx.target(change, "planner")
    item = _schedule_item(pk)
    if item is None:
        return _deleted()
    if ctx.newer(item.updated_at, change):
        return _skipped()
    p = change["payload"]
    return ctx.run(reverse("albright_planner:quick_edit", args=[pk]), {
        "field": p.get("field"), "value": p.get("value", ""), "date": p.get("date"),
        "start": p.get("start", ""), "end": p.get("end", "")})


def planner_checklist(ctx, change):
    from albright_planner.models import ChecklistItemCompletion, TaskChecklistItem
    pk = ctx.target(change, "planner")
    p = change["payload"]
    row = TaskChecklistItem.objects.filter(pk=p.get("row"), task_id=pk).first()
    if _schedule_item(pk) is None or row is None:
        return _deleted()
    tick = ChecklistItemCompletion.objects.filter(checklist_item=row, occurrence_date=p.get("date")).first()
    if tick is not None and not p.get("done") and ctx.newer(tick.done_at, change):
        return _skipped()
    return ctx.run(reverse("albright_planner:checklist_toggle", args=[pk, row.pk]),
                   {"date": p.get("date"), "checked": "1" if p.get("done") else "0"})


def planner_delete(ctx, change):
    pk = ctx.target(change, "planner")
    item = _schedule_item(pk)
    if item is None:
        return {"status": "applied"}                    # already gone: what was asked for
    p = change["payload"]
    scope = p.get("scope") if p.get("scope") in ("this", "all") else "all"
    return ctx.run(reverse("albright_planner:remove", args=[pk]), {"date": p.get("date") or item.date, "scope": scope})


# ---------------------------------------------------------------------------
# Groceries
# ---------------------------------------------------------------------------

def _grocery(pk):
    from albright_meals.models import GroceryItem
    return GroceryItem.objects.active().filter(pk=pk).first()


def grocery_add(ctx, change):
    result = ctx.run(reverse("albright_meals:groceries"), {"line": _text(change["payload"].get("line"), 300)})
    return _created(ctx, change, "grocery", result, result.get("reply", {}).get("id"))


def grocery_check(ctx, change):
    pk = ctx.target(change, "grocery")
    item = _grocery(pk)
    if item is None:
        return _deleted()
    checked = bool(change["payload"].get("checked"))
    if item.checked != checked and ctx.newer(item.updated_at, change):
        return _skipped()
    return ctx.run(reverse("albright_meals:grocery_toggle", args=[pk]), {"checked": "1" if checked else "0"})


def grocery_edit(ctx, change):
    pk = ctx.target(change, "grocery")
    item = _grocery(pk)
    if item is None:
        return _deleted()
    if ctx.newer(item.updated_at, change):
        return _skipped()
    return ctx.run(reverse("albright_meals:grocery_edit", args=[pk]), {"line": _text(change["payload"].get("line"), 300)})


def grocery_delete(ctx, change):
    pk = ctx.target(change, "grocery")
    if _grocery(pk) is None:
        return {"status": "applied"}
    return ctx.run(reverse("albright_meals:grocery_delete", args=[pk]))


# ---------------------------------------------------------------------------
# Notes
# ---------------------------------------------------------------------------

def _note(pk):
    from albright_notes.models import Note
    return Note.all_objects.filter(pk=pk).first()


def _capture(ctx, title, body):
    result = ctx.run(reverse("albright_notes:capture"), {"title": _text(title, 200) or "Untitled", "text": body or ""},
                     quick=True)
    return result, result.get("reply", {}).get("id")


def note_create(ctx, change):
    p = change["payload"]
    result, pk = _capture(ctx, p.get("title"), p.get("body"))
    return _created(ctx, change, "note", result, pk)


def note_edit(ctx, change):
    """The note's version decides: changed on the server since the version this edit started from -> the edit is
    kept as "<title> (offline copy)" instead of overwriting anyone's newer text."""
    pk = ctx.target(change, "note")
    note = _note(pk)
    if note is None or note.deleted_at is not None:
        return _deleted()
    p = change["payload"]
    base = change.get("base") if isinstance(change.get("base"), int) else None
    body = {"version": base} if base is not None else {"version": note.version}
    for key in ("title", "body"):
        if isinstance(p.get(key), str):
            body[key] = p[key]
    result = ctx.run(reverse("albright_notes:save", args=[pk]), body=body)
    if result["status"] != "conflict":
        result.pop("reply", None)
        return result
    title = p.get("title") if isinstance(p.get("title"), str) else note.title
    copy, copy_id = _capture(ctx, f"{(title or 'Untitled')[:180]} (offline copy)",
                             p.get("body") if isinstance(p.get("body"), str) else note.body)
    if copy["status"] != "applied" or not copy_id:
        return copy
    return {"status": "conflict", "note_id": pk, "copy_id": copy_id, "title": title or "Untitled",
            "message": "This note changed on another device - your offline edit was saved as a copy."}


def note_check(ctx, change):
    pk = ctx.target(change, "note")
    note = _note(pk)
    if note is None or note.deleted_at is not None:
        return _deleted()
    p = change["payload"]
    result = ctx.run(reverse("albright_notes:check", args=[pk]),
                     body={"item_id": str(p.get("item_id") or ""), "checked": bool(p.get("checked"))})
    if result["status"] == "conflict":
        return {"status": "rejected", "message": "That checklist item was changed on another device."}
    return result


def note_delete(ctx, change):
    pk = ctx.target(change, "note")
    note = _note(pk)
    if note is None or note.deleted_at is not None:
        return {"status": "applied"}
    return ctx.run(reverse("albright_notes:delete", args=[pk]))


def note_restore(ctx, change):
    """Restore after "deleted on another device": out of Recently deleted with the offline text - or, if it's gone
    for good, as a new note."""
    p = change["payload"]
    try:
        pk = ctx.target(change, "note")
    except Missing:
        pk = None
    note = _note(pk) if pk else None
    if note is not None and note.deleted_at is not None and note.owner_id == ctx.user.pk:
        result = ctx.run(reverse("albright_notes:restore", args=[pk]))
        if result["status"] != "applied":
            return result
        note.refresh_from_db()
        fields = {key: p[key] for key in ("title", "body") if isinstance(p.get(key), str)}
        if fields:
            result = ctx.run(reverse("albright_notes:save", args=[pk]), body={**fields, "force": True})
        result.pop("reply", None)
        return {**result, "object_id": pk}
    result, new_pk = _capture(ctx, p.get("title"), p.get("body"))
    return _created(ctx, change, "note", result, new_pk)


def note_resolve(ctx, change):
    """After a conflict: keep mine (the offline copy's text goes into the note, the copy is deleted), keep theirs (the
    copy is deleted) or keep both (nothing changes)."""
    p = change["payload"]
    keep = p.get("keep")
    if keep == "both":
        return {"status": "applied"}
    note, copy = _note(p.get("note_id")), _note(p.get("copy_id"))
    if note is not None:
        ctx.touched = f"note:{note.pk}"
    if copy is None or copy.deleted_at is not None:
        return {"status": "applied"} if keep == "theirs" else             {"status": "rejected", "message": "The offline copy was deleted - nothing to keep."}
    if keep == "mine":
        if note is None or note.deleted_at is not None:
            return _deleted()
        title = p.get("title") if isinstance(p.get("title"), str) and p.get("title") else note.title
        result = ctx.run(reverse("albright_notes:save", args=[note.pk]), body={"title": title, "body": copy.body, "force": True})
        if result["status"] != "applied":
            return result
    elif keep != "theirs":
        return {"status": "rejected", "message": "Pick which version to keep."}
    return ctx.run(reverse("albright_notes:delete", args=[copy.pk]))


# ---------------------------------------------------------------------------
# Fitness
# ---------------------------------------------------------------------------

def food_log(ctx, change):
    from albright_fitness.models import FoodLogEntry
    p = change["payload"]
    before = _newest(FoodLogEntry, owner=ctx.user)
    result = ctx.run(reverse("albright_fitness:food_log"), {
        "kind": "quick", "date": p.get("date") or "", "slot": p.get("slot") or "", "label": _text(p.get("label"), 200),
        "calories": p.get("calories") or "", "protein": p.get("protein") or "", "carbs": p.get("carbs") or "",
        "fat": p.get("fat") or ""})
    after = _newest(FoodLogEntry, owner=ctx.user)
    return _created(ctx, change, "food", result, after if after > before else None)


def weight_log(ctx, change):
    from albright_fitness.models import WeightEntry
    p = change["payload"]
    result = ctx.run(reverse("albright_fitness:log_weight"), {"weight": p.get("weight") or "", "unit": p.get("unit") or "",
                                                               "date": p.get("date") or ""}, quick=True)
    entry = WeightEntry.objects.filter(owner=ctx.user, date=p.get("date")).values_list("pk", flat=True).first() \
        if p.get("date") else _newest(WeightEntry, owner=ctx.user)
    return _created(ctx, change, "weight", result, entry)


def workout_quick(ctx, change):
    from albright_fitness.models import Workout
    before = _newest(Workout, owner=ctx.user)
    result = ctx.run(reverse("albright_fitness:quick"), {"text": _text(change["payload"].get("text"), 2000)}, quick=True)
    after = _newest(Workout, owner=ctx.user)
    return _created(ctx, change, "workout", result, after if after > before else None)


# ---------------------------------------------------------------------------
# Meals
# ---------------------------------------------------------------------------

def recipe_rate(ctx, change):
    from albright_meals.models import Recipe, RecipeRating
    pk = ctx.target(change, "recipe")
    if not Recipe.objects.filter(pk=pk).exists():
        return _deleted()
    rating = RecipeRating.objects.filter(recipe_id=pk, user=ctx.user).first()
    if rating is not None and ctx.newer(rating.updated_at, change):
        return _skipped()
    level = change["payload"].get("level")
    return ctx.run(reverse("albright_meals:recipe_rate", args=[pk]), {"level": "" if level in (None, "") else level})


def meal_move(ctx, change):
    from albright_meals.models import MealPlanEntry
    pk = ctx.target(change, "meal")
    entry = MealPlanEntry.objects.filter(pk=pk).first()
    if entry is None:
        return _deleted()
    if ctx.newer(entry.updated_at, change):
        return _skipped()
    p = change["payload"]
    result = ctx.run(reverse("albright_meals:plan_move", args=[pk]), {"date": p.get("date"), "slot": p.get("slot") or entry.slot},
                     quick=True)
    result.pop("reply", None)
    return result


ACTIONS = {
    "planner.add": planner_add, "planner.check": planner_check, "planner.skip": planner_skip,
    "planner.edit": planner_edit, "planner.checklist": planner_checklist, "planner.delete": planner_delete,
    "planner.restore": planner_add,
    "grocery.add": grocery_add, "grocery.check": grocery_check, "grocery.edit": grocery_edit,
    "grocery.delete": grocery_delete, "grocery.restore": grocery_add,
    "note.create": note_create, "note.edit": note_edit, "note.check": note_check, "note.delete": note_delete,
    "note.restore": note_restore, "note.resolve": note_resolve,
    "food.log": food_log, "weight.log": weight_log, "workout.quick": workout_quick,
    "recipe.rate": recipe_rate, "meal.move": meal_move,
}


def apply(ctx, change):
    """One change -> its result (recorded by id, so it is never applied twice)."""
    try:
        change_id = uuid.UUID(str(change.get("id") or ""))
    except ValueError:
        return {"status": "rejected", "message": "That change has no id."}
    out = {"id": str(change_id)}
    if str(change.get("user") or "") != str(ctx.user.pk):
        return {**out, "status": "rejected", "message": "This change was made by someone else on this device."}
    done = SyncChange.objects.filter(user=ctx.user, client_change_id=change_id).first()
    if done is not None:
        return {**out, "status": done.status, **done.result, "repeat": True}
    handler = ACTIONS.get(change.get("action"))
    if handler is None or not isinstance(change.get("payload", {}), dict):
        return {**out, "status": "rejected", "message": "That kind of change can't be made offline."}
    change.setdefault("payload", {})
    ctx.touched = ""
    try:
        with transaction.atomic():
            try:
                result = handler(ctx, change)
            except Missing:
                result = {"status": "rejected", "message": "It belongs to something new that didn't save."}
            result.pop("reply", None)
            status = result.pop("status")
            SyncChange.objects.create(user=ctx.user, client_change_id=change_id, action=change["action"][:40],
                                      target=ctx.touched[:40], status=status, result=result)
    except IntegrityError:                              # the same change arrived twice at once: the other one won
        done = SyncChange.objects.filter(user=ctx.user, client_change_id=change_id).first()
        if done is None:
            return {**out, "status": "error", "message": "Try again."}
        return {**out, "status": done.status, **done.result, "repeat": True}
    except Exception:  # noqa: BLE001 - one broken change must not sink the batch; the browser retries it
        log.exception("Offline sync: %s failed", change.get("action"))
        return {**out, "status": "error", "message": "Something went wrong saving this - it will try again."}
    return {**out, "status": status, **result}


@login_required
@require_POST
def sync(request):
    try:
        data = json.loads(request.body or b"{}")
    except (ValueError, UnicodeDecodeError):
        return JsonResponse({"ok": False, "error": "bad request"}, status=400)
    changes = data.get("changes") if isinstance(data, dict) else None
    if not isinstance(changes, list):
        return JsonResponse({"ok": False, "error": "bad request"}, status=400)
    sent = data.get("sent_at")
    offset = timedelta(0)
    if isinstance(sent, (int, float)):
        offset = timezone.now() - datetime.fromtimestamp(sent / 1000, tz=dt_timezone.utc)
    ctx = Context(request, offset)
    results = [apply(ctx, change) if isinstance(change, dict) else {"status": "rejected", "message": "bad change"}
               for change in changes[:MAX_BATCH]]
    return JsonResponse({"ok": True, "user": request.user.pk, "results": results})
