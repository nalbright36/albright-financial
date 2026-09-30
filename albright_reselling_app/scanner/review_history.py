"""Query/filter/pagination/CSV logic for the AI Reviews history page
(scanner_views.review_history). Kept separate from the view - same pattern
as scanner/dashboard.py - so it's testable without going through a
request/response cycle.
"""
import csv
import io
from datetime import datetime
from decimal import Decimal

from django.conf import settings
from django.db.models import F, Q, Sum
from django.utils import timezone

from ..scanner_models import AIReview
from .ai_review_service import months_review_cost
from .dashboard import _format_duration, _source_display

PAGE_SIZE = 25

SOURCE_CHOICES = [("shopgoodwill", "ShopGoodwill"), ("maxsold", "MaxSold")]
CATEGORY_CHOICES = [("coins", "Coins"), ("jewelry", "Jewelry"), ("games", "Games"), ("cards", "Cards")]
CONFIDENCE_CHOICES = [("high", "High"), ("medium", "Medium"), ("low", "Low")]
OUTCOME_CHOICES = [("live", "Live"), ("under", "Closed under AI max"), ("over", "Closed over AI max")]

CSV_HEADERS = [
    "Reviewed At", "Lot Title", "Source", "Category", "Bid At Review", "Scanner Max At Review",
    "AI Resale Low", "AI Resale High", "AI Suggested Max Bid", "Confidence", "Verified Comps",
    "Dropped Comps", "Red Flag Count", "Cost", "Status", "Outcome", "Final Price", "Summary",
    "Red Flags", "Comp URLs",
]


def _parse_date(value):
    """"2026-09-01" -> a date, or None if blank/unparseable. A bad date in
    a hand-edited URL is silently ignored rather than 500ing the page."""
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None


def filters_from_params(params):
    """Normalizes the GET params this page understands into one dict, so
    the view, the template (to re-populate the filter form) and tests all
    agree on the same keys."""
    return {
        "source": params.get("source") or "",
        "category": params.get("category") or "",
        "confidence": params.get("confidence") or "",
        "status": params.get("status") or "",
        "outcome": params.get("outcome") or "",
        "q": (params.get("q") or "").strip(),
        "date_from": params.get("from") or "",
        "date_to": params.get("to") or "",
    }


def filtered_reviews(filters):
    """A queryset, newest first (the model's default ordering), narrowed by
    every filter that was actually set."""
    qs = AIReview.objects.select_related("lot", "lot__evaluation")

    if filters["source"]:
        qs = qs.filter(lot__source=filters["source"])
    if filters["category"]:
        qs = qs.filter(lot__evaluation__category=filters["category"])
    if filters["confidence"]:
        qs = qs.filter(confidence=filters["confidence"])
    if filters["status"]:
        qs = qs.filter(status=filters["status"])
    if filters["q"]:
        qs = qs.filter(lot__title__icontains=filters["q"])

    date_from = _parse_date(filters["date_from"])
    if date_from:
        qs = qs.filter(created_at__date__gte=date_from)
    date_to = _parse_date(filters["date_to"])
    if date_to:
        qs = qs.filter(created_at__date__lte=date_to)

    outcome = filters["outcome"]
    if outcome == "live":
        qs = qs.filter(Q(lot__is_closed=False) | Q(lot__final_price__isnull=True))
    elif outcome == "under":
        qs = qs.filter(lot__is_closed=True, lot__final_price__isnull=False,
                        suggested_max_bid__isnull=False, lot__final_price__lte=F("suggested_max_bid"))
    elif outcome == "over":
        qs = qs.filter(lot__is_closed=True, lot__final_price__isnull=False,
                        suggested_max_bid__isnull=False, lot__final_price__gt=F("suggested_max_bid"))

    return qs


