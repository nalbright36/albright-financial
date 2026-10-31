"""Albright Notes pages (/notes/): the list (with quick capture) and one
note's page (Markdown editor with autosave, preview, checklist, tags, pin,
archive, sharing, links, delete).

Who can do what - always checked here, on the server:
  - see a note: its owner and the people it's shared with; anyone else gets 404
    (Note.objects.visible_to);
  - change its title, body, tags, checklist and links: the owner, and people
    it's shared with "Can edit";
  - pin, archive, share and delete it: the owner only.
Opening Notes at all needs access in Admin > Users (albright_labs.app_access)."""
import json

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST

from albright_control.review_list import ago
from albright_crypto_app.links import is_web_url, link_status

from . import links, notes, render as md
from .models import Note, NoteLink, NoteShare


def _json(request):
    try:
        data = json.loads(request.body or b"{}")
    except (ValueError, UnicodeDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _visible(request, pk):
    return get_object_or_404(Note.objects.visible_to(request.user).select_related("owner"), pk=pk)


def _editable(request, pk):
    note = _visible(request, pk)
    if not note.can_edit(request.user):
        raise Http404          # read-only for this user: no write endpoint admits it exists for them
    return note


def _owned(request, pk):
    return get_object_or_404(Note.objects.select_related("owner"), pk=pk, owner=request.user)


def _note_url(note):
    return reverse("albright_notes:note", args=[note.pk])


def _back(request, note=None):
    target = request.POST.get("next", "")
    if target.startswith("/") and not target.startswith("//"):
        return redirect(target)
    return redirect(_note_url(note) if note else "albright_notes:list")


def _state(note, user):
    """What the editor needs after a change."""
    return {"ok": True, "version": note.version, "html": md.render(note.body, editable=note.can_edit(user)),
            "body": note.body, "title": note.title, "tags": note.tags, "updated_by": note.updated_by,
            "updated": f"{timezone.localtime(note.updated_at):%I:%M %p}".lstrip("0")}


# ---------------------------------------------------------------------------
# The list
# ---------------------------------------------------------------------------

@login_required
def note_list(request):
    user = request.user
    q = request.GET.get("q", "").strip()[:200]
    tag = request.GET.get("tag", "").strip().lower()[:50]
    shared = request.GET.get("shared") == "1"
    archived = request.GET.get("archived") == "1"

    visible = Note.objects.visible_to(user)
    qs = visible.filter(archived=archived)
    if shared:
        qs = qs.exclude(owner=user)
    if tag:
        qs = qs.filter(tags__contains=[tag])
    qs = notes.search(qs, q) if q else qs.order_by("-pinned", "-updated_at", "-pk")
    rows = list(qs.select_related("owner").prefetch_related("links", "shares")[:300])

    allowed = links._allowed_apps(user)
    now = timezone.now()
    items = [{
        "note": n, "preview": md.preview(n.body), "updated": ago(n.updated_at, now),
        "shared": bool(n.shares.all()) or n.owner_id != user.pk, "mine": n.owner_id == user.pk,
        "chips": links.chips(user, list(n.links.all()), allowed),
    } for n in rows]
    return render(request, "albright_notes/list.html", {
        "items": items, "q": q, "tag": tag, "shared": shared, "archived": archived,
        "tags": notes.all_tags(visible.filter(archived=False)),
        "filtered": bool(q or tag or shared or archived),
    })


@login_required
@require_POST
def capture(request):
    title, body = notes.split_capture(request.POST.get("text", ""))
    if not request.POST.get("text", "").strip():
        messages.error(request, "Type something first.")
        return redirect("albright_notes:list")
    note = notes.create(request.user, title, body)
    if request.POST.get("open"):
        return redirect(_note_url(note))
    messages.success(request, f"Saved “{note.title}”.")
    return redirect("albright_notes:list")


# ---------------------------------------------------------------------------
# One note
# ---------------------------------------------------------------------------

@login_required
@never_cache
def note_page(request, pk):
    note = _visible(request, pk)
    user = request.user
    can_edit, is_owner = note.can_edit(user), note.owner_id == user.pk
    shares = list(note.shares.select_related("user")) if is_owner else []
    shared_ids = {s.user_id for s in shares} | {note.owner_id}
    return render(request, "albright_notes/note.html", {
        "note": note, "can_edit": can_edit, "is_owner": is_owner,
        "html": md.render(note.body, editable=can_edit),
        "chips": links.chips(user, list(note.links.all())),
        "shares": shares, "share_choices": [u for u in notes.notes_users() if u.pk not in shared_ids] if is_owner else [],
        "permissions": NoteShare.PERMISSIONS, "updated": ago(note.updated_at),
        "my_permission": None if is_owner else note.shares.filter(user=user).values_list("permission", flat=True).first(),
    })


@login_required
@require_POST
def note_save(request, pk):
    """Autosave (JSON): {title?, body?, tags?, version, force?}. 409 with the newer version's details when
    someone else saved it since `version` (force: true overwrites on purpose)."""
    note = _editable(request, pk)
    data = _json(request)
    fields = {key: data[key] for key in ("title", "body", "tags") if key in data and data[key] is not None}
    for key in ("title", "body"):
        if key in fields and not isinstance(fields[key], str):
            return JsonResponse({"ok": False, "error": f"{key} must be text"}, status=400)
    if not fields:
        return JsonResponse({"ok": False, "error": "nothing to save"}, status=400)
    base = None if data.get("force") else data.get("version")
    if base is not None and not isinstance(base, int):
        return JsonResponse({"ok": False, "error": "version must be a number"}, status=400)
    try:
        note = notes.save(note, notes.user_label(request.user), base_version=base, **fields)
    except notes.Conflict as conflict:
        return JsonResponse({"ok": False, "conflict": True, **_state(conflict.note, request.user)}, status=409)
    return JsonResponse(_state(note, request.user))


@login_required
@require_POST
def note_check(request, pk):
    """Tick or untick checklist item `index` (JSON {index, checked, version}) - rewrites that Markdown line."""
    note = _editable(request, pk)
    data = _json(request)
    index, checked, version = data.get("index"), data.get("checked"), data.get("version")
    if not isinstance(index, int) or isinstance(index, bool) or not isinstance(checked, bool):
        return JsonResponse({"ok": False, "error": "bad request"}, status=400)
    body = md.toggle(note.body, index, checked)
    if body is None:
        return JsonResponse({"ok": False, "error": "no such checklist item"}, status=400)
    try:
        note = notes.save(note, notes.user_label(request.user),
                          base_version=version if isinstance(version, int) else None, body=body)
    except notes.Conflict as conflict:
        return JsonResponse({"ok": False, "conflict": True, **_state(conflict.note, request.user)}, status=409)
    return JsonResponse(_state(note, request.user))


@login_required
@require_POST
def note_pin(request, pk):
    note = _owned(request, pk)
    note.pinned = not note.pinned
    note.save(update_fields=["pinned", "updated_at"])
    messages.success(request, "Pinned." if note.pinned else "Unpinned.")
    return _back(request, note)


@login_required
@require_POST
def note_archive(request, pk):
    note = _owned(request, pk)
    note.archived = not note.archived
    note.save(update_fields=["archived", "updated_at"])
    messages.success(request, f"Archived “{note.title}”." if note.archived else "Moved back to your notes.")
    return redirect("albright_notes:list") if note.archived else _back(request, note)


@login_required
@require_POST
def note_delete(request, pk):
    note = _owned(request, pk)
    title = note.title
    note.delete()
    messages.success(request, f"Deleted “{title}”.")
    return redirect("albright_notes:list")


# ---------------------------------------------------------------------------
# Sharing (owner only)
# ---------------------------------------------------------------------------

@login_required
@require_POST
def share_add(request, pk):
    note = _owned(request, pk)
    permission = request.POST.get("permission", NoteShare.VIEW)
    target = notes.notes_users().exclude(pk=request.user.pk).filter(pk=request.POST.get("user") or 0).first()
    if target is None or permission not in dict(NoteShare.PERMISSIONS):
        messages.error(request, "Pick someone who can open Notes.")
        return _back(request, note)
    NoteShare.objects.update_or_create(note=note, user=target, defaults={"permission": permission})
    messages.success(request, f"Shared with {target.get_username()} ({dict(NoteShare.PERMISSIONS)[permission].lower()}).")
    return _back(request, note)


@login_required
@require_POST
def share_update(request, pk, share_id):
    note = _owned(request, pk)
    share = get_object_or_404(NoteShare, pk=share_id, note=note)
    if request.POST.get("remove"):
        share.delete()
        messages.success(request, f"Stopped sharing with {share.user.get_username()}.")
    elif request.POST.get("permission") in dict(NoteShare.PERMISSIONS):
        share.permission = request.POST["permission"]
        share.save(update_fields=["permission"])
        messages.success(request, f"{share.user.get_username()}: {share.get_permission_display().lower()}.")
    return _back(request, note)


# ---------------------------------------------------------------------------
# Links to records in other apps
# ---------------------------------------------------------------------------

@login_required
@require_GET
def link_search(request):
    """The picker: records matching ?q= in the apps this user can open."""
    return JsonResponse({"results": links.search(request.user, request.GET.get("q", ""))})


@login_required
@require_POST
def link_add(request, pk):
    """Link a record: picked ({app, type, id}) or a pasted site URL ({url})."""
    note = _editable(request, pk)
    data = request.POST
    if data.get("url"):
        found = links.from_url(request.user, data["url"], host=request.get_host())
        error = "That link isn't a record page on this site you can open (a recipe, ledger item, opportunity or strategy)."
    else:
        found = links.get_record(request.user, data.get("app"), data.get("type"), data.get("id"))
        error = "You can't link that record."
    if found is None:
        messages.error(request, error)
    else:
        link = links.add(note, *found)
        Note.objects.filter(pk=note.pk).update(updated_at=timezone.now())
        messages.success(request, f"Linked {link.label}.")
    return redirect(_note_url(note) + "#links")


@login_required
@require_POST
def link_remove(request, pk, link_id):
    note = _editable(request, pk)
    NoteLink.objects.filter(pk=link_id, note=note).delete()
    messages.success(request, "Link removed.")
    return redirect(_note_url(note) + "#links")


# ---------------------------------------------------------------------------
# External links in notes (render.outbound_href)
# ---------------------------------------------------------------------------

@login_required
def leave(request):
    """The warning page every unverified external link in a note goes through."""
    url = request.GET.get("to", "")
    domain, status = link_status(url)
    if status == "allowlisted":
        return redirect(url)
    return render(request, "albright_notes/leave.html", {
        "url": url if is_web_url(url) else "", "domain": domain, "status": status})
