"""Views for the crypto opportunity finder: dashboard (tiles + tabs),
opportunity detail (checklist, time, status, earnings, fact edits), the
manual "Add opportunity" form, the earnings ledger (+ CSV for taxes), the
Settings page, and the external-link warning page.

Nothing here (or anywhere in this app) connects a wallet, signs or sends a
transaction, approves a token, or buys anything - it tracks opportunities
and what I did about them.
"""
import csv
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Case, F, IntegerField, Sum, Value, When
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from . import config
from .forms import EarningForm, ManualOpportunityForm, OpportunityEditForm, SettingsForm, TimeLogForm
from .links import is_web_url, link_status
from .models import ACTIVE_STATUSES, LEVELS, REWARD_TYPES, STATUSES, Earning, Opportunity, Source, TaskStep, TimeLog
from .pipeline import add_manual, rescore, sync_sources

TABS = [("ranked", "Ranked"), ("doing", "Doing"), ("submitted", "Submitted"), ("blocked", "Blocked"),
        ("excluded", "Excluded")]
CONFIDENCE_ORDER = Case(When(confidence="high", then=Value(2)), When(confidence="medium", then=Value(1)),
                        default=Value(0), output_field=IntegerField())


def _wants_json(request):
    return request.headers.get("x-requested-with") == "XMLHttpRequest"


def tab_queryset(tab):
    """Ranked: actionable (new/reviewing), not blocked, not excluded, by
    EV/hour (unknown hours last) then confidence. Blocked never shows
    anywhere but its own tab; Excluded (spending above budget) likewise."""
    qs = Opportunity.objects.select_related("source")
    not_blocked = qs.exclude(safety_verdict="block")
    if tab == "blocked":
        return qs.filter(safety_verdict="block").order_by("-discovered_at")
    if tab == "excluded":
        return not_blocked.filter(excluded=True).order_by("-discovered_at")
    if tab in ("doing", "submitted"):
        return not_blocked.filter(excluded=False, status=tab).order_by(F("deadline").asc(nulls_last=True))
    return (not_blocked.filter(excluded=False, status__in=("new", "reviewing"))
            .annotate(conf_rank=CONFIDENCE_ORDER)
            .order_by(F("ev_per_hour").desc(nulls_last=True), "-conf_rank", "-discovered_at"))


def dashboard_tiles(today=None):
    today = today or timezone.localdate()
    confirmed = Earning.objects.filter(kind="confirmed")
    all_time = confirmed.aggregate(t=Sum("usd_value_at_receipt"))["t"] or Decimal("0")
    month = confirmed.filter(date__gte=today.replace(day=1)).aggregate(t=Sum("usd_value_at_receipt"))["t"] or Decimal("0")
    potential = (Opportunity.objects.filter(status__in=ACTIVE_STATUSES, excluded=False)
                 .exclude(safety_verdict="block").aggregate(t=Sum("ev_mid"))["t"] or Decimal("0"))
    minutes = TimeLog.objects.aggregate(t=Sum("minutes"))["t"] or 0
    hours = Decimal(minutes) / Decimal(60)
    return {
        "earned_all_time": all_time,
        "earned_this_month": month,
        "potential": potential,
        "hours_logged": hours,
        "realized_per_hour": (all_time / hours).quantize(Decimal("0.01")) if hours else None,
    }


@login_required
def dashboard(request):
    tab = request.GET.get("tab") if request.GET.get("tab") in dict(TABS) else "ranked"
    filters = {key: request.GET.get(key, "") for key in ("type", "confidence", "status")}
    qs = tab_queryset(tab)
    if filters["type"] in dict(REWARD_TYPES):
        qs = qs.filter(reward_type=filters["type"])
    if filters["confidence"] in dict(LEVELS):
        qs = qs.filter(confidence=filters["confidence"])
    if filters["status"] in dict(STATUSES):
        qs = qs.filter(status=filters["status"])

    counts = {key: tab_queryset(key).count() for key, _label in TABS}
    unchecked = Source.objects.filter(active=True, terms_checked=False).exclude(kind="manual").count()
    return render(request, "albright_crypto_app/dashboard.html", {
        "tiles": dashboard_tiles(), "tab": tab, "tabs": [(k, label, counts[k]) for k, label in TABS],
        "opportunities": qs[:200], "filters": filters, "reward_types": REWARD_TYPES, "levels": LEVELS,
        "statuses": STATUSES, "unchecked_sources": unchecked, "budget_usd": config.budget_usd(),
    })


