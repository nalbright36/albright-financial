"""AI extraction: turns an opportunity's raw text into structured facts.

Same OpenAI client/settings pattern as the reseller app's AI review
(scanner/ai_review_service.py): model and per-token prices come from
RESELLING_SCANNER["AI_REVIEW"], and the monthly budget there is shared -
this app's extractions plus the reseller's reviews must stay under it.
CRYPTO["ai_daily_limit"] caps extractions per day.

The model may only report facts present in the text; anything unclear is
null/"unknown". Its evidence_quote is then checked against the source text
- a quote that isn't actually there sets evidence to "none".

Income opportunities use their own prompt (same rules) that also asks for
pay type and rate, required skills, location and the application deadline.
Hours: an hourly job's stated hours, or - for a fixed-price job only - an
estimate, marked as one (hours_is_estimate). A freelance job's reward is
its fixed price, or rate x hours. A refundable deposit (e.g. a bank bonus's
opening deposit) is recorded separately and is not spending.
"""
import json
import logging
import re
from datetime import datetime, time
from decimal import Decimal

from django.db.models import Sum
from django.utils import timezone

from . import config
from .models import PAY_TYPES, REWARD_TYPES_BY_OPPORTUNITY_TYPE, AIExtraction

log = logging.getLogger(__name__)

REWARD_TYPE_KEYS = {t: {key for key, _label in choices} for t, choices in REWARD_TYPES_BY_OPPORTUNITY_TYPE.items()}
PAY_TYPE_KEYS = {key for key, _label in PAY_TYPES}
MAX_SKILLS = 30
EVIDENCE_KEYS = {"explicit", "stated", "none"}
MAX_TEXT_CHARS = 12000

SYSTEM_PROMPT = """You extract facts about one crypto-related opportunity (a bounty, promo, task, testnet, airdrop...).
Use ONLY facts stated in the text. Never guess, infer amounts, or use outside knowledge.
Anything not clearly stated is null, or "unknown" for categories.

Return ONLY a JSON object, no other text:
{
 "reward_usd": number or null (only if a USD amount, or an amount in a USD stablecoin, is stated),
 "reward_currency": string ("" if none stated),
 "reward_type": one of "bounty","promo_reward","microtask","testnet","speculative_airdrop","points","unknown",
 "evidence": "explicit" (the text itself states the reward amount and terms), "stated" (an amount is mentioned without clear terms), or "none",
 "evidence_quote": an EXACT short quote (max 200 chars) copied from the text that states the reward, or "",
 "deadline": ISO 8601 date or datetime if stated, else null,
 "hours_estimate": number or null (only if the text gives a time estimate),
 "costs_usd": number (stated costs to take part; 0 if none stated),
 "requires_spending": true only if the text says you must buy, deposit, stake or pay,
 "steps": [{"text": short step, "automatable": true if a script could do it without logging in, signing, or spending}],
 "eligibility_notes": string (stated eligibility limits, or "")
}"""

INCOME_SYSTEM_PROMPT = """You extract facts about one way to earn money online (a freelance job, bounty, paid research study, competition, sign-up bonus, or class-action settlement).
Use ONLY facts stated in the text. Never guess, infer amounts, or use outside knowledge.
Anything not clearly stated is null, or "unknown" for categories. The one exception is hours_estimate for fixed-price jobs (see below).

Return ONLY a JSON object, no other text:
{
 "reward_type": one of "freelance","bounty","research_study","competition","bonus","settlement","unknown",
 "pay_type": "hourly", "fixed", "prize", "bonus", or null,
 "pay_rate_usd": number or null (hourly: the stated USD rate per hour; fixed: the stated USD price),
 "reward_usd": number or null (the stated USD amount paid: fixed price, prize, bonus, study payment or settlement amount; null for hourly jobs),
 "reward_currency": string ("" if none stated),
 "evidence": "explicit" (the text itself states the pay and terms), "stated" (an amount is mentioned without clear terms), or "none",
 "evidence_quote": an EXACT short quote (max 200 chars) copied from the text that states the pay, or "",
 "deadline": ISO 8601 date or datetime the work or claim is due, if stated, else null,
 "application_deadline": ISO 8601 date or datetime applications close, if stated, else null,
 "hours_estimate": number or null - hourly jobs: the hours stated in the text (null if none); fixed-price jobs: your estimate of the hours the stated scope needs; anything else: only if the text gives a time,
 "hours_is_estimate": true only when hours_estimate is your own estimate for a fixed-price job,
 "skills_required": list of short skill names the text says are required (e.g. ["Python", "Django"]), or [],
 "location_requirement": string as stated (e.g. "Remote", "US only", "Remote (EU time zones)") or null,
 "costs_usd": number (stated non-refundable costs to take part; 0 if none),
 "refundable_deposit_usd": number or null (a deposit you get back, e.g. an opening deposit for a bank bonus - NOT a cost),
 "requires_spending": true only if the text says you must buy or pay something you don't get back,
 "steps": [{"text": short step, "automatable": true if a script could do it without logging in, signing, or spending}],
 "eligibility_notes": string (stated eligibility limits, or "")
}"""
PROMPTS = {"crypto": SYSTEM_PROMPT, "income": INCOME_SYSTEM_PROMPT}


