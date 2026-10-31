"""Albright Notes (/notes/): one three-pane workspace - Option A of
docs/design/"Albright Notes - design options.html". The folders and tags
sidebar (with counts), the notes list with search, and the open note's
editor (formatting toolbar, Markdown with a tickable checklist, autosave,
word count, links, sharing, pin / archive / delete). /notes/ opens the first
note in the list; /notes/<id>/ opens that one. The list's view, tag and
search stay in the query string as you move between notes.

Who can do what - always checked here, on the server:
  - see a note: its owner and the people it's shared with; anyone else gets 404
    (Note.objects.visible_to);
  - change its title, body, tags, checklist and links: the owner, and people
    it's shared with "Can edit";
  - pin, archive, share and delete it: the owner only.
Opening Notes at all needs access in Admin > Users (albright_labs.app_access)."""
import json
import re
from urllib.parse import urlencode

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

# The sidebar's folders: (key, label, icon) - Option A's order and glyphs.
VIEWS = [("all", "All notes", ""), ("pinned", "Pinned", "\U0001F4CC"), ("shared", "Shared with me", "\U0001F465"),
         ("checklists", "Checklists", "\u2611"), ("archived", "Archived", "\U0001F5C4")]
# A Markdown checklist line ("- [ ] ..." / "* [x] ..."), as a PostgreSQL regex (the Checklists folder).
CHECKLIST_SQL = r"(^|\n)[ \t]*(>[ \t]*)*[-*+][ \t]+\[[ xX]\]"
WORD = re.compile("[0-9A-Za-zÀ-￿]")     # a word has a letter or digit (as notes.js counts)
LIST_LIMIT = 300


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


def word_count(text):
    return sum(1 for w in (text or "").split() if WORD.search(w))


def _folder(qs, view, user):
    """Notes in one sidebar folder (archived notes only in Archived)."""
    if view == "archived":
        return qs.filter(archived=True)
    qs = qs.filter(archived=False)
    if view == "pinned":
        return qs.filter(pinned=True)
    if view == "shared":
        return qs.exclude(owner=user)
    if view == "checklists":
        return qs.filter(body__regex=CHECKLIST_SQL)
    return qs


def _row(note, user, now, allowed):
    tasks = md.checklist_lines(note.body)
    done = sum(1 for _line, checked in tasks if checked)
    shared_with_me = note.owner_id != user.pk
    if shared_with_me:
        summary = f"Shared by {note.owner.get_username()} \u00b7 edited {ago(note.updated_at, now)}"
    elif tasks:
        summary = f"{done} of {len(tasks)} done"                       # a checklist shows its progress
    else:
        summary = md.preview(note.body) or f"Edited {ago(note.updated_at, now)}"
    return {"note": note, "summary": summary, "updated": ago(note.updated_at, now),
            "shared": shared_with_me or bool(note.shares.all()), "chips": links.chips(user, list(note.links.all()), allowed)}


@login_required
@never_cache
def workspace(request, pk=None):
    user = request.user
    view = request.GET.get("view", "")
    if request.GET.get("archived") == "1":
        view = "archived"
    elif request.GET.get("shared") == "1":
        view = "shared"
    view = view if view in dict((k, l) for k, l, _i in VIEWS) else "all"
    q = request.GET.get("q", "").strip()[:200]
    tag = request.GET.get("tag", "").strip().lstrip("#").lower()[:50]

    visible = Note.objects.visible_to(user)
    qs = _folder(visible, view, user)
    if tag:
        qs = qs.filter(tags__contains=[tag])
    qs = notes.search(qs, q) if q else qs.order_by("-pinned", "-updated_at", "-pk")
    rows = list(qs.select_related("owner").prefetch_related("links", "shares")[:LIST_LIMIT])

    open_note = _visible(request, pk) if pk else (rows[0] if rows else None)
    allowed = links._allowed_apps(user)
    now = timezone.now()
    current = visible.filter(archived=False)
    tag_counts = {}
    for tags in current.values_list("tags", flat=True):
        for t in tags or []:
            tag_counts[t] = tag_counts.get(t, 0) + 1
    keep = {k: v for k, v in (("view", "" if view == "all" else view), ("tag", tag), ("q", q)) if v}
    query = ("?" + urlencode(keep)) if keep else ""
    context = {
        "rows": [_row(n, user, now, allowed) for n in rows], "view": view, "tag": tag, "q": q, "query": query,
        "folders": [{"key": k, "label": l, "icon": i, "count": _folder(visible, k, user).count(),
                     "url": reverse("albright_notes:list") + ("" if k == "all" else "?view=" + k)} for k, l, i in VIEWS],
        "tags": sorted(tag_counts.items()), "open": open_note, "list_url": reverse("albright_notes:list"),
    }
    if open_note:
        context.update(_editor(open_note, user, now, allowed))
    return render(request, "albright_notes/workspace.html", context)


def _editor(note, user, now, allowed):
    can_edit, is_owner = note.can_edit(user), note.owner_id == user.pk
    shares = list(note.shares.select_related("user")) if is_owner else []
    shared_ids = {s.user_id for s in shares} | {note.owner_id}
    return {
        "can_edit": can_edit, "is_owner": is_owner, "html": md.render(note.body, editable=can_edit),
        "chips": links.chips(user, list(note.links.all()), allowed), "words": word_count(note.body),
        "shares": shares, "share_choices": [u for u in notes.notes_users() if u.pk not in shared_ids] if is_owner else [],
        "permissions": NoteShare.PERMISSIONS, "updated": ago(note.updated_at, now),
        "my_permission": None if is_owner else note.shares.filter(user=user).values_list("permission", flat=True).first(),
    }


def note_list(request):
    return workspace(request)


def note_page(request, pk):
    return workspace(request, pk)


@login_required
@require_POST
def note_new(request):
    """"+ New note": an empty note, opened in the editor ready to type."""
    note = notes.create(request.user, notes.UNTITLED)
    return redirect(_note_url(note) + "?new=1")


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
    messages.success(request, f"Saved \u201c{note.title}\u201d.")
    return redirect("albright_notes:list")


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
