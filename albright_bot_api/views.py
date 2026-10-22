"""The site side of the Bot API that stays here: accepting/dismissing the
bot's pending inventory suggestions (an item owner's action, on the item's
page). Token management and the activity log moved to the Admin area
(albright_control, /control/bot-api/) - the old /bot-api/ pages redirect
there."""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from .models import InventorySuggestion, SiteSuggestion, TradingNote


NOTES_ANCHOR = "#bot-notes"


def _back_to_notes():
    return redirect(reverse("grok_bot") + NOTES_ANCHOR)


def _delete_notes(request, notes):
    from albright_control.access import audit
    titles = [n.title for n in notes]
    TradingNote.objects.filter(pk__in=[n.pk for n in notes]).delete()
    audit(request, "bot_notes_deleted", f"{len(titles)} note(s): " + "; ".join(f'"{t[:60]}"' for t in titles))
    messages.success(request, f"Deleted {len(titles)} note{'s' if len(titles) != 1 else ''}.")


@login_required
@require_POST
def trading_notes_bulk(request):
    """Grok Bot page, "Bot notes": Delete or Move to Bot suggestions for the
    ticked notes (mine only). Moving makes one Admin-inbox suggestion per
    note (title, text as description, app "all", medium, New) and removes
    the note from Trading."""
    from albright_control.access import audit
    notes = list(TradingNote.objects.filter(owner=request.user, pk__in=request.POST.getlist("note"))
                 .order_by("date", "created_at", "pk"))   # oldest first, so suggestions keep the notes' order
    if not notes:
        messages.info(request, "Tick at least one note first.")
        return _back_to_notes()
    action = request.POST.get("action")
    if action == "delete":
        _delete_notes(request, notes)
    elif action == "move":
        created = [SiteSuggestion.objects.create(token=n.token, app="all", title=n.title[:200], description=n.text,
                                                 priority="medium", status="new") for n in notes]
        TradingNote.objects.filter(pk__in=[n.pk for n in notes]).delete()
        audit(request, "bot_notes_moved", f"{len(created)} note(s) -> Bot suggestions #"
              + ", #".join(str(s.pk) for s in created))
        messages.success(request, f"Moved {len(created)} note{'s' if len(created) != 1 else ''} to Bot suggestions.")
    else:
        messages.error(request, "Unknown action.")
    return _back_to_notes()


@login_required
@require_POST
def trading_note_delete(request, pk):
    note = get_object_or_404(TradingNote, pk=pk, owner=request.user)
    _delete_notes(request, [note])
    return _back_to_notes()


def moved_to_admin(request, target="albright_control:bot_api"):
    """The old Trading-app Bot API pages - now in Admin."""
    return redirect(target, permanent=True)


@login_required
@require_POST
def decide_suggestion(request, pk, decision):
    """Accept or dismiss a pending inventory suggestion. Accepting a price
    sets the item's list price - my action, on my page."""
    suggestion = get_object_or_404(InventorySuggestion.objects.select_related("entry"), pk=pk,
                                   entry__owner=request.user)
    if suggestion.status != "pending":
        messages.info(request, "That suggestion was already handled.")
    elif decision == "accept":
        if suggestion.suggested_price is not None:
            suggestion.entry.list_price = suggestion.suggested_price
            suggestion.entry.save(update_fields=["list_price"])
        suggestion.status, suggestion.decided_at = "accepted", timezone.now()
        suggestion.save(update_fields=["status", "decided_at"])
        messages.success(request, f"Accepted - list price set to ${suggestion.suggested_price}."
                         if suggestion.suggested_price is not None else "Accepted.")
    else:
        suggestion.status, suggestion.decided_at = "dismissed", timezone.now()
        suggestion.save(update_fields=["status", "decided_at"])
        messages.success(request, "Dismissed.")
    return redirect(reverse("albright_reselling_app:ledger_manage", args=[suggestion.entry_id]) + "#grok-suggestions")
