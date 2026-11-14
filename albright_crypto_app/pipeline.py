"""Store an opening, extract facts (AI), safety-check and score it.

The safety check (core.safety.check_opportunity) and the scoring
(core.scoring.score) are the provided, tested pure-Python modules; this
file only feeds them and saves what they return. rescore() runs both again
whenever an opportunity is edited.

Each opportunity is "crypto" or "income", taken from its source. Income
adds two things on top of the core modules: skill_match (my skills
profile vs. the skills required), and the "requires a refundable deposit"
warning - a deposit you get back isn't spending, so it never excludes an
opportunity under the budget, but it's always flagged.
"""
import logging
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from django.utils import timezone

from . import config, relevance
from .core.safety import check_domain, check_opportunity
from .core.scoring import score
from .extraction import ExtractionLimitReached, extract
from .models import Opportunity, Source, TaskStep
from .skills import skill_match

log = logging.getLogger(__name__)


@dataclass
class RawOpportunity:
    """A posting about to be stored (used by add/store and leftover merge tests)."""
    external_id: str
    url: str
    title: str
    text: str = ""
    metadata: dict = field(default_factory=dict)
    published: datetime | None = None
    facts: dict = field(default_factory=dict)
    facts_complete: bool = False


MANUAL_SOURCE_NAMES = {"crypto": "Manual", "income": "Manual (income)"}
REFUNDABLE_DEPOSIT_REASON = "requires a refundable deposit"
# Facts a structured API states in its own fields (RawOpportunity.facts) - saved as-is.
STRUCTURED_FACTS = ("reward_type", "location_requirement", "skills_required")
FIT_FIELDS = ("fit_score", "matched_skills", "missing_requirements", "role_match", "seniority", "fit_reason")
# Everything extraction.validate() returns - what a collector that ran the AI itself may set.
AI_FACT_FIELDS = (
    "reward_usd", "reward_currency", "reward_type", "evidence", "evidence_quote", "deadline", "hours_estimate",
    "costs_usd", "requires_spending", "steps", "automatable_steps", "eligibility_notes", "pay_type", "pay_rate_usd",
    "hours_is_estimate", "skills_required", "location_requirement", "application_deadline", "refundable_deposit_usd",
)


# ---------------------------------------------------------------------------
# Manual source (typed in by the owner) and scoring
# ---------------------------------------------------------------------------

def manual_source(opportunity_type="crypto", owner=None):
    if owner is None:
        raise TypeError("manual_source requires owner")
    source, _ = Source.objects.get_or_create(
        kind="manual", url=f"user:{owner.pk}", opportunity_type=opportunity_type,
        defaults={"name": MANUAL_SOURCE_NAMES[opportunity_type], "terms_checked": True})
    return source


def apply_safety_and_score(opp):
    """Runs check_opportunity() and score() on the opportunity's current
    facts and sets every result field (doesn't save)."""
    allow = config.allowlist()
    safety = check_opportunity(f"{opp.title}\n{opp.raw_text}\n{opp.url}", opp.url, allow)
    opp.safety_verdict = safety.verdict
    opp.risk_score = safety.risk_score
    opp.safety_reasons = list(safety.reasons)
    if opp.refundable_deposit_usd and REFUNDABLE_DEPOSIT_REASON not in opp.safety_reasons:
        # The safety module only spots some deposit wording ("minimum deposit");
        # a recorded refundable deposit always gets the warning.
        opp.safety_reasons.append(REFUNDABLE_DEPOSIT_REASON)
        if opp.safety_verdict == "ok":
            opp.safety_verdict = "warn"
    opp.domain = safety.domain
    opp.domain_status = safety.domain_status
    opp.official_source = check_domain(opp.url, allow)[1] == "allowlisted"

    steps = opp.steps or []
    s = score(
        float(opp.reward_usd) if opp.reward_usd is not None else None,
        opp.reward_type,
        float(opp.hours_estimate) if opp.hours_estimate is not None else None,
        costs_usd=float(opp.costs_usd or 0),
        requires_spending=opp.requires_spending,
        evidence=opp.evidence,
        official_source=opp.official_source,
        automatable_steps=opp.automatable_steps,
        total_steps=len(steps),
        budget_usd=config.budget_usd(),
    )
    opp.excluded = s.excluded
    opp.exclude_reason = s.exclude_reason
    opp.ev_low, opp.ev_mid, opp.ev_high = (Decimal(str(v)) for v in (s.ev_low, s.ev_mid, s.ev_high))
    opp.ev_per_hour = Decimal(str(s.ev_per_hour_mid)) if s.ev_per_hour_mid is not None else None
    opp.confidence = s.confidence
    opp.automation = s.automation
    opp.reward_counts_as_confirmed = s.reward_counts_as_confirmed
    opp.skill_match = (skill_match(opp.skills_required, config.skills())
                       if opp.opportunity_type == "income" else None)
    relevance.derive(opp)
    relevance.apply_hidden(opp)   # the free prefilter (and a low fit score) -> Hidden
    return opp


