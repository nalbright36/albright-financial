"""Views for the Bid Calculator page: two tabs, Buy cost and eBay profit,
both backed entirely by scanner/simple_calc.py's pure functions. Logic
lives there (tested in tests/test_simple_calc.py) - everything in this
file is just pulling values out of a request (POST form or JSON body),
coercing them to the types simple_calc expects, and handing the result
back as a dict/JsonResponse.

Two ways to compute, same as the rest of this app's pages: calculator_page
itself (a plain POST, the JavaScript-off fallback - its "Calculate" button
re-renders the page with server-computed results) and calculate_api (the
JSON endpoint the page's JS calls, debounced, as values change). Both go
through _buy_result()/_ebay_result() below, so they can never disagree.

The old multi-item table, melt helper, and max-bid-from-profit-target
calculator (scanner/calculator.py) are no longer used by this page -
nothing here imports it. Saved calculations (BidCalculation) were dropped
too: they don't map cleanly onto two independent flat tabs, and the
feature wasn't worth re-building for this simpler page.
"""
import json
from dataclasses import asdict

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, render
from django.views.decorators.http import require_POST

from .models import LedgerEntry
from .scanner.simple_calc import EBAY_PRESETS, break_even_price, buy_cost, ebay_profit, max_bid_for_total, price_for_profit
from .scanner_models import SourcedLot

SOURCE_CHOICES = [
    ("shopgoodwill", "ShopGoodwill"),
    ("maxsold", "MaxSold"),
    ("hibid", "HiBid"),
    ("custom", "Custom"),
]
EBAY_CATEGORY_CHOICES = [(key, EBAY_PRESETS[key]["label"]) for key in ("most", "jewelry", "custom")]


def _num(value, default=0.0):
    """Request POST/JSON values arrive as strings, numbers, or None - this
    is the one place that tolerance lives, so every field below can just
    assume a plain float."""
    if value in (None, ""):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _checkbox(data, key, default):
    """True/False from either a JSON body (a real bool, always present)
    or a plain form POST (present with some truthy string only when
    checked - absent entirely when unchecked, per HTML's own checkbox
    behavior)."""
    if key not in data:
        return default
    value = data[key]
    if isinstance(value, bool):
        return value
    return str(value).lower() not in ("", "0", "false", "off", "no")


def source_defaults(source: str) -> dict:
    """Buy-side defaults for a source, straight from RESELLING_SCANNER -
    percent-scale (18 for 18%) and dollars, matching the Buy cost tab's
    own fields exactly. An unconfigured (or "custom") source is all
    zeros, so picking it just clears the fields back to blank/manual."""
    src = settings.RESELLING_SCANNER["SOURCES"].get(source, {})
    premium_pct = src.get("buyer_premium_pct", src.get("default_buyer_premium_pct", 0.0))
    tax_pct = src.get("sales_tax_pct", 0.0)
    shipping = src.get("default_inbound_shipping", 0.0)
    return {"premium_pct": round(premium_pct * 100, 2), "tax_pct": round(tax_pct * 100, 2), "shipping": shipping}


def all_source_defaults() -> dict:
    return {source: source_defaults(source) for source, _ in SOURCE_CHOICES}


def _buy_result(data) -> dict:
    bid = _num(data.get("bid"))
    premium_pct = _num(data.get("premium_pct"))
    tax_pct = _num(data.get("tax_pct"))
    shipping = _num(data.get("shipping"))
    other_fees = _num(data.get("other_fees"))
    tax_on_premium = _checkbox(data, "tax_on_premium", True)
    tax_on_shipping = _checkbox(data, "tax_on_shipping", False)

    result = asdict(buy_cost(
        bid, premium_pct=premium_pct, tax_pct=tax_pct, shipping=shipping, other_fees=other_fees,
        tax_on_premium=tax_on_premium, tax_on_shipping=tax_on_shipping,
    ))

    budget = data.get("budget")
    if budget not in (None, ""):
        result["max_bid_for_budget"] = max_bid_for_total(
            _num(budget), premium_pct=premium_pct, tax_pct=tax_pct, shipping=shipping, other_fees=other_fees,
            tax_on_premium=tax_on_premium, tax_on_shipping=tax_on_shipping,
        )
    else:
        result["max_bid_for_budget"] = None
    return result


