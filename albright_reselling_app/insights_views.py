"""Views for the Insights page: market history over closed lots, pending
calibration suggestions (apply/dismiss/undo), open "Wrong?" lot-feedback
reports (create/resolve/export), and AI review accuracy. Logic lives in
scanner/insights.py - kept out of views.py per project convention, same as
ledger_views.py/scanner_views.py.
"""
from datetime import datetime

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse

from .forms import LotFeedbackForm
from .scanner import insights
from .scanner_models import CalibrationOverride, CalibrationSuggestion, LotFeedback, SourcedLot


def _parse_date(value):
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None


def _safe_next(request, default):
    """request.POST/GET["next"], only if it's a same-site path - never
    redirects off-site from a value a form happened to carry."""
    next_url = request.POST.get("next") or request.GET.get("next")
    if next_url and next_url.startswith("/"):
        return next_url
    return default


@login_required
def insights_page(request):
    """Everything on one page, each section independently filterable by
    its own GET params so the URL stays bookmarkable/shareable."""
    source = request.GET.get("source") or ""
    category = request.GET.get("category") or ""
    date_from = _parse_date(request.GET.get("from"))
    date_to = _parse_date(request.GET.get("to"))

    history = insights.market_history(source=source, category=category, date_from=date_from, date_to=date_to)

    pending_suggestions = CalibrationSuggestion.objects.filter(status="pending")
    applied_overrides = CalibrationOverride.objects.all()

    open_feedback = (
        LotFeedback.objects.filter(resolved=False)
        .select_related("lot", "lot__evaluation").order_by("-created_at")
    )

    return render(request, "insights.html", {
        "history": history,
        "filters": {"source": source, "category": category,
                    "from": request.GET.get("from") or "", "to": request.GET.get("to") or ""},
        "pending_suggestions": pending_suggestions,
        "applied_overrides": applied_overrides,
        "open_feedback": open_feedback,
        "ai_accuracy": insights.ai_review_accuracy(),
    })


@login_required
def apply_suggestion(request, suggestion_id):
    suggestion = get_object_or_404(CalibrationSuggestion, pk=suggestion_id, status="pending")
    if request.method == "POST":
        CalibrationOverride.objects.update_or_create(
            key=suggestion.key,
            defaults={
                "value": suggestion.suggested_value, "source_note": suggestion.evidence,
                "sample_size": suggestion.sample_size,
                "applied_by": request.user if request.user.is_authenticated else None,
            },
        )
        suggestion.status = "applied"
        suggestion.save(update_fields=["status"])
        messages.success(request, f"Applied {suggestion.key}: {suggestion.suggested_value}.")
    return redirect("albright_reselling_app:insights")


@login_required
def dismiss_suggestion(request, suggestion_id):
    suggestion = get_object_or_404(CalibrationSuggestion, pk=suggestion_id, status="pending")
    if request.method == "POST":
        suggestion.status = "dismissed"
        suggestion.save(update_fields=["status"])
        messages.info(request, f"Dismissed {suggestion.key}.")
    return redirect("albright_reselling_app:insights")


@login_required
def undo_override(request, override_id):
    override = get_object_or_404(CalibrationOverride, pk=override_id)
    if request.method == "POST":
        key = override.key
        override.delete()
        messages.success(request, f"Reverted {key} to its settings.py value.")
    return redirect("albright_reselling_app:insights")


@login_required
def create_lot_feedback(request, lot_id):
    """"Wrong?" - a short kind+note form, reachable from the dashboard's
    expandable lot rows and the AI review page. Redirects back to
    wherever it was opened from (?next=), same lot can get more than one
    report (e.g. one for the scanner, a separate one for an AI review)."""
    lot = get_object_or_404(SourcedLot, pk=lot_id)
    next_url = _safe_next(request, reverse("albright_reselling_app:dashboard"))

    if request.method == "POST":
        form = LotFeedbackForm(request.POST)
        if form.is_valid():
            feedback = form.save(commit=False)
            feedback.lot = lot
            feedback.save()
            messages.success(request, f'Thanks - noted for "{lot.title}".')
            return redirect(next_url)
    else:
        form = LotFeedbackForm()

    return render(request, "lot_feedback.html", {"lot": lot, "form": form, "next": next_url})


@login_required
def resolve_lot_feedback(request, feedback_id):
    feedback = get_object_or_404(LotFeedback, pk=feedback_id)
    if request.method == "POST":
        feedback.resolved = True
        feedback.save(update_fields=["resolved"])
        messages.success(request, "Marked resolved.")
    return redirect("albright_reselling_app:insights")


@login_required
def export_lot_feedback(request):
    """One "title / description / note" block per open report, as a plain
    text file - test-case material for scanner/valuers.py & friends."""
    reports = LotFeedback.objects.filter(resolved=False).select_related("lot").order_by("-created_at")
    lines = []
    for report in reports:
        lot = report.lot
        lines.append(f"Title: {lot.title}")
        lines.append(f"Description: {lot.description}")
        lines.append(f"Note: {report.note}")
        lines.append("")

    response = HttpResponse("\n".join(lines), content_type="text/plain")
    response["Content-Disposition"] = 'attachment; filename="lot_feedback_test_cases.txt"'
    return response
