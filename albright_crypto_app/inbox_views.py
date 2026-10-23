"""The workflow pages: Inbox (triage one at a time, keyboard first - the
reseller review queue's pattern: inbox.html + static/js/opportunity_inbox.js),
Hidden (blocked and filtered-out items, with why), and the Pipeline board.
Everything the Inbox card does is a small JSON endpoint below (K keep, S skip
+ reason, U undo, W walkthrough, P proposal / tailored resume). Also the
redirects from the old Crypto / Income pages.

Nothing here connects a wallet, signs, sends or buys anything.
"""
import json

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import F, Q
from django.db.models.functions import Coalesce, Least
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from . import my_profile, relevance, walkthrough as walkthroughs, workflow
from .extraction import ExtractionLimitReached
from .links import outbound_href
from .models import CLOSE_REASONS, SKIP_REASONS, MyProfile, Opportunity

# One row per shortcut: (keys, legend label, action id). The legend, the
# help overlay and the phone buttons all render from this list, and
# opportunity_inbox.js maps keys to the same ids.
SHORTCUTS = [
    (["K"], "Keep (to Pipeline: Saved)", "keep"),
    (["S"], "Skip, then 1-4 for a reason", "skip"),
    (["W"], "Walk me through it", "walkthrough"),
    (["P"], "Draft proposal / tailor resume", "draft"),
    (["O"], "Open posting", "open"),
    (["→"], "Next", "next"),
    (["←"], "Back", "back"),
    (["U"], "Undo", "undo"),
    (["?"], "Help", "help"),
]
SKIP_REASON_KEYS = [(str(i), value, label) for i, (value, label) in enumerate(SKIP_REASONS, 1)]


def _json_body(request):
    if request.content_type == "application/json":
        try:
            return json.loads(request.body or "{}")
        except json.JSONDecodeError:
            return {}
    return request.POST


def base_queryset():
    """Not merged into another posting; still in the Inbox."""
    return Opportunity.objects.select_related("source").filter(status="inbox", duplicate_of__isnull=True)


def inbox_queryset(type_key="all"):
    """What the Inbox shows: not blocked, not excluded, not filtered out.
    Highest fit first, then the soonest deadline; among the rest (bounties,
    paid tasks, crypto - no fit score) the best EV/hour, then newest."""
    qs = (base_queryset().filter(hidden_reason="", excluded=False).exclude(safety_verdict="block")
          .annotate(next_deadline=Coalesce(Least("application_deadline", "deadline"), "application_deadline", "deadline"))
          .order_by(F("fit_score").desc(nulls_last=True), F("next_deadline").asc(nulls_last=True),
                    F("ev_per_hour").desc(nulls_last=True), "-discovered_at"))
    return relevance.type_filter(qs, type_key)


def hidden_queryset(type_key="all"):
    qs = base_queryset().filter(Q(safety_verdict="block") | Q(excluded=True) | ~Q(hidden_reason=""))
    return relevance.type_filter(qs, type_key).order_by("-discovered_at")


def _type_key(request):
    key = request.GET.get("type", "all")
    return key if key in dict(relevance.TYPE_FILTERS) else "all"


def _date_portable(value):
    if not value:
        return ""
    value = timezone.localtime(value)
    return f"{value:%b} {value.day}, {value.year}"


def pay_text(opp):
    salary = str((opp.metadata or {}).get("salary") or "").strip()
    if salary:
        return salary[:80]
    if opp.pay_rate_usd is not None and opp.pay_type == "hourly":
        return f"${opp.pay_rate_usd:,.2f}/hour"
    if opp.pay_type == "fixed" and (opp.pay_rate_usd or opp.reward_usd) is not None:
        return f"${(opp.pay_rate_usd or opp.reward_usd):,.2f} fixed"
    if opp.reward_usd is not None:
        return f"${opp.reward_usd:,.2f} {opp.get_reward_type_display().lower()}"
    return ""


def open_url(opp):
    """Job boards' own links open directly (their terms want the visit);
    everything else goes through the verified-list check."""
    from .collectors.job_boards import _on_site
    if opp.source.attribution and _on_site(opp.url, opp.source.attribution[1]):
        return opp.url
    return outbound_href(opp.url)


