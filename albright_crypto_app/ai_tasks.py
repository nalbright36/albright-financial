"""One AI call that returns JSON, for every AI feature beyond fact
extraction (email splitting, HN filtering, walkthroughs, resumes,
proposals). Same client, model, per-token prices, daily limit and shared
monthly budget as extraction.py - each call is an AIExtraction row (with a
purpose), so all of them count toward ai_daily_limit and the budget.

Raises ExtractionLimitReached (nothing is called) when over a limit; any
other failure is recorded as an error row and re-raised.
"""
import logging
from decimal import Decimal

from . import config
from .extraction import MAX_TEXT_CHARS, _extract_json, _get, _response_text, check_limits, make_client
from .models import AIExtraction

log = logging.getLogger(__name__)


def run_json(instructions, user_text, purpose, opportunity=None, client=None, max_tokens=None):
    """Returns (parsed JSON dict, cost_usd as Decimal)."""
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
