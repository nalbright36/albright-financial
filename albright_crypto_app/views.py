"""Views for the Opportunities app (package albright_crypto_app), besides
the Inbox / Hidden / Pipeline pages (inbox_views.py): opportunity detail
(checklist, time, stage, earnings, fact edits), the manual "Add
opportunity" form, the earnings ledger (+ CSV for taxes, hours and $/hour
by type), Settings (Profile, Filters, Sources & links), the external-link
warning page, and the redirect from the old URL prefixes.

Nothing here (or anywhere in this app) connects a wallet, signs or sends a
transaction, approves a token, or buys anything - it tracks opportunities
and what I did about them.
"""
import csv
import secrets
from decimal import Decimal
from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Sum
from django.http import HttpResponse, HttpResponsePermanentRedirect, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from . import config, relevance, workflow
from .forms import (EarningForm, FiltersForm, IncomeSourceForm, ManualOpportunityForm, OpportunityEditForm,
                    SettingsForm, TimeLogForm)
from .links import is_web_url, link_status
from .models import ACTIVE_STATUSES, CLOSE_REASONS, OPPORTUNITY_TYPES, Earning, Opportunity, Source, TaskStep, TimeLog
from .pipeline import add_manual, rescore, sync_default_income_sources, sync_sources
from .skills import matched_skills, skill_match

# Features added later, kept together: walkthroughs, profile + drafts, Gmail.
from . import gmail, my_profile, walkthrough as walkthroughs  # noqa: E402
from .extraction import ExtractionLimitReached  # noqa: E402
from .forms import GmailLabelForm, ProfileForm  # noqa: E402
from .models import ApplicationDraft, GmailConnection, MyProfile, Walkthrough  # noqa: E402

TYPE_LABELS = dict(OPPORTUNITY_TYPES)


def _wants_json(request):
    return request.headers.get("x-requested-with") == "XMLHttpRequest"


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


@login_required
def opportunity_detail(request, pk):
    opp = get_object_or_404(Opportunity.objects.select_related("source"), pk=pk)
    edit_form = OpportunityEditForm(instance=opp)
    time_form = TimeLogForm()
    earning_form = EarningForm()

    if request.method == "POST":
        action = request.POST.get("action")
        if action == "status" and request.POST.get("status") in workflow.STAGE_LABELS:
            workflow.move(opp, request.POST["status"], request.POST.get("reason", ""))
            messages.success(request, f"Stage: {opp.get_status_display()}.")
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
        elif action in AI_ACTIONS:
            return _run_ai_action(request, opp, action)
        elif action == "walkthrough_steps":
            chosen = get_object_or_404(Walkthrough, pk=request.POST.get("walkthrough"), opportunity=opp)
            created = walkthroughs.add_steps_to_checklist(chosen)
            messages.success(request, f"Added {len(created)} step{'s' if len(created) != 1 else ''} to the checklist."
                             if created else "Those steps are already in the checklist.")
            return redirect(reverse("albright_crypto_app:opportunity", args=[opp.pk]) + "#steps")
        elif action == "draft_sent":
            draft = get_object_or_404(ApplicationDraft, pk=request.POST.get("draft"), opportunity=opp)
            draft.sent_at = timezone.now()
            draft.save(update_fields=["sent_at"])
            workflow.move(opp, "applied", now=draft.sent_at)
            messages.success(request, "Marked as sent - moved to Applied in the Pipeline.")
            return redirect(reverse("albright_crypto_app:opportunity", args=[opp.pk]) + "#drafts")

    minutes = opp.time_logs.aggregate(t=Sum("minutes"))["t"] or 0
    saved_walkthroughs = list(opp.walkthroughs.all())
    shown = next((w for w in saved_walkthroughs if str(w.pk) == request.GET.get("walkthrough")),
                 saved_walkthroughs[0] if saved_walkthroughs else None)
    profile = MyProfile.objects.filter(pk=MyProfile.SINGLETON_PK).first()
    return render(request, "albright_crypto_app/opportunity.html", {
        "opp": opp, "edit_form": edit_form, "time_form": time_form, "earning_form": earning_form,
        "steps": opp.task_steps.all(), "time_logs": opp.time_logs.all(), "earnings": opp.earnings.all(),
        "minutes_logged": minutes, "stages": list(workflow.STAGE_LABELS.items()), "close_reasons": CLOSE_REASONS,
        "budget_usd": config.budget_usd(),
        "type_label": TYPE_LABELS[opp.opportunity_type], "is_income": opp.opportunity_type == "income",
        "work_type_label": relevance.WORK_TYPE_LABELS.get(opp.work_type, "Other"),
        "uses_fit": opp.work_type in relevance.JOB_WORK_TYPES,
        "back_to_pipeline": opp.status in workflow.STAGE_LABELS and opp.status not in ("inbox", "skipped"),
        "my_matched_skills": matched_skills(opp.skills_required, config.skills()),
        "walkthroughs": saved_walkthroughs, "walkthrough": shown,
        "drafts": opp.drafts.all(), "profile_ready": bool(profile and (profile.work_history or profile.summary)),
    })