def _ebay_tiers(data):
    category = data.get("category") or "most"
    if category == "custom":
        default_rate = EBAY_PRESETS["custom"]["tiers"][0][1] * 100
        rate = _num(data.get("custom_rate_pct"), default_rate)
        return category, [(None, rate / 100)]
    preset = EBAY_PRESETS.get(category, EBAY_PRESETS["most"])
    return category, preset["tiers"]


def _ebay_result(data) -> dict:
    item_cost = _num(data.get("item_cost"))
    category, tiers = _ebay_tiers(data)
    kwargs = dict(
        shipping_charged=_num(data.get("shipping_charged")),
        shipping_cost=_num(data.get("shipping_cost")),
        packaging=_num(data.get("packaging")),
        other_costs=_num(data.get("other_costs")),
        tiers=tiers,
        buyer_tax_pct=_num(data.get("buyer_tax_pct"), 7.0),
        promoted_pct=_num(data.get("promoted_pct")),
    )
    sale_price = _num(data.get("sale_price"))

    result = asdict(ebay_profit(item_cost, sale_price, **kwargs))
    result["category"] = category

    target_profit = _num(data.get("target_profit"), 20.0)
    result["target_profit"] = target_profit
    try:
        result["break_even_price"] = break_even_price(item_cost, **kwargs)
        result["price_for_profit"] = price_for_profit(target_profit, item_cost, **kwargs)
    except ValueError:
        result["break_even_price"] = None
        result["price_for_profit"] = None
    return result


def _lot_prefill(lot) -> dict:
    """Buy cost tab prefill for the "Open in Calculator" button: the lot's
    current bid, plus that source's own defaults (same values the Source
    dropdown would fill in if picked by hand)."""
    source = lot.source if lot.source in dict(SOURCE_CHOICES) else "custom"
    prefill = {"bid": float(lot.current_price), "source": source}
    prefill.update(source_defaults(source))
    return prefill


def _ledger_pick_items(user):
    """Holding/partially-sold entries for the eBay tab's "Pick from
    Ledger" dropdown - their buy-side total (what the item actually cost,
    all in) is what should become "Item cost" on this tab."""
    entries = LedgerEntry.objects.filter(owner=user, status__in=("holding", "partially_sold"))
    return [{"id": e.pk, "label": e.item, "cost": float(e.buy_side_cost)} for e in entries]


@login_required
def calculator_page(request):
    buy_results = None
    ebay_results = None
    active_tab = "buy"

    if request.method == "POST":
        active_tab = request.POST.get("tab") or "buy"
        if active_tab == "ebay":
            ebay_results = _ebay_result(request.POST)
        else:
            active_tab = "buy"
            buy_results = _buy_result(request.POST)

    lot_prefill = {}
    lot_id = request.GET.get("lot")
    if request.method == "GET" and lot_id:
        lot = get_object_or_404(SourcedLot, pk=lot_id)
        lot_prefill = _lot_prefill(lot)

    return render(request, "calculator.html", {
        "source_choices": SOURCE_CHOICES,
        "ebay_category_choices": EBAY_CATEGORY_CHOICES,
        "source_defaults_json": json.dumps(all_source_defaults()),
        "ledger_items_json": json.dumps(_ledger_pick_items(request.user)),
        "lot_prefill_json": json.dumps(lot_prefill),
        "active_tab": active_tab,
        "buy_results": buy_results,
        "ebay_results": ebay_results,
        "buy_values": request.POST if (request.method == "POST" and active_tab == "buy") else None,
        "ebay_values": request.POST if (request.method == "POST" and active_tab == "ebay") else None,
    })


@login_required
@require_POST
def calculate_api(request):
    try:
        data = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON."}, status=400)

    if data.get("tab") == "ebay":
        return JsonResponse(_ebay_result(data))
    return JsonResponse(_buy_result(data))