def _outcome_for(review):
    """Display-only outcome, one of Live/Unknown/Under AI max/Over AI max -
    "Unknown" (closed, but no suggested max bid to compare against) isn't
    one of the filterable OUTCOME_CHOICES above, since there's nothing
    meaningful to filter *for* there, but it still needs to render."""
    lot = review.lot
    if not lot.is_closed or lot.final_price is None:
        return {"label": "Live", "final_price": None, "verdict": "live"}
    if review.suggested_max_bid is None:
        return {"label": "Unknown", "final_price": lot.final_price, "verdict": "unknown"}
    under = lot.final_price <= review.suggested_max_bid
    return {
        "label": "Under AI max" if under else "Over AI max",
        "final_price": lot.final_price,
        "verdict": "under" if under else "over",
    }


def time_remaining_at_review(review):
    """"3h 12m" as of when the review ran, from the snapshot fields - None
    for reviews saved before the snapshot existed."""
    if not review.lot_end_time_at_review:
        return None
    delta = review.lot_end_time_at_review - review.created_at
    return _format_duration(delta) if delta.total_seconds() > 0 else "Ended"


def row_for(review):
    lot = review.lot
    evaluation = getattr(lot, "evaluation", None)
    result = review.result or {}
    comps = result.get("comps") or []
    red_flags = result.get("red_flags") or []
    return {
        "review": review,
        "reviewed_at": timezone.localtime(review.created_at),
        "reviewed_at_epoch_ms": int(review.created_at.timestamp() * 1000),
        "lot_title": lot.title,
        "lot_url": lot.url,
        "source": lot.source,
        "source_display": _source_display(lot.source),
        "category": evaluation.category if evaluation else "unknown",
        "bid_at_review": review.bid_at_review,
        "scanner_max_bid_at_review": review.scanner_max_bid_at_review,
        "resale_low": review.resale_low,
        "resale_high": review.resale_high,
        "suggested_max_bid": review.suggested_max_bid,
        "confidence": review.confidence,
        "comps_count": len(comps),
        "dropped_comps": result.get("dropped_comps") or 0,
        "red_flags_count": len(red_flags),
        "red_flags": red_flags,
        "cost_usd": review.cost_usd,
        "status": review.status,
        "summary": result.get("summary") or "",
        "comp_urls": [c.get("url") for c in comps if c.get("url")],
        "outcome": _outcome_for(review),
    }


def summary_stats():
    today = timezone.localdate()
    month_start = today.replace(day=1)
    all_reviews = AIReview.objects.all()
    this_month_qs = all_reviews.filter(created_at__date__gte=month_start)

    total = all_reviews.count()
    total_cost = all_reviews.aggregate(total=Sum("cost_usd"))["total"] or Decimal("0")
    cfg = settings.RESELLING_SCANNER["AI_REVIEW"]

    return {
        "total_reviews": total,
        "reviews_this_month": this_month_qs.count(),
        "total_cost": total_cost,
        "month_cost": months_review_cost(today),  # same value the dashboard shows, can't drift apart
        "monthly_budget": Decimal(str(cfg["monthly_budget_usd"])),
        "avg_cost": (total_cost / total) if total else Decimal("0"),
        "by_confidence": {c: all_reviews.filter(confidence=c).count() for c in ("high", "medium", "low")},
        "by_status": {s: all_reviews.filter(status=s).count() for s, _ in AIReview.STATUS_CHOICES},
    }


def reviews_to_csv(queryset):
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(CSV_HEADERS)
    for review in queryset:
        row = row_for(review)
        outcome = row["outcome"]
        writer.writerow([
            row["reviewed_at"].strftime("%Y-%m-%d %H:%M"),
            row["lot_title"],
            row["source_display"],
            row["category"],
            row["bid_at_review"] if row["bid_at_review"] is not None else "",
            row["scanner_max_bid_at_review"] if row["scanner_max_bid_at_review"] is not None else "",
            row["resale_low"] if row["resale_low"] is not None else "",
            row["resale_high"] if row["resale_high"] is not None else "",
            row["suggested_max_bid"] if row["suggested_max_bid"] is not None else "",
            row["confidence"],
            row["comps_count"],
            row["dropped_comps"],
            row["red_flags_count"],
            row["cost_usd"],
            row["status"],
            outcome["label"],
            outcome["final_price"] if outcome["final_price"] is not None else "",
            row["summary"],
            "; ".join(row["red_flags"]),
            " | ".join(row["comp_urls"]),
        ])
    return buffer.getvalue()
