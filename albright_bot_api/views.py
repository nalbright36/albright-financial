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

from .models import InventorySuggestion


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
