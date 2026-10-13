"""Views for the Calculators page: three tabs, Buy cost, eBay profit, and
Melt value, all backed entirely by scanner/simple_calc.py's pure
functions. Logic lives there (tested in tests/test_simple_calc.py) -
everything in this file is just pulling values out of a request (POST
form or JSON body), coercing them to the types simple_calc expects, and
handing the result back as a dict/JsonResponse.

Two ways to compute, same as the rest of this app's pages: calculator_page
itself (a plain POST, the JavaScript-off fallback - its "Calculate" button
re-renders the page with server-computed results) and calculate_api (the
JSON endpoint the page's JS calls, debounced, as values change). Both go
through _buy_result()/_ebay_result()/_melt_result() below, so they can
never disagree.

The old multi-item table, melt helper, and max-bid-from-profit-target
calculator (scanner/calculator.py) are no longer used by this page -
nothing here imports it. Saved calculations (BidCalculation) were dropped
too: they don't map cleanly onto independent flat tabs, and the feature
wasn't worth re-building for this simpler page.
"""
import json
from dataclasses import asdict

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.utils import dateformat, timezone
from django.views.decorators.http import require_POST

from .models import LedgerEntry
from .scanner.simple_calc import (
    EBAY_PRESETS, GRAMS_PER, PURITY_PRESETS, break_even_price, buy_cost, ebay_profit, junk_silver_value,
    max_bid_for_total, melt_value, price_for_profit,
)
from .scanner_models import SourcedLot, SpotPrice

SOURCE_CHOICES = [
    ("shopgoodwill", "ShopGoodwill"),
    ("maxsold", "MaxSold"),
    ("hibid", "HiBid"),
    ("custom", "Custom"),
]
EBAY_CATEGORY_CHOICES = [(key, EBAY_PRESETS[key]["label"]) for key in ("most", "jewelry", "custom")]
METAL_CHOICES = [("gold", "Gold"), ("silver", "Silver")]
UNIT_CHOICES = [
    ("g", "Grams"), ("dwt", "Pennyweight (dwt)"), ("ozt", "Troy ounces (ozt)"), ("oz", "Ounces (kitchen scale)"),
]


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


def purity_choices_json() -> dict:
    """{"gold": [{"value": "0.583", "label": "14k (0.583)"}, ...], "silver": [...]}
    - the melt tab's purity <select> re-populates from this (plus a
    trailing "Custom" option, always) when the Metal toggle changes, same
    pattern as the Buy cost tab's Source presets."""
    return {
        metal: [{"value": str(fraction), "label": f"{label} ({fraction})"} for label, fraction in presets.items()]
        for metal, presets in PURITY_PRESETS.items()
    }


def _latest_spot(metal: str) -> dict:
    """The latest stored SpotPrice for this metal, regardless of age (the
    melt tab just prefills it and shows its date - unlike
    scanner/spot.get_spot(), it never raises on a stale/missing price,
    since the field stays editable either way)."""
    latest = SpotPrice.objects.filter(metal=metal).order_by("-fetched_at").first()
    if latest is None:
        return {"price": None, "date": None}
    return {"price": float(latest.price_usd), "date": dateformat.format(timezone.localtime(latest.fetched_at), "M j, Y")}


def all_latest_spot() -> dict:
    return {metal: _latest_spot(metal) for metal, _ in METAL_CHOICES}


