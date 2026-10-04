from django import forms
from django.forms import modelformset_factory
from .models import LedgerEntry, LedgerSale, ScanRequest, HarvestRequest, AnalysisRequest
from .scanner_models import BidWatch, LotFeedback

_NUM_WIDGET = lambda placeholder=None: forms.NumberInput(attrs={  # noqa: E731
    "class": "ledger-input num", "step": "0.01", **({"placeholder": placeholder} if placeholder else {}),
})


class LedgerEntryForm(forms.ModelForm):
    """The ledger spreadsheet's quick-edit row: buy/sell cost columns only
    (the old single fees/shipping columns are gone - see LedgerEntry.fees/
    shipping's docstring. Everything else (date, source, category, status,
    sales...) is edited on the entry's manage page instead."""
    class Meta:
        model = LedgerEntry
        fields = ["item", "cost", "buyer_premium", "sales_tax", "buy_fees", "inbound_shipping",
                   "sold_for", "sell_fees", "sell_shipping"]
        widgets = {
            "item": forms.TextInput(attrs={"class": "ledger-input", "placeholder": "Item name"}),
            "cost": _NUM_WIDGET(),
            "buyer_premium": _NUM_WIDGET(),
            "sales_tax": _NUM_WIDGET(),
            "buy_fees": _NUM_WIDGET(),
            "inbound_shipping": _NUM_WIDGET(),
            "sold_for": _NUM_WIDGET(),
            "sell_fees": _NUM_WIDGET(),
            "sell_shipping": _NUM_WIDGET(),
        }


LedgerEntryFormSet = modelformset_factory(
    LedgerEntry,
    form=LedgerEntryForm,
    extra=0,       # 3 blank rows always ready to fill in, spreadsheet-style
    can_delete=True,
)


class WinLotForm(forms.ModelForm):
    """The "I won this" form: everything pre-filled from the scanner lot/
    evaluation/AI review (see ledger_views._win_lot_initial), but every
    field here stays editable so it can be corrected against the real
    invoice. The prediction snapshot fields (predicted_sale_low/high/
    source, scanner_max_bid, ai_suggested_max_bid, melt_value) are NOT
    here - they're captured automatically by the view at save time, not
    typed in by hand."""
    class Meta:
        model = LedgerEntry
        fields = ["item", "purchase_date", "source", "category", "listing_url", "cost",
                   "buyer_premium", "sales_tax", "buy_fees", "inbound_shipping", "purchase_hours"]
        widgets = {
            "item": forms.TextInput(attrs={"class": "ledger-input", "placeholder": "Item name"}),
            "purchase_date": forms.DateInput(attrs={"class": "ledger-input", "type": "date"}),
            "source": forms.TextInput(attrs={"class": "ledger-input"}),
            "category": forms.TextInput(attrs={"class": "ledger-input"}),
            "listing_url": forms.URLInput(attrs={"class": "ledger-input"}),
            "cost": _NUM_WIDGET("Winning bid"),
            "buyer_premium": _NUM_WIDGET(),
            "sales_tax": _NUM_WIDGET(),
            "buy_fees": _NUM_WIDGET("Optional extra fees"),
            "inbound_shipping": _NUM_WIDGET(),
            "purchase_hours": _NUM_WIDGET("Sourcing/pickup hours"),
        }


class EntryDetailsForm(forms.ModelForm):
    """Edits the non-financial fields from an entry's manage page."""
    class Meta:
        model = LedgerEntry
        fields = ["item", "purchase_date", "source", "category", "listing_url", "purchase_hours"]
        widgets = {
            "item": forms.TextInput(attrs={"class": "ledger-input"}),
            "purchase_date": forms.DateInput(attrs={"class": "ledger-input", "type": "date"}),
            "source": forms.TextInput(attrs={"class": "ledger-input"}),
            "category": forms.TextInput(attrs={"class": "ledger-input"}),
            "listing_url": forms.URLInput(attrs={"class": "ledger-input"}),
            "purchase_hours": _NUM_WIDGET(),
        }


class LedgerSaleForm(forms.ModelForm):
    class Meta:
        model = LedgerSale
        fields = ["sale_date", "sale_price", "channel", "selling_fees", "outbound_shipping",
                   "hours_spent", "notes"]
        widgets = {
            "sale_date": forms.DateInput(attrs={"class": "ledger-input", "type": "date"}),
            "sale_price": _NUM_WIDGET(),
            "channel": forms.Select(attrs={"class": "ledger-input"}),
            "selling_fees": _NUM_WIDGET(),
            "outbound_shipping": _NUM_WIDGET(),
            "hours_spent": _NUM_WIDGET("Listing/packing hours"),
            "notes": forms.TextInput(attrs={"class": "ledger-input", "placeholder": "e.g. half the lot"}),
        }


