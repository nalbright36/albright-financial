"""Opportunity scoring. Pure Python, deterministic. AI extracts the facts; this does the math.

Four separate scores, never merged into one: expected value per hour, confidence, risk
(from safety.py), and automation potential.
"""
from dataclasses import dataclass

# Default payout probabilities (low, mid, high) by reward type, used when the opportunity
# doesn't give firmer evidence. Speculative types get wide ranges on purpose.
DEFAULT_PROBABILITY = {
    "bounty":            (0.30, 0.55, 0.80),   # stated reward, but work can be rejected
    "promo_reward":      (0.60, 0.80, 0.95),   # explicit promotional reward from an official program
    "microtask":         (0.50, 0.75, 0.90),
    "testnet":           (0.00, 0.03, 0.15),   # test tokens are worthless; any airdrop is speculative
    "speculative_airdrop": (0.00, 0.05, 0.20),
    "points":            (0.00, 0.05, 0.25),   # points have no established value
    "unknown":           (0.00, 0.10, 0.30),
    # Income opportunities
    "freelance":         (0.10, 0.25, 0.50),   # chance a proposal wins the job
    "research_study":    (0.20, 0.40, 0.60),   # chance you qualify through the screener
    "competition":       (0.00, 0.03, 0.10),   # prize competitions rarely pay
    "bonus":             (0.70, 0.85, 0.95),   # sign-up bonus when you meet the stated terms
    "settlement":        (0.50, 0.80, 0.95),   # payout for a valid claim (amount often reduced)
}
CONFIRMED_TYPES = {"bounty", "promo_reward", "microtask",   # types where a stated amount can count as real
                   "freelance", "research_study", "bonus", "settlement"}


@dataclass
class Score:
    excluded: bool
    exclude_reason: str
    ev_low: float
    ev_mid: float
    ev_high: float
    ev_per_hour_mid: float | None
    confidence: str          # high / medium / low
    automation: str          # high / medium / low / none
    reward_counts_as_confirmed: bool


def score(reward_usd: float | None, reward_type: str, hours: float | None, costs_usd: float = 0.0,
          requires_spending: bool = False, evidence: str = "none", official_source: bool = False,
          automatable_steps: int = 0, total_steps: int = 0, budget_usd: float = 0.0,
          probability_override: float | None = None) -> Score:
    """evidence: 'explicit' (amount + terms stated by the official source), 'stated' (amount stated
    elsewhere), 'none'. Costs and spending are excluded entirely when they exceed the budget."""
    rtype = reward_type if reward_type in DEFAULT_PROBABILITY else "unknown"
    if requires_spending or costs_usd > budget_usd:
        return Score(True, "requires spending above your budget", 0, 0, 0, None, "low", "none", False)

    lo, mid, hi = DEFAULT_PROBABILITY[rtype]
    if probability_override is not None:
        mid = max(0.0, min(1.0, probability_override))
        lo, hi = min(lo, mid), max(hi, mid)
    if evidence == "explicit" and official_source and rtype in CONFIRMED_TYPES:
        lo, mid, hi = min(lo + 0.15, 1), min(mid + 0.1, 1), hi
    elif evidence == "none":
        lo, mid, hi = 0.0, mid / 2, hi

    r = float(reward_usd or 0)
    ev = [round(p * r - costs_usd, 2) for p in (lo, mid, hi)]
    per_hour = round(ev[1] / hours, 2) if hours and hours > 0 else None

    if rtype in CONFIRMED_TYPES and evidence == "explicit" and official_source and reward_usd:
        confidence = "high"
    elif evidence in ("explicit", "stated") and reward_usd:
        confidence = "medium"
    else:
        confidence = "low"

    if total_steps <= 0:
        automation = "none"
    else:
        share = automatable_steps / total_steps
        automation = "high" if share >= 0.7 else "medium" if share >= 0.3 else "low" if share > 0 else "none"

    confirmed = rtype in CONFIRMED_TYPES and bool(reward_usd) and evidence != "none"
    return Score(False, "", ev[0], ev[1], ev[2], per_hour, confidence, automation, confirmed)