class ExtractionLimitReached(Exception):
    """Daily extraction limit or the shared monthly AI budget is used up."""


# ---------------------------------------------------------------------------
# Limits (daily count; shared monthly budget with the reseller AI reviews)
# ---------------------------------------------------------------------------

def todays_extraction_count(today=None):
    today = today or timezone.localdate()
    return AIExtraction.objects.filter(created_at__date=today).count()


def months_ai_spend(today=None):
    """This app's extraction cost plus the reseller app's AI review cost
    this month - one budget for both."""
    from albright_reselling_app.scanner.ai_review_service import months_review_cost
    today = today or timezone.localdate()
    mine = AIExtraction.objects.filter(created_at__date__gte=today.replace(day=1)).aggregate(
        total=Sum("cost_usd"))["total"] or Decimal("0")
    return mine + months_review_cost(today)


def check_limits():
    limit = int(config.get("ai_daily_limit") or 0)
    count = todays_extraction_count()
    if count >= limit:
        raise ExtractionLimitReached(f"Daily AI extraction limit reached ({count}/{limit})")
    budget = Decimal(str(config.ai_review_cfg()["monthly_budget_usd"]))
    spent = months_ai_spend()
    if spent >= budget:
        raise ExtractionLimitReached(f"Monthly AI budget reached (${spent:.2f} of ${budget:.2f})")


# ---------------------------------------------------------------------------
# The call (mockable: tests pass a fake client)
# ---------------------------------------------------------------------------

def make_client():
    """Imported inside the function, like the reseller's _make_client - the
    site still loads if the package is missing."""
    from albright_reselling_app.scanner.ai_review_service import ProviderUnavailable
    try:
        import openai
    except ImportError as exc:
        raise ProviderUnavailable("The openai package is not installed on this server") from exc
    return openai.OpenAI(timeout=60)


def _get(obj, name, default=None):
    return obj.get(name, default) if isinstance(obj, dict) else getattr(obj, name, default)


def _response_text(resp):
    texts = []
    for item in _get(resp, "output") or []:
        if _get(item, "type") == "message":
            for part in _get(item, "content") or []:
                if _get(part, "type") == "output_text":
                    texts.append(_get(part, "text", ""))
    return "\n".join(texts) or (_get(resp, "output_text") or "")


def _extract_json(text):
    text = re.sub(r"```(?:json)?", "", text or "")
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("No JSON object in model response")
    return json.loads(text[start:end + 1])


def call_model(client, title, text, cfg, opportunity_type="crypto"):
    """Returns (facts dict, cost_usd)."""
    user = f"TITLE: {title}\n\nTEXT:\n{(text or '')[:MAX_TEXT_CHARS]}"
    resp = client.responses.create(model=cfg["model"], instructions=PROMPTS[opportunity_type], input=user,
                                   max_output_tokens=cfg.get("max_tokens", 2000))
    usage = _get(resp, "usage") or {}
    cost = (_get(usage, "input_tokens", 0) or 0) / 1e6 * cfg.get("input_usd_per_mtok", 0) \
        + (_get(usage, "output_tokens", 0) or 0) / 1e6 * cfg.get("output_usd_per_mtok", 0)
    return _extract_json(_response_text(resp)), round(cost, 4)


# ---------------------------------------------------------------------------
# Validation - nothing the model says is trusted as-is
# ---------------------------------------------------------------------------

def _normalize(text):
    return re.sub(r"\s+", " ", (text or "")).strip().lower()


def quote_in_text(quote, text):
    q = _normalize(quote).strip("\"'“”‘’ ")
    return bool(q) and q in _normalize(text)


def _number(value, minimum=0):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number >= minimum else None


def _deadline(value):
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = datetime.combine(datetime.strptime(value.strip()[:10], "%Y-%m-%d").date(), time(23, 59))
        except ValueError:
            return None
    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed)
    return parsed


def _money(value):
    number = _number(value)
    return Decimal(str(round(number, 2))) if number is not None else None