@login_required
def opportunity_detail(request, pk):
    opp = get_object_or_404(Opportunity.objects.select_related("source"), pk=pk)
    edit_form = OpportunityEditForm(instance=opp)
    time_form = TimeLogForm()
    earning_form = EarningForm()

    if request.method == "POST":
        action = request.POST.get("action")
        if action == "status" and request.POST.get("status") in dict(STATUSES):
            opp.status = request.POST["status"]
            opp.save(update_fields=["status"])
            messages.success(request, f"Status: {opp.get_status_display()}.")
            return redirect("albright_crypto_app:opportunity", pk=opp.pk)
        if action == "edit":
            edit_form = OpportunityEditForm(request.POST, instance=opp)
            if edit_form.is_valid():
                rescore(edit_form.save(commit=False))
                messages.success(request, "Saved - safety check and scores recalculated.")
                return redirect("albright_crypto_app:opportunity", pk=opp.pk)
        elif action == "time":
            time_form = TimeLogForm(request.POST)
            if time_form.is_valid():
                log = time_form.save(commit=False)
                log.opportunity = opp
                log.save()
                messages.success(request, f"Logged {log.minutes} minutes.")
                return redirect("albright_crypto_app:opportunity", pk=opp.pk)
        elif action == "earning":
            earning_form = EarningForm(request.POST, instance=Earning(opportunity=opp))
            if earning_form.is_valid():
                earning_form.save()
                messages.success(request, "Earning recorded.")
                return redirect("albright_crypto_app:opportunity", pk=opp.pk)

    minutes = opp.time_logs.aggregate(t=Sum("minutes"))["t"] or 0
    return render(request, "albright_crypto_app/opportunity.html", {
        "opp": opp, "edit_form": edit_form, "time_form": time_form, "earning_form": earning_form,
        "steps": opp.task_steps.all(), "time_logs": opp.time_logs.all(), "earnings": opp.earnings.all(),
        "minutes_logged": minutes, "statuses": STATUSES, "budget_usd": config.budget_usd(),
    })


@login_required
@require_POST
def toggle_step(request, pk, step_id):
    step = get_object_or_404(TaskStep, pk=step_id, opportunity_id=pk)
    step.done = not step.done
    step.save(update_fields=["done"])
    if _wants_json(request):
        return JsonResponse({"done": step.done})
    return redirect(reverse("albright_crypto_app:opportunity", args=[pk]) + "#steps")


@login_required
def add_opportunity(request):
    form = ManualOpportunityForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        opp, created = add_manual(form.cleaned_data["url"], form.cleaned_data["title"], form.cleaned_data["text"])
        if created:
            messages.success(request, "Added - facts extracted and scored." if opp.extraction_status == "done"
                             else "Added and scored (AI extraction skipped - limit reached or unavailable).")
        else:
            messages.info(request, "You've already added this one.")
        return redirect("albright_crypto_app:opportunity", pk=opp.pk)
    return render(request, "albright_crypto_app/add.html", {"form": form})


@login_required
def earnings(request):
    rows = Earning.objects.filter(kind="confirmed").select_related("opportunity")
    if request.GET.get("export") == "csv":
        response = HttpResponse(content_type="text/csv")
        response["Content-Disposition"] = 'attachment; filename="crypto_earnings.csv"'
        writer = csv.writer(response)
        writer.writerow(["date", "amount", "currency", "usd_value_at_receipt", "opportunity", "note"])
        for e in rows.order_by("date", "pk"):
            writer.writerow([e.date.isoformat(), f"{e.amount.normalize():f}", e.currency,
                             f"{e.usd_value_at_receipt:.2f}", e.opportunity.title if e.opportunity else "", e.note])
        return response
    totals = rows.aggregate(usd=Sum("usd_value_at_receipt"))
    by_currency = rows.values("currency").annotate(amount=Sum("amount"), usd=Sum("usd_value_at_receipt")).order_by("currency")
    return render(request, "albright_crypto_app/earnings.html", {
        "earnings": rows, "total_usd": totals["usd"] or Decimal("0"), "by_currency": by_currency,
        "tiles": dashboard_tiles(),
    })


@login_required
def settings_view(request):
    sync_sources()
    if request.method == "POST" and request.POST.get("action") == "sources":
        for source in Source.objects.exclude(kind="manual"):
            source.active = f"active_{source.pk}" in request.POST
            source.terms_checked = f"terms_{source.pk}" in request.POST
            source.save(update_fields=["active", "terms_checked"])
        messages.success(request, "Sources updated.")
        return redirect("albright_crypto_app:settings")

    form = SettingsForm(request.POST or None, initial={
        "allowlist": "\n".join(config.allowlist()), "rss_feeds": "\n".join(config.get("rss_feeds") or [])})
    if request.method == "POST" and form.is_valid():
        config.set_value("allowlist", form.cleaned_data["allowlist"])
        config.set_value("rss_feeds", form.cleaned_data["rss_feeds"])
        sync_sources()
        Source.objects.filter(kind="rss", url__in=form.cleaned_data["rss_feeds"]).update(active=True)  # re-added = on
        messages.success(request, "Settings saved. Existing opportunities keep their scores until edited or rescanned.")
        return redirect("albright_crypto_app:settings")
    return render(request, "albright_crypto_app/settings.html", {
        "form": form, "sources": Source.objects.exclude(kind="manual"),
        "github_queries": config.get("github_queries"), "budget_usd": config.budget_usd(),
    })


@login_required
def leave(request):
    """The warning page every non-allowlisted external link goes through."""
    url = request.GET.get("to", "")
    domain, status = link_status(url)
    if status == "allowlisted":
        return redirect(url)
    return render(request, "albright_crypto_app/leave.html", {
        "url": url if is_web_url(url) else "", "domain": domain, "status": status,
    })
