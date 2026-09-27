"""Max bid math. Pure Python (no Django) so it can be unit tested on its own.

Works backward from the expected eBay sale to the highest hammer price
that still leaves your target profit after every fee.
"""
from dataclasses import dataclass


@dataclass
class SellFees:
    ebay_fee_pct: float = 0.1325    # final value fee rate - check your category's rate
    ebay_fixed_fee: float = 0.40    # per-order fee
    outbound_shipping: float = 5.00  # what YOU pay to ship to the buyer (free-shipping listing)
    packaging: float = 0.50
    min_profit: float = 10.00       # never target less than this in dollars...
    min_profit_pct: float = 0.20    # ...or less than this share of the expected sale


@dataclass
class BuyCosts:
    buyer_premium_pct: float = 0.0
    sales_tax_pct: float = 0.0
    inbound_shipping: float = 0.0   # what the auction site charges to ship to you


def target_profit(expected_sale: float, fees: SellFees) -> float:
    return max(fees.min_profit, expected_sale * fees.min_profit_pct)


def net_from_sale(expected_sale: float, fees: SellFees) -> float:
    """Cash left after eBay fees, shipping out, and packaging."""
    return (expected_sale * (1 - fees.ebay_fee_pct)
            - fees.ebay_fixed_fee - fees.outbound_shipping - fees.packaging)


def all_in_cost(hammer: float, buy: BuyCosts) -> float:
    """Total paid for a lot: premium on the hammer, tax on hammer + premium, then shipping."""
    return hammer * (1 + buy.buyer_premium_pct) * (1 + buy.sales_tax_pct) + buy.inbound_shipping


def max_bid(expected_sale: float, fees: SellFees, buy: BuyCosts) -> float:
    budget = net_from_sale(expected_sale, fees) - target_profit(expected_sale, fees) - buy.inbound_shipping
    hammer = budget / ((1 + buy.buyer_premium_pct) * (1 + buy.sales_tax_pct))
    return max(0.0, round(hammer, 2))


def projected_profit(hammer: float, expected_sale: float, fees: SellFees, buy: BuyCosts) -> float:
    return round(net_from_sale(expected_sale, fees) - all_in_cost(hammer, buy), 2)
