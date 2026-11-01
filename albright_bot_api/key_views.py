"""My account > My API keys (/bot-api/keys/): anyone allowed API keys (superusers, or "Allow API keys" in Admin >
Users) makes keys that act as themselves - only with the scopes for apps they can open (scopes.py; admin-only
scopes are for superusers). A new or rotated key is shown once; only its hash is stored. Creating, changing
permissions and rotating need the account password. They see their own keys' activity here; Admin > Bot API
still shows everyone's. Every change goes in the audit log. Not allowed: 404, as if the page didn't exist."""
from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from albright_control.access import audit

from . import scopes
from .models import BotRequestLog, BotToken

API_ADDRESS = "https://natealbright36.pythonanywhere.com/api/bot/v1/"
ACTIVITY_LIMIT = 100


class KeyForm(forms.Form):
    name = forms.CharField(max_length=100, widget=forms.TextInput(attrs={"class": "ledger-input"}))
    scopes = forms.MultipleChoiceField(widget=forms.CheckboxSelectMultiple, label="What this key may do",
                                       error_messages={"required": "Pick at least one."})
    password = forms.CharField(label="Your password (to confirm)",
                               widget=forms.PasswordInput(attrs={"class": "ledger-input",
                                                                 "autocomplete": "current-password"}))

    def __init__(self, *args, user, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user
        self.fields["scopes"].choices = scopes.choices(user)       # only what this person may grant

    def clean_password(self):
        if not self.user.check_password(self.cleaned_data["password"]):
            raise forms.ValidationError("That isn't your password - no key was created.")
        return ""


def _allowed_or_404(request):
    if not scopes.keys_allowed(request.user):
        raise Http404


def _mine(request, pk):
    return get_object_or_404(BotToken, pk=pk, owner=request.user, revoked_at=None)


def _password_ok(request, what):
    if request.user.check_password(request.POST.get("password", "")):
        return True
    messages.error(request, f"Wrong password - {what}")
    return False


def _page(request, form=None, new_key=None, new_key_note=""):
    tokens = list(BotToken.objects.filter(owner=request.user))
    labels = dict(scopes.choices(request.user))
    for t in tokens:
        t.scope_rows = [{"key": key, "label": labels.get(key, key), "usable": scopes.allowed(request.user, key)}
                        for key in t.scopes]
    return render(request, "albright_bot_api/my_keys.html", {
        "form": form or KeyForm(user=request.user), "tokens": tokens, "choices": scopes.choices(request.user),
        "new_key": new_key, "new_key_note": new_key_note, "api_address": API_ADDRESS,
        "docs_address": API_ADDRESS + "docs",
        "logs": BotRequestLog.objects.filter(token__owner=request.user).select_related("token")[:ACTIVITY_LIMIT],
        "activity_limit": ACTIVITY_LIMIT,
    })


@login_required
def my_keys(request):
    _allowed_or_404(request)
    form = KeyForm(request.POST or None, user=request.user)
    if request.method == "POST" and form.is_valid():
        token, raw = BotToken.issue(request.user, form.cleaned_data["name"].strip(), form.cleaned_data["scopes"])
        audit(request, "token_created", f'{request.user.username}: "{token.name}" ({token.display_prefix}…) '
                                        f'scopes: {", ".join(token.scopes)}')
        messages.success(request, f'Created "{token.name}". Copy the key now - it won\'t be shown again.')
        return _page(request, new_key=raw)
    return _page(request, form)


@login_required
@require_POST
def my_key_scopes(request, pk):
    _allowed_or_404(request)
    token = _mine(request, pk)
    if not _password_ok(request, f'"{token.name}"\'s permissions were not changed.'):
        return redirect("albright_bot_api:my_keys")
    allowed = set(scopes.available(request.user))
    wanted = request.POST.getlist("scopes")
    if any(key not in allowed for key in wanted):
        messages.error(request, "You can only give a key permissions for apps you can open.")
        return redirect("albright_bot_api:my_keys")
    old = list(token.scopes)
    token.set_scopes(wanted)
    if old != token.scopes:
        audit(request, "token_scopes_changed", f'{request.user.username}: "{token.name}" ({token.display_prefix}…): '
                                               f'{", ".join(old) or "none"} -> {", ".join(token.scopes) or "none"}')
    messages.success(request, f'Updated "{token.name}" - in effect now; the key itself is unchanged.')
    return redirect("albright_bot_api:my_keys")


@login_required
@require_POST
def my_key_rotate(request, pk):
    _allowed_or_404(request)
    token = _mine(request, pk)
    if not _password_ok(request, f'"{token.name}" was not rotated.'):
        return redirect("albright_bot_api:my_keys")
    old_prefix = token.display_prefix
    raw = token.rotate()
    audit(request, "token_rotated", f'{request.user.username}: "{token.name}": {old_prefix}… -> '
                                    f'{token.display_prefix}… (old value invalidated)')
    messages.success(request, f'Rotated "{token.name}". The old key no longer works - copy the new one now.')
    return _page(request, new_key=raw, new_key_note=f'New key for "{token.name}"')


@login_required
@require_POST
def my_key_revoke(request, pk):
    _allowed_or_404(request)
    token = _mine(request, pk)
    token.revoke()
    audit(request, "token_revoked", f'{request.user.username}: "{token.name}" ({token.display_prefix}…)')
    messages.success(request, f'Revoked "{token.name}" - it stops working immediately.')
    return redirect(reverse("albright_bot_api:my_keys"))
