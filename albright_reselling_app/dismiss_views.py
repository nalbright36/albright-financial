"""Views for the dashboard's Act Now "Dismiss" button (see scanner/
dashboard.py for the hide/reopen rule and LotDismissal, and static/js/
auction_scanner.js for the vanilla-JS side). Pure JSON AJAX endpoints -
the dashboard calls these without a page reload, so there's no template
here, just a JsonResponse either way.
"""
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.views.decorators.http import require_POST

from .scanner_models import LotDismissal, SourcedLot

VALID_REASONS = dict(LotDismissal.REASON_CHOICES)


def record_dismissal(lot, reason=""):
    """Creates (or refreshes, if the lot was dismissed before and then
    reopened) one LotDismissal for this lot - headroom_at_dismissal is
    always the lot's current headroom, snapshotted fresh here rather than
    trusted from the request, so the reopen threshold is measured from a
    real baseline even on a re-dismiss. An unknown reason saves as none.
    Shared with the /review/ queue's Dismiss (review_views.py)."""
    if reason not in VALID_REASONS:
        reason = ""
    evaluation = getattr(lot, "evaluation", None)
    headroom = evaluation.headroom if evaluation else None
    dismissal, _created = LotDismissal.objects.update_or_create(
        lot=lot, defaults={
            "headroom_at_dismissal": headroom, "reason": reason, "dismissed_at": timezone.now(),
        },
    )
    return dismissal


@login_required
@require_POST
def dismiss_lot(request, lot_id):
    lot = get_object_or_404(SourcedLot.objects.select_related("evaluation"), pk=lot_id)
    record_dismissal(lot, request.POST.get("reason", ""))
    return JsonResponse({"ok": True, "lot_id": lot.pk})


@login_required
@require_POST
def undo_dismiss(request, lot_id):
    """Deletes the dismissal outright - undoing never leaves a record
    behind, matching "Undo" rather than "snooze"."""
    LotDismissal.objects.filter(lot_id=lot_id).delete()
    return JsonResponse({"ok": True, "lot_id": lot_id})
