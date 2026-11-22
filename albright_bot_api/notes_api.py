"""Bot API v1 - Notes (/api/bot/v1/notes...): a bot saves notes for its owner.

Like Meals, these write straight to the Notes app's own table - that's what
they're for. A note a bot creates belongs to the token's owner and is labeled
with the bot's name (Note.created_by). notes:write creates notes and changes
only the ones this bot created or that someone shared with the owner "Can
edit"; notes:read lists the owner's own notes (never ones others shared with
them). If the owner can't open Notes (Admin > Users), every call is a 403.
Every call is in the Activity log (auth.bot_endpoint)."""
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

from albright_labs import attachments

from .auth import ApiError, bot_endpoint, json_body

LIST_LIMIT = 200
MAX_TEXT = 100_000


def _models():
    from albright_notes import models
    return models


def _require_notes_access(token):
    from albright_labs import app_access
    from albright_labs.models import UserPreference
    if not app_access.can_use(token.owner, UserPreference.for_user(token.owner), "notes"):
        raise ApiError(403, "The token's owner can't open Notes.", "rejected: owner has no Notes access")


def _str(data, key, limit, required=False):
    value = data.get(key)
    if value in (None, ""):
        if required:
            raise ApiError(400, f'"{key}" is required.', f"rejected: missing {key}")
        return ""
    if not isinstance(value, str):
        raise ApiError(400, f'"{key}" must be a string.', f"rejected: {key} not a string")
    if len(value) > limit:
        raise ApiError(400, f'"{key}" is longer than {limit} characters.', f"rejected: {key} too long")
    if required and not value.strip():
        raise ApiError(400, f'"{key}" is required.', f"rejected: missing {key}")
    return value


def _tags(data, key="tags"):
    from albright_notes.models import MAX_TAG_LENGTH, MAX_TAGS
    from albright_notes.notes import clean_tags
    value = data.get(key)
    if value in (None, ""):
        return []
    if not isinstance(value, list) or len(value) > MAX_TAGS or \
            any(not isinstance(t, str) or len(t) > MAX_TAG_LENGTH for t in value):
        raise ApiError(400, f'"{key}" must be a list of up to {MAX_TAGS} strings (each <= {MAX_TAG_LENGTH}).',
                       f"rejected: bad {key}")
    return clean_tags(value)


def _link(request, token, value):
    """{"app", "type", "id"} or {"url"} -> (kind, record) the owner can open, or a 400."""
    from albright_notes import links
    if not isinstance(value, dict):
        raise ApiError(400, '"link" must be an object.', "rejected: bad link")
    if value.get("url"):
        if not isinstance(value["url"], str) or len(value["url"]) > 2000:
            raise ApiError(400, '"link.url" must be a string.', "rejected: bad link")
        found = links.from_url(token.owner, value["url"], host=request.get_host())
    else:
        found = links.get_record(token.owner, value.get("app"), value.get("type"), value.get("id"))
    if found is None:
        kinds = ", ".join(f"{k.app}/{k.type}" for k in links.KINDS)
        raise ApiError(400, f"No record the owner can open for that link. Kinds: {kinds}.", "rejected: bad link")
    return found


def _note_json(note, request=None, files=None):
    """files: {note id: [...]} from attachments.api_lists (a list); without it this note's own are looked up."""
    from albright_notes import smart
    due = smart.next_due(note.body, timezone.localdate()) if "@[date:" in (note.body or "") else None
    return {"id": note.id, "title": note.title, "tags": note.tags, "pinned": note.pinned, "archived": note.archived,
            "next_due": due[0].isoformat() if due else None,
            "created_by": note.created_by, "created_at": note.created_at.isoformat(),
            "updated_at": note.updated_at.isoformat(),
            "attachments": files.get(note.pk, []) if files is not None else attachments.api_list(note, request)}


@bot_endpoint("notes:read")
def notes_list(request, token):
    from albright_notes import notes
    _require_notes_access(token)
    qs = _models().Note.objects.filter(owner=token.owner, archived=request.GET.get("archived") == "1")
    tag = request.GET.get("tag", "").strip().lstrip("#").lower()[:50]
    if tag:
        qs = qs.filter(tags__contains=[tag])
    q = request.GET.get("q", "").strip()[:200]
    qs = notes.search(qs, q) if q else qs.order_by("-pinned", "-updated_at", "-pk")
    page = list(qs[:LIST_LIMIT])
    files = attachments.api_lists(_models().Note, [n.pk for n in page], request)
    rows = [_note_json(n, request, files) for n in page]
    return {"notes": rows}, 200, f"listed {len(rows)} notes" + (f" q={q[:40]}" if q else "") + (f" tag={tag}" if tag else "")


