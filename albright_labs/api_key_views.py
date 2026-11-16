"""My account > API keys (/account/api-keys/) - moved here from the old /bot-api/keys/ page (which redirects), so it
looks like My account, not Trading. The keys and the API itself stay in albright_bot_api.

  - Who: anyone with "Can create API keys" (Admin > Users, UserPreference.api_keys_allowed; superusers always).
    Without it the page says so - no form, no list - and every POST is a 403. Turning it off disables (not deletes)
    their keys: requests get 403 "API access disabled" until it's back on (albright_bot_api.auth).
  - Making a key: a name, then Full access / Read only / Custom (scopes.preset - only apps they can open; admin-only
    scopes never in the first two, only ticked under Custom by an admin), then an optional expiry. The key is shown
    once (only its hash is kept), with the "Connect your AI" tabs right below it.
  - The list: active keys only (revoked ones disappear) - access summary, created, last used, expiry; Rename,
    Edit access, Rotate and Revoke. Creating, changing access and rotating ask for the account password.
  - "Connect your AI": one tab per AI (templates albright_labs/connect/_<tab>.html) and the instructions block
    (albright_bot_api.connect.instructions - never a key).
Every change goes in the audit log.
"""
from datetime import timedelta

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from albright_bot_api import connect, scopes
from albright_bot_api.models import BotRequestLog, BotToken
from albright_control.access import audit

ACTIVITY_LIMIT = 100
EXPIRY_CHOICES = [("never", "Never"), ("30", "30 days"), ("90", "90 days"), ("365", "1 year")]
TABS = [("chatgpt", "ChatGPT"), ("claude", "Claude"), ("grok", "Grok"), ("gemini", "Gemini"), ("other", "Other / any AI")]


