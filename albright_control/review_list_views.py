"""Admin > Ideas and Admin > Bot suggestions (review_list.py): the list page,
and the endpoints its rows and review mode post to - status (also used to
undo), notes, and deleting a suggestion. Superusers only.

The page always opens on New; ?status=<key> (or all) picks another tab, so
those views can be bookmarked. Nothing remembers the last tab."""
import json
from urllib.parse import urlencode

from django.contrib import messages
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from albright_bot_api.models import SiteSuggestion

from .access import audit, superuser_required
from .review_list import DEFAULT_TAB, KINDS, LIST_LIMIT, ROW_STATUSES, SHORTCUTS, STATUSES, TABS, new_counts

STATUS_LABELS = dict(STATUSES)
TEXT_LIMIT = 5000


def _json_body(request):
    """A JSON body from review_list.js, or a plain form post (no JS)."""
    if request.content_type == "application/json":
        try:
            body = json.loads(request.body or "{}")
        except json.JSONDecodeError:
            return {}
        return body if isinstance(body, dict) else {}
    return request.POST


def _wants_json(request):
    return request.headers.get("X-Requested-With") == "XMLHttpRequest" or request.content_type == "application/json"


def _back(request, kind):
    target = request.POST.get("next", "")
    if url_has_allowed_host_and_scheme(target, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
        return redirect(target)
    return redirect(KINDS[kind].page_url)


def list_page(request, kind):
    k = KINDS[kind]
    status = request.GET.get("status", "")
    status = status if status in dict(TABS) else DEFAULT_TAB
    values = k.filter_values(request.GET)
    qs = k.queryset(values, status)
    rows = [k.row(obj) for obj in qs[:LIST_LIMIT]]
    keep = {name: value for name, value in values.items() if value}
    queue = []
    if status == "new":
        # Review mode: the same New items (with the same filters), oldest first.
        queue = [k.row(obj) for obj in k.queryset({**values, "sort": "oldest"}, "new")[:LIST_LIMIT]]
    counts = k.counts()
    return render(request, "albright_control/review_list.html", {
        "kind": k, "status": status, "status_label": dict(TABS)[status], "rows": rows, "total": qs.count(),
        "counts": counts, "tabs": [{"key": key, "label": label, "count": counts[key],
                                    "url": "?" + urlencode({**keep, "status": key})} for key, label in TABS],
        "filters": [{"name": name, "label": label, "value": values[name],
                     "choices": [(t, t) for t in k.tags()] if name == "tag" else choices}
                    for name, label, choices in k.filters],
        "values": values, "filtered": any(values[name] for name in values if name != "sort"),
        "row_statuses": ROW_STATUSES, "shortcuts": SHORTCUTS, "queue": queue,
        "merge_targets": (k.model.objects.exclude(status__in=("done", "declined")).only("pk", "title")[:LIST_LIMIT]
                          if kind == "ideas" else []),
        "next": request.get_full_path(),
    })


@superuser_required
def ideas(request):
    return list_page(request, "ideas")


@superuser_required
def suggestions(request):
    return list_page(request, "suggestions")


@superuser_required
@require_POST
def set_status(request, kind, pk):
    """{"status": key} (plus "undo": true when it's putting one back). JSON callers get the new tab
    counts and the nav's New counts; a plain form post goes back to the page."""
    k = KINDS[kind]
    obj = get_object_or_404(k.model, pk=pk)
    data = _json_body(request)
    status = data.get("status")
    if status not in STATUS_LABELS:
        if _wants_json(request):
            return JsonResponse({"error": "Unknown status."}, status=400)
        messages.error(request, "Unknown status.")
        return _back(request, kind)
    previous = obj.status
    if previous != status:
        obj.status = status
        if kind == "ideas":
            obj.save()                  # keeps status_changed_at up to date
        else:
            obj.save(update_fields=["status", "updated_at"])
        undo = " (undo)" if str(data.get("undo", "")).lower() in ("1", "true") else ""
        audit(request, k.audit_status, f'#{pk} "{obj.title[:80]}": {previous} -> {status}{undo}')
    if not _wants_json(request):
        messages.success(request, f"Moved to {STATUS_LABELS[status]}.")
        return _back(request, kind)
    return JsonResponse({"ok": True, "id": obj.pk, "status": status, "label": STATUS_LABELS[status],
                         "previous": previous, "previous_label": STATUS_LABELS.get(previous, previous),
                         "counts": k.counts(), "new_counts": new_counts()})


@superuser_required
@require_POST
def save_notes(request, kind, pk):
    obj = get_object_or_404(KINDS[kind].model, pk=pk)
    notes = str(_json_body(request).get("my_notes", ""))
    if len(notes) > TEXT_LIMIT:
        if _wants_json(request):
            return JsonResponse({"error": "At most 5,000 characters."}, status=400)
        messages.error(request, "Notes can be at most 5,000 characters.")
        return _back(request, kind)
    obj.my_notes = notes
    obj.save(update_fields=["my_notes", "updated_at"])
    if not _wants_json(request):
        messages.success(request, "Notes saved.")
        return _back(request, kind)
    return JsonResponse({"ok": True, "id": obj.pk, "notes": obj.my_notes})


@superuser_required
@require_POST
def suggestion_delete(request, pk):
    suggestion = get_object_or_404(SiteSuggestion, pk=pk)
    audit(request, "suggestion_deleted", f'#{pk} "{suggestion.title[:80]}"')
    suggestion.delete()
    messages.success(request, "Suggestion deleted.")
    return _back(request, "suggestions")

