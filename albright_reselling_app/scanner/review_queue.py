"""The /review/ queue (review_views.py, review_queue.html, static/js/
review_queue.js): one lot at a time, in one of three scopes (SCOPES):

  review      Check by hand, then Leads - each soonest-ending first
  candidates  Act now's Candidates ending soon, soonest-ending first
  later       the Watch tab's Ending later candidates, soonest-ending first

All under the review settings (RESELLING_SCANNER["REVIEW"]) where they
apply, minus every watchlisted or still-dismissed lot. The card's "Why
it's here" box is why_its_here() for Check by hand/Leads (flags and lead
signals in plain words) and valuation_summary() for candidates (what the
scanner identified and how it priced it).

Read-only, like scanner/dashboard.py - the queue's actions (watch,
dismiss, undo, AI review) live in review_views.py.
"""
from datetime import timedelta

from django.conf import settings
from django.urls import reverse
from django.utils import timezone

from ..scanner_models import LotEvaluation
from .coins import COIN_TYPES
from .dashboard import (
    _end_time_epoch_ms, _item_label, _latest_ai_reviews, _source_display, _time_remaining, _watched_lot_ids,
    check_by_hand_evaluations, exclude_dismissed_evaluations, lead_evaluations, passes_review_settings,
    review_settings,
)
from .jewelry import GOLD_KARATS

# (key, label) in selector order; "review" is the default.
SCOPES = [
    ("review", "Check by hand + Leads"),
    ("candidates", "Candidates ending soon"),
    ("later", "Ending later"),
]
SCOPE_LABELS = dict(SCOPES)
DEFAULT_SCOPE = "review"

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


# ---------------------------------------------------------------------------
# Candidates: the valuation in plain words
# ---------------------------------------------------------------------------

GRAMS_PER_OZT = 31.1035
STERLING_PURITY = 0.925
_COIN_OZ = {}
for _ct in COIN_TYPES:
    _COIN_OZ.setdefault(_ct.key, (_ct.metal, _ct.oz))  # first entry per key - the standard size


def _money(value):
    return f"${value:,.2f}"


def _identified(evaluation):
    """What the scanner identified, from what it stored: coin types (with a
    count when one coin type explains all of that metal's weight -
    total pure ounces / ounces per coin) or jewelry (karat or sterling,
    with the approximate gross weight implied by the pure ounces). The
    parser's per-item notes aren't stored, so this is reconstructed."""
    keys = [k for k in (evaluation.coin_keys or "").split(",") if k]
    silver, gold = float(evaluation.silver_oz or 0), float(evaluation.gold_oz or 0)
    parts = []
    for key in keys:
        label = _item_label(key)
        if key.startswith("jewelry_gold_") and gold:
            karat = int(key[len("jewelry_gold_"):-1])
            grams = gold * GRAMS_PER_OZT / GOLD_KARATS[karat]
            parts.append(f"{karat}k gold, ~{grams:.1f}g")
        elif key == "jewelry_sterling" and silver:
            parts.append(f"Sterling silver, ~{silver * GRAMS_PER_OZT / STERLING_PURITY:.1f}g")
        elif key in _COIN_OZ and len(keys) == 1 and "(" not in label and not key.startswith("generic_"):
            # A count only makes sense for a real coin type ("10 Mercury dimes") - not
            # face-value junk silver or generic bars/rounds.
            metal, oz_each = _COIN_OZ[key]
            total = silver if metal == "silver" else gold
            count = round(total / oz_each) if oz_each else 0
            parts.append(f"{count} {label}{'s' if count != 1 and not label.endswith('s') else ''}"
                         if count > 1 else label)
        else:
            parts.append(label)
    metal = ", ".join(p for p in (f"{silver:.2f} oz silver" if silver else "",
                                  f"{gold:.3f} oz gold" if gold else "") if p)
    text = "; ".join(parts) if parts else "Nothing specific identified"
    return f"{text} ({metal} pure)" if metal else text


def valuation_summary(evaluation):
    """The candidate card's "Why it's here": what was identified, melt
    value, expected sale, max bid, headroom, confidence, then any flags
    in plain words (FLAG_TEXT) - the stored stand-in for parser notes."""
    lines = [
        f"Identified: {_identified(evaluation)}",
        f"Melt value {_money(evaluation.melt_value)} · expected sale {_money(evaluation.expected_sale)}",
        f"Max bid {_money(evaluation.max_bid)} · headroom {_money(evaluation.headroom)}",
        f"Confidence: {evaluation.confidence or 'unknown'}",
    ]
    for flag in evaluation.flags or []:
        if flag in FLAG_TEXT:
            lines.append(FLAG_TEXT[flag])
        elif flag.startswith("designer:"):
            lines.append(f"Designer piece ({flag.split(':', 1)[1].title()}) - may sell well above scrap")
        else:
            lines.append(flag.replace("_", " ").capitalize())
    return lines


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
    has_max = kind in ("check", "candidate") and evaluation.max_bid and evaluation.max_bid > 0
    return {
        "lot_id": lot.pk,
        "kind": kind,
        "kind_display": {"check": "Check by hand", "lead": "Lead", "candidate": "Candidate"}[kind],
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
        "why": valuation_summary(evaluation) if kind == "candidate" else why_its_here(evaluation, kind),
        "why_label": "The valuation" if kind == "candidate" else "Why it's here",
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


def candidate_evaluations(now, scope, excluded_lot_ids, rs):
    """Live candidates for the "candidates" (ending within the dashboard's
    CANDIDATE_WINDOW_HOURS - same split as Act now's table) or "later"
    (ending after it) scope, soonest-ending first, under the review
    settings: min_headroom and hidden categories/sources always; the
    window only for "candidates" (Ending later is past it by definition)."""
    cutoff = now + timedelta(hours=settings.RESELLING_SCANNER["CANDIDATE_WINDOW_HOURS"])
    qs = (
        LotEvaluation.objects.select_related("lot")
        .filter(is_candidate=True, lot__end_time__gt=now)
        .exclude(lot_id__in=excluded_lot_ids)
        .order_by("lot__end_time")
    )
    qs = qs.filter(lot__end_time__lte=cutoff) if scope == "candidates" else qs.filter(lot__end_time__gt=cutoff)
    return [ev for ev in qs
            if passes_review_settings(ev, now, rs, apply_min_headroom=True, apply_window=scope == "candidates")]


def review_queue(now=None, scope=DEFAULT_SCOPE):
    """One scope's queue as a list of review_card() dicts, never a
    watchlisted lot, never a still-dismissed one (same reopen rule as the
    dashboard - exclude_dismissed_evaluations()). "review" is Check by
    hand (by end time) then Leads (by end time), never the same lot twice
    (a lot that's both goes in once, as Check by hand)."""
    now = now or timezone.now()
    rs = review_settings()
    excluded = _watched_lot_ids()

    if scope in ("candidates", "later"):
        candidates = exclude_dismissed_evaluations(candidate_evaluations(now, scope, excluded, rs))
        reviews = _latest_ai_reviews(ev.lot_id for ev in candidates)
        return [review_card(ev, "candidate", now, reviews.get(ev.lot_id)) for ev in candidates]

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


def all_queues(now=None):
    """Every scope's queue, so the page can switch scope without a reload."""
    now = now or timezone.now()
    return {key: review_queue(now, key) for key, _label in SCOPES}
