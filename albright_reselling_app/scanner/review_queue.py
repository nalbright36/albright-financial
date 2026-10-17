"""The /review/ queue (review_views.py, review_queue.html, static/js/
review_queue.js): one lot at a time through Check by hand and Leads to
review, as one combined list - Check by hand first (soonest-ending first),
then Leads (soonest-ending first) - under exactly the same review settings
(RESELLING_SCANNER["REVIEW"]) the dashboard's two tables use, minus every
watchlisted or still-dismissed lot. Plus why_its_here(): the evaluation's
flags and lead signals in plain words for the card's "Why it's here" box.

Read-only, like scanner/dashboard.py - the queue's actions (watch,
dismiss, undo, AI review) live in review_views.py.
"""
from django.conf import settings
from django.urls import reverse
from django.utils import timezone

from .dashboard import (
    _end_time_epoch_ms, _latest_ai_reviews, _source_display, _time_remaining, _watched_lot_ids,
    check_by_hand_evaluations, exclude_dismissed_evaluations, lead_evaluations, review_settings,
)

DESCRIPTION_PREVIEW_CHARS = 400

# evaluation.flags -> plain words. Coin/jewelry parser flags (scanner/
# coins.py, scanner/jewelry.py, scanner/llm.py) and pipeline.py's own
# reserve_not_met. Lead signals (scanner/leads.py: "bulk lot", "sealed",
# "graded"...) are already plain words and are listed together instead.
FLAG_TEXT = {
    "no_weight": "No weight stated",
    "mostly_stones": "Stones may be most of the weight",
    "weight_implausible": "Stated weight looks implausible for this item - check the listing",
    "weight_mismatch": "Stated weight doesn't fit this coin - possible fake",
    "key_date_verify": "Famous key date - verify (cheap ones are usually fakes)",
    "numismatic_upside": "May be worth more to a collector than its melt value",
    "graded": "Graded coin - may sell above melt",
    "reserve_not_met": "Reserve not met yet - the current bid isn't a real signal",
}
LEAD_SIGNALS = {"bulk lot", "complete in box", "sealed", "console", "graded", "vintage", "key cards"}


def why_its_here(evaluation, kind):
    """Plain-language reasons this lot is in the queue, most specific
    first: each parser flag (FLAG_TEXT, or a designer name), a game/card
    lead's value signals as one line ("Pokemon lead - value signals:
    graded, sealed"), the lead's own reason when nothing more specific
    applies, and - for Check by hand - that the parse itself was
    low-confidence."""
    flags = list(evaluation.flags or [])
    # Games/cards (scanner/leads.py) store their lead signals as flags.
    lead_category = evaluation.category in ("games", "cards")
    reasons = []
    signals = []
    for flag in flags:
        if lead_category and flag in LEAD_SIGNALS:
            signals.append(flag)
        elif flag in FLAG_TEXT:
            reasons.append(FLAG_TEXT[flag])
        elif flag.startswith("designer:"):
            reasons.append(f"Designer piece ({flag.split(':', 1)[1].title()}) - may sell well above scrap")
        else:
            reasons.append(flag.replace("_", " ").capitalize())

    if kind == "lead":
        reason = evaluation.lead_reason or ""
        subtype, _sep, _rest = reason.partition(": ")
        if signals:
            # Short platform keys read as acronyms ("n64" -> "N64", "ps1" -> "PS1").
            label = subtype.upper() if len(subtype) <= 4 else subtype.title()
            prefix = f"{label} lead - " if subtype else ""
            reasons.append(f"{prefix}value signals: {', '.join(signals)}")
        elif reason and not reasons:
            reasons.append(reason[0].upper() + reason[1:])
        if lead_category:
            reasons.append("No price data for this category - price it by hand")
    else:
        if signals:
            reasons.append(f"Signals: {', '.join(signals)}")
        reasons.append("Low-confidence read - the scanner isn't sure, you decide")
    # Keep order, drop exact duplicates (a flag can come from two parsers).
    return list(dict.fromkeys(reasons))


def _ai_payload(review):
    if review is None:
        return None
    return {
        "resale_low": float(review.resale_low) if review.resale_low is not None else None,
        "resale_high": float(review.resale_high) if review.resale_high is not None else None,
        "suggested_max_bid": float(review.suggested_max_bid) if review.suggested_max_bid is not None else None,
        "confidence": review.confidence,
        "summary": (review.result or {}).get("summary", ""),
        "detail_url": reverse("albright_reselling_app:ai_review_detail", args=[review.pk]),
    }


def review_card(evaluation, kind, now, ai_review=None):
    """Everything the queue's card shows for one lot, JSON-ready (the
    page embeds the whole queue up front so moving between lots never
    waits on the server)."""
    lot = evaluation.lot
    description = (lot.description or "").strip()
    has_max = kind == "check" and evaluation.max_bid and evaluation.max_bid > 0
    return {
        "lot_id": lot.pk,
        "kind": kind,
        "kind_display": "Check by hand" if kind == "check" else "Lead",
        "title": lot.title,
        "url": lot.url,
        "image_url": lot.image_url,
        "source": lot.source,
        "source_display": _source_display(lot.source),
        "category": evaluation.category,
        "current_bid": float(lot.current_price),
        "max_bid": float(evaluation.max_bid) if has_max else None,
        "headroom": float(evaluation.headroom) if has_max else None,
        "expected_sale": float(evaluation.expected_sale) if evaluation.expected_sale else None,
        "time_remaining": _time_remaining(lot, now) if lot.end_time else "unknown",
        "end_time_epoch_ms": _end_time_epoch_ms(lot),
        "why": why_its_here(evaluation, kind),
        "description_preview": description[:DESCRIPTION_PREVIEW_CHARS],
        "description_more": description[DESCRIPTION_PREVIEW_CHARS:],
        "ai_review": _ai_payload(ai_review),
        "urls": {
            "watch": reverse("albright_reselling_app:review_watch", args=[lot.pk]),
            "dismiss": reverse("albright_reselling_app:review_dismiss", args=[lot.pk]),
            "undo": reverse("albright_reselling_app:review_undo", args=[lot.pk]),
            "ai_review": reverse("albright_reselling_app:review_ai", args=[lot.pk]),
            "estimate": reverse("albright_reselling_app:lot_estimate", args=[lot.pk]),
        },
    }


def review_queue(now=None):
    """The combined queue as a list of review_card() dicts: Check by hand
    (by end time), then Leads (by end time), each under the review
    settings, never a watchlisted lot, never a still-dismissed one (same
    reopen rule as the dashboard - exclude_dismissed_evaluations()), and
    never the same lot twice (a lot that's both goes in once, as Check by
    hand)."""
    now = now or timezone.now()
    rs = review_settings()
    excluded = _watched_lot_ids()
    checks, _hidden = check_by_hand_evaluations(now, excluded, rs)
    leads, _hidden = lead_evaluations(now, settings.RESELLING_SCANNER["LEAD_WINDOW_HOURS"], excluded, rs)

    checks = exclude_dismissed_evaluations(checks)
    seen = {ev.lot_id for ev in checks}
    leads = [ev for ev in exclude_dismissed_evaluations(leads) if ev.lot_id not in seen]

    reviews = _latest_ai_reviews(ev.lot_id for ev in checks + leads)
    return (
        [review_card(ev, "check", now, reviews.get(ev.lot_id)) for ev in checks]
        + [review_card(ev, "lead", now, reviews.get(ev.lot_id)) for ev in leads]
    )
