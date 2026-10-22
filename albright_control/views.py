"""Admin area (/control/) - superuser-only; everyone else gets 404.
Manages things that affect all three apps: the Bot API, the bot's site
suggestions, system health, integrations, AI usage, data/backups, and the
audit log of what was done here."""
from django import forms
from django.contrib import messages
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from albright_bot_api.models import SCOPES, BotRequestLog, BotToken, SiteSuggestion

from . import ai_usage, data, integrations, jobs
from .access import audit, superuser_required
from .models import AuditEntry, IntegrationCheck
from .prompts import claude_code_prompt

ACTIVITY_LIMIT = 300
AUDIT_LIMIT = 300


class PasswordField(forms.CharField):
    widget = forms.PasswordInput(attrs={"class": "ledger-input", "autocomplete": "current-password"})


class TokenForm(forms.Form):
    name = forms.CharField(max_length=100, initial="Grok bot", widget=forms.TextInput(attrs={"class": "ledger-input"}))
    scopes = forms.MultipleChoiceField(choices=SCOPES, widget=forms.CheckboxSelectMultiple,
                                       error_messages={"required": "Pick at least one scope."})
    password = PasswordField(label="Your password (to confirm)")

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user

    def clean_password(self):
        if not self.user.check_password(self.cleaned_data["password"]):
            raise forms.ValidationError("That isn't your password - no token was created.")
        return ""


class SuggestionForm(forms.Form):
    status = forms.ChoiceField(choices=SiteSuggestion.STATUSES, widget=forms.Select(attrs={"class": "ledger-input"}))
    my_notes = forms.CharField(required=False, max_length=5000,
                               widget=forms.Textarea(attrs={"class": "ledger-input", "rows": 3}))


# ---------------------------------------------------------------------------
# Overview
# ---------------------------------------------------------------------------

@superuser_required
def overview(request):
    health = jobs.health_rows()
    status_counts = {s: sum(1 for r in health if r["status"] == s) for s in ("ok", "late", "failing", "off")}
    integ = integrations.integration_rows()
    usage = ai_usage.month_usage()
    return render(request, "albright_control/overview.html", {
        "status_counts": status_counts, "problems": [r for r in health if r["status"] in ("late", "failing")],
        "integrations": integ, "usage": usage,
        "new_suggestions": SiteSuggestion.objects.filter(status="new").count(),
        "active_tokens": BotToken.objects.filter(revoked_at=None).count(),
        "recent_audit": AuditEntry.objects.select_related("user")[:5],
    })


# ---------------------------------------------------------------------------
# Bot API (moved here from the Trading app)
# ---------------------------------------------------------------------------

@superuser_required
def bot_api(request):
    form = TokenForm(request.POST or None, user=request.user)
    new_token = None
    if request.method == "POST" and form.is_valid():
        token, new_token = BotToken.issue(request.user, form.cleaned_data["name"].strip(), form.cleaned_data["scopes"])
        audit(request, "token_created", f'"{token.name}" ({token.display_prefix}…) scopes: {", ".join(token.scopes)}')
        messages.success(request, f'Created "{token.name}". Copy the token now - it won\'t be shown again.')
        form = TokenForm(user=request.user)
    docs = reverse("bot_api_v1:docs")
    return render(request, "albright_control/bot_api.html", {
        "form": form, "new_token": new_token, "tokens": BotToken.objects.select_related("owner"),
        "docs_path": docs, "api_base": docs.removesuffix("docs"),
    })


@superuser_required
@require_POST
def revoke_token(request, pk):
    token = get_object_or_404(BotToken, pk=pk)
    if not request.user.check_password(request.POST.get("password", "")):
        messages.error(request, f'Wrong password - "{token.name}" was not revoked.')
        return redirect("albright_control:bot_api")
    token.revoke()
    audit(request, "token_revoked", f'"{token.name}" ({token.display_prefix}…)')
    messages.success(request, f'Revoked "{token.name}" - it stops working immediately.')
    return redirect("albright_control:bot_api")


@superuser_required
def bot_activity(request):
    logs = BotRequestLog.objects.select_related("token")
    selected = request.GET.get("token", "")
    if selected == "none":
        logs = logs.filter(token__isnull=True)
    elif selected.isdigit():
        logs = logs.filter(token_id=int(selected))
    return render(request, "albright_control/bot_activity.html", {
        "logs": logs[:ACTIVITY_LIMIT], "tokens": BotToken.objects.all(), "selected": selected, "limit": ACTIVITY_LIMIT,
    })


