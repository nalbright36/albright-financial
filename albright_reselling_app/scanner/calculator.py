"""Logic behind the Bid Calculator page (calculator_views.py). The actual
math is still scanner/max_bid.py's SellFees/BuyCosts/all_in_cost/max_bid/
projected_profit - every result below is computed by calling those
functions directly. What's extended here (not in max_bid.py itself) is
how this calculator's extra inputs - a card/processing fee %, a flat
per-lot fee, a tax-on-premium toggle, pickup miles, multiple resale items,
and "other" sell-side costs - get folded into the BuyCosts/SellFees those
functions already accept, via as_buy_costs()/as_sell_fees() below. Pure
Python plus two small read-only lookups (today's spot price, calibration
overrides) - no Django forms/HTTP here, so it's directly unit-testable.
"""
from dataclasses import dataclass, field, replace

from django.conf import settings

from .coins import GRAMS_PER_TROY_OZ
from .insights import calibrated_category_fees, get_calibrated
from .jewelry import GOLD_KARATS, GRAMS_PER_DWT
from .max_bid import BuyCosts, SellFees, all_in_cost
from .max_bid import max_bid as compute_max_bid
from .max_bid import net_from_sale, projected_profit
from .max_bid import target_profit as mb_target_profit
from .spot import get_all_spot

WEIGHT_UNITS = ("g", "dwt", "oz")


def _num(value, default=0.0):
    """request.POST/JSON values arrive as strings (or None) - this is the
    one place that tolerance lives, so every field below can just assume
    a plain float."""
    if value in (None, ""):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# Buy side
# ---------------------------------------------------------------------------

@dataclass
class BuySideInputs:
    bid: float = 0.0
    premium_pct: float = 0.0          # e.g. 0.18 for 18%
    tax_pct: float = 0.0
    tax_on_premium: bool = True       # matches max_bid.all_in_cost's own (hardcoded) assumption
    card_fee_pct: float = 0.0
    per_lot_fee: float = 0.0
    inbound_mode: str = "shipping"    # "shipping" or "pickup"
    shipping: float = 0.0
    pickup_miles: float = 0.0
    pickup_rate_per_mile: float = 0.0
    pickup_fixed_cost: float = 0.0

    @property
    def inbound(self):
        if self.inbound_mode == "pickup":
            return 2 * self.pickup_miles * self.pickup_rate_per_mile + self.pickup_fixed_cost
        return self.shipping

    @property
    def _cost_multiplier(self):
        """The constant K such that all_in_cost-style total cost = hammer*K
        + flat extras - hammer, premium, tax, and the card fee are all
        proportional to the hammer, so this is linear regardless of the
        tax_on_premium toggle."""
        if self.tax_on_premium:
            base = (1 + self.premium_pct) * (1 + self.tax_pct)
        else:
            base = 1 + self.premium_pct + self.tax_pct
        return base * (1 + self.card_fee_pct)

    def as_buy_costs(self) -> BuyCosts:
        """Folds every extended input into a plain max_bid.BuyCosts that,
        fed into all_in_cost()/max_bid(), reproduces this calculator's
        full math exactly: all_in_cost only ever uses the *product*
        (1+premium)*(1+tax), so folding everything into "premium" and
        zeroing "tax" is lossless; per_lot_fee folds into "inbound" the
        same way (both flat, bid-independent add-ons). When card_fee_pct
        and per_lot_fee are both 0 and tax_on_premium is True (max_bid.py's
        own built-in assumption), this reduces to exactly
        BuyCosts(premium_pct, tax_pct, inbound) - see
        tests/test_calculator.py's exact-match cases."""
        return BuyCosts(self._cost_multiplier - 1, 0.0, self.per_lot_fee + self.inbound)

    def breakdown(self, bid=None):
        """Line-by-line buy-side breakdown for the given bid (the entered
        one by default) - purely presentational."""
        bid = self.bid if bid is None else bid
        premium_amount = bid * self.premium_pct
        tax_base = bid + premium_amount if self.tax_on_premium else bid
        tax_amount = tax_base * self.tax_pct
        subtotal = bid + premium_amount + tax_amount
        card_fee_amount = subtotal * self.card_fee_pct
        return {
            "bid": round(bid, 2),
            "buyer_premium": round(premium_amount, 2),
            "sales_tax": round(tax_amount, 2),
            "card_fee": round(card_fee_amount, 2),
            "per_lot_fee": round(self.per_lot_fee, 2),
            "inbound": round(self.inbound, 2),
            "total": round(all_in_cost(bid, self.as_buy_costs()), 2),
        }


