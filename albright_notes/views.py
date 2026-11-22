"""Albright Notes (/notes/): one three-pane workspace - Option A of
docs/design/"Albright Notes - design options.html". The sidebar ("+ New note" with templates, tags, Archived,
Recently deleted), the notes list (filter chips, search with highlighted matches, sort, a Pinned section, swipe /
hover actions) and the open note in the rich editor (notes.js + the vendored TipTap bundle, note_editor.js;
autosave, word count, links, sharing, pin / archive / delete). /notes/ opens the first note in the list;
/notes/<id>/ opens that one. The list's view, tag, sort and search stay in the query string as you move between
notes. Deleting moves a note to Recently deleted (Undo for a few seconds; restore or delete forever there; gone
for good after 30 days).

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
from django.db.models import Q
from django.db.models.functions import Lower
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.html import escape
from django.utils.safestring import mark_safe
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST

from albright_control.review_list import ago
from albright_labs.links import is_web_url, link_status

from albright_labs import attachments, sharing

from . import links, notes, render as md, smart
from .models import TRASH_DAYS, Note, NoteLink, NotesSettings

# The list's filter chips, then the sidebar's other places: (key, label).
CHIPS = [("all", "All"), ("pinned", "Pinned"), ("shared", "Shared"), ("checklists", "Checklists")]
PLACES = [("archived", "Archived"), ("deleted", "Recently deleted")]
VIEW_KEYS = {k for k, _l in CHIPS + PLACES}
SORTS = [("edited", "Last edited"), ("created", "Created"), ("title", "Title")]
SORT_ORDER = {"edited": ("-updated_at", "-pk"), "created": ("-created_at", "-pk"), "title": (Lower("title"), "pk")}
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
            "sort_checked": note.sort_checked, "updated_iso": note.updated_at.isoformat(),
            "link_state": smart.link_states(note.body), "sync": getattr(note, "sync", []),
            "updated": f"{timezone.localtime(note.updated_at):%I:%M %p}".lstrip("0")}


def word_count(text):
    return sum(1 for w in (text or "").split() if WORD.search(w))


def _shared_ids(user):
    """Ids of my own notes that are shared with anyone."""
    from django.contrib.contenttypes.models import ContentType

    from albright_labs.models import ShareGrant
    mine = Note.objects.filter(owner=user).values_list("pk", flat=True)
    return set(ShareGrant.objects.filter(content_type=ContentType.objects.get_for_model(Note),
                                         object_id__in=mine).values_list("object_id", flat=True))


def _folder(qs, view, user):
    """Notes under one filter chip or sidebar place (archived notes only in Archived; deleted ones never - they're
    only in Recently deleted, notes.trashed)."""
    if view == "archived":
        return qs.filter(archived=True)
    qs = qs.filter(archived=False)
    if view == "pinned":
        return qs.filter(pinned=True)
    if view == "shared":                  # shared with me, or mine that I've shared
        return qs.filter(~Q(owner=user) | Q(pk__in=_shared_ids(user)))
    if view == "checklists":
        return qs.filter(body__regex=CHECKLIST_SQL)
    return qs


def _shared_out(notes_list):
    """Ids of these notes that are shared with anyone (the "Shared" icon on my own)."""
    from django.contrib.contenttypes.models import ContentType

    from albright_labs.models import ShareGrant
    return set(ShareGrant.objects.filter(content_type=ContentType.objects.get_for_model(Note),
                                         object_id__in=[n.pk for n in notes_list]).values_list("object_id", flat=True))


def _groups(user, notes_list):
    """"Shared with me", grouped: {note id: "<circle name>" or "From <person>"} - a person share first."""
    out = {}
    for g in sharing.grants_reaching(user, Note).filter(object_id__in=[n.pk for n in notes_list]).select_related("circle"):
        if g.user_id or g.object_id not in out:
            out[g.object_id] = g.circle.name if g.circle_id else ""
    owners = {n.pk: n.owner for n in notes_list}
    return {pk: label or f"From {sharing.person_name(owners[pk])}" for pk, label in out.items() if pk in owners}


def _matcher(q):
    words = re.findall(r"\w+", q or "")[:notes.SEARCH_WORDS]
    return re.compile(r"\b(?:" + "|".join(re.escape(w) for w in words) + r")\w*", re.IGNORECASE) if words else None


def _mark(text, matcher):
    """Escaped text with every search match in <mark>."""
    if not matcher:
        return escape(text)
    out, at = [], 0
    for m in matcher.finditer(text):
        out += [escape(text[at:m.start()]), "<mark>", escape(m.group(0)), "</mark>"]
        at = m.end()
    out.append(escape(text[at:]))
    return mark_safe("".join(out))


def _snippet(body, matcher, limit=120):
    """The first line of the note with a match in it, cut down around the match."""
    for line in (body or "").split("\n"):
        text = md.preview(line, limit=1000)
        found = text and matcher.search(text)
        if found:
            start = max(0, found.start() - 40)
            cut = ("…" if start else "") + text[start:start + limit].strip()
            return cut + ("…" if start + limit < len(text) else "")
    return ""


def _date_label(when, now):
    """Edited today: the time; this week: the weekday; this year: "Nov 3"; else "Nov 3, 2024"."""
    local, today = timezone.localtime(when), timezone.localtime(now)
    if local.date() == today.date():
        return f"{local:%I:%M %p}".lstrip("0")
    if (today.date() - local.date()).days < 7:
        return f"{local:%a}"
    if local.year == today.year:
        return f"{local:%b} {local.day}"
    return f"{local:%b} {local.day}, {local.year}"


def _row(note, user, now, allowed, shared_out=(), groups=None, matcher=None, view="all"):
    groups = groups or {}
    tasks = md.checklist_lines(note.body)
    shared_with_me = note.owner_id != user.pk
    preview = (_snippet(note.body, matcher) if matcher else "") or md.preview(note.body)
    if view == "deleted":
        left = max(0, TRASH_DAYS - (now - note.deleted_at).days)
        when = f"Deleted {_date_label(note.deleted_at, now)} \u00b7 {left} day{'s' if left != 1 else ''} left"
    else:
        when = _date_label(note.updated_at, now)
    due = smart.next_due(note.body, timezone.localdate()) if view != "deleted" and "@[date:" in (note.body or "") else None
    group = ""
    if view == "shared":
        group = groups.get(note.pk, "") if shared_with_me else "Shared by you"
    return {"note": note, "title": _mark(note.title, matcher), "preview": _mark(preview, matcher) if preview else "",
            "when": when, "updated": ago(note.updated_at, now), "owner_name": sharing.person_name(note.owner) if shared_with_me else "",
            "tasks_done": sum(1 for _line, checked in tasks if checked), "tasks": len(tasks),
            "shared": shared_with_me or note.pk in shared_out, "shared_with_me": shared_with_me,
            "chips": links.chips(user, list(note.links.all()), allowed) if view != "deleted" else [],
            "group": group, "can_manage": note.owner_id == user.pk,
            "due": smart.label(due[0], today=timezone.localdate()) if due else "", "overdue": bool(due and due[1])}


@login_required
@never_cache
def workspace(request, pk=None):
    user = request.user
    view = request.GET.get("view", "")
    if request.GET.get("archived") == "1":
        view = "archived"
    elif request.GET.get("shared") == "1":
        view = "shared"
    view = view if view in VIEW_KEYS else "all"
    q = request.GET.get("q", "").strip()[:200]
    tag = request.GET.get("tag", "").strip().lstrip("#").lower()[:50]
    sort = request.GET.get("sort", "")
    sort = sort if sort in SORT_ORDER else "edited"

    visible = Note.objects.visible_to(user)
    if view == "deleted":
        qs = notes.trashed(user)
        if q:
            qs = notes.search(qs, q).order_by("-deleted_at", "-pk")
    else:
        qs = _folder(visible, view, user)
        if tag:
            qs = qs.filter(tags__contains=[tag])
        qs = notes.search(qs, q) if q else qs.order_by("-pinned", *SORT_ORDER[sort])
    rows = list(attachments.with_counts(qs).select_related("owner").prefetch_related("links")[:LIST_LIMIT])
    shared_out = _shared_out([n for n in rows if n.owner_id == user.pk])
    groups = _groups(user, [n for n in rows if n.owner_id != user.pk]) if view == "shared" else {}
    if view == "shared":                              # mine first, then by circle and by person
        rows.sort(key=lambda n: (n.owner_id != user.pk, groups.get(n.pk, ""), not n.pinned))

    open_note = None if view == "deleted" else (_visible(request, pk) if pk else (rows[0] if rows else None))
    allowed = links._allowed_apps(user)
    now = timezone.now()
    matcher = _matcher(q)
    row_dicts = [_row(n, user, now, allowed, shared_out, groups, matcher, view) for n in rows]
    # A Pinned section on top (not where everything is pinned anyway, or in Archived / Recently deleted / Shared).
    sectioned = view in ("all", "checklists") and any(r["note"].pinned for r in row_dicts) \
        and not all(r["note"].pinned for r in row_dicts)
    sections = ([("Pinned", [r for r in row_dicts if r["note"].pinned]), ("Notes", [r for r in row_dicts if not r["note"].pinned])]
                if sectioned else [("", row_dicts)])
    current = visible.filter(archived=False)
    tag_counts = {}
    for tags in current.values_list("tags", flat=True):
        for t in tags or []:
            tag_counts[t] = tag_counts.get(t, 0) + 1
    keep = {k: v for k, v in (("view", "" if view == "all" else view), ("tag", tag), ("q", q),
                              ("sort", "" if sort == "edited" else sort)) if v}
    query = ("?" + urlencode(keep)) if keep else ""
    list_url = reverse("albright_notes:list")

    def link(**changes):
        params = {**keep, **changes}
        params = {k: v for k, v in params.items() if v}
        return list_url + (("?" + urlencode(params)) if params else "")
    undo = request.GET.get("undo", "")
    undo_note = notes.trashed(user).filter(pk=undo).first() if undo.isdigit() else None
    context = {
        "sections": sections, "rows": row_dicts, "view": view, "tag": tag, "q": q, "sort": sort, "query": query,
        "filter_chips": [{"key": k, "label": l, "url": link(view="" if k == "all" else k)} for k, l in CHIPS],
        "places": [{"key": k, "label": l, "url": link(view=k, tag=""),
                    "count": (notes.trashed(user).count() if k == "deleted" else _folder(visible, k, user).count())}
                   for k, l in PLACES],
        "sorts": [{"key": k, "label": l, "url": link(sort="" if k == "edited" else k)} for k, l in SORTS],
        "templates": [(k, label) for k, (label, _t, _b) in notes.TEMPLATES.items()],
        "tags": sorted(tag_counts.items()), "open": open_note, "list_url": list_url, "trash_days": TRASH_DAYS,
        "undo_note": undo_note,
        # A phone shows one screen at a time (notes.css): the list at /notes/, the note at /notes/<id>/.
        "screen": "note" if pk and open_note else "list",
    }
    if open_note:
        context.update(_editor(open_note, user, now, allowed))
    return render(request, "albright_notes/workspace.html", context)


def _people(user):
    """The people in this person's circles, for "@" mentions: [{id, name}]."""
    from albright_labs.models import CircleMembership
    circles = CircleMembership.objects.filter(user=user, status=CircleMembership.ACTIVE).values_list("circle_id", flat=True)
    members = CircleMembership.objects.filter(circle_id__in=list(circles), status=CircleMembership.ACTIVE) \
        .exclude(user=user).select_related("user").order_by("user__first_name", "user__username")
    seen, out = set(), []
    for m in members:
        if m.user_id not in seen:
            seen.add(m.user_id)
            out.append({"id": m.user_id, "name": sharing.person_name(m.user)})
    return out[:50]