def rescore(opp):
    apply_safety_and_score(opp)
    opp.save()
    return opp


def _sync_task_steps(opp):
    """The extracted steps become a tickable checklist (only on first
    extraction - never wipes steps already ticked off)."""
    if opp.task_steps.exists():
        return
    TaskStep.objects.bulk_create([
        TaskStep(opportunity=opp, text=step["text"], automatable=step.get("automatable", False), order=i)
        for i, step in enumerate(opp.steps or [])
    ])


def extract_and_score(opp, use_ai=True, client=None):
    """AI facts (when allowed and within limits), then safety + scores.
    Jobs from Remotive / Remote OK are never sent to the AI provider
    (Source.keep_in_app) - their facts come from the API's own fields.
    An item whose collector already extracted its facts (email alerts, HN)
    isn't sent to the AI again.

    To keep AI cost down: a posting that's the same as one already found
    elsewhere is merged into it and goes no further; one the free prefilter
    already rules out (from its title and the API's own fields) isn't sent
    for extraction; and only job/freelance postings that pass every filter
    get a fit score (relevance.score_fit)."""
    if opp.source.kind != "manual":
        primary = relevance.find_duplicate(relevance.derive(opp))
        if primary is not None:
            relevance.merge_into(opp, primary)
            opp.extraction_status = "filtered"
            return rescore(opp)
    if opp.extraction_status == "done":
        pass
    elif not opp.show_anyway and relevance.prefilter(relevance.derive(opp)):
        opp.extraction_status = "filtered"
    elif use_ai and not opp.source.keep_in_app:
        try:
            facts = extract(opp, client=client)
        except ExtractionLimitReached as exc:
            log.info("Skipping AI extraction for %s: %s", opp.pk, exc)
            opp.extraction_status = "skipped"
        except Exception:  # noqa: BLE001 - already recorded as an AIExtraction error
            opp.extraction_status = "error"
        else:
            for field, value in facts.items():
                setattr(opp, field, value)
            opp.extraction_status = "done"
    else:
        opp.extraction_status = "skipped"
    apply_safety_and_score(opp)
    if opp.safety_verdict != "block" and not opp.excluded and relevance.score_fit(opp, use_ai=use_ai, client=client):
        relevance.apply_hidden(opp)
    opp.save()
    _sync_task_steps(opp)
    return opp


# ---------------------------------------------------------------------------
# Ingest
# ---------------------------------------------------------------------------

def store(source, raw, owner=None):
    """Creates the Opportunity for a collected item, or returns None if
    it's already stored (same source + external_id)."""
    if Opportunity.objects.filter(source=source, external_id=raw.external_id).exists():
        return None
    if raw.facts_complete:
        facts = {k: v for k, v in (raw.facts or {}).items() if k in AI_FACT_FIELDS}
        facts["extraction_status"] = "done"
    else:
        facts = {k: v for k, v in (raw.facts or {}).items() if k in STRUCTURED_FACTS and v not in (None, "", [])}
    metadata = dict(raw.metadata or {})
    fit = metadata.pop("fit", None)   # a collector that scored fit in its own AI call (HN)
    if isinstance(fit, dict) and fit.get("fit_score") is not None:
        facts.update({k: fit[k] for k in FIT_FIELDS}, fit_method="ai")
        facts["company"] = fit.get("company") or ""
    return Opportunity.objects.create(
        source=source, owner=owner, opportunity_type=source.opportunity_type, external_id=raw.external_id, url=raw.url[:1000], title=raw.title[:500] or raw.url[:500],
        raw_text=raw.text or "", metadata=metadata, posted_at=raw.published, **facts,
    )


def add_manual(url, title, text, use_ai=True, client=None, opportunity_type="crypto", owner=None):
    """The "Add opportunity" form: something I found myself."""
    from hashlib import sha256
    if owner is None:
        raise TypeError("add_manual requires owner")
    source = manual_source(opportunity_type, owner=owner)
    external_id = "manual:" + sha256(f"{url}\n{text}".encode()).hexdigest()[:32]
    opp, created = Opportunity.objects.get_or_create(
        source=source, external_id=external_id,
        defaults={"url": url, "title": (title or url)[:500], "raw_text": text or "",
                  "opportunity_type": opportunity_type, "owner": owner,
                  "show_anyway": True},   # something I added myself is never filtered out (only blocked)
    )
    if created:
        extract_and_score(opp, use_ai=use_ai, client=client)
    return opp, created