# ---------------------------------------------------------------------------
# Resale items + sell side
# ---------------------------------------------------------------------------

@dataclass
class LineItem:
    name: str = ""
    quantity: float = 1.0
    price_each: float = 0.0

    @property
    def subtotal(self):
        return self.quantity * self.price_each


@dataclass
class SellSideInputs:
    items: list = field(default_factory=list)   # list[LineItem]
    platform_fee_pct: float = 0.0
    fixed_fee: float = 0.0
    outbound_shipping: float = 0.0
    packaging: float = 0.0
    other_costs: float = 0.0
    target_profit_dollars: float = 0.0
    target_profit_pct: float = 0.0    # e.g. 0.20 for 20% of sale

    @property
    def expected_sale(self):
        return sum(item.subtotal for item in self.items)

    def as_sell_fees(self) -> SellFees:
        """Folds other_costs into "outbound_shipping" (both flat
        subtractions from the sale price, exactly as net_from_sale sums
        them) - lossless for net_from_sale()/target_profit()/
        projected_profit(). When other_costs is 0, this is exactly
        SellFees(platform_fee_pct, fixed_fee, outbound_shipping,
        packaging, target_profit_dollars, target_profit_pct)."""
        return SellFees(
            ebay_fee_pct=self.platform_fee_pct, ebay_fixed_fee=self.fixed_fee,
            outbound_shipping=self.outbound_shipping + self.other_costs, packaging=self.packaging,
            min_profit=self.target_profit_dollars, min_profit_pct=self.target_profit_pct,
        )


# ---------------------------------------------------------------------------
# Putting it together - every result comes from max_bid.py's own functions,
# fed the adapted BuyCosts/SellFees above.
# ---------------------------------------------------------------------------

def calculate(buy: BuySideInputs, sell: SellSideInputs) -> dict:
    expected_sale = sell.expected_sale
    sell_fees = sell.as_sell_fees()
    buy_costs = buy.as_buy_costs()

    net_proceeds = net_from_sale(expected_sale, sell_fees)
    target = mb_target_profit(expected_sale, sell_fees)
    max_bid_value = compute_max_bid(expected_sale, sell_fees, buy_costs)
    # Break-even bid: the same solve, with the required profit zeroed out.
    break_even_bid = compute_max_bid(expected_sale, replace(sell_fees, min_profit=0.0, min_profit_pct=0.0), buy_costs)

    entered_total_cost = all_in_cost(buy.bid, buy_costs)
    profit = projected_profit(buy.bid, expected_sale, sell_fees, buy_costs)
    roi_pct = (profit / entered_total_cost * 100) if entered_total_cost else None
    margin_pct = (profit / expected_sale * 100) if expected_sale else None

    return {
        "buy_breakdown": buy.breakdown(),
        "expected_sale": round(expected_sale, 2),
        "net_proceeds": round(net_proceeds, 2),
        "target_profit": round(target, 2),
        "max_bid": max_bid_value,
        "break_even_bid": break_even_bid,
        "entered_bid": round(buy.bid, 2),
        "total_cost": round(entered_total_cost, 2),
        "profit": round(profit, 2),
        "roi_pct": round(roi_pct, 2) if roi_pct is not None else None,
        "margin_pct": round(margin_pct, 2) if margin_pct is not None else None,
        "over_max": buy.bid > max_bid_value,
    }