def _editor(note, user, now, allowed):
    can_edit, is_owner = note.can_edit(user), note.owner_id == user.pk
    # The Planner links to notes by id (ScheduleItem.note): "Add to schedule", and a chip per linked item.
    can_schedule = "planner" in allowed
    if can_schedule:
        from albright_planner import planner
        from albright_planner.forms import FromNoteForm
    return {
        "can_schedule": can_schedule,
        "scheduled": planner.scheduled_for_note(note, user) if can_schedule else [],
        "schedule_form": FromNoteForm(initial={"date": timezone.localdate()}) if can_schedule else None,
        "can_edit": can_edit, "is_owner": is_owner, "html": md.render(note.body, editable=can_edit),
        "chips": links.chips(user, list(note.links.all()), allowed), "words": word_count(note.body),
        "share": sharing.control(user, note), "updated": ago(note.updated_at, now),
        "updated_iso": note.updated_at.isoformat(),
        "smart": {"link_state": smart.link_states(note.body), "people": _people(user),
                  "groceries": "meals" in allowed, "planner": can_schedule,
                  "auto_planner": smart.auto_planner(user), "today": timezone.localdate().isoformat()} if can_edit else None,
        "my_permission": None if is_owner else sharing.permission_for(user, note),
        "att": attachments.section(user, note, "notes", editable=can_edit,
                                   next_url=reverse("albright_notes:note", args=[note.pk])),
    }