def validate(facts, source_text, opportunity_type="crypto"):
    """The model's JSON -> clean, typed facts. Unknown categories become
    "unknown"/"none"; a quote that isn't in the source text drops the
    evidence to "none". Income adds the income fields (validate_income)."""
    facts = facts if isinstance(facts, dict) else {}
    allowed = REWARD_TYPE_KEYS[opportunity_type]
    reward_type = facts.get("reward_type") if facts.get("reward_type") in allowed else "unknown"
    evidence = facts.get("evidence") if facts.get("evidence") in EVIDENCE_KEYS else "none"
    quote = str(facts.get("evidence_quote") or "").strip()[:500]
    if evidence != "none" and not quote_in_text(quote, source_text):
        evidence, quote = "none", ""
    if evidence == "none":
        quote = quote if quote_in_text(quote, source_text) else ""

    steps = []
    raw_steps = facts.get("steps")
    for step in raw_steps if isinstance(raw_steps, list) else []:
        if isinstance(step, dict) and str(step.get("text") or "").strip():
            steps.append({"text": str(step["text"]).strip()[:500], "automatable": bool(step.get("automatable"))})
        elif isinstance(step, str) and step.strip():
            steps.append({"text": step.strip()[:500], "automatable": False})

    reward = _number(facts.get("reward_usd"))
    cleaned = {
        "reward_usd": Decimal(str(round(reward, 2))) if reward is not None else None,
        "reward_currency": str(facts.get("reward_currency") or "")[:20],
        "reward_type": reward_type,
        "evidence": evidence,
        "evidence_quote": quote,
        "deadline": _deadline(facts.get("deadline")),
        "hours_estimate": (Decimal(str(round(h, 2))) if (h := _number(facts.get("hours_estimate"))) else None),
        "costs_usd": Decimal(str(round(_number(facts.get("costs_usd")) or 0, 2))),
        "requires_spending": facts.get("requires_spending") is True,
        "steps": steps,
        "automatable_steps": sum(1 for s in steps if s["automatable"]),
        "eligibility_notes": str(facts.get("eligibility_notes") or "")[:2000],
    }
    if opportunity_type == "income":
        cleaned.update(validate_income(facts, cleaned))
    return cleaned


def validate_income(facts, cleaned):
    """The income-only fields, plus the hours and reward rules for them:
    hours may be an estimate only for a fixed-price job (marked as one); a
    freelance job's reward is the fixed price or rate x hours; a refundable
    deposit isn't spending."""
    pay_type = facts.get("pay_type") if facts.get("pay_type") in PAY_TYPE_KEYS else None
    rate = _money(facts.get("pay_rate_usd"))
    hours = cleaned["hours_estimate"]
    hours_is_estimate = facts.get("hours_is_estimate") is True
    if hours is not None and pay_type == "fixed":
        hours_is_estimate = True  # a fixed job's hours are never part of the stated terms
    elif hours_is_estimate:
        hours, hours_is_estimate = None, False  # only fixed-price jobs may carry an estimate

    reward = cleaned["reward_usd"]
    if cleaned["reward_type"] == "freelance":
        if pay_type == "fixed":
            reward = reward if reward is not None else rate
        elif pay_type == "hourly":
            reward = (rate * hours).quantize(Decimal("0.01")) if rate is not None and hours is not None else None

    raw_skills = facts.get("skills_required")
    skills = []
    for skill in raw_skills if isinstance(raw_skills, list) else []:
        text = str(skill or "").strip()[:60]
        if text and text.lower() not in (s.lower() for s in skills):
            skills.append(text)

    deposit = _money(facts.get("refundable_deposit_usd")) or None
    requires_spending = cleaned["requires_spending"]
    if deposit and not cleaned["costs_usd"]:
        requires_spending = False  # the only money involved comes back

    return {
        "pay_type": pay_type,
        "pay_rate_usd": rate,
        "reward_usd": reward,
        "hours_estimate": hours,
        "hours_is_estimate": hours_is_estimate,
        "skills_required": skills[:MAX_SKILLS],
        "location_requirement": str(facts.get("location_requirement") or "").strip()[:200] or None,
        "application_deadline": _deadline(facts.get("application_deadline")),
        "refundable_deposit_usd": deposit,
        "requires_spending": requires_spending,
    }


def extract(opportunity, client=None):
    """Runs one extraction for an Opportunity (respecting the limits),
    records it as an AIExtraction, and returns the validated facts.
    Raises ExtractionLimitReached when over a limit (nothing is called)."""
    check_limits()
    cfg = config.ai_review_cfg()
    client = client or make_client()
    try:
        facts, cost = call_model(client, opportunity.title, opportunity.raw_text, cfg, opportunity.opportunity_type)
    except Exception as exc:  # noqa: BLE001 - record the failure, never crash the scan
        log.warning("AI extraction failed for opportunity %s: %s", opportunity.pk, exc)
        AIExtraction.objects.create(opportunity=opportunity, model_name=cfg.get("model", ""), status="error",
                                    error=str(exc)[:2000])
        raise
    AIExtraction.objects.create(opportunity=opportunity, model_name=cfg.get("model", ""), cost_usd=Decimal(str(cost)))
    return validate(facts, f"{opportunity.title}\n{opportunity.raw_text}", opportunity.opportunity_type)
