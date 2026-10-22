"""AI spend for the current month, by day and by feature, against the
shared monthly budget (RESELLING_SCANNER["AI_REVIEW"]["monthly_budget_usd"]
- reseller AI reviews and every Opportunities AI call count toward it) and
the daily limits (reseller reviews per day; Opportunities AI calls per day)."""
from collections import defaultdict
from decimal import Decimal

from django.conf import settings
from django.db.models import Count, Sum
from django.db.models.functions import TruncDate
from django.utils import timezone

FEATURES = [
    ("reseller_review", "Reseller AI reviews"),
    ("extract", "Opportunities: fact extraction"),
    ("email_split", "Opportunities: email alerts"),
    ("hn_filter", "Opportunities: HN filter"),
    ("walkthrough", "Walkthroughs"),
    ("proposal", "Proposals"),
    ("resume", "Tailored resumes"),
]
UNTRACKED = ["Reseller deep-dive photo checks (Anthropic) don't record their cost - not included."]


def month_usage(today=None):
    from albright_crypto_app import config as crypto_config
    from albright_crypto_app.models import AIExtraction
    from albright_reselling_app.scanner_models import AIReview

    today = today or timezone.localdate()
    start = today.replace(day=1)
    cells = defaultdict(lambda: {"cost": Decimal("0"), "calls": 0})   # (day, feature) -> totals
    for row in (AIReview.objects.filter(created_at__date__gte=start).annotate(day=TruncDate("created_at"))
                .values("day").annotate(cost=Sum("cost_usd"), calls=Count("pk"))):
        cells[(row["day"], "reseller_review")] = {"cost": row["cost"] or Decimal("0"), "calls": row["calls"]}
    for row in (AIExtraction.objects.filter(created_at__date__gte=start).annotate(day=TruncDate("created_at"))
                .values("day", "purpose").annotate(cost=Sum("cost_usd"), calls=Count("pk"))):
        cells[(row["day"], row["purpose"])] = {"cost": row["cost"] or Decimal("0"), "calls": row["calls"]}

    features = [key for key, _label in FEATURES]
    days = sorted({day for day, _f in cells}, reverse=True)
    by_day = [{"day": day, "cells": [cells.get((day, f), {"cost": Decimal("0"), "calls": 0}) for f in features],
               "total": sum((cells[(day, f)]["cost"] for f in features if (day, f) in cells), Decimal("0"))}
              for day in days]
    by_feature = [{"key": key, "label": label,
                   "cost": sum((v["cost"] for (d, f), v in cells.items() if f == key), Decimal("0")),
                   "calls": sum(v["calls"] for (d, f), v in cells.items() if f == key)} for key, label in FEATURES]
    total = sum((f["cost"] for f in by_feature), Decimal("0"))
    cfg = settings.RESELLING_SCANNER["AI_REVIEW"]
    budget = Decimal(str(cfg["monthly_budget_usd"]))
    today_reviews = cells.get((today, "reseller_review"), {"calls": 0})["calls"]
    today_opps = sum(v["calls"] for (d, f), v in cells.items() if d == today and f != "reseller_review")
    return {
        "month": start, "features": FEATURES, "by_day": by_day, "by_feature": by_feature, "total": total,
        "budget": budget, "remaining": budget - total, "pct": (total / budget * 100) if budget else None,
        "limits": [
            {"label": "Monthly budget (shared by every AI feature)", "limit": f"${budget:.2f}", "used": f"${total:.2f}"},
            {"label": "Reseller AI reviews per day", "limit": cfg["daily_limit"], "used": today_reviews},
            {"label": "Opportunities AI calls per day", "limit": crypto_config.get("ai_daily_limit"), "used": today_opps},
        ],
        "model": cfg.get("model", ""), "untracked": UNTRACKED,
    }