# ---------------------------------------------------------------------------
# Bot suggestions inbox
# ---------------------------------------------------------------------------

@superuser_required
def suggestions(request):
    items = SiteSuggestion.objects.select_related("token")
    app = request.GET.get("app", "")
    status = request.GET.get("status", "")
    if app in dict(SiteSuggestion.APPS):
        items = items.filter(app=app)
    if status in dict(SiteSuggestion.STATUSES):
        items = items.filter(status=status)
    rows = [{"s": s, "form": SuggestionForm(initial={"status": s.status, "my_notes": s.my_notes}, prefix=f"s{s.pk}"),
             "prompt": claude_code_prompt(s) if s.status == "accepted" else ""} for s in items]
    return render(request, "albright_control/suggestions.html", {
        "rows": rows, "apps": SiteSuggestion.APPS, "statuses": SiteSuggestion.STATUSES, "app": app, "status": status,
        "status_tabs": [(key, label, SiteSuggestion.objects.filter(status=key).count())
                        for key, label in SiteSuggestion.STATUSES],
    })


@superuser_required
@require_POST
def update_suggestion(request, pk):
    suggestion = get_object_or_404(SiteSuggestion, pk=pk)
    form = SuggestionForm(request.POST, prefix=f"s{pk}")
    if form.is_valid():
        old_status = suggestion.status
        suggestion.status = form.cleaned_data["status"]
        suggestion.my_notes = form.cleaned_data["my_notes"]
        suggestion.save(update_fields=["status", "my_notes", "updated_at"])
        if old_status != suggestion.status:
            audit(request, "suggestion_status", f'#{pk} "{suggestion.title[:80]}": {old_status} -> {suggestion.status}')
        messages.success(request, "Saved.")
    else:
        messages.error(request, "Couldn't save that suggestion.")
    return redirect(reverse("albright_control:suggestions") + (f"?{request.GET.urlencode()}" if request.GET else "")
                    + f"#suggestion-{pk}")


# ---------------------------------------------------------------------------
# System health, integrations, AI usage
# ---------------------------------------------------------------------------

@superuser_required
def health(request):
    return render(request, "albright_control/health.html", {"rows": jobs.health_rows(), "late_factor": jobs.LATE_FACTOR})


@superuser_required
def integrations_view(request):
    return render(request, "albright_control/integrations.html", {"rows": integrations.integration_rows()})


@superuser_required
@require_POST
def test_integration(request, key):
    test = integrations.TESTS.get(key)
    if test is None:
        messages.error(request, "There's no test for that integration.")
        return redirect("albright_control:integrations")
    ok, message = test()
    IntegrationCheck.objects.create(integration=key, ok=ok, message=message[:300])
    audit(request, "integration_test", f"{key}: {'passed' if ok else 'failed'} - {message}")
    (messages.success if ok else messages.error)(request, message)
    return redirect("albright_control:integrations")


@superuser_required
def ai_usage_view(request):
    return render(request, "albright_control/ai_usage.html", {"usage": ai_usage.month_usage()})


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

@superuser_required
def data_view(request):
    dry_run = None
    if request.method == "POST" and request.POST.get("action") == "prune_dry_run":
        dry_run = data.prune_dry_run()
        audit(request, "prune_dry_run", dry_run.strip().splitlines()[-1][:300] if dry_run.strip() else "")
    return render(request, "albright_control/data.html", {
        "groups": data.row_counts(), "dry_run": dry_run, "backups": [(k, label) for k, (label, _fn) in data.BACKUPS.items()],
    })


@superuser_required
def backup(request, key):
    if key not in data.BACKUPS:
        from django.http import Http404
        raise Http404
    filename, payload, counts = data.backup_zip(key)
    audit(request, "backup_download", f"{filename}: " + ", ".join(f"{n} ({c} rows)" for n, c in counts.items()))
    response = HttpResponse(payload, content_type="application/zip")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


@superuser_required
def audit_log(request):
    entries = AuditEntry.objects.select_related("user")
    action = request.GET.get("action", "")
    if action:
        entries = entries.filter(action=action)
    return render(request, "albright_control/audit.html", {
        "entries": entries[:AUDIT_LIMIT], "actions": AuditEntry.objects.values_list("action", flat=True).distinct()
        .order_by("action"), "action": action, "limit": AUDIT_LIMIT,
    })
