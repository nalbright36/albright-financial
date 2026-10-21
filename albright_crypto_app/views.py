"""Views for the Opportunities app (package albright_crypto_app): the "All"
overview (confirmed earnings, hours and realized $/hour, split by type), a
Crypto and an Income dashboard (tiles + tabs, same template), opportunity
detail (checklist, time, status, earnings, fact edits), the manual "Add
opportunity" form, the earnings ledger (+ CSV for taxes), the Settings
page, the external-link warning page, and the redirect from the old URLs.

Nothing here (or anywhere in this app) connects a wallet, signs or sends a
transaction, approves a token, or buys anything - it tracks opportunities
and what I did about them.
"""
import csv
from decimal import Decimal
from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Case, F, IntegerField, Sum, Value, When
from django.http import HttpResponse, HttpResponsePermanentRedirect, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from . import config
from .forms import (EarningForm, IncomeSourceForm, ManualOpportunityForm, OpportunityEditForm, SettingsForm,
                    TimeLogForm)
from .links import is_web_url, link_status
from .models import (ACTIVE_STATUSES, LEVELS, OPPORTUNITY_TYPES, REWARD_TYPES_BY_OPPORTUNITY_TYPE, STATUSES, Earning,
                     Opportunity, Source, TaskStep, TimeLog)
from .pipeline import add_manual, rescore, sync_sources
from .skills import matched_skills, skill_match

TABS = [("ranked", "Ranked"), ("doing", "Doing"), ("submitted", "Submitted"), ("blocked", "Blocked"),
        ("excluded", "Excluded")]
TYPE_LABELS = dict(OPPORTUNITY_TYPES)
CONFIDENCE_ORDER = Case(When(confidence="high", then=Value(2)), When(confidence="medium", then=Value(1)),
                        default=Value(0), output_field=IntegerField())


def _wants_json(request):
    return request.headers.get("x-requested-with") == "XMLHttpRequest"


def tab_queryset(tab, opportunity_type=None):
    """Ranked: actionable (new/reviewing), not blocked, not excluded, by
    EV/hour (unknown hours last) then confidence. Blocked never shows
    anywhere but its own tab; Excluded (spending above budget) likewise.
    opportunity_type limits it to crypto or income (None: both)."""
    qs = Opportunity.objects.select_related("source")
    if opportunity_type:
        qs = qs.filter(opportunity_type=opportunity_type)
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


def hide_low_skill_match(qs):
    """Freelance work below freelance_min_skill_match is hidden from Ranked
    by default. Unknown skill_match (no skills listed) stays visible."""
    return qs.exclude(reward_type="freelance", skill_match__lt=int(config.get("freelance_min_skill_match")))


def dashboard_tiles(today=None, opportunity_type=None):
    today = today or timezone.localdate()
    confirmed = Earning.objects.filter(kind="confirmed")
    potential_qs = Opportunity.objects.filter(status__in=ACTIVE_STATUSES, excluded=False).exclude(safety_verdict="block")
    time_logs = TimeLog.objects.all()
    if opportunity_type:
        confirmed = confirmed.filter(opportunity_type=opportunity_type)
        potential_qs = potential_qs.filter(opportunity_type=opportunity_type)
        time_logs = time_logs.filter(opportunity__opportunity_type=opportunity_type)
    all_time = confirmed.aggregate(t=Sum("usd_value_at_receipt"))["t"] or Decimal("0")
    month = confirmed.filter(date__gte=today.replace(day=1)).aggregate(t=Sum("usd_value_at_receipt"))["t"] or Decimal("0")
    potential = potential_qs.aggregate(t=Sum("ev_mid"))["t"] or Decimal("0")
    minutes = time_logs.aggregate(t=Sum("minutes"))["t"] or 0
    hours = Decimal(minutes) / Decimal(60)
    return {
        "earned_all_time": all_time,
        "earned_this_month": month,
        "potential": potential,
        "hours_logged": hours,
        "realized_per_hour": (all_time / hours).quantize(Decimal("0.01")) if hours else None,
    }


def _unchecked_sources(opportunity_type=None):
    qs = Source.objects.filter(active=True, terms_checked=False).exclude(kind="manual")
    return qs.filter(opportunity_type=opportunity_type).count() if opportunity_type else qs.count()