class LegacySplitForm(forms.Form):
    """Moves an entry's legacy (pre-split) fees/shipping into the new buy/
    sell buckets. Only asks about whichever legacy field is actually
    nonzero; each pair must sum to exactly the legacy amount being split,
    so nothing is silently gained or lost. Never auto-guesses a split."""
    new_buy_fees = forms.DecimalField(max_digits=10, decimal_places=2, required=False, min_value=0,
                                       widget=_NUM_WIDGET())
    new_sell_fees = forms.DecimalField(max_digits=10, decimal_places=2, required=False, min_value=0,
                                        widget=_NUM_WIDGET())
    new_inbound_shipping = forms.DecimalField(max_digits=10, decimal_places=2, required=False, min_value=0,
                                               widget=_NUM_WIDGET())
    new_sell_shipping = forms.DecimalField(max_digits=10, decimal_places=2, required=False, min_value=0,
                                            widget=_NUM_WIDGET())

    def __init__(self, *args, entry=None, **kwargs):
        self.entry = entry
        super().__init__(*args, **kwargs)

    def clean(self):
        cleaned = super().clean()
        if self.entry and self.entry.fees:
            total = (cleaned.get("new_buy_fees") or 0) + (cleaned.get("new_sell_fees") or 0)
            if total != self.entry.fees:
                raise forms.ValidationError(
                    f"Buy fees + sell fees must add up to the legacy fees amount (${self.entry.fees})."
                )
        if self.entry and self.entry.shipping:
            total = (cleaned.get("new_inbound_shipping") or 0) + (cleaned.get("new_sell_shipping") or 0)
            if total != self.entry.shipping:
                raise forms.ValidationError(
                    f"Inbound + sell shipping must add up to the legacy shipping amount (${self.entry.shipping})."
                )
        return cleaned

class ScanRequestForm(forms.ModelForm):
    class Meta:
        model = ScanRequest
        fields = ["source_url", "max_pages", "reference_analysis"]
        widgets = {
            "source_url": forms.URLInput(attrs={"class": "ledger-input", "placeholder": "https://hibid.com/catalog/.../some-auction"}),
            "max_pages": forms.NumberInput(attrs={"class": "ledger-input num", "min": "1"}),
            "reference_analysis": forms.Select(attrs={"class": "ledger-input"}),
        }

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        if user is not None:
            self.fields["reference_analysis"].queryset = AnalysisRequest.objects.filter(owner=user, status="complete")
        self.fields["reference_analysis"].required = False
        self.fields["reference_analysis"].empty_label = "None (title-mismatch scoring only)"

class HarvestRequestForm(forms.ModelForm):
    class Meta:
        model = HarvestRequest
        fields = ["source_url", "max_pages"]
        widgets = {
            "source_url": forms.URLInput(attrs={
                "class": "ledger-input",
                "placeholder": "https://hibid.com/lots/... with status=CLOSED",
            }),
            "max_pages": forms.NumberInput(attrs={"class": "ledger-input num", "min": "1"}),
        }

class AnalysisRequestForm(forms.ModelForm):
    class Meta:
        model = AnalysisRequest
        fields = ["category", "auctioneer_name", "keyword", "min_final_price", "max_final_price", "max_lots"]
        widgets = {
            "category": forms.TextInput(attrs={"class": "ledger-input", "placeholder": "leave blank for all"}),
            "auctioneer_name": forms.TextInput(attrs={"class": "ledger-input", "placeholder": "leave blank for all"}),
            "keyword": forms.TextInput(attrs={"class": "ledger-input", "placeholder": "title/description contains..."}),
            "min_final_price": forms.NumberInput(attrs={"class": "ledger-input num"}),
            "max_final_price": forms.NumberInput(attrs={"class": "ledger-input num"}),
            "max_lots": forms.NumberInput(attrs={"class": "ledger-input num", "min": "1"}),
        }


class BidWatchForm(forms.ModelForm):
    """The "I bid on this" form: just the one number the user actually
    needs to supply - everything else on BidWatch is set by the view
    (the lot) or by the watch-resolution sweep (status, alerts sent)."""
    class Meta:
        model = BidWatch
        fields = ["my_max_bid"]
        widgets = {
            "my_max_bid": forms.NumberInput(attrs={"class": "ledger-input num", "step": "0.01"}),
        }


class LotFeedbackForm(forms.ModelForm):
    """The "Wrong?" form: what kind of mistake, and an optional note - the
    lot itself is set by the view, same pattern as BidWatchForm above."""
    class Meta:
        model = LotFeedback
        fields = ["kind", "note"]
        widgets = {
            "kind": forms.Select(attrs={"class": "ledger-input"}),
            "note": forms.Textarea(attrs={"class": "ledger-input", "rows": 3,
                                            "placeholder": "What's wrong? (optional)"}),
        }