@bot_endpoint("notes:write", methods=("POST",))
def notes_create(request, token):
    from albright_notes import links, notes
    _require_notes_access(token)
    data = json_body(request)
    title = _str(data, "title", 200, required=True).strip()
    body = _str(data, "body", MAX_TEXT)
    tags = _tags(data)
    pinned = data.get("pinned", False)
    if not isinstance(pinned, bool):
        raise ApiError(400, '"pinned" must be true or false.', "rejected: bad pinned")
    found = _link(request, token, data["link"]) if data.get("link") else None
    from . import share_field
    share_field.validate(token, "notes", data)
    items = _items(data)
    note = notes.create(token.owner, title, body, tags, pinned=pinned, token=token)
    added = _add_items(note, items, token) if items else None
    if added:
        note = notes.save(note, token.name, actor=token.owner, body=added[0])
    shared = share_field.apply(token, note, data)
    if found:
        links.add(note, *found)
    return ({**_note_json(note, request), "url": request.build_absolute_uri(reverse("albright_notes:note", args=[note.pk])),
             "linked": found[0].title(found[1]) if found else None, "shared_with": shared}, 201, f"note #{note.pk}: {note.title[:80]}")


@bot_endpoint("notes:write", methods=("PATCH",))
def note_update(request, token, note_id):
    from albright_notes import notes
    _require_notes_access(token)
    models = _models()
    note = models.Note.objects.filter(pk=note_id).first()
    from albright_labs import sharing
    shared_edit = note is not None and note.owner_id != token.owner_id and sharing.can_edit(token.owner, note)
    if note is None or not (note.bot_token_id == token.pk or shared_edit):
        # Someone else's note, or one this bot didn't write: same answer as a missing one.
        raise ApiError(404, f"No note {note_id} this bot can change.", "rejected: note not found or not the bot's")
    data = json_body(request)
    if not {"append", "body", "tags", "items"} & set(data):
        raise ApiError(400, 'Send "append", "body", "items" or "tags".', "rejected: nothing to change")
    items = _items(data)
    if "append" in data and "body" in data:
        raise ApiError(400, 'Send "append" or "body", not both.', "rejected: append and body")
    fields, changed = {}, []
    if "body" in data:
        fields["body"] = _str(data, "body", MAX_TEXT)
        changed.append("replaced body")
    if "append" in data:
        text = _str(data, "append", MAX_TEXT, required=True)
        fields["body"] = (note.body.rstrip("\n") + "\n\n" + text) if note.body.strip() else text
        if len(fields["body"]) > MAX_TEXT:
            raise ApiError(400, f"The note would be longer than {MAX_TEXT} characters.", "rejected: too long")
        changed.append("appended")
    if "tags" in data:
        fields["tags"] = list(note.tags or []) + _tags(data)
        changed.append("added tags")
    if items:
        if "body" in fields:
            note.body = fields["body"]                # items go after the new text
        fields["body"], summary = _add_items(note, items, token)
        changed.append(f"added {summary['lines']} item(s)")
    note = notes.save(note, token.name, actor=token.owner, **fields)
    return _note_json(note, request), 200, f"updated note #{note.pk}: {', '.join(changed)}"


def _items(data):
    """Validate {"items": [{"text", "due"?, "time"?, "grocery"?, "checklist"?}]} (at most 50)."""
    items = data.get("items")
    if items is None:
        return []
    if not isinstance(items, list) or len(items) > 50 or not all(isinstance(i, dict) for i in items):
        raise ApiError(400, '"items" must be a list of up to 50 objects.', "rejected: bad items")
    from albright_notes import smart
    for item in items:
        if not isinstance(item.get("text"), str) or not item["text"].strip():
            raise ApiError(400, 'Each item needs "text".', "rejected: item without text")
        if item.get("due") is not None:
            try:
                smart.parse_when(item["due"], item.get("time"))
            except (TypeError, ValueError):
                raise ApiError(400, '"due" must be YYYY-MM-DD and "time" HH:MM.', "rejected: bad due date")
        for flag in ("grocery", "checklist"):
            if flag in item and not isinstance(item[flag], bool):
                raise ApiError(400, f'"{flag}" must be true or false.', f"rejected: bad {flag}")
    return items


def _add_items(note, items, token):
    from albright_notes import smart
    try:
        return smart.append_items(note, items, token.owner, token=token)
    except smart.NoGroceries:
        raise ApiError(400, "The owner has no grocery list (Meals).", "rejected: no grocery list")


@csrf_exempt
def notes_collection(request):
    """GET lists (notes:read); POST creates (notes:write)."""
    return notes_create(request) if request.method == "POST" else notes_list(request)


def note_item(request, note_id):
    """GET reads one note with its text (notes:read - overview_api); PATCH changes it (notes:write)."""
    if request.method == "GET":
        from .overview_api import note_detail
        return note_detail(request, note_id=note_id)
    return note_update(request, note_id=note_id)
