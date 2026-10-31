"""Bot API v1 - Notes (/api/bot/v1/notes...): a bot saves notes for its owner.

Like Meals, these write straight to the Notes app's own table - that's what
they're for. A note a bot creates belongs to the token's owner and is labeled
with the bot's name (Note.created_by). notes:write creates notes and changes
only the ones this bot created or that someone shared with the owner "Can
edit"; notes:read lists the owner's own notes (never ones others shared with
them). If the owner can't open Notes (Admin > Users), every call is a 403.
Every call is in the Activity log (auth.bot_endpoint)."""
from django.urls import reverse
from django.views.decorators.csrf import csrf_exempt

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


def _note_json(note):
    return {"id": note.id, "title": note.title, "tags": note.tags, "pinned": note.pinned, "archived": note.archived,
            "created_by": note.created_by, "created_at": note.created_at.isoformat(),
            "updated_at": note.updated_at.isoformat()}


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
    rows = [_note_json(n) for n in qs[:LIST_LIMIT]]
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
    note = notes.create(token.owner, title, body, tags, pinned=pinned, token=token)
    if found:
        links.add(note, *found)
    return ({**_note_json(note), "url": request.build_absolute_uri(reverse("albright_notes:note", args=[note.pk])),
             "linked": found[0].title(found[1]) if found else None}, 201, f"note #{note.pk}: {note.title[:80]}")


@bot_endpoint("notes:write", methods=("PATCH",))
def note_update(request, token, note_id):
    from albright_notes import notes
    _require_notes_access(token)
    models = _models()
    note = models.Note.objects.filter(pk=note_id).first()
    shared_edit = note is not None and models.NoteShare.objects.filter(
        note=note, user=token.owner, permission=models.NoteShare.EDIT).exists()
    if note is None or not (note.bot_token_id == token.pk or shared_edit):
        # Someone else's note, or one this bot didn't write: same answer as a missing one.
        raise ApiError(404, f"No note {note_id} this bot can change.", "rejected: note not found or not the bot's")
    data = json_body(request)
    if not {"append", "body", "tags"} & set(data):
        raise ApiError(400, 'Send "append", "body" or "tags".', "rejected: nothing to change")
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
    note = notes.save(note, token.name, **fields)
    return _note_json(note), 200, f"updated note #{note.pk}: {', '.join(changed)}"


@csrf_exempt
def notes_collection(request):
    """GET lists (notes:read); POST creates (notes:write)."""
    return notes_create(request) if request.method == "POST" else notes_list(request)
