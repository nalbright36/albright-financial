from django import forms

from albright_crypto_app.guards import reject_secrets

from . import apps_registry, landing
from .models import LANDING_CHOICES, LandingSettings

INPUT = {"class": "ledger-input"}


class LandingPreferenceForm(forms.Form):
    landing = forms.ChoiceField(choices=LANDING_CHOICES, widget=forms.RadioSelect, label="Where I land after signing in")
    landing_page = forms.ChoiceField(required=False, label="Page", widget=forms.Select(attrs=INPUT))

    def __init__(self, *args, user, pref=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.user, self.pref = user, pref
        # Only pages of apps the user can open and has kept in their navigation.
        self.fields["landing_page"].choices = [("", "Pick a page")] + [
            (f"{group}", [(key, label) for key, label in pages]) for group, pages in landing.page_choices(user, pref)]

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("landing") == "page":
            key = cleaned.get("landing_page")
            if not key:
                self.add_error("landing_page", "Pick the page to land on.")
            elif not landing.allowed_page(self.user, key, self.pref):
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


class CreateUserForm(forms.Form):
    """Admin > Users: add someone and choose the apps they can open."""
    username = forms.CharField(max_length=150, widget=forms.TextInput(attrs={**INPUT, "autocomplete": "off"}))
    email = forms.EmailField(required=False, widget=forms.EmailInput(attrs={**INPUT, "autocomplete": "off"}))
    password = forms.CharField(widget=forms.PasswordInput(attrs={**INPUT, "autocomplete": "new-password"}),
                               help_text="Give it to them yourself; they sign in with it.")
    apps = forms.MultipleChoiceField(required=False, widget=forms.CheckboxSelectMultiple, label="Apps they can open")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["apps"].choices = [(app.key, app.label) for app in apps_registry.APPS if not app.superuser_only]
        self.fields["apps"].initial = apps_registry.grantable_keys()

    def clean_username(self):
        from django.contrib.auth import get_user_model
        username = self.cleaned_data["username"].strip()
        user_model = get_user_model()
        user_model.username_validator(username)
        if user_model.objects.filter(username__iexact=username).exists():
            raise forms.ValidationError("That username is taken.")
        return username

    def clean(self):
        cleaned = super().clean()
        from django.contrib.auth import get_user_model
        from django.contrib.auth.password_validation import validate_password
        if cleaned.get("password") and cleaned.get("username"):
            try:
                validate_password(cleaned["password"], user=get_user_model()(username=cleaned["username"],
                                                                             email=cleaned.get("email") or ""))
            except forms.ValidationError as exc:
                self.add_error("password", exc)
        return cleaned
