from django import forms

from albright_crypto_app.guards import reject_secrets

from . import apps_registry, landing
from .models import INVITE_DAYS_CHOICES, LANDING_CHOICES, LandingSettings

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
    first_name = forms.CharField(max_length=150, required=False, label="First name",
                                 widget=forms.TextInput(attrs={**INPUT, "autocomplete": "off"}),
                                 help_text='Shown as who added things (e.g. on the grocery list).')
    email = forms.EmailField(required=False, widget=forms.EmailInput(attrs={**INPUT, "autocomplete": "off"}),
                             help_text="Where the password setup link goes, if this site can send email.")
    password = forms.CharField(required=False, widget=forms.PasswordInput(attrs={**INPUT, "autocomplete": "new-password"}),
                               help_text='Leave blank and use "Send password setup link" to let them choose their own.')
    apps = forms.MultipleChoiceField(required=False, widget=forms.CheckboxSelectMultiple, label="Apps they can open")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["apps"].choices = _app_choices()

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


def _app_choices():
    return [(app.key, app.label) for app in apps_registry.APPS if not app.superuser_only]


def _email_taken(email):
    from django.contrib.auth import get_user_model
    return get_user_model().objects.filter(email__iexact=email).exists()


class InviteForm(forms.Form):
    """Admin > Users > Invite user: who, which apps they'll get, and how long the link works."""
    email = forms.EmailField(widget=forms.EmailInput(attrs={**INPUT, "autocomplete": "off"}))
    name = forms.CharField(max_length=120, required=False,
                           widget=forms.TextInput(attrs={**INPUT, "autocomplete": "off"}),
                           help_text="Used to greet them in the email.")
    apps = forms.MultipleChoiceField(required=False, widget=forms.CheckboxSelectMultiple, label="Apps to grant")
    days = forms.TypedChoiceField(choices=INVITE_DAYS_CHOICES, coerce=int, initial=7, label="Link expires after",
                                  widget=forms.Select(attrs=INPUT))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["apps"].choices = _app_choices()

    def clean_email(self):
        email = self.cleaned_data["email"].strip()
        if _email_taken(email):
            raise forms.ValidationError("There's already an account with this email.")
        return email

    def clean_name(self):
        name = self.cleaned_data["name"].strip()
        reject_secrets(name, "name")
        return name


class SignupForm(forms.Form):
    """The invite link's sign-up page. The email comes from the invite (shown locked), never from the form."""
    username = forms.CharField(max_length=150, widget=forms.TextInput(attrs={**INPUT, "autocomplete": "username",
                                                                            "autocapitalize": "none"}),
                               help_text="Letters, numbers and @ . + - _ only.")
    password1 = forms.CharField(label="Password", strip=False,
                                widget=forms.PasswordInput(attrs={**INPUT, "autocomplete": "new-password"}))
    password2 = forms.CharField(label="Password again", strip=False,
                                widget=forms.PasswordInput(attrs={**INPUT, "autocomplete": "new-password"}))

    def __init__(self, *args, invite, **kwargs):
        super().__init__(*args, **kwargs)
        self.invite = invite
        from django.contrib.auth.password_validation import password_validators_help_text_html
        self.fields["password1"].help_text = password_validators_help_text_html()

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
        first, second = cleaned.get("password1"), cleaned.get("password2")
        if first and second and first != second:
            self.add_error("password2", "The two passwords don't match.")
        elif first and cleaned.get("username"):
            try:
                validate_password(first, user=get_user_model()(username=cleaned["username"], email=self.invite.email,
                                                               first_name=self.invite.name))
            except forms.ValidationError as exc:
                self.add_error("password1", exc)
        if _email_taken(self.invite.email):
            raise forms.ValidationError("There's already an account with this email - sign in instead.")
        return cleaned

