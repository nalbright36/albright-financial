"""Views connecting the scanner to the Ledger: "I won this" (create a
LedgerEntry from a SourcedLot, optionally an AIReview), per-entry
management (edit details, record a sale, mark sold out/written off, split
legacy costs), and the Scorecard. Kept out of views.py per project
convention - mirrors scanner_views.py.
"""
from decimal import Decimal

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from . import ledger_metrics
from .forms import EntryDetailsForm, LedgerSaleForm, LegacySplitForm, WinLotForm
from .models import LedgerEntry
from .scanner.max_bid import BuyCosts, all_in_cost
from .scanner.pipeline import _buyer_premium_pct, _inbound_shipping, _sales_tax_pct
from .scanner_models import AIReview, SourcedLot


def _d2(value):
    return Decimal(str(round(value, 2)))


def _win_lot_initial(lot, evaluation, review):
    """Form pre-fill values plus the server-computed prediction snapshot,
    for the GET (prefill) and POST (save-time recompute) paths of
    win_lot() respectively - see that view for why the snapshot is always
    recomputed fresh rather than trusted from hidden form fields."""
    cfg = settings.RESELLING_SCANNER
    src = cfg["SOURCES"].get(lot.source, {})
    buyer_premium_pct = _buyer_premium_pct(lot.raw, src, lot.source) if src else 0.0
    sales_tax_pct = _sales_tax_pct(src, lot.source) if src else 0.0
    inbound = _inbound_shipping(lot.raw, src) if src else 0.0

    winning_bid = lot.final_price if lot.final_price is not None else lot.current_price
    hammer = float(winning_bid or 0)

    # Same building blocks the scanner's own max bid uses (BuyCosts +
    # all_in_cost) - decomposed into three separately editable dollar
    # amounts (so each can be corrected against the real invoice) instead
    # of one combined total, by isolating each component's contribution to
    # all_in_cost with the others zeroed out.
    hammer_plus_premium = all_in_cost(hammer, BuyCosts(buyer_premium_pct, 0.0, 0.0))
    hammer_plus_premium_plus_tax = all_in_cost(hammer, BuyCosts(buyer_premium_pct, sales_tax_pct, 0.0))
    premium_amount = hammer_plus_premium - hammer
    tax_amount = hammer_plus_premium_plus_tax - hammer_plus_premium

    category = evaluation.category if evaluation else ""
    melt_value = evaluation.melt_value if evaluation else None
    scanner_max_bid = evaluation.max_bid if evaluation else None
    ai_suggested_max_bid = review.suggested_max_bid if review else None

    if review is not None and review.resale_low is not None:
        predicted_low, predicted_high, predicted_source = review.resale_low, review.resale_high, "ai_review"
    elif evaluation is not None and evaluation.expected_sale:
        predicted_low = predicted_high = evaluation.expected_sale
        predicted_source = "scanner"
    else:
        predicted_low = predicted_high = None
        predicted_source = ""

    return {
        "form_initial": {
            "item": lot.title[:255],
            "purchase_date": timezone.localdate(),
            "source": lot.source,
            "category": category,
            "listing_url": lot.url,
            "cost": _d2(hammer),
            "buyer_premium": _d2(premium_amount),
            "sales_tax": _d2(tax_amount),
            "inbound_shipping": _d2(inbound),
        },
        "snapshot": {
            "melt_value": melt_value,
            "scanner_max_bid": scanner_max_bid,
            "ai_suggested_max_bid": ai_suggested_max_bid,
            "predicted_sale_low": predicted_low,
            "predicted_sale_high": predicted_high,
            "predicted_sale_source": predicted_source,
        },
    }


@login_required
def win_lot(request, lot_id):
    """GET: a short form pre-filled from the scanner lot/evaluation/AI
    review (or, if this lot already has a ledger item, a warning with a
    link to it instead - "warn", not a hard block: ?confirm=1 proceeds to
    the form anyway). POST: creates the LedgerEntry, snapshotting the
    prediction fresh (never trusting whatever the browser had in the
    pre-filled form, since the lot/evaluation/review may have moved on
    since the page was loaded)."""
    lot = get_object_or_404(SourcedLot.objects.select_related("evaluation"), pk=lot_id)
    evaluation = getattr(lot, "evaluation", None)

    review_id = request.GET.get("review") or request.POST.get("review")
    review = None
    if review_id:
        review = AIReview.objects.filter(pk=review_id, lot=lot, status="done").first()
    if review is None:
        review = AIReview.objects.filter(lot=lot, status="done").order_by("-created_at").first()

    existing = LedgerEntry.objects.filter(owner=request.user, scanner_lot=lot).first()
    confirmed = request.GET.get("confirm") == "1" or request.POST.get("confirm") == "1"
    show_warning = bool(existing) and not confirmed

    if show_warning:
        confirm_url = f"?review={review.pk}&confirm=1" if review else "?confirm=1"
        return render(request, "win_lot.html", {
            "lot": lot, "existing": existing, "show_warning": True, "confirm_url": confirm_url,
        })

    computed = _win_lot_initial(lot, evaluation, review)

    if request.method == "POST":
        form = WinLotForm(request.POST)
        if form.is_valid():
            entry = form.save(commit=False)
            entry.owner = request.user
            entry.scanner_lot = lot
            entry.ai_review = review
            for field, value in computed["snapshot"].items():
                setattr(entry, field, value)
            entry.save()
            messages.success(request, f'Added "{entry.item}" to the ledger.')
            return redirect("albright_reselling_app:ledger")
    else:
        form = WinLotForm(initial=computed["form_initial"])

    return render(request, "win_lot.html", {
        "lot": lot, "evaluation": evaluation, "review": review, "form": form, "show_warning": False,
    })