def card(opp):
    """One Inbox card, as JSON for opportunity_inbox.js."""
    pk = opp.pk
    uses_fit = opp.work_type in relevance.JOB_WORK_TYPES
    return {
        "id": pk,
        "title": opp.title,
        "company": opp.company,
        "type": relevance.WORK_TYPE_LABELS.get(opp.work_type, "Other"),
        "is_income": opp.opportunity_type == "income",
        "pay": pay_text(opp),
        "location": opp.location_requirement or "",
        "uses_fit": uses_fit,
        "fit_score": opp.fit_score,
        "fit_method": opp.get_fit_method_display() if opp.fit_method else "",
        "matched_skills": list(opp.matched_skills or []),
        "fit_reason": opp.fit_reason,
        "missing_requirements": list(opp.missing_requirements or []),
        "role_match": opp.role_match,
        "ev_per_hour": float(opp.ev_per_hour) if opp.ev_per_hour is not None else None,
        "ev_mid": float(opp.ev_mid),
        "confidence": opp.confidence,
        "safety_verdict": opp.get_safety_verdict_display(),
        "safety_warnings": list(opp.safety_reasons or []),
        "deadline": _date_portable(opp.any_deadline),
        "source": relevance.source_label(opp),
        "via": bool(opp.source.attribution),
        "posted": _date_portable(relevance.posted(opp)),
        "also_on": [{"source": link.get("source", ""), "href": outbound_href(link.get("url", ""))}
                    for link in opp.other_links or []],
        "open_url": open_url(opp),
        "detail_url": reverse("albright_crypto_app:opportunity", args=[pk]),
        "urls": {name: reverse(f"albright_crypto_app:inbox_{name}", args=[pk])
                 for name in ("keep", "skip", "undo", "walkthrough", "draft")},
    }


@login_required
def inbox(request):
    relevance.expire_stale()   # cheap - so nothing stale is ever shown
    type_key = _type_key(request)
    queue = [card(o) for o in inbox_queryset(type_key)[:300]]
    return render(request, "albright_crypto_app/inbox.html", {
        "queue": queue, "total": len(queue), "type_key": type_key,
        "type_options": [(key, label, inbox_queryset(key).count()) for key, label in relevance.TYPE_FILTERS],
        "hidden_count": hidden_queryset(type_key).count(), "skip_summary": workflow.skip_summary(),
        "shortcuts": SHORTCUTS, "skip_reasons": SKIP_REASON_KEYS,
        "pending_fit": inbox_queryset(type_key).filter(fit_method="", work_type__in=relevance.JOB_WORK_TYPES).count(),
    })


@login_required
def hidden(request):
    type_key = _type_key(request)
    return render(request, "albright_crypto_app/hidden.html", {
        "opportunities": hidden_queryset(type_key)[:300], "type_key": type_key,
        "type_filters": relevance.TYPE_FILTERS, "total": hidden_queryset(type_key).count(),
    })


@login_required
@require_POST
def show_in_inbox(request, pk):
    """From Hidden: the filters stop hiding this one (blocked and over-budget stay hidden)."""
    opp = get_object_or_404(Opportunity, pk=pk)
    if opp.is_blocked or opp.excluded:
        messages.error(request, "Blocked and over-budget opportunities can't be moved to the Inbox.")
    else:
        opp.show_anyway = True
        relevance.apply_hidden(opp)
        opp.save(update_fields=["show_anyway", "hidden_reason"])
        messages.success(request, "Moved to the Inbox.")
    return redirect(request.POST.get("next") or reverse("albright_crypto_app:hidden"))


# ---------------------------------------------------------------------------
# Inbox card actions (JSON)
# ---------------------------------------------------------------------------

def _inbox_item(pk):
    return get_object_or_404(Opportunity.objects.select_related("source"), pk=pk)


@login_required
@require_POST
def inbox_keep(request, pk):
    """K: into the Pipeline as Saved."""
    opp = workflow.move(_inbox_item(pk), "saved")
    return JsonResponse({"ok": True, "id": opp.pk, "status": opp.status})


@login_required
@require_POST
def inbox_skip(request, pk):
    """S: skipped - posted at once with no reason, then again with one if
    1-4 is picked within the reason window."""
    reason = _json_body(request).get("reason", "")
    if reason and reason not in dict(SKIP_REASONS):
        return JsonResponse({"error": "Unknown reason."}, status=400)
    opp = workflow.move(_inbox_item(pk), "skipped", reason)
    return JsonResponse({"ok": True, "id": opp.pk, "status": opp.status, "reason": opp.skip_reason})


@login_required
@require_POST
def inbox_undo(request, pk):
    """U: back to the Inbox, as if the keep/skip never happened."""
    opp = _inbox_item(pk)
    if opp.status not in ("saved", "skipped"):
        return JsonResponse({"error": "Nothing to undo."}, status=400)
    workflow.move(opp, "inbox")
    return JsonResponse({"ok": True, "id": opp.pk, "status": opp.status})


def _walkthrough_payload(w):
    c = w.content or {}
    return {"warnings": c.get("warnings") or [], "sections": c.get("sections") or [],
            "difficulty": c.get("difficulty"), "hours": c.get("time_estimate_hours"),
            "detail_url": reverse("albright_crypto_app:opportunity", args=[w.opportunity_id]) + "#walkthrough"}


