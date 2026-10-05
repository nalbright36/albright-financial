"""Views for the Bid Calculator page. Logic lives in scanner/calculator.py
- kept out of views.py per project convention, same as ledger_views.py/
scanner_views.py/insights_views.py.

Two ways to compute: the JSON endpoint (calculate_api) the page's JS calls
as values change, and a plain POST to calculator_page itself (the
JavaScript-off fallback, via the page's "Calculate" button) - both go
through the exact same scanner/calculator.py functions, so they can never
disagree.
"""
import json

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from .scanner import calculator
from .scanner_models import AIReview, BidCalculation, SourcedLot


def _data_from_post(post):
    """Same flat shape calculate_from_dict() expects, built from a plain
    form POST (the no-JS fallback) - repeated item_name/item_quantity/
    item_price fields (one per row) become the "items" list."""
    names = post.getlist("item_name")
    quantities = post.getlist("item_quantity")
    prices = post.getlist("item_price")
    items = [
        {"name": n, "quantity": q, "price_each": p}
        for n, q, p in zip(names, quantities, prices)
        if n or q or p
    ]
    data = {key: post.get(key) for key in (
        "bid", "premium_pct", "tax_pct", "card_fee_pct", "per_lot_fee", "inbound_mode", "shipping",
        "pickup_miles", "pickup_rate_per_mile", "pickup_fixed_cost", "platform_fee_pct", "fixed_fee",
        "outbound_shipping", "packaging", "other_costs", "target_profit_dollars", "target_profit_pct",
    )}
    data["tax_on_premium"] = post.get("tax_on_premium") is not None
    data["items"] = items
    return data


def _percent_fields_to_fraction(data):
    """The form/JS send percentages as whole numbers (18 for 18%) - this
    converts every *_pct field to the 0-1 fraction scanner/calculator.py
    (and max_bid.py underneath it) expects. Values already below 1 are
    left alone, so a prefill that already supplies a fraction (e.g. from
    source_preset()) still works."""
    for key in ("premium_pct", "tax_pct", "card_fee_pct", "platform_fee_pct", "target_profit_pct"):
        value = data.get(key)
        if value in (None, ""):
            continue
        value = float(value)
        data[key] = value / 100 if value > 1 else value
    return data


@login_required
def calculator_page(request):
    if request.method == "POST":
        data = _percent_fields_to_fraction(_data_from_post(request.POST))
        results = calculator.calculate_from_dict(data)
        saved = BidCalculation.objects.all()[:50]
        return render(request, "calculator.html", {
            "prefill": json.dumps(data), "results": results, "saved_calculations": saved,
            "source_choices": calculator.SOURCE_CHOICES, "channel_choices": calculator.CHANNEL_CHOICES,
        })

    prefill = {}
    lot_id = request.GET.get("lot")
    load_id = request.GET.get("load")
    if lot_id:
        lot = get_object_or_404(SourcedLot.objects.select_related("evaluation"), pk=lot_id)
        prefill = calculator.prefill_from_lot(lot)
        review = None
        review_id = request.GET.get("review")
        if review_id:
            review = AIReview.objects.filter(pk=review_id, lot=lot, status="done").first()
        evaluation = getattr(lot, "evaluation", None)
        expected_sale = calculator.prefill_expected_sale(evaluation, review)
        if expected_sale:
            prefill["items"] = [{"name": lot.title[:100], "quantity": 1, "price_each": expected_sale}]
    elif load_id:
        saved_calc = get_object_or_404(BidCalculation, pk=load_id)
        prefill = saved_calc.inputs

    saved = BidCalculation.objects.all()[:50]
    return render(request, "calculator.html", {
        "prefill": json.dumps(prefill), "results": None, "saved_calculations": saved,
        "source_choices": calculator.SOURCE_CHOICES, "channel_choices": calculator.CHANNEL_CHOICES,
        "load_id": load_id,
    })


@login_required
@require_POST
def calculate_api(request):
    try:
        data = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON."}, status=400)
    return JsonResponse(calculator.calculate_from_dict(data))


@login_required
@require_POST
def melt_api(request):
    try:
        data = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON."}, status=400)
    result = calculator.melt_value(
        data.get("metal", "gold"), data.get("purity"), data.get("weight", 0), data.get("unit", "g"),
        data.get("payout_pct", 1.0),
    )
    return JsonResponse(result)


@login_required
@require_POST
def preset_api(request):
    try:
        data = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON."}, status=400)
    kind = data.get("kind")
    if kind == "source":
        return JsonResponse(calculator.source_preset(data.get("source", "")))
    if kind == "channel":
        return JsonResponse(calculator.channel_preset(data.get("channel", ""), data.get("category", "")))
    return JsonResponse({"error": "Unknown preset kind."}, status=400)


@login_required
@require_POST
def save_calculation(request):
    try:
        inputs = json.loads(request.POST.get("inputs") or "{}")
        results = json.loads(request.POST.get("results") or "{}")
    except json.JSONDecodeError:
        inputs, results = {}, {}
    name = request.POST.get("name", "").strip()
    lot_id = request.POST.get("scanner_lot")

    calc = BidCalculation.objects.create(
        name=name, inputs=inputs, results=results,
        scanner_lot_id=lot_id if lot_id else None,
    )
    messages.success(request, f'Saved "{calc.name or f"Calculation #{calc.pk}"}".')
    return redirect(f"{reverse('albright_reselling_app:calculator')}?load={calc.pk}")


@login_required
@require_POST
def rename_calculation(request, pk):
    calc = get_object_or_404(BidCalculation, pk=pk)
    calc.name = request.POST.get("name", "").strip()
    calc.save(update_fields=["name"])
    messages.success(request, "Renamed.")
    return redirect("albright_reselling_app:calculator")


@login_required
@require_POST
def delete_calculation(request, pk):
    calc = get_object_or_404(BidCalculation, pk=pk)
    calc.delete()
    messages.success(request, "Deleted.")
    return redirect("albright_reselling_app:calculator")
