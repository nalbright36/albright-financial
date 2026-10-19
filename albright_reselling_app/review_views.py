"""The /review/ queue - one lot at a time through Check by hand and Leads
(scanner/review_queue.py builds the queue; review_queue.html + static/js/
review_queue.js drive it, keyboard first). The page embeds the whole
queue up front; everything below the page view is a small JSON endpoint
the card's actions call without a reload: Watchlist, Dismiss (optionally
with a reason - posted again with one if picked within the 3-second
reason picker), Undo of either, and an on-demand AI review.
"""
import json

from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from . import calculator_views
from .dismiss_views import record_dismissal
from .scanner.ai_review_service import ProviderUnavailable, ReviewLimitExceeded, review_lot
from .scanner.bid_watch import restore
from .scanner.review_queue import DEFAULT_SCOPE, SCOPE_LABELS, SCOPES, _ai_payload, all_queues
from .scanner_models import AIReview, BidWatch, LotDismissal, SourcedLot
from .scanner_views import RECENT_REVIEW_WINDOW

# One row per shortcut: (keys, action, legend row id). The legend panel,
# the help overlay, and the phone buttons all render from this one list,
# so they can never disagree with each other (review_queue.js maps keys
# to the same ids).
SHORTCUTS = [
    (["W"], "Watchlist", "watch"),
    (["D"], "Dismiss, then 1-4 for a reason", "dismiss"),
    (["C"], "Calculator", "calc"),
    (["A"], "AI review", "ai"),
    (["→", "Space"], "Skip", "skip"),
    (["←"], "Back", "back"),
    (["O"], "Open listing", "open"),
    (["U"], "Undo", "undo"),
    (["?"], "Help", "help"),
]
DISMISS_REASON_KEYS = [(str(i), value, label) for i, (value, label) in enumerate(LotDismissal.REASON_CHOICES, 1)]


def _json_body(request):
    """Accepts a JSON body or a plain form POST - the queue's fetch() calls
    send JSON; a test or no-JS client can send either."""
    if request.content_type == "application/json":
        try:
            return json.loads(request.body or "{}")
        except json.JSONDecodeError:
            return {}
    return request.POST


POSITIONS_SESSION_KEY = "review_queue_positions"


def _todays_positions(request):
    """{scope: lot_id} saved today (review_position below) - an earlier
    day's positions are ignored, so every scope starts from the top again."""
    saved = request.session.get(POSITIONS_SESSION_KEY) or {}
    if saved.get("date") != timezone.localdate().isoformat():
        return {}
    return {scope: lot_id for scope, lot_id in (saved.get("positions") or {}).items() if scope in SCOPE_LABELS}


@login_required
def review_page(request):
    """The queue page, opened on ?scope= (from a table's "Start review"
    button; default "review"). Every scope's queue is embedded, so the
    page's scope selector switches without a reload; each scope resumes at
    the lot it was on earlier today (session-saved - see review_position)."""
    scope = request.GET.get("scope")
    if scope not in SCOPE_LABELS:
        scope = DEFAULT_SCOPE
    queues = all_queues()
    context = {
        "scope": scope,
        "scope_label": SCOPE_LABELS[scope],
        "scope_options": [(key, label, len(queues[key])) for key, label in SCOPES],
        "queues": queues,
        "queue": queues[scope],
        "any_queued": any(queues.values()),
        "positions": _todays_positions(request),
        "shortcuts": SHORTCUTS,
        "dismiss_reasons": DISMISS_REASON_KEYS,
    }
    context.update(calculator_views.panel_context(request.user))
    return render(request, "review_queue.html", context)


@login_required
@require_POST
def review_position(request):
    """Remembers which lot a scope is on (posted by review_queue.js as you
    move), for the rest of today, in the session."""
    data = _json_body(request)
    scope = data.get("scope")
    try:
        lot_id = int(data.get("lot_id"))
    except (TypeError, ValueError):
        return JsonResponse({"error": "Bad lot id."}, status=400)
    if scope not in SCOPE_LABELS:
        return JsonResponse({"error": "Unknown scope."}, status=400)
    positions = _todays_positions(request)
    positions[scope] = lot_id
    request.session[POSITIONS_SESSION_KEY] = {"date": timezone.localdate().isoformat(), "positions": positions}
    return JsonResponse({"ok": True, "positions": positions})


@login_required
@require_POST
def review_watch(request, lot_id):
    """W: adds the lot to the Watchlist (idempotent - already watched is
    fine). Never for a lot whose auction has already closed."""
    lot = get_object_or_404(SourcedLot, pk=lot_id)
    if lot.end_time is not None and lot.end_time <= timezone.now():
        return JsonResponse({"error": "This lot has already closed."}, status=400)
    watch, created = BidWatch.objects.get_or_create(lot=lot, defaults={"status": "watching"})
    if not created and watch.archived_at is not None:
        restore(watch)  # soft-removed earlier - back on the watchlist
    return JsonResponse({"ok": True, "lot_id": lot.pk, "watching": True})


@login_required
@require_POST
def review_dismiss(request, lot_id):
    """D: dismisses the lot (same LotDismissal/reopen rule as the
    dashboard's Dismiss). Called once with no reason the moment D is
    pressed, then again with one if a reason is picked in time."""
    lot = get_object_or_404(SourcedLot.objects.select_related("evaluation"), pk=lot_id)
    dismissal = record_dismissal(lot, _json_body(request).get("reason", ""))
    return JsonResponse({"ok": True, "lot_id": lot.pk, "reason": dismissal.reason})


@login_required
@require_POST
def review_undo(request, lot_id):
    """U: reverses the last Watchlist ("watch") or Dismiss ("dismiss") on
    this lot outright - no record left behind, matching the dashboard's
    own Undo."""
    action = _json_body(request).get("action")
    if action == "watch":
        BidWatch.objects.filter(lot_id=lot_id).delete()
    elif action == "dismiss":
        LotDismissal.objects.filter(lot_id=lot_id).delete()
    else:
        return JsonResponse({"error": "Nothing to undo."}, status=400)
    return JsonResponse({"ok": True, "lot_id": lot_id, "undone": action})


@login_required
@require_POST
def review_ai(request, lot_id):
    """A: an AI review for this lot, as JSON for the card - reusing a
    successful one from the last RECENT_REVIEW_WINDOW instead of paying
    again (same rule as the dashboard's AI review button)."""
    lot = get_object_or_404(SourcedLot.objects.select_related("evaluation"), pk=lot_id)
    cutoff = timezone.now() - RECENT_REVIEW_WINDOW
    review = (
        AIReview.objects.filter(lot=lot, status="done", created_at__gte=cutoff).order_by("-created_at").first()
    )
    if review is None:
        try:
            review = review_lot(lot)
        except (ReviewLimitExceeded, ProviderUnavailable) as exc:
            return JsonResponse({"error": str(exc)}, status=400)
    if review.status != "done":
        return JsonResponse({"error": review.error or "The AI review failed."}, status=502)
    return JsonResponse({"ok": True, "ai_review": _ai_payload(review)})
