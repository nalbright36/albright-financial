"""The site side of the Bot API (Trading app nav, "Bot API"): create and
revoke tokens, the activity log, and accepting/dismissing the bot's
pending inventory suggestions. These are normal logged-in pages - the bot
itself can't reach any of them."""
from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from .models import SCOPES, BotRequestLog, BotToken, InventorySuggestion

ACTIVITY_LIMIT = 300


class TokenForm(forms.Form):
    name = forms.CharField(max_length=100, initial="Grok bot", widget=forms.TextInput(attrs={"class": "ledger-input"}))
    scopes = forms.MultipleChoiceField(choices=SCOPES, widget=forms.CheckboxSelectMultiple,
                                       error_messages={"required": "Pick at least one scope."})


@login_required
def tokens(request):
    form = TokenForm(request.POST or None)
    new_token = None
    if request.method == "POST" and form.is_valid():
        token, new_token = BotToken.issue(request.user, form.cleaned_data["name"].strip(), form.cleaned_data["scopes"])
        messages.success(request, f'Created "{token.name}". Copy the token now - it won\'t be shown again.')
        form = TokenForm()
    return render(request, "albright_bot_api/tokens.html", {
        "form": form, "new_token": new_token, "tokens": BotToken.objects.filter(owner=request.user),
        "docs_path": reverse("bot_api_v1:docs"), "api_base": reverse("bot_api_v1:docs").removesuffix("docs"),
    })


@login_required
@require_POST
def revoke(request, pk):
    token = get_object_or_404(BotToken, pk=pk, owner=request.user)
    token.revoke()
    messages.success(request, f'Revoked "{token.name}" - it stops working immediately.')
    return redirect("albright_bot_api:tokens")


@login_required
def activity(request):
    user_tokens = BotToken.objects.filter(owner=request.user)
    logs = BotRequestLog.objects.select_related("token").filter(token__owner=request.user)
    if request.GET.get("token") == "none":
        logs = BotRequestLog.objects.filter(token__isnull=True)
    elif request.GET.get("token"):
        logs = logs.filter(token_id=request.GET["token"])
    return render(request, "albright_bot_api/activity.html", {
        "logs": logs[:ACTIVITY_LIMIT], "tokens": user_tokens, "selected": request.GET.get("token", ""),
        "limit": ACTIVITY_LIMIT,
    })


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
