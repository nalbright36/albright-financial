"""Django-side wiring for scanner/ai_review.py: turns one SourcedLot into
the plain dict run_review() expects, fetches optional eBay active listings,
calls the Anthropic API, and saves the result as an AIReview.

This is deliberately separate from ai_review.py itself (which has no Django
imports and is unit tested with a mocked client) and from pipeline.py (which
runs unattended from the scheduled task) - reviews here only ever run when a
user clicks the button, and are rate/cost limited accordingly.
"""
import logging
from decimal import Decimal

import anthropic
from django.conf import settings
from django.db.models import Sum
from django.utils import timezone

from ..scanner_models import AIReview
from . import ebay
from .ai_review import run_review
from .max_bid import BuyCosts, SellFees
from .max_bid import max_bid as compute_max_bid
from .pipeline import _inbound_shipping

log = logging.getLogger(__name__)


class ReviewLimitExceeded(Exception):
    """Raised instead of running (and paying for) a review once the daily
    count or monthly budget is reached. Callers (the view) catch this
    specifically to redirect back with a message, rather than treating it
    as a review failure."""


def _to_decimal(value):
    return Decimal(str(value)) if value is not None else None


def todays_review_count(today=None):
    """Also used by the dashboard's "AI reviews: N today" line, so the
    count there and the daily-limit check here can never disagree."""
    today = today or timezone.localdate()
    return AIReview.objects.filter(created_at__date=today).count()


def months_review_cost(today=None):
    """Also used by the dashboard's "cost this month" line."""
    today = today or timezone.localdate()
    month_start = today.replace(day=1)
    return AIReview.objects.filter(created_at__date__gte=month_start).aggregate(
        total=Sum("cost_usd")
    )["total"] or Decimal("0")


def _check_limits(cfg):
    today = timezone.localdate()
    today_count = todays_review_count(today)
    if today_count >= cfg["daily_limit"]:
        raise ReviewLimitExceeded(f"Daily AI review limit reached ({today_count}/{cfg['daily_limit']})")

    month_cost = months_review_cost(today)
    budget = Decimal(str(cfg["monthly_budget_usd"]))
    if month_cost >= budget:
        raise ReviewLimitExceeded(
            f"Monthly AI review budget reached (${month_cost:.2f} of ${budget:.2f})"
        )


def _lot_dict(lot, evaluation):
    return {
        "title": lot.title,
        "description": lot.description,
        "category": evaluation.category if evaluation else "unknown",
        "source": lot.source,
        "current_bid": float(lot.current_price),
        "ends": lot.end_time.isoformat() if lot.end_time else "unknown",
        "melt_value": float(evaluation.melt_value) if evaluation else 0.0,
    }


def _ebay_listings(lot):
    if not ebay.is_configured():
        return []
    try:
        return ebay.search_active(ebay.query_from_title(lot.title))
    except Exception as exc:  # noqa: BLE001 - eBay is optional; never block a review over it
        log.warning("eBay search failed for lot %s: %s", lot.pk, exc)
        return []


def _suggested_max_bid(resale_low, category, lot):
    """Same fee/cost math the pipeline uses for its own max bid: FEES
    merged with CATEGORY_FEES for this category, and the lot's actual
    buy-side costs (including the MaxSold pickup cost, if any)."""
    cfg = settings.RESELLING_SCANNER
    src = cfg["SOURCES"][lot.source]
    fees = {**cfg["FEES"], **cfg["CATEGORY_FEES"].get(category, {})}
    buy = BuyCosts(src["buyer_premium_pct"], src["sales_tax_pct"], _inbound_shipping(lot.raw, src))
    return compute_max_bid(resale_low, SellFees(**fees), buy)


def review_lot(lot):
    """lot: a SourcedLot instance. Returns the saved AIReview (status "done"
    or "error" - only ReviewLimitExceeded is raised instead of saved,
    since that means we refused to spend anything at all)."""
    cfg = settings.RESELLING_SCANNER["AI_REVIEW"]
    _check_limits(cfg)

    evaluation = getattr(lot, "evaluation", None)
    lot_dict = _lot_dict(lot, evaluation)
    ebay_listings = _ebay_listings(lot)

    try:
        client = anthropic.Anthropic(timeout=120)
        result = run_review(client, lot_dict, cfg, ebay_listings)
    except Exception as exc:  # noqa: BLE001 - always save what happened, never crash the request
        log.error("AI review failed for lot %s: %s", lot.pk, exc)
        return AIReview.objects.create(
            lot=lot, model_name=cfg.get("model", ""), status="error", error=str(exc), cost_usd=Decimal("0"),
        )

    suggested_max_bid = None
    if result.resale_low is not None:
        category = evaluation.category if evaluation else "unknown"
        suggested_max_bid = _suggested_max_bid(result.resale_low, category, lot)

    return AIReview.objects.create(
        lot=lot,
        model_name=cfg.get("model", ""),
        status="done",
        result=result.to_dict(),
        resale_low=_to_decimal(result.resale_low),
        resale_high=_to_decimal(result.resale_high),
        suggested_max_bid=_to_decimal(suggested_max_bid),
        confidence=result.confidence,
        cost_usd=Decimal(str(result.cost_usd)),
    )
