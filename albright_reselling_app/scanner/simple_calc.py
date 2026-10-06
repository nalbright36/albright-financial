"""Bid Calculator math. Pure Python, no Django. Two everyday questions:

1. buy_cost():    what does this lot cost me out the door (bid + premium + tax + shipping)?
2. ebay_profit(): if I list an item I already own on eBay at price X, what do I make?
   plus break_even_price() and price_for_profit() to solve for the listing price.

eBay's final value fee is charged on the TOTAL the buyer pays: item price + shipping
charged + sales tax, in tiers by category, plus a per-order fee ($0.30 for orders of
$10 or less, $0.40 above). Verify rates on eBay's fee page; presets are editable.
"""
from dataclasses import dataclass, field

# (up_to_amount or None for "everything above", rate)
EBAY_PRESETS = {
    "most": {"label": "Most categories", "tiers": [(7500, 0.136), (None, 0.0235)]},
    "jewelry": {"label": "Jewelry & watches", "tiers": [(1000, 0.15), (7500, 0.065), (None, 0.03)]},
    "custom": {"label": "Custom", "tiers": [(None, 0.136)]},
}


def _r(x: float) -> float:
    return round(x + 1e-9, 2)


# ---------------------------------------------------------------- buy side
@dataclass
class BuyCost:
    bid: float
    premium: float
    tax: float
    shipping: float
    other_fees: float
    total: float


def buy_cost(bid: float, premium_pct: float = 0.0, tax_pct: float = 0.0, shipping: float = 0.0,
             other_fees: float = 0.0, tax_on_premium: bool = True, tax_on_shipping: bool = False) -> BuyCost:
    """Percentages as numbers like 18 for 18%."""
    premium = bid * premium_pct / 100
    taxable = bid + (premium if tax_on_premium else 0) + (shipping if tax_on_shipping else 0)
    tax = taxable * tax_pct / 100
    total = bid + premium + tax + shipping + other_fees
    return BuyCost(_r(bid), _r(premium), _r(tax), _r(shipping), _r(other_fees), _r(total))


def max_bid_for_total(budget: float, premium_pct: float = 0.0, tax_pct: float = 0.0, shipping: float = 0.0,
                      other_fees: float = 0.0, tax_on_premium: bool = True, tax_on_shipping: bool = False) -> float:
    """Highest bid that keeps the out-the-door total at or under budget."""
    p, t = premium_pct / 100, tax_pct / 100
    fixed = shipping + other_fees + (shipping * t if tax_on_shipping else 0)
    per_dollar = 1 + p + t * (1 + (p if tax_on_premium else 0))
    return max(0.0, _r((budget - fixed) / per_dollar - 0.005))  # round down to stay under budget


# ---------------------------------------------------------------- eBay side
def tiered_fee(amount: float, tiers: list) -> float:
    fee, lower = 0.0, 0.0
    for upper, rate in tiers:
        top = amount if upper is None else min(amount, upper)
        if top > lower:
            fee += (top - lower) * rate
        if upper is None or amount <= upper:
            break
        lower = upper
    return fee


@dataclass
class EbayResult:
    sale_price: float
    shipping_charged: float
    buyer_tax: float
    fee_base: float
    final_value_fee: float
    per_order_fee: float
    promoted_fee: float
    total_ebay_fees: float
    payout: float                 # what eBay pays you
    shipping_cost: float
    packaging: float
    other_costs: float
    item_cost: float
    profit: float
    margin_pct: float | None      # profit / sale price
    roi_pct: float | None         # profit / item cost
    notes: list = field(default_factory=list)


def ebay_profit(item_cost: float, sale_price: float, shipping_charged: float = 0.0, shipping_cost: float = 0.0,
                packaging: float = 0.0, other_costs: float = 0.0, tiers: list | None = None,
                buyer_tax_pct: float = 7.0, promoted_pct: float = 0.0) -> EbayResult:
    tiers = tiers or EBAY_PRESETS["most"]["tiers"]
    order_total = sale_price + shipping_charged
    buyer_tax = order_total * buyer_tax_pct / 100          # eBay collects it, but charges its fee on it
    fee_base = order_total + buyer_tax
    fvf = tiered_fee(fee_base, tiers)
    per_order = 0.30 if fee_base <= 10 else 0.40
    promoted = fee_base * promoted_pct / 100 if promoted_pct else 0.0
    fees = fvf + per_order + promoted
    payout = order_total - fees
    profit = payout - shipping_cost - packaging - other_costs - item_cost
    res = EbayResult(
        _r(sale_price), _r(shipping_charged), _r(buyer_tax), _r(fee_base), _r(fvf), _r(per_order), _r(promoted),
        _r(fees), _r(payout), _r(shipping_cost), _r(packaging), _r(other_costs), _r(item_cost), _r(profit),
        round(profit / sale_price * 100, 1) if sale_price else None,
        round(profit / item_cost * 100, 1) if item_cost else None,
    )
    if buyer_tax_pct:
        res.notes.append(f"eBay's fee includes the buyer's sales tax (estimated at {buyer_tax_pct:g}%).")
    return res


def price_for_profit(target_profit: float, item_cost: float, **kwargs) -> float:
    """Lowest listing price (to the cent) that reaches target_profit. kwargs as ebay_profit()."""
    lo, hi = 0.0, max(100.0, (item_cost + target_profit) * 4 + 100)
    while ebay_profit(item_cost, hi, **kwargs).profit < target_profit:
        hi *= 2
        if hi > 10_000_000:
            raise ValueError("Target profit not reachable")
    for _ in range(60):
        mid = (lo + hi) / 2
        if ebay_profit(item_cost, mid, **kwargs).profit >= target_profit:
            hi = mid
        else:
            lo = mid
    price = round(hi + 0.004, 2)
    while ebay_profit(item_cost, price, **kwargs).profit < target_profit:   # guard rounding at fee steps
        price = round(price + 0.01, 2)
    return price


def break_even_price(item_cost: float, **kwargs) -> float:
    return price_for_profit(0.0, item_cost, **kwargs)