AI_ACTIONS = {"walkthrough": "Walkthrough", "proposal": "Proposal", "resume": "Tailored resume"}


def _run_ai_action(request, opp, action):
    """Walk me through it / Generate proposal / Tailor resume - each one AI
    call within the shared limits; failures become a message, never an error page."""
    anchor = "#walkthrough" if action == "walkthrough" else "#drafts"
    try:
        if action == "walkthrough":
            walkthroughs.generate(opp)
        else:
            if opp.opportunity_type != "income":
                messages.error(request, "Proposals and resumes are for income opportunities.")
                return redirect(reverse("albright_crypto_app:opportunity", args=[opp.pk]))
            profile = MyProfile.load()
            if not (profile.work_history or profile.summary or profile.skills):
                messages.error(request, "Fill in Settings > Profile first - drafts only use facts from it.")
                return redirect("albright_crypto_app:profile")
            draft = (my_profile.generate_proposal if action == "proposal" else my_profile.tailor_resume)(opp, profile)
            if draft.removed_claims:
                messages.warning(request, f"Removed {len(draft.removed_claims)} sentence(s) with facts that aren't in your profile.")
        messages.success(request, f"{AI_ACTIONS[action]} ready.")
    except ExtractionLimitReached as exc:
        messages.error(request, f"Not generated: {exc}.")
    except walkthroughs.WalkthroughError as exc:
        messages.error(request, str(exc))
    except Exception:  # noqa: BLE001 - already recorded as an AI error row
        messages.error(request, "The AI request failed - try again in a minute.")
    return redirect(reverse("albright_crypto_app:opportunity", args=[opp.pk]) + anchor)