@login_required
def overview(request):
    """"All": confirmed earnings, hours and realized $/hour - combined and by type."""
    by_type = []
    for key, label in OPPORTUNITY_TYPES:
        ranked = tab_queryset("ranked", key)
        if key == "income":
            ranked = hide_low_skill_match(ranked)
        by_type.append({"key": key, "label": label, "tiles": dashboard_tiles(opportunity_type=key),
                        "ranked": ranked.count(), "doing": tab_queryset("doing", key).count(),
                        "top": list(ranked[:3]), "url_name": f"albright_crypto_app:{key}"})
    return render(request, "albright_crypto_app/overview.html", {
        "tiles": dashboard_tiles(), "by_type": by_type, "unchecked_sources": _unchecked_sources(),
    })


@login_required
def type_dashboard(request, opportunity_type):
    tab = request.GET.get("tab") if request.GET.get("tab") in dict(TABS) else "ranked"
    filters = {key: request.GET.get(key, "") for key in ("type", "confidence", "status")}
    reward_types = REWARD_TYPES_BY_OPPORTUNITY_TYPE[opportunity_type]
    is_income = opportunity_type == "income"
    show_low_match = request.GET.get("show_low_match") == "1"

    def listed(key):
        qs = tab_queryset(key, opportunity_type)
        return hide_low_skill_match(qs) if key == "ranked" and is_income and not show_low_match else qs

    qs = listed(tab)
    if filters["type"] in dict(reward_types):
        qs = qs.filter(reward_type=filters["type"])
    if filters["confidence"] in dict(LEVELS):
        qs = qs.filter(confidence=filters["confidence"])
    if filters["status"] in dict(STATUSES):
        qs = qs.filter(status=filters["status"])

    hidden_low_match = 0
    if is_income and tab == "ranked":
        hidden_low_match = tab_queryset("ranked", "income").count() - hide_low_skill_match(
            tab_queryset("ranked", "income")).count()
    toggle_params = {k: v for k, v in {"tab": tab, **filters}.items() if v}
    if not show_low_match:
        toggle_params["show_low_match"] = "1"

    counts = {key: listed(key).count() for key, _label in TABS}
    return render(request, "albright_crypto_app/dashboard.html", {
        "opportunity_type": opportunity_type, "type_label": TYPE_LABELS[opportunity_type], "is_income": is_income,
        "tiles": dashboard_tiles(opportunity_type=opportunity_type), "tab": tab,
        "tabs": [(k, label, counts[k]) for k, label in TABS],
        "opportunities": qs[:200], "filters": filters, "reward_types": reward_types, "levels": LEVELS,
        "statuses": STATUSES, "unchecked_sources": _unchecked_sources(opportunity_type),
        "budget_usd": config.budget_usd(),
        "show_low_match": show_low_match, "hidden_low_match": hidden_low_match,
        "min_skill_match": int(config.get("freelance_min_skill_match")),
        "low_match_query": urlencode(toggle_params),
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
        "type_label": TYPE_LABELS[opp.opportunity_type], "is_income": opp.opportunity_type == "income",
        "dashboard_url_name": f"albright_crypto_app:{opp.opportunity_type}",
        "my_matched_skills": matched_skills(opp.skills_required, config.skills()),
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
    initial_type = request.GET.get("type") if request.GET.get("type") in TYPE_LABELS else "crypto"
    form = ManualOpportunityForm(request.POST or None, initial={"opportunity_type": initial_type})
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        opp, created = add_manual(data["url"], data["title"], data["text"], opportunity_type=data["opportunity_type"])
        if created:
            messages.success(request, "Added - facts extracted and scored." if opp.extraction_status == "done"
                             else "Added and scored (AI extraction skipped - limit reached or unavailable).")
        else:
            messages.info(request, "You've already added this one.")
        return redirect("albright_crypto_app:opportunity", pk=opp.pk)
    return render(request, "albright_crypto_app/add.html", {"form": form})


@login_required
def earnings(request):
    type_filter = request.GET.get("type") if request.GET.get("type") in TYPE_LABELS else ""
    rows = Earning.objects.filter(kind="confirmed").select_related("opportunity")
    if type_filter:
        rows = rows.filter(opportunity_type=type_filter)
    if request.GET.get("export") == "csv":
        response = HttpResponse(content_type="text/csv")
        name = f"{type_filter}_earnings.csv" if type_filter else "opportunity_earnings.csv"
        response["Content-Disposition"] = f'attachment; filename="{name}"'
        writer = csv.writer(response)
        writer.writerow(["date", "amount", "currency", "usd_value_at_receipt", "opportunity", "note", "type"])
        for e in rows.order_by("date", "pk"):
            writer.writerow([e.date.isoformat(), f"{e.amount.normalize():f}", e.currency,
                             f"{e.usd_value_at_receipt:.2f}", e.opportunity.title if e.opportunity else "", e.note,
                             e.opportunity_type])
        return response
    totals = rows.aggregate(usd=Sum("usd_value_at_receipt"))
    by_currency = rows.values("currency").annotate(amount=Sum("amount"), usd=Sum("usd_value_at_receipt")).order_by("currency")
    return render(request, "albright_crypto_app/earnings.html", {
        "earnings": rows, "total_usd": totals["usd"] or Decimal("0"), "by_currency": by_currency,
        "tiles": dashboard_tiles(opportunity_type=type_filter or None), "type_filter": type_filter,
        "opportunity_types": OPPORTUNITY_TYPES,
        "csv_query": urlencode({"export": "csv", **({"type": type_filter} if type_filter else {})}),
    })


def _recompute_skill_matches():
    """After the skills profile changes, every income opportunity's skill_match follows."""
    mine = config.skills()
    for opp in Opportunity.objects.filter(opportunity_type="income").only("pk", "skills_required", "skill_match"):
        new = skill_match(opp.skills_required, mine)
        if new != opp.skill_match:
            Opportunity.objects.filter(pk=opp.pk).update(skill_match=new)


@login_required
def settings_view(request):
    sync_sources()
    action = request.POST.get("action") if request.method == "POST" else None
    if action == "sources":
        for source in Source.objects.exclude(kind="manual"):
            source.active = f"active_{source.pk}" in request.POST
            source.terms_checked = f"terms_{source.pk}" in request.POST
            source.save(update_fields=["active", "terms_checked"])
        messages.success(request, "Sources updated.")
        return redirect("albright_crypto_app:settings")

    source_form = IncomeSourceForm(request.POST if action == "add_source" else None, prefix="source")
    if action == "add_source" and source_form.is_valid():
        source = source_form.save()
        messages.success(request, f"Added {source.name}. It won't run until you tick \"terms checked\".")
        return redirect("albright_crypto_app:settings")

    settings_post = request.method == "POST" and action in (None, "", "settings")
    form = SettingsForm(request.POST if settings_post else None, initial={
        "allowlist": "\n".join(config.allowlist()), "rss_feeds": "\n".join(config.get("rss_feeds") or []),
        "skills": "\n".join(config.skills())})
    if settings_post and form.is_valid():
        config.set_value("allowlist", form.cleaned_data["allowlist"])
        config.set_value("rss_feeds", form.cleaned_data["rss_feeds"])
        skills_changed = form.cleaned_data["skills"] != config.skills()
        config.set_value("skills", form.cleaned_data["skills"])
        if skills_changed:
            _recompute_skill_matches()
        sync_sources()
        Source.objects.filter(kind="rss", url__in=form.cleaned_data["rss_feeds"]).update(active=True)  # re-added = on
        messages.success(request, "Settings saved. Existing opportunities keep their scores until edited or rescanned"
                                  + (" (skill matches updated)." if skills_changed else "."))
        return redirect("albright_crypto_app:settings")
    return render(request, "albright_crypto_app/settings.html", {
        "form": form, "source_form": source_form, "sources": Source.objects.exclude(kind="manual"),
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


def legacy_redirect(request, rest=""):
    """/crypto/... and /albright_crypto_app/... (the app's old prefixes) ->
    the same path under the Opportunities prefix, query string kept."""
    url = reverse("albright_crypto_app:dashboard") + rest
    query = request.META.get("QUERY_STRING", "")
    return HttpResponsePermanentRedirect(f"{url}?{query}" if query else url)