def buy_inputs_from_dict(data: dict) -> BuySideInputs:
    return BuySideInputs(
        bid=_num(data.get("bid")),
        premium_pct=_num(data.get("premium_pct")),
        tax_pct=_num(data.get("tax_pct")),
        tax_on_premium=bool(data.get("tax_on_premium", True)),
        card_fee_pct=_num(data.get("card_fee_pct")),
        per_lot_fee=_num(data.get("per_lot_fee")),
        inbound_mode=data.get("inbound_mode") or "shipping",
        shipping=_num(data.get("shipping")),
        pickup_miles=_num(data.get("pickup_miles")),
        pickup_rate_per_mile=_num(data.get("pickup_rate_per_mile")),
        pickup_fixed_cost=_num(data.get("pickup_fixed_cost")),
    )


def sell_inputs_from_dict(data: dict) -> SellSideInputs:
    items = [
        LineItem(name=i.get("name", ""), quantity=_num(i.get("quantity"), 1.0), price_each=_num(i.get("price_each")))
        for i in (data.get("items") or [])
    ]
    return SellSideInputs(
        items=items,
        platform_fee_pct=_num(data.get("platform_fee_pct")),
        fixed_fee=_num(data.get("fixed_fee")),
        outbound_shipping=_num(data.get("outbound_shipping")),
        packaging=_num(data.get("packaging")),
        other_costs=_num(data.get("other_costs")),
        target_profit_dollars=_num(data.get("target_profit_dollars")),
        target_profit_pct=_num(data.get("target_profit_pct")),
    )


def calculate_from_dict(data: dict) -> dict:
    """data holds every field flat (bid, premium_pct, ..., platform_fee_pct,
    ..., items: [...]) - the shape the calculator page's JS posts."""
    return calculate(buy_inputs_from_dict(data), sell_inputs_from_dict(data))


# ---------------------------------------------------------------------------
# Source/channel presets (settings.py + calibration overrides)
# ---------------------------------------------------------------------------

SOURCE_CHOICES = ("shopgoodwill", "maxsold", "hibid")
CHANNEL_CHOICES = ("ebay", "scrap_gold", "local_facebook", "custom")


def source_preset(source: str) -> dict:
    """Buy-side defaults for a source, overrides-first - same values
    scanner/pipeline.py would actually use for a lot from this source."""
    cfg = settings.RESELLING_SCANNER
    src = cfg["SOURCES"].get(source, {})
    if not src:
        return {}
    default_premium = src.get("buyer_premium_pct", src.get("default_buyer_premium_pct", 0.0))
    premium_pct = get_calibrated(f"SOURCES.{source}.buyer_premium_pct", default_premium)
    tax_pct = get_calibrated(f"SOURCES.{source}.sales_tax_pct", src.get("sales_tax_pct", 0.0))
    preset = {"premium_pct": premium_pct, "tax_pct": tax_pct}

    if src.get("mileage_rate"):
        preset.update({
            "inbound_mode": "pickup", "pickup_rate_per_mile": src["mileage_rate"],
            "pickup_fixed_cost": src.get("pickup_fixed_cost", 0.0),
        })
    else:
        preset.update({"inbound_mode": "shipping", "shipping": src.get("default_inbound_shipping", 0.0)})
    return preset


def channel_preset(channel: str, category: str = "") -> dict:
    """Sell-side defaults for a resale channel. "ebay"/"custom" use the
    global FEES; "scrap_gold"/"local_facebook" assume no platform/listing
    fees at all (a buyer paying cash, not a marketplace) - CATEGORY_FEES's
    jewelry override (drops eBay/shipping fees - scrap sells differently)
    is folded in automatically when category="jewelry", same as the
    pipeline's own valuation would."""
    cfg = settings.RESELLING_SCANNER
    if channel in ("scrap_gold", "local_facebook"):
        return {"platform_fee_pct": 0.0, "fixed_fee": 0.0, "outbound_shipping": 0.0, "packaging": 0.0}

    base_fees = {**cfg["FEES"], **cfg["CATEGORY_FEES"].get(category, {})}
    fees = calibrated_category_fees(category, base_fees) if category else base_fees
    return {
        "platform_fee_pct": fees["ebay_fee_pct"], "fixed_fee": fees["ebay_fixed_fee"],
        "outbound_shipping": fees["outbound_shipping"], "packaging": fees["packaging"],
        "target_profit_dollars": fees["min_profit"], "target_profit_pct": fees["min_profit_pct"],
    }


