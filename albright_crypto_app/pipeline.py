"""Collect -> store -> extract (AI) -> safety check + score -> alert.

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
from decimal import Decimal

from django.db.models import F
from django.utils import timezone

from . import config, relevance
from .collectors import COLLECTORS
from .collectors.base import SourceBlocked, SourceUnavailable
from .core.safety import check_domain, check_opportunity
from .core.scoring import score
from .extraction import ExtractionLimitReached, extract
from .models import Opportunity, Source, TaskStep
from .skills import skill_match

log = logging.getLogger(__name__)

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
# Sources: one row per configured GitHub query / RSS feed, plus "Manual"
# (one per type). Income sources are added on the Settings page.
# ---------------------------------------------------------------------------

def manual_source(opportunity_type="crypto"):
    source, _ = Source.objects.get_or_create(kind="manual", url="", opportunity_type=opportunity_type,
                                             defaults={"name": MANUAL_SOURCE_NAMES[opportunity_type]})
    return source


def sync_sources():
    """Make sure every configured GitHub query and RSS feed has a Source
    row (so it can be switched off or marked "terms checked"). A feed no
    longer in the list is deactivated, not deleted (its opportunities keep
    their source); re-adding a feed on the Settings page reactivates it
    (settings_view). A source switched off by hand stays off."""
    queries = list(config.get("github_queries") or [])
    feeds = list(config.get("rss_feeds") or [])
    for query in queries:
        Source.objects.get_or_create(kind="github", url=query, defaults={"name": f"GitHub: {query}"})
    for feed in feeds:
        Source.objects.get_or_create(kind="rss", url=feed, defaults={"name": feed})
    Source.objects.filter(kind="rss", active=True).exclude(url__in=feeds).update(active=False)
    for opportunity_type in MANUAL_SOURCE_NAMES:
        manual_source(opportunity_type)


# The default job sources (tuned for my target roles). Created once - each
# can be switched off on the Settings page and stays off. A new one inherits
# "terms checked" from another source on the same site (the terms are the
# site's), so a site whose terms I haven't checked still never runs; every
# site's minimum interval (Remotive / Remote OK: one fetch per 6 hours across
# all their sources) still applies.
DEFAULT_INCOME_SOURCES = [
    ("remotive", "Remotive: data", {"category": "data"}),
    ("remotive", "Remotive: software-dev", {"category": "software-dev"}),
    ("remotive", "Remotive: power bi", {"search": "power bi"}),
    ("remotive", "Remotive: tableau", {"search": "tableau"}),
    ("remotive", "Remotive: analytics engineer", {"search": "analytics engineer"}),
    ("remotive", "Remotive: salesforce administrator", {"search": "salesforce administrator"}),
    ("remoteok", "Remote OK: data", {"tag": "data"}),
    ("remoteok", "Remote OK: analytics", {"tag": "analytics"}),
    ("remoteok", "Remote OK: python", {"tag": "python"}),
    ("remoteok", "Remote OK: salesforce", {"tag": "salesforce"}),
    ("rss_income", "We Work Remotely: programming",
     "https://weworkremotely.com/categories/remote-programming-jobs.rss"),
    ("rss_income", "We Work Remotely: data", "https://weworkremotely.com/categories/remote-data-jobs.rss"),
    ("hn_hiring", "HN: Who is hiring", None),
]
# Couldn't be confirmed when added (the site refuses automated checks) - starts switched off.
UNVERIFIED_SOURCE_URLS = {"https://weworkremotely.com/categories/remote-data-jobs.rss"}


def sync_default_income_sources():
    from .collectors.hn_hiring import API as HN_API
    from .collectors.job_boards import endpoint_url
    for kind, name, spec in DEFAULT_INCOME_SOURCES:
        options = spec if isinstance(spec, dict) else {}
        url = endpoint_url(kind, options) if kind in Source.ATTRIBUTED_KINDS else (HN_API if kind == "hn_hiring" else spec)
        if Source.objects.filter(kind=kind, url=url, opportunity_type="income").exists():
            continue
        site_terms = Source.objects.filter(kind=kind, opportunity_type="income", terms_checked=True)
        if kind == "rss_income":
            site_terms = site_terms.filter(url__startswith=url.split("/categories/")[0])
        unverified = url in UNVERIFIED_SOURCE_URLS
        Source.objects.create(
            kind=kind, url=url, name=name, opportunity_type="income", options=options,
            active=not unverified, terms_checked=site_terms.exists(),
            notes="Feed URL not confirmed - check it opens, then switch it on." if unverified else "")


# ---------------------------------------------------------------------------
# Safety + scoring (the provided core modules)
# ---------------------------------------------------------------------------

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

def store(source, raw):
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
        source=source, opportunity_type=source.opportunity_type, external_id=raw.external_id, url=raw.url[:1000], title=raw.title[:500] or raw.url[:500],
        raw_text=raw.text or "", metadata=metadata, posted_at=raw.published, **facts,
    )


def add_manual(url, title, text, use_ai=True, client=None, opportunity_type="crypto"):
    """The "Add opportunity" form: something I found myself."""
    from hashlib import sha256
    source = manual_source(opportunity_type)
    external_id = "manual:" + sha256(f"{url}\n{text}".encode()).hexdigest()[:32]
    opp, created = Opportunity.objects.get_or_create(
        source=source, external_id=external_id,
        defaults={"url": url, "title": (title or url)[:500], "raw_text": text or "",
                  "opportunity_type": opportunity_type,
                  "show_anyway": True},   # something I added myself is never filtered out (only blocked)
    )
    if created:
        extract_and_score(opp, use_ai=use_ai, client=client)
    return opp, created


def runnable_sources():
    """Active collected sources - income ones only once their terms are checked.
    Least recently checked first, so sources sharing a site's interval take turns."""
    qs = Source.objects.filter(active=True).exclude(kind="manual").order_by(F("last_checked").asc(nulls_first=True))
    return [s for s in qs if s.runnable]