class AccessField(forms.Form):
    """Full access / Read only / Custom (+ the Custom checkboxes) -> .cleaned_data["scope_list"]."""
    access = forms.ChoiceField(choices=[(key, label) for key, label, _help in scopes.ACCESS_CHOICES], required=False)
    scopes = forms.MultipleChoiceField(required=False, error_messages={
        "invalid_choice": "You can only give a key access to apps you can open."})

    def __init__(self, *args, user, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user
        self.fields["scopes"].choices = scopes.choices(user)          # only what this person may grant

    def clean(self):
        data = super().clean()
        access = data.get("access") or (scopes.CUSTOM if data.get("scopes") else scopes.FULL)
        picked = scopes.preset(self.user, access)
        if picked is None:
            picked = data.get("scopes") or []
            if not picked and "scopes" not in self.errors:
                self.add_error("scopes", "Pick at least one.")
        elif not picked:
            raise forms.ValidationError("None of your apps can be used with a key yet.")
        data["access"], data["scope_list"] = access, picked
        return data


class KeyForm(AccessField):
    name = forms.CharField(max_length=100, widget=forms.TextInput(attrs={
        "class": "ledger-input", "placeholder": "e.g. My ChatGPT", "autocomplete": "off", "spellcheck": "false"}))
    expiry = forms.ChoiceField(choices=EXPIRY_CHOICES, required=False, initial="never")
    password = forms.CharField(label="Your password (to confirm)", widget=forms.PasswordInput(attrs={
        "class": "ledger-input", "autocomplete": "current-password"}))

    field_order = ["name", "access", "scopes", "expiry", "password"]

    def clean_password(self):
        if not self.user.check_password(self.cleaned_data["password"]):
            raise forms.ValidationError("That isn't your password - no key was created.")
        return ""

    def expires_at(self):
        days = self.cleaned_data.get("expiry") or "never"
        return None if days == "never" else timezone.now() + timedelta(days=int(days))


def _require_allowed(request):
    if not scopes.keys_allowed(request.user):
        raise PermissionDenied("API access isn't turned on for your account.")


def _mine(request, pk):
    return get_object_or_404(BotToken, pk=pk, owner=request.user, revoked_at=None)


def _password_ok(request, what):
    if request.user.check_password(request.POST.get("password", "")):
        return True
    messages.error(request, f"Wrong password - {what}")
    return False


def _page(request, form=None, new_key=None, new_token=None, status=200):
    links = connect.urls(request)
    context = {"allowed": scopes.keys_allowed(request.user), "tabs": TABS, "links": links,
               "instructions": connect.instructions(links["base"])}
    if not context["allowed"]:
        return render(request, "albright_labs/api_keys.html", context, status=status)
    labels = dict(scopes.choices(request.user))
    tokens = list(BotToken.objects.filter(owner=request.user, revoked_at=None))
    for t in tokens:
        t.summary = scopes.summary(request.user, t.scopes)
        t.access = scopes.access_of(request.user, t.scopes)
        t.unusable = [labels.get(key, key) for key in t.scopes if not scopes.allowed(request.user, key)]
    form = form or KeyForm(user=request.user)
    picked = [str(v) for v in (form["scopes"].value() or [])]
    context.update({
        "form": form, "picked": picked, "picked_access": form["access"].value() or (
            scopes.CUSTOM if picked else scopes.FULL),
        "access_choices": scopes.ACCESS_CHOICES, "groups": scopes.grouped_choices(request.user),
        "expiry_choices": EXPIRY_CHOICES, "picked_expiry": form["expiry"].value() or "never",
        "full_apps": ", ".join(name for name, _kind in scopes.by_app(scopes.preset(request.user, scopes.FULL) or [])),
        "tokens": tokens, "new_key": new_key, "new_token": new_token,
        "new_summary": scopes.summary(request.user, new_token.scopes) if new_token else "",
        "logs": BotRequestLog.objects.filter(token__owner=request.user).select_related("token")[:ACTIVITY_LIMIT],
        "activity_limit": ACTIVITY_LIMIT,
    })
    return render(request, "albright_labs/api_keys.html", context, status=status)


@login_required
def api_keys(request):
    if request.method != "POST":
        return _page(request)
    _require_allowed(request)
    form = KeyForm(request.POST, user=request.user)
    if not form.is_valid():
        return _page(request, form, status=400)
    token, raw = BotToken.issue(request.user, form.cleaned_data["name"].strip(), form.cleaned_data["scope_list"],
                                expires_at=form.expires_at())
    audit(request, "token_created", f'{request.user.username}: "{token.name}" ({token.display_prefix}…) '
                                    f'scopes: {", ".join(token.scopes)}'
                                    + (f'; expires {token.expires_at:%Y-%m-%d}' if token.expires_at else ""))
    messages.success(request, f'Created "{token.name}". Copy the key now - it won\'t be shown again.')
    return _page(request, new_key=raw, new_token=token)


@login_required
@require_POST
def api_key_scopes(request, pk):
    _require_allowed(request)
    token = _mine(request, pk)
    if not _password_ok(request, f'"{token.name}"\'s access was not changed.'):
        return redirect("labs:api_keys")
    form = AccessField(request.POST, user=request.user)
    if not form.is_valid():
        messages.error(request, " ".join(e for errors in form.errors.values() for e in errors))
        return redirect("labs:api_keys")
    old = list(token.scopes)
    token.set_scopes(form.cleaned_data["scope_list"])
    if old != token.scopes:
        audit(request, "token_scopes_changed", f'{request.user.username}: "{token.name}" ({token.display_prefix}…): '
                                               f'{", ".join(old) or "none"} -> {", ".join(token.scopes) or "none"}')
    messages.success(request, f'Updated "{token.name}" - in effect now; the key itself is unchanged.')
    return redirect("labs:api_keys")


@login_required
@require_POST
def api_key_rename(request, pk):
    _require_allowed(request)
    token = _mine(request, pk)
    name = request.POST.get("name", "").strip()[:100]
    if not name:
        messages.error(request, "Give the key a name.")
        return redirect("labs:api_keys")
    if name != token.name:
        audit(request, "token_renamed", f'{request.user.username}: "{token.name}" -> "{name}" ({token.display_prefix}…)')
        token.name = name
        token.save(update_fields=["name"])
    messages.success(request, f'Renamed to "{name}".')
    return redirect("labs:api_keys")


@login_required
@require_POST
def api_key_rotate(request, pk):
    _require_allowed(request)
    token = _mine(request, pk)
    if not _password_ok(request, f'"{token.name}" was not rotated.'):
        return redirect("labs:api_keys")
    old_prefix = token.display_prefix
    raw = token.rotate()
    audit(request, "token_rotated", f'{request.user.username}: "{token.name}": {old_prefix}… -> '
                                    f'{token.display_prefix}… (old value invalidated)')
    messages.success(request, f'Rotated "{token.name}". The old key no longer works - copy the new one now.')
    return _page(request, new_key=raw, new_token=token)


@login_required
@require_POST
def api_key_revoke(request, pk):
    _require_allowed(request)
    token = _mine(request, pk)
    token.revoke()
    audit(request, "token_revoked", f'{request.user.username}: "{token.name}" ({token.display_prefix}…)')
    messages.success(request, f'Revoked "{token.name}" - it stops working immediately.')
    return redirect("labs:api_keys")
