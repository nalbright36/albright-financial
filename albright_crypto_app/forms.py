"""Forms. SECURITY RULE: no form here has (or may ever get) a field for a
seed phrase, private key, or exchange API key - tests/test_security.py
checks every form's field names. Free text is checked by reject_secrets()."""
from django import forms

from .extraction import quote_in_text
from .guards import reject_secrets
from .links import is_web_url
from .models import Earning, Opportunity, TimeLog

INPUT = {"class": "ledger-input"}


def _lines(text):
    return [line.strip() for line in (text or "").splitlines() if line.strip()]


class ManualOpportunityForm(forms.Form):
    url = forms.URLField(max_length=1000, widget=forms.URLInput(attrs={**INPUT, "placeholder": "https://..."}),
                         help_text="Where you found it.")
    title = forms.CharField(max_length=500, required=False, widget=forms.TextInput(attrs=INPUT))
    text = forms.CharField(widget=forms.Textarea(attrs={**INPUT, "rows": 10}),
                           help_text="Paste the description, terms, and reward details as they appear.")

    def clean_url(self):
        url = self.cleaned_data["url"]
        if not is_web_url(url):
            raise forms.ValidationError("Enter an http(s) link.")
        return url

    def clean_text(self):
        text = self.cleaned_data["text"]
        reject_secrets(text)
        return text


class OpportunityEditForm(forms.ModelForm):
    """Correcting the extracted facts - saving re-runs the safety check and scoring."""
    class Meta:
        model = Opportunity
        fields = ["reward_usd", "reward_currency", "reward_type", "evidence", "evidence_quote", "deadline",
                  "hours_estimate", "costs_usd", "requires_spending", "eligibility_notes", "notes"]
        widgets = {
            "deadline": forms.DateTimeInput(attrs={**INPUT, "type": "datetime-local"}, format="%Y-%m-%dT%H:%M"),
            "evidence_quote": forms.TextInput(attrs=INPUT),
            "eligibility_notes": forms.Textarea(attrs={**INPUT, "rows": 2}),
            "notes": forms.Textarea(attrs={**INPUT, "rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name, field in self.fields.items():
            if not isinstance(field.widget, forms.CheckboxInput):
                field.widget.attrs.setdefault("class", "ledger-input")

    def clean(self):
        cleaned = super().clean()
        quote = (cleaned.get("evidence_quote") or "").strip()
        source_text = f"{self.instance.title}\n{self.instance.raw_text}"
        if quote and not quote_in_text(quote, source_text):
            self.add_error("evidence_quote", "This quote doesn't appear in the opportunity's text.")
        if cleaned.get("evidence") in ("explicit", "stated") and not quote:
            self.add_error("evidence_quote", "Evidence needs a quote from the text that states the reward.")
        return cleaned


class TimeLogForm(forms.ModelForm):
    class Meta:
        model = TimeLog
        fields = ["minutes", "date", "note"]
        widgets = {"minutes": forms.NumberInput(attrs={**INPUT, "min": 1}),
                   "date": forms.DateInput(attrs={**INPUT, "type": "date"}),
                   "note": forms.TextInput(attrs=INPUT)}


class EarningForm(forms.ModelForm):
    """Confirmed earnings only. tx_link must be on an allowlisted domain
    (Earning.clean)."""
    class Meta:
        model = Earning
        fields = ["date", "amount", "currency", "usd_value_at_receipt", "tx_link", "note"]
        widgets = {"date": forms.DateInput(attrs={**INPUT, "type": "date"}),
                   "amount": forms.NumberInput(attrs={**INPUT, "step": "any"}),
                   "currency": forms.TextInput(attrs=INPUT),
                   "usd_value_at_receipt": forms.NumberInput(attrs={**INPUT, "step": "0.01"}),
                   "tx_link": forms.URLInput(attrs={**INPUT, "placeholder": "Block explorer link (verified domains only)"}),
                   "note": forms.TextInput(attrs=INPUT)}
        labels = {"usd_value_at_receipt": "USD value when received", "tx_link": "Transaction link"}


class SettingsForm(forms.Form):
    allowlist = forms.CharField(
        required=False, widget=forms.Textarea(attrs={**INPUT, "rows": 6}),
        help_text="One domain per line (e.g. github.com). Subdomains are included. Links anywhere else show a warning first.")
    rss_feeds = forms.CharField(
        required=False, widget=forms.Textarea(attrs={**INPUT, "rows": 4}),
        help_text="One feed URL per line - official project and program feeds only.")

    def clean_allowlist(self):
        domains = []
        for line in _lines(self.cleaned_data["allowlist"]):
            domain = line.lower().removeprefix("https://").removeprefix("http://").split("/")[0].removeprefix("www.")
            if "." not in domain or " " in domain:
                raise forms.ValidationError(f"“{line}” isn't a domain.")
            if domain not in domains:
                domains.append(domain)
        return domains

    def clean_rss_feeds(self):
        feeds = _lines(self.cleaned_data["rss_feeds"])
        for feed in feeds:
            if not is_web_url(feed):
                raise forms.ValidationError(f"“{feed}” isn't an http(s) link.")
        return list(dict.fromkeys(feeds))