@login_required
def manage_entry(request, entry_id):
    """The per-entry hub: edit the non-financial details, see its
    prediction/scanner snapshot and computed metrics, its itemized sales,
    and (if it still has legacy unsplit amounts) the split prompt."""
    entry = get_object_or_404(LedgerEntry.objects.prefetch_related("sales"), pk=entry_id, owner=request.user)

    if request.method == "POST":
        form = EntryDetailsForm(request.POST, instance=entry)
        if form.is_valid():
            form.save()
            messages.success(request, "Details updated.")
            return redirect("albright_reselling_app:ledger_manage", entry_id=entry.pk)
    else:
        form = EntryDetailsForm(instance=entry)

    return render(request, "manage_entry.html", {
        "entry": entry, "form": form,
        "legacy_form": LegacySplitForm(entry=entry) if entry.has_legacy_amounts else None,
    })


@login_required
def record_sale(request, entry_id):
    """Adds one LedgerSale against this entry - the partial-sale path.
    Recording a sale against a still-"holding" item promotes it to
    "partially_sold"; reaching "sold_out" is always the owner's own call
    (set_status below), never inferred from the sales recorded so far."""
    entry = get_object_or_404(LedgerEntry, pk=entry_id, owner=request.user)

    if request.method == "POST":
        form = LedgerSaleForm(request.POST)
        if form.is_valid():
            sale = form.save(commit=False)
            sale.entry = entry
            sale.save()
            if entry.status == "holding":
                entry.status = "partially_sold"
                entry.save(update_fields=["status"])
            messages.success(request, f"Recorded a sale of ${sale.sale_price} for {entry.item}.")
            return redirect("albright_reselling_app:ledger_manage", entry_id=entry.pk)
    else:
        form = LedgerSaleForm()

    return render(request, "record_sale.html", {"entry": entry, "form": form})


@login_required
def set_status(request, entry_id):
    """POST-only: the owner's explicit "mark sold out" / "write off"
    actions - see record_sale's docstring for why this is never automatic."""
    entry = get_object_or_404(LedgerEntry, pk=entry_id, owner=request.user)
    if request.method == "POST":
        new_status = request.POST.get("status")
        if new_status in ("sold_out", "written_off"):
            entry.status = new_status
            entry.save(update_fields=["status"])
            messages.success(request, f"{entry.item} marked {entry.get_status_display()}.")
    return redirect("albright_reselling_app:ledger_manage", entry_id=entry.pk)


@login_required
def split_legacy(request, entry_id):
    """Moves an entry's legacy (pre-split) fees/shipping into the new buy/
    sell fields - never automatic/guessed, see LegacySplitForm. The form
    itself is embedded on manage_entry's page; this view only ever
    redirects back there (with a success or error message), so there's no
    separate template for it."""
    entry = get_object_or_404(LedgerEntry, pk=entry_id, owner=request.user)

    if request.method == "POST":
        form = LegacySplitForm(request.POST, entry=entry)
        if form.is_valid():
            cleaned = form.cleaned_data
            if entry.fees:
                entry.buy_fees += cleaned.get("new_buy_fees") or 0
                entry.sell_fees += cleaned.get("new_sell_fees") or 0
                entry.fees = Decimal("0")
            if entry.shipping:
                entry.inbound_shipping += cleaned.get("new_inbound_shipping") or 0
                entry.sell_shipping += cleaned.get("new_sell_shipping") or 0
                entry.shipping = Decimal("0")
            entry.save()
            messages.success(request, f"Split legacy costs for {entry.item}.")
        else:
            for error in form.non_field_errors():
                messages.error(request, error)

    return redirect("albright_reselling_app:ledger_manage", entry_id=entry.pk)


@login_required
def scorecard(request):
    context = ledger_metrics.scorecard_context(request.user)
    return render(request, "scorecard.html", context)