def _melt_result(data) -> dict:
    mode = data.get("mode") or "weight"
    metal = data.get("metal") or "gold"
    payout_pct = _num(data.get("payout_pct"), 100.0)
    spot = _num(data.get("spot"))

    if mode == "junk":
        face_value = _num(data.get("face_value"))
        result = asdict(junk_silver_value(face_value, spot, payout_pct=payout_pct))
    else:
        weight = _num(data.get("weight"))
        unit = data.get("unit") or "g"
        if unit not in GRAMS_PER:
            unit = "g"
        purity_choice = data.get("purity") or "custom"
        purity = _num(data.get("custom_purity")) if purity_choice == "custom" else _num(purity_choice)
        result = asdict(melt_value(weight, unit, purity, spot, payout_pct=payout_pct))

    result["mode"] = mode
    result["metal"] = metal
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
    all in) is what should become "Item cost" on this tab. Personal
    items are included too (unlike every reselling calculation, which
    excludes them via ledger_metrics.exclude_personal) - they were never
    bought for resale, so their cost is always $0, and the label says so."""
    entries = LedgerEntry.objects.filter(owner=user, status__in=("holding", "partially_sold"))
    items = []
    for e in entries:
        if e.ledger_type == "personal":
            items.append({"id": e.pk, "label": f"{e.item} (personal)", "cost": 0.0})
        else:
            items.append({"id": e.pk, "label": e.item, "cost": float(e.buy_side_cost)})
    return items


def shared_calculator_context(user, melt_metal="gold"):
    """Everything _calculator_tabs.html needs that doesn't depend on the
    current request's method/results - shared verbatim by calculator_page()
    (the full Calculators page) and panel_context() (the dashboard/Today
    pages' slide-in panel), so the two can never render different choices/
    defaults for the same underlying data."""
    return {
        "source_choices": SOURCE_CHOICES,
        "ebay_category_choices": EBAY_CATEGORY_CHOICES,
        "metal_choices": METAL_CHOICES,
        "unit_choices": UNIT_CHOICES,
        "purity_choices": PURITY_PRESETS[melt_metal].items(),
        "melt_metal": melt_metal,
        "calc_data_json": json.dumps({
            "calculateUrl": reverse("albright_reselling_app:calculator_calculate"),
            "sourcePresets": all_source_defaults(),
            "ledgerItems": _ledger_pick_items(user),
            "lotPrefill": {},
            "purityChoices": purity_choices_json(),
            "latestSpot": all_latest_spot(),
            "justSubmittedBuy": False,
            "justSubmittedEbay": False,
            "justSubmittedMelt": False,
        }),
    }


def panel_context(user):
    """The slide-in Calculator panel (dashboard.html/today.html, via
    _calculator_panel.html) - the same shared tabs/forms calculator_page()
    renders, just with no server-computed results: the panel never POSTs
    a no-JS fallback, it only ever starts from localStorage plus a
    client-side lot prefill (see static/js/calculator.js's openFromLot())."""
    context = shared_calculator_context(user)
    context.update({
        "active_tab": "buy", "buy_results": None, "ebay_results": None, "melt_results": None,
        "buy_values": None, "ebay_values": None, "melt_values": None,
    })
    return context


@login_required
def calculator_page(request):
    buy_results = None
    ebay_results = None
    melt_results = None
    active_tab = "buy"

    if request.method == "POST":
        active_tab = request.POST.get("tab") or "buy"
        if active_tab == "ebay":
            ebay_results = _ebay_result(request.POST)
        elif active_tab == "melt":
            melt_results = _melt_result(request.POST)
        else:
            active_tab = "buy"
            buy_results = _buy_result(request.POST)

    lot_prefill = {}
    lot_id = request.GET.get("lot")
    if request.method == "GET" and lot_id:
        lot = get_object_or_404(SourcedLot, pk=lot_id)
        lot_prefill = _lot_prefill(lot)

    melt_values = request.POST if (request.method == "POST" and active_tab == "melt") else None
    melt_metal = (melt_values.get("metal") if melt_values else None) or "gold"

    context = shared_calculator_context(request.user, melt_metal=melt_metal)
    # This page (unlike the panel) can have real server-computed results
    # from a no-JS POST, and a GET ?lot= prefill - both override the
    # shared calc_data_json's defaults (lotPrefill={}, justSubmitted*=False).
    context["calc_data_json"] = json.dumps({
        **json.loads(context["calc_data_json"]),
        "lotPrefill": lot_prefill,
        "justSubmittedBuy": bool(buy_results),
        "justSubmittedEbay": bool(ebay_results),
        "justSubmittedMelt": bool(melt_results),
    })
    context.update({
        "active_tab": active_tab,
        "buy_results": buy_results,
        "ebay_results": ebay_results,
        "melt_results": melt_results,
        "buy_values": request.POST if (request.method == "POST" and active_tab == "buy") else None,
        "ebay_values": request.POST if (request.method == "POST" and active_tab == "ebay") else None,
        "melt_values": melt_values,
    })
    return render(request, "calculator.html", context)


@login_required
@require_POST
def calculate_api(request):
    try:
        data = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON."}, status=400)

    if data.get("tab") == "ebay":
        return JsonResponse(_ebay_result(data))
    if data.get("tab") == "melt":
        return JsonResponse(_melt_result(data))
    return JsonResponse(_buy_result(data))