# ---------------------------------------------------------------------------
# Melt helper
# ---------------------------------------------------------------------------

def _purity_fraction(metal: str, purity_input) -> float:
    """purity_input is a gold karat (8/9/10/14/18/22/24) when metal is
    "gold" and it matches a known karat; otherwise it's taken as a literal
    decimal purity (0.925 sterling, 0.999 fine silver, or any custom gold
    fraction)."""
    if metal == "gold":
        try:
            karat = int(purity_input)
        except (TypeError, ValueError):
            karat = None
        if karat in GOLD_KARATS:
            return GOLD_KARATS[karat]
    return _num(purity_input)


def _grams(weight: float, unit: str) -> float:
    if unit == "dwt":
        return weight * GRAMS_PER_DWT
    if unit == "oz":
        return weight * GRAMS_PER_TROY_OZ
    return weight  # "g"


def melt_value(metal: str, purity_input, weight: float, unit: str, payout_pct: float, spot_price=None) -> dict:
    """$ value of a given weight/purity of metal at today's spot (or a
    supplied override, mainly for tests), after a payout % (scrap buyers
    rarely pay full spot)."""
    if spot_price is None:
        try:
            spot_price = get_all_spot().get(metal)
        except RuntimeError:
            # fetch_spot_prices has never run (or this metal has no row
            # yet) - get_all_spot() raises rather than returning a
            # missing key, same as the dashboard's own spot-price card
            # has to tolerate (scanner/dashboard.py's _spot_price_status).
            spot_price = None
    spot_price = float(spot_price) if spot_price is not None else None
    purity = _purity_fraction(metal, purity_input)
    grams = _grams(_num(weight), unit)
    troy_oz = grams / GRAMS_PER_TROY_OZ

    if spot_price is None:
        return {"value": None, "troy_oz": round(troy_oz, 4), "purity": purity, "spot_price": None}

    value = troy_oz * purity * spot_price * _num(payout_pct, 1.0)
    return {"value": round(value, 2), "troy_oz": round(troy_oz, 4), "purity": purity, "spot_price": spot_price}


# ---------------------------------------------------------------------------
# "Open in calculator" prefill
# ---------------------------------------------------------------------------

def prefill_from_lot(lot) -> dict:
    """Buy-side costs (and a starting bid) for the "Open in calculator"
    button - the same per-lot/source costs scanner/pipeline.py would
    price this lot with."""
    from .pipeline import _buyer_premium_pct, _inbound_shipping, _sales_tax_pct

    cfg = settings.RESELLING_SCANNER
    src = cfg["SOURCES"].get(lot.source, {})
    data = {"bid": float(lot.current_price), "source": lot.source}
    if not src:
        return data

    data["premium_pct"] = _buyer_premium_pct(lot.raw, src, lot.source)
    data["tax_pct"] = _sales_tax_pct(src, lot.source)
    if src.get("mileage_rate") and (lot.raw or {}).get("_pickup"):
        pickup = lot.raw["_pickup"]
        data.update({
            "inbound_mode": "pickup", "pickup_miles": pickup.get("distance_miles") or 0.0,
            "pickup_rate_per_mile": src["mileage_rate"], "pickup_fixed_cost": src.get("pickup_fixed_cost", 0.0),
        })
    else:
        data.update({"inbound_mode": "shipping", "shipping": _inbound_shipping(lot.raw, src)})
    return data


def prefill_expected_sale(evaluation=None, review=None) -> float:
    """Best available expected resale for a lot: the AI review's resale
    range midpoint if one was given, else the scanner's own
    expected_sale."""
    if review is not None and review.resale_low is not None:
        high = review.resale_high if review.resale_high is not None else review.resale_low
        return float((review.resale_low + high) / 2)
    if evaluation is not None and evaluation.expected_sale:
        return float(evaluation.expected_sale)
    return 0.0
