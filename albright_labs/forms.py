from django import forms

from albright_crypto_app.guards import reject_secrets

from . import landing
from .models import LANDING_CHOICES, LandingSettings

INPUT = {"class": "ledger-input"}


class LandingPreferenceForm(forms.Form):
    landing = forms.ChoiceField(choices=LANDING_CHOICES, widget=forms.RadioSelect, label="Where I land after signing in")
    landing_page = forms.ChoiceField(required=False, label="Page", widget=forms.Select(attrs=INPUT))

    def __init__(self, *args, user, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user
        self.fields["landing_page"].choices = [("", "Pick a page")] + [
            (f"{group}", [(key, label) for key, label in pages]) for group, pages in landing.page_choices(user)]

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("landing") == "page":
            key = cleaned.get("landing_page")
            if not key:
                self.add_error("landing_page", "Pick the page to land on.")
            elif not landing.allowed_page(self.user, key):
                self.add_error("landing_page", "That page isn't available.")
        return cleaned


class LandingSettingsForm(forms.ModelForm):
    class Meta:
        model = LandingSettings
        fields = ["name", "headline"]
        widgets = {"name": forms.TextInput(attrs=INPUT), "headline": forms.TextInput(attrs=INPUT)}

    def clean(self):
        cleaned = super().clean()
        for field in ("name", "headline"):
            reject_secrets(cleaned.get(field) or "", field)
        return cleaned