@login_required
def draft_download(request, pk):
    """A tailored resume as .docx, or a proposal as .txt."""
    draft = get_object_or_404(ApplicationDraft.objects.select_related("opportunity"), pk=pk)
    stem = f"{draft.kind}-{draft.opportunity_id}-{draft.created_at:%Y%m%d}"
    if draft.kind == "resume":
        response = HttpResponse(my_profile.resume_docx(MyProfile.load(), draft), content_type=(
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"))
        response["Content-Disposition"] = f'attachment; filename="{stem}.docx"'
        return response
    response = HttpResponse(draft.text, content_type="text/plain; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{stem}.txt"'
    return response


@login_required
def profile_view(request):
    """Settings > Profile. "Fill from my resume" fills only empty fields."""
    profile = MyProfile.load()
    if request.method == "POST" and request.POST.get("action") == "prefill":
        skills_before = config.skills()
        changed = my_profile.prefill_profile(profile)
        if config.skills() != skills_before:
            _recompute_skill_matches()
        messages.success(request, f"Filled from your resume: {', '.join(f.replace('_', ' ') for f in changed)}."
                         if changed else "Nothing to fill - every resume field already has something.")
        return redirect("albright_crypto_app:profile")
    form = ProfileForm(request.POST or None, initial=ProfileForm.initial_for(profile))
    if request.method == "POST" and form.is_valid():
        skills_before = config.skills()
        form.save(profile)
        if config.skills() != skills_before:
            _recompute_skill_matches()
        messages.success(request, "Profile saved.")
        return redirect("albright_crypto_app:profile")
    return render(request, "albright_crypto_app/profile.html", {"form": form, "profile": profile,
                                                                "settings_tab": "profile"})


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
    initial_type = "crypto" if request.GET.get("type") == "crypto" else "income"
    form = ManualOpportunityForm(request.POST or None, initial={"opportunity_type": initial_type})
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        opp, created = add_manual(data["url"], data["title"], data["text"], opportunity_type=data["opportunity_type"])
        if created and opp.opportunity_type == "income" and (data.get("reward_type") or data.get("platform_name")
                                                            or data.get("pay_rate_usd") is not None
                                                            or data.get("application_status")):
            # What I typed wins over the AI's reading (e.g. a paid task tracked by hand).
            if data.get("reward_type"):
                opp.reward_type = data["reward_type"]
            if data.get("pay_rate_usd") is not None:
                opp.pay_rate_usd, opp.pay_type = data["pay_rate_usd"], "hourly"
            opp.platform_name = data.get("platform_name") or opp.platform_name
            opp.application_status = data.get("application_status") or opp.application_status
            rescore(opp)
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
    by_type = earnings_by_type(rows)
    by_currency = rows.values("currency").annotate(amount=Sum("amount"), usd=Sum("usd_value_at_receipt")).order_by("currency")
    return render(request, "albright_crypto_app/earnings.html", {
        "earnings": rows, "total_usd": totals["usd"] or Decimal("0"), "by_currency": by_currency,
        "tiles": dashboard_tiles(opportunity_type=type_filter or None), "type_filter": type_filter,
        "opportunity_types": OPPORTUNITY_TYPES, "by_type": by_type,
        "csv_query": urlencode({"export": "csv", **({"type": type_filter} if type_filter else {})}),
    })


EARNING_GROUPS = [("jobs", "Jobs"), ("freelance", "Freelance"), ("bounties", "Bounties"),
                  ("paid_tasks", "Paid tasks"), ("crypto", "Crypto"), ("other", "Other")]
WORK_TYPE_GROUP = {"full_time": "jobs", "part_time": "jobs", "contract": "freelance", "freelance": "freelance",
                   "bounty": "bounties", "paid_task": "paid_tasks"}


def _group(opportunity_type, work_type):
    return "crypto" if opportunity_type == "crypto" else WORK_TYPE_GROUP.get(work_type, "other")


def earnings_by_type(rows):
    """[{key, label, earned, hours, per_hour}] - confirmed earnings and logged
    hours per kind of work (an earning without an opportunity counts as
    Crypto or Other by its own type). Only groups with either appear."""
    earned, hours = {}, {}
    for e in rows.values("opportunity_type", "opportunity__work_type").annotate(usd=Sum("usd_value_at_receipt")):
        key = _group(e["opportunity_type"], e["opportunity__work_type"])
        earned[key] = earned.get(key, Decimal("0")) + (e["usd"] or Decimal("0"))
    logs = TimeLog.objects.all()
    if rows.query.where:   # the page's type filter applies to hours too
        types = set(rows.values_list("opportunity_type", flat=True).distinct())
        logs = logs.filter(opportunity__opportunity_type__in=types)
    for t in logs.values("opportunity__opportunity_type", "opportunity__work_type").annotate(m=Sum("minutes")):
        key = _group(t["opportunity__opportunity_type"], t["opportunity__work_type"])
        hours[key] = hours.get(key, Decimal("0")) + Decimal(t["m"] or 0) / Decimal(60)
    result = []
    for key, label in EARNING_GROUPS:
        if key in earned or key in hours:
            e, h = earned.get(key, Decimal("0")), hours.get(key, Decimal("0"))
            result.append({"key": key, "label": label, "earned": e, "hours": h,
                           "per_hour": (e / h).quantize(Decimal("0.01")) if h else None})
    return result


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
    sync_default_income_sources()
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
        "allowlist": "\n".join(config.allowlist()), "rss_feeds": "\n".join(config.get("rss_feeds") or [])})
    if settings_post and form.is_valid():
        config.set_value("allowlist", form.cleaned_data["allowlist"])
        config.set_value("rss_feeds", form.cleaned_data["rss_feeds"])
        sync_sources()
        Source.objects.filter(kind="rss", url__in=form.cleaned_data["rss_feeds"]).update(active=True)  # re-added = on
        messages.success(request, "Settings saved. Existing opportunities keep their scores until edited or rescanned.")
        return redirect("albright_crypto_app:settings")
    return render(request, "albright_crypto_app/settings.html", {
        "form": form, "source_form": source_form, "sources": Source.objects.exclude(kind="manual"),
        "github_queries": config.get("github_queries"), "budget_usd": config.budget_usd(),
        "unchecked_sources": Source.objects.filter(active=True, terms_checked=False).exclude(kind="manual").count(),
        "settings_tab": "sources",
    })


@login_required
def filters_view(request):
    """Settings > Filters. Saving re-sorts the Inbox and Hidden at once (no AI)."""
    form = FiltersForm(request.POST or None, initial=FiltersForm.initial_for(config.filters()))
    if request.method == "POST" and form.is_valid():
        config.set_value("filters", form.to_config())
        result = relevance.refresh_inbox()
        messages.success(request, f"Filters saved - {result['updated']} Inbox item{'s' if result['updated'] != 1 else ''} "
                                  "moved between the Inbox and Hidden.")
        return redirect("albright_crypto_app:filters")
    return render(request, "albright_crypto_app/filters.html", {"form": form, "settings_tab": "filters"})


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
    url = reverse("albright_crypto_app:inbox") + rest
    query = request.META.get("QUERY_STRING", "")
    return HttpResponsePermanentRedirect(f"{url}?{query}" if query else url)


# ---------------------------------------------------------------------------
# Gmail (Email alerts source) - read-only, gmail.readonly scope only
# ---------------------------------------------------------------------------

GMAIL_STATE_KEY = "opportunities_gmail_oauth_state"


def _gmail_redirect_uri(request):
    return request.build_absolute_uri(reverse("albright_crypto_app:gmail_callback"))


def email_source():
    return Source.objects.filter(kind="email", opportunity_type="income").first()


@login_required
def gmail_setup(request):
    connection = GmailConnection.objects.filter(pk=GmailConnection.SINGLETON_PK).first()
    source = email_source()
    form = GmailLabelForm(request.POST or None,
                          initial={"label": (source.options or {}).get("label") if source else "opportunities"})
    if request.method == "POST" and form.is_valid():
        label = form.cleaned_data["label"].strip()
        if source is None:
            source = Source(kind="email", opportunity_type="income")
        source.name, source.url, source.options = f"Gmail label: {label}", f"gmail:{label}", {"label": label}
        source.save()
        messages.success(request, f'Reading the "{label}" label.')
        return redirect("albright_crypto_app:gmail")
    configured = True
    try:
        gmail.client_config()
    except gmail.GmailError:
        configured = False
    return render(request, "albright_crypto_app/gmail.html", {
        "connection": connection, "source": source, "form": form, "configured": configured,
        "redirect_uri": _gmail_redirect_uri(request), "scope": gmail.SCOPE,
        "processed": source.processed_items.count() if source else 0, "settings_tab": "gmail",
    })


@login_required
@require_POST
def gmail_connect(request):
    state = secrets.token_urlsafe(24)
    request.session[GMAIL_STATE_KEY] = state
    try:
        return redirect(gmail.authorization_url(_gmail_redirect_uri(request), state))
    except gmail.GmailError as exc:
        messages.error(request, str(exc))
        return redirect("albright_crypto_app:gmail")


@login_required
def gmail_callback(request):
    expected = request.session.pop(GMAIL_STATE_KEY, None)
    if not expected or request.GET.get("state") != expected:
        messages.error(request, "That Gmail sign-in didn't match a connect request from this page - nothing was saved.")
        return redirect("albright_crypto_app:gmail")
    if request.GET.get("error") or not request.GET.get("code"):
        messages.error(request, "Gmail access wasn't granted.")
        return redirect("albright_crypto_app:gmail")
    try:
        token = gmail.exchange_code(request.GET["code"], _gmail_redirect_uri(request))
        connection = GmailConnection(pk=GmailConnection.SINGLETON_PK, scope=token.get("scope", ""))
        connection.set_token(token)
        connection.email = gmail.GmailReader(connection).profile_email()
        connection.save()
    except (gmail.GmailError, ValueError, KeyError) as exc:
        messages.error(request, str(exc) if isinstance(exc, gmail.GmailError) else "Couldn't finish connecting Gmail.")
        return redirect("albright_crypto_app:gmail")
    if email_source() is None:
        Source.objects.create(kind="email", opportunity_type="income", name="Gmail label: opportunities",
                              url="gmail:opportunities", options={"label": "opportunities"})
    messages.success(request, f"Gmail connected ({connection.email}) - read-only.")
    return redirect("albright_crypto_app:gmail")


@login_required
@require_POST
def gmail_disconnect(request):
    connection = GmailConnection.objects.filter(pk=GmailConnection.SINGLETON_PK).first()
    if connection:
        try:
            gmail.revoke(connection.get_token())
        except Exception:  # noqa: BLE001 - unreadable token: just forget it
            pass
        connection.delete()
    messages.success(request, "Gmail disconnected - the stored token was deleted.")
    return redirect("albright_crypto_app:gmail")