def note_list(request):
    return workspace(request)


def note_page(request, pk):
    return workspace(request, pk)


@login_required
@require_POST
def note_new(request):
    """"+ New note": a note from one of the starters (notes.TEMPLATES - Blank by default), opened in the editor
    ready to type."""
    _label, title, body = notes.TEMPLATES.get(request.POST.get("template", ""), notes.TEMPLATES["blank"])
    note = notes.create(request.user, title, body)
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
    """Autosave (JSON): {title?, body?, tags?, sort_checked?, version, force?}. 409 with the newer version's details
    when someone else saved it since `version` (force: true overwrites on purpose)."""
    note = _editable(request, pk)
    data = _json(request)
    fields = {key: data[key] for key in ("title", "body", "tags", "sort_checked") if key in data and data[key] is not None}
    for key in ("title", "body"):
        if key in fields and not isinstance(fields[key], str):
            return JsonResponse({"ok": False, "error": f"{key} must be text"}, status=400)
    if "sort_checked" in fields and not isinstance(fields["sort_checked"], bool):
        return JsonResponse({"ok": False, "error": "sort_checked must be true or false"}, status=400)
    if not fields:
        return JsonResponse({"ok": False, "error": "nothing to save"}, status=400)
    base = None if data.get("force") else data.get("version")
    if base is not None and not isinstance(base, int):
        return JsonResponse({"ok": False, "error": "version must be a number"}, status=400)
    try:
        link_base = data.get("link_base") if isinstance(data.get("link_base"), dict) else None
        note = notes.save(note, notes.user_label(request.user), base_version=base, editor="body" in fields,
                          actor=request.user, link_base=link_base, **fields)
    except notes.Conflict as conflict:
        return JsonResponse({"ok": False, "conflict": True, **_state(conflict.note, request.user)}, status=409)
    return JsonResponse(_state(note, request.user))


