"""Routes a lot to the right valuer based on what the listing actually is.

Order: coins -> jewelry -> games/cards leads. The search keyword only decides
what we FIND; this decides what the lot IS, so a gold coin pendant found by a
"silver coins" search is still valued as jewelry.
"""
from dataclasses import dataclass

from .coins import ParseResult, parse_coin_text
from .jewelry import parse_jewelry_text
from .leads import LeadResult, evaluate_lead


@dataclass
class Classification:
    category: str                      # coins / jewelry / games / cards / none
    parse: ParseResult | None = None   # coins & jewelry: metal-based valuation
    lead: LeadResult | None = None     # games & cards
    reason: str = ""                   # why it was excluded (category "none")


def classify(title: str, description: str, current_price: float, lead_limits: dict) -> Classification:
    coin = parse_coin_text(title, description)
    if coin.items or coin.needs_llm:
        return Classification("coins", parse=coin)

    jewelry = parse_jewelry_text(title, description)
    if jewelry.items or jewelry.needs_review:
        return Classification("jewelry", parse=jewelry)

    lead = evaluate_lead(title, description, current_price, lead_limits)
    if lead.category:
        return Classification(lead.category, lead=lead)

    # Nothing fits: keep the most specific reason for the admin/debug view
    reason = next(r for r in (coin.excluded_reason, jewelry.excluded_reason, lead.excluded_reason, "unrecognized") if r)
    if jewelry.excluded_reason.startswith("plated"):
        reason = jewelry.excluded_reason
    return Classification("none", reason=reason)