@login_required
@require_POST
def inbox_walkthrough(request, pk):
    """W: the saved walkthrough if there is one (no new AI call), else a new one."""
    opp = _inbox_item(pk)
    existing = opp.walkthroughs.first()
    if existing:
        return JsonResponse({"ok": True, "walkthrough": _walkthrough_payload(existing), "reused": True})
    try:
        made = walkthroughs.generate(opp)
    except ExtractionLimitReached as exc:
        return JsonResponse({"error": f"Not generated: {exc}."}, status=400)
    except walkthroughs.WalkthroughError as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    except Exception:  # noqa: BLE001 - recorded as an AI error row
        return JsonResponse({"error": "The AI request failed - try again in a minute."}, status=502)
    return JsonResponse({"ok": True, "walkthrough": _walkthrough_payload(made), "reused": False})


@login_required
@require_POST
def inbox_draft(request, pk):
    """P: a proposal (or, with kind=resume, a tailored resume) - from My
    profile's facts only. The latest one is reused rather than paid for again."""
    opp = _inbox_item(pk)
    kind = _json_body(request).get("kind") or "proposal"
    if kind not in ("proposal", "resume"):
        return JsonResponse({"error": "Unknown draft kind."}, status=400)
    if opp.opportunity_type != "income":
        return JsonResponse({"error": "Proposals and resumes are for jobs and freelance work."}, status=400)
    profile = MyProfile.load()
    if not (profile.work_history or profile.summary or profile.skills):
        return JsonResponse({"error": "Fill in Settings > Profile first - drafts only use facts from it."}, status=400)
    draft = opp.drafts.filter(kind=kind).first()
    reused = draft is not None
    if draft is None:
        try:
            draft = (my_profile.generate_proposal if kind == "proposal" else my_profile.tailor_resume)(opp, profile)
        except ExtractionLimitReached as exc:
            return JsonResponse({"error": f"Not generated: {exc}."}, status=400)
        except Exception:  # noqa: BLE001 - recorded as an AI error row
            return JsonResponse({"error": "The AI request failed - try again in a minute."}, status=502)
    return JsonResponse({"ok": True, "reused": reused, "draft": {
        "kind": draft.kind, "label": draft.get_kind_display(), "text": draft.text,
        "unmet": draft.unmet_requirements, "removed": draft.removed_claims,
        "download_url": reverse("albright_crypto_app:draft_download", args=[draft.pk]),
        "detail_url": reverse("albright_crypto_app:opportunity", args=[opp.pk]) + "#drafts"}})


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

@login_required
def pipeline(request):
    qs = (Opportunity.objects.select_related("source").filter(duplicate_of__isnull=True)
          .order_by(F("stage_changed_at").asc(nulls_first=True), "pk"))
    columns = [{"stage": stage, "label": label, "cards": list(qs.filter(status=stage))}
               for stage, label in workflow.BOARD_COLUMNS]
    closed = list(qs.filter(status="closed").order_by("-stage_changed_at")[:100])
    for opp in closed + [o for c in columns for o in c["cards"]]:
        opp.pay_display = pay_text(opp)
    return render(request, "albright_crypto_app/pipeline.html", {
        "columns": columns, "closed": closed, "now": timezone.now(),
        "closed_count": qs.filter(status="closed").count(), "stages": workflow.BOARD_COLUMNS + [("closed", "Closed")],
        "close_reasons": CLOSE_REASONS, "followups": sum(1 for c in columns for o in c["cards"] if o.followup_due),
    })


@login_required
@require_POST
def pipeline_move(request, pk):
    opp = get_object_or_404(Opportunity, pk=pk)
    stage = request.POST.get("stage")
    if stage not in dict(workflow.BOARD_COLUMNS + [("closed", "Closed"), ("inbox", "Inbox")]):
        messages.error(request, "Pick a stage.")
    else:
        workflow.move(opp, stage, request.POST.get("reason", ""))
        if request.headers.get("x-requested-with") == "XMLHttpRequest":
            return JsonResponse({"ok": True, "status": opp.status})
        messages.success(request, f"{opp.title[:60]}: {workflow.STAGE_LABELS[stage]}.")
    return redirect(request.POST.get("next") or reverse("albright_crypto_app:pipeline"))


# ---------------------------------------------------------------------------
# The old pages
# ---------------------------------------------------------------------------

def _old_page(request, type_key):
    tab = request.GET.get("tab")
    query = f"?type={type_key}" if type_key != "all" else ""
    if tab in ("blocked", "excluded"):
        return redirect(reverse("albright_crypto_app:hidden") + query)
    if tab in ("doing", "submitted"):
        return redirect("albright_crypto_app:pipeline")
    return redirect(reverse("albright_crypto_app:inbox") + query)


@login_required
def old_crypto(request):
    return _old_page(request, "crypto")


@login_required
def old_income(request):
    return _old_page(request, "all")