@login_required
@require_POST
def note_check(request, pk):
    """Tick or untick ONE checklist item: JSON {item_id (data-item-id), checked, version} - index instead of item_id
    still works. Only that Markdown line changes, on the latest saved text (notes.set_checked), so it never needs the
    version to match. Tiny reply: {ok, version, checked}; when the note had changed since `version` (someone else's
    edit), also the new body and html so the editor catches up. 409 {missing} when the item's text was edited since."""
    note = _editable(request, pk)
    data = _json(request)
    item_id, index, checked, version = data.get("item_id"), data.get("index"), data.get("checked"), data.get("version")
    if isinstance(index, bool) or not isinstance(checked, bool) or not (isinstance(item_id, str) or isinstance(index, int)):
        return JsonResponse({"ok": False, "error": "bad request"}, status=400)
    try:
        note, before = notes.set_checked(note, notes.user_label(request.user), checked,
                                         item_id=item_id if isinstance(item_id, str) else None, index=index,
                                         by_user=request.user)
    except notes.NoSuchItem:
        fresh = Note.objects.get(pk=note.pk)
        return JsonResponse({"ok": False, "missing": True, **_state(fresh, request.user)}, status=409)
    out = {"ok": True, "version": note.version, "checked": checked}
    if not isinstance(version, int) or before != version:       # changed elsewhere meanwhile: send the latest
        out.update(_state(note, request.user))
    return JsonResponse(out)


def _wants_json(request):
    return request.headers.get("X-Requested-With") == "XMLHttpRequest"


@login_required
@require_POST
def note_pin(request, pk):
    note = _owned(request, pk)
    note.pinned = not note.pinned
    note.save(update_fields=["pinned", "updated_at"])
    if _wants_json(request):
        return JsonResponse({"ok": True, "pinned": note.pinned})
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
    """Move the note to Recently deleted. JSON for notes.js (its Undo toast posts to restore_url)."""
    note = _owned(request, pk)
    notes.trash(note)
    if _wants_json(request):
        return JsonResponse({"ok": True, "title": note.title,
                             "restore_url": reverse("albright_notes:restore", args=[note.pk])})
    messages.success(request, f"Moved “{note.title}” to Recently deleted.")
    return redirect(reverse("albright_notes:list") + f"?undo={note.pk}")


