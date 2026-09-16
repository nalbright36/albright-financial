from django import forms
from django.forms import modelformset_factory
from .models import LedgerEntry, ScanRequest


class LedgerEntryForm(forms.ModelForm):
    class Meta:
        model = LedgerEntry
        fields = ["item", "cost", "fees", "shipping", "sold_for"]
        widgets = {
            "item": forms.TextInput(attrs={"class": "ledger-input", "placeholder": "Item name"}),
            "cost": forms.NumberInput(attrs={"class": "ledger-input num", "step": "0.01"}),
            "fees": forms.NumberInput(attrs={"class": "ledger-input num", "step": "0.01"}),
            "shipping": forms.NumberInput(attrs={"class": "ledger-input num", "step": "0.01"}),
            "sold_for": forms.NumberInput(attrs={"class": "ledger-input num", "step": "0.01"}),
        }


LedgerEntryFormSet = modelformset_factory(
    LedgerEntry,
    form=LedgerEntryForm,
    extra=0,       # 3 blank rows always ready to fill in, spreadsheet-style
    can_delete=True,
)

class ScanRequestForm(forms.ModelForm):
    class Meta:
        model = ScanRequest
        fields = ["source_url", "max_pages"]
        widgets = {
            "source_url": forms.URLInput(attrs={
                "class": "ledger-input",
                "placeholder": "https://hibid.com/catalog/.../some-auction",
            }),
            "max_pages": forms.NumberInput(attrs={
                "class": "ledger-input num",
                "min": "1",
            }),
        }