def fetched_too_recently(source, min_interval, now=None):
    """A site with a minimum interval (Remotive, Remote OK: 6 hours) is
    fetched at most once per interval - across every source of that kind,
    so several sources for one site can't add up to more fetches."""
    now = now or timezone.now()
    return Source.objects.filter(kind=source.kind, last_checked__gt=now - min_interval).exists()


def scan_sources(sources=None, use_ai=True, client=None, collectors=None):
    """Runs every runnable source (GitHub/RSS, and income RSS/API sources
    whose terms are checked). Returns (new opportunities, errors). A
    blocked source (403/429) stops that source only."""
    collectors = collectors or COLLECTORS
    sources = sources if sources is not None else runnable_sources()
    new, errors = [], []
    for source in sources:
        collector_class = collectors.get(source.kind)
        if collector_class is None or not source.runnable:
            continue  # never fetch from a source that's off, or whose terms I haven't checked
        if collector_class.min_interval and fetched_too_recently(source, collector_class.min_interval):
            log.info("Skipping %s: fetched less than %s ago", source.name, collector_class.min_interval)
            continue  # not an error - the next scan after the interval picks it up
        collector = collector_class()
        collector.use_ai, collector.ai_client = use_ai, client
        try:
            for raw in collector.collect(source):
                opp = store(source, raw)
                if opp is not None:
                    new.append(extract_and_score(opp, use_ai=use_ai, client=client))
        except SourceBlocked as exc:
            errors.append(f"{source.name}: blocked - {exc}")
        except SourceUnavailable as exc:
            errors.append(f"{source.name}: unavailable - {exc}")
        except Exception as exc:  # noqa: BLE001 - one bad source never stops the others
            log.exception("Source %s failed", source.pk)
            errors.append(f"{source.name}: {exc}")
        source.last_checked = timezone.now()
        source.save(update_fields=["last_checked"])
    return new, errors