def _trashed(request, pk):
    return get_object_or_404(Note.all_objects, pk=pk, owner=request.user, deleted_at__isnull=False)


@login_required
@require_POST
def note_restore(request, pk):
    note = _trashed(request, pk)
    notes.restore(note)
    if _wants_json(request):
        return JsonResponse({"ok": True, "url": _note_url(note)})
    messages.success(request, f"Restored “{note.title}”.")
    return redirect(_note_url(note))


@login_required
@require_POST
def note_purge(request, pk):
    """Delete forever - only from Recently deleted."""
    note = _trashed(request, pk)
    title = note.title
    note.delete()
    if _wants_json(request):
        return JsonResponse({"ok": True})
    messages.success(request, f"Deleted “{title}” forever.")
    return redirect(reverse("albright_notes:list") + "?view=deleted")


# ---------------------------------------------------------------------------
# Sharing (owner only)
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Smart links in a note (smart.py): groceries, Planner tasks, settings
# ---------------------------------------------------------------------------

@login_required
@require_POST
def smart_groceries(request, pk):
    """"@grocery" on a line: {items: ["milk", "eggs"]} go on the grocery list of the person who typed it. Returns
    {ids, added (ids newly put on the list - what Undo takes off), already (bool: every one was on it), names}."""
    _editable(request, pk)
    data = _json(request)
    texts = [smart.split_items(t)[0] for t in data.get("items") or [] if isinstance(t, str) and smart.split_items(t)]
    if not texts:
        return JsonResponse({"ok": False, "error": "Nothing to add"}, status=400)
    try:
        made = smart.add_groceries(request.user, texts[:smart.MAX_GROCERIES])
    except smart.NoGroceries:
        return JsonResponse({"ok": False, "error": "You don't have a grocery list (Meals)."}, status=403)
    return JsonResponse({"ok": True, "ids": [g.pk for g, _s in made], "added": [g.pk for g, s in made if s == "added"],
                         "already": all(s == "already" for _g, s in made), "names": [g.name for g, _s in made]})


@login_required
@require_POST
def smart_groceries_remove(request, pk):
    """Undo an "@grocery", or "Remove from groceries" after the tag or line went: {ids}."""
    _editable(request, pk)
    ids = [i for i in _json(request).get("ids") or [] if isinstance(i, int)]
    return JsonResponse({"ok": True, "removed": smart.remove_groceries(request.user, ids) if ids else 0})


@login_required
@require_POST
def smart_planner(request, pk):
    """"Add to Planner" on a date chip: {date, time?, text} -> {task_id, url}."""
    note = _editable(request, pk)
    if "planner" not in links._allowed_apps(request.user):
        return JsonResponse({"ok": False, "error": "You can't open the Planner."}, status=403)
    data = _json(request)
    try:
        day, at = smart.parse_when(data.get("date"), data.get("time"))
    except (TypeError, ValueError):
        return JsonResponse({"ok": False, "error": "Pick a date"}, status=400)
    task = smart.add_task(note, request.user, day, at, str(data.get("text") or "")[:200])
    return JsonResponse({"ok": True, "task_id": task.pk,
                         "url": reverse("albright_planner:item", args=[task.pk]) + f"?date={day.isoformat()}"})


@login_required
@require_POST
def smart_planner_delete(request, pk, task_id):
    """The dated line (or its date) went and the person said to delete its Planner task too."""
    _editable(request, pk)
    task = smart.planner_task(request.user, task_id)
    if task is None:
        return JsonResponse({"ok": False}, status=404)
    with smart.quiet():
        task.delete()
    return JsonResponse({"ok": True})


@login_required
@require_GET
def smart_state(request, pk):
    """The note's link states as saved now (a task checked off in the Planner meanwhile, say) - the editor checks
    when you come back to the tab: {version, link_state}."""
    note = _visible(request, pk)
    return JsonResponse({"ok": True, "version": note.version, "link_state": smart.link_states(note.body)})


@login_required
@require_POST
def smart_settings(request):
    """{auto_planner: bool} - "Always add dated note items to Planner"."""
    value = _json(request).get("auto_planner")
    if not isinstance(value, bool):
        return JsonResponse({"ok": False, "error": "auto_planner must be true or false"}, status=400)
    NotesSettings.objects.update_or_create(user=request.user, defaults={"auto_planner": value})
    return JsonResponse({"ok": True, "auto_planner": value})
