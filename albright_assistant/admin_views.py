"""Admin > AI assistant (/control/assistant/): who has a provider connected, which provider and model, how many
messages they sent in the last 7 and 30 days, when they last used it, the kinds of errors they've hit lately, and a
switch to disable someone's assistant. Never a key (not even its last 4) and never any message text."""
from collections import Counter
from datetime import timedelta

from django.contrib import messages
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from albright_control.access import audit, superuser_required

from . import providers
from .models import Connection, Message, Run

ERROR_LABELS = {"invalid_key": "Key not accepted", "billing": "No credits / billing", "rate_limited": "Rate limited",
                "model_not_found": "Model not found", "timeout": "Timed out", "unavailable": "Provider down",
                "permission": "Not permitted", "network": "Couldn't connect", "bad_request": "Request rejected",
                "blocked": "Safety filter", "refused": "Refused"}


@superuser_required
def overview(request):
    now = timezone.now()
    week, month = now - timedelta(days=7), now - timedelta(days=30)
    conns = list(Connection.objects.select_related("user").order_by("user__username"))
    counts = {row["chat__user"]: row for row in Message.objects.filter(role=Message.USER, created_at__gte=month)
              .values("chat__user").annotate(m30=Count("pk"), m7=Count("pk", filter=Q(created_at__gte=week)))}
    errors = {}
    for run in Run.objects.filter(started_at__gte=month).exclude(error_kind="").values("user_id", "error_kind"):
        errors.setdefault(run["user_id"], Counter())[run["error_kind"]] += 1
    rows = [{"conn": c, "m7": counts.get(c.user_id, {}).get("m7", 0), "m30": counts.get(c.user_id, {}).get("m30", 0),
             "errors": [(ERROR_LABELS.get(k, k), n) for k, n in errors.get(c.user_id, Counter()).most_common(3)]}
            for c in conns]
    return render(request, "albright_assistant/admin.html",
                  {"rows": rows, "hosts": [(p.label, p.host) for p in providers.PROVIDERS]})


@require_POST
@superuser_required
def toggle(request, pk):
    conn = get_object_or_404(Connection, pk=pk)
    conn.disabled = not conn.disabled
    conn.save(update_fields=["disabled"])
    audit(request, "assistant_disabled" if conn.disabled else "assistant_enabled", conn.user.username)
    messages.success(request, f"{conn.user.username}'s assistant is {'disabled' if conn.disabled else 'enabled'}.")
    return redirect("assistant_admin:overview")
