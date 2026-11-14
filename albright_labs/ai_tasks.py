"""Shared AI call helpers (limits, client, JSON responses).

Meals recipe import and Opportunities extraction share one daily limit and
the reseller app's monthly AI budget. The AIExtraction table still lives in
albright_crypto_app (including purpose="recipe" rows for Meals).
"""
import json
import logging
import re
from decimal import Decimal

from django.db.models import Sum
from django.utils import timezone

log = logging.getLogger(__name__)

MAX_TEXT_CHARS = 12000


class ExtractionLimitReached(Exception):
    """Daily extraction limit or the shared monthly AI budget is used up."""


def todays_extraction_count(today=None):
    from albright_crypto_app.models import AIExtraction
    today = today or timezone.localdate()
    return AIExtraction.objects.filter(created_at__date=today).count()


def months_ai_spend(today=None):
    """This app's extraction cost plus the reseller app's AI review cost
    this month - one budget for both."""
    from albright_crypto_app.models import AIExtraction
    from albright_reselling_app.scanner.ai_review_service import months_review_cost
    today = today or timezone.localdate()
    mine = AIExtraction.objects.filter(created_at__date__gte=today.replace(day=1)).aggregate(
        total=Sum("cost_usd"))["total"] or Decimal("0")
    return mine + months_review_cost(today)


def check_limits():
    from albright_crypto_app import config
    limit = int(config.get("ai_daily_limit") or 0)
    count = todays_extraction_count()
    if count >= limit:
        raise ExtractionLimitReached(f"Daily AI extraction limit reached ({count}/{limit})")
    budget = Decimal(str(config.ai_review_cfg()["monthly_budget_usd"]))
    spent = months_ai_spend()
    if spent >= budget:
        raise ExtractionLimitReached(f"Monthly AI budget reached (${spent:.2f} of ${budget:.2f})")


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


def run_json(instructions, user_text, purpose, opportunity=None, client=None, max_tokens=None):
    """One AI call that returns JSON. Returns (parsed dict, cost_usd as Decimal).

    Raises ExtractionLimitReached (nothing is called) when over a limit; any
    other failure is recorded as an error row and re-raised.
    """
    from albright_crypto_app import config
    from albright_crypto_app.models import AIExtraction

    check_limits()
    cfg = config.ai_review_cfg()
    client = client or make_client()
    try:
        resp = client.responses.create(model=cfg["model"], instructions=instructions,
                                       input=(user_text or "")[:MAX_TEXT_CHARS * 2],
                                       max_output_tokens=max_tokens or cfg.get("max_tokens", 2000))
        usage = _get(resp, "usage") or {}
        cost = (_get(usage, "input_tokens", 0) or 0) / 1e6 * cfg.get("input_usd_per_mtok", 0) \
            + (_get(usage, "output_tokens", 0) or 0) / 1e6 * cfg.get("output_usd_per_mtok", 0)
        data = _extract_json(_response_text(resp))
    except Exception as exc:  # noqa: BLE001 - recorded, then re-raised for the caller to report
        log.warning("AI %s call failed: %s", purpose, exc)
        AIExtraction.objects.create(opportunity=opportunity, purpose=purpose, model_name=cfg.get("model", ""),
                                    status="error", error=str(exc)[:2000])
        raise
    cost = Decimal(str(round(cost, 4)))
    AIExtraction.objects.create(opportunity=opportunity, purpose=purpose, model_name=cfg.get("model", ""),
                                cost_usd=cost)
    return (data if isinstance(data, dict) else {}), cost
