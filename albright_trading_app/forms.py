from django import forms
from django.contrib.auth.models import User
from albright_trading_app.models import BotStrategy, UserProfileInfo,InvestorProfile
from django.contrib.auth.forms import UserCreationForm, AuthenticationForm
from django.forms.widgets import PasswordInput, TextInput
from django.forms import ModelForm

class UserForm(forms.ModelForm):
    password = forms.CharField(widget=forms.PasswordInput())

    class Meta():
        model = User
        fields = ('username','email','password')

class LoginForm(AuthenticationForm):

    username = forms.CharField(widget=TextInput())
    password = forms.CharField(widget=PasswordInput())

class InvestorProfileForm(forms.ModelForm):

    username = forms.CharField(disabled=True, required=False)
    
    first_name = forms.CharField(max_length=100)
    last_name = forms.CharField(max_length=100)

    email = forms.EmailField(disabled=True)

    date_of_birth = forms.DateField(
        widget=forms.DateInput(attrs={'type': 'date'})
    )

    expected_retirement_date = forms.DateField(
        widget=forms.DateInput(attrs={'type': 'date'})
    )

    risk_tolerance = forms.ChoiceField(
        choices=[
            ('Low', 'Low'),
            ('Medium', 'Medium'),
            ('High', 'High'),
        ],
        widget=forms.Select
    )

    marital_status = forms.ChoiceField(
        choices=[
            ('Single', 'Single'),
            ('Married', 'Married'),
            ('Divorced', 'Divorced'),
        ],
        widget=forms.Select
    )

    dependents_count = forms.IntegerField(
        min_value=0,
        widget=forms.NumberInput
    )

    home_street_address = forms.CharField(max_length=255)

    home_state = forms.CharField(
        max_length=2,
        widget=forms.TextInput(attrs={'placeholder': 'NY'})
    )

    home_city = forms.CharField(max_length=100)

    home_zip = forms.CharField(
        max_length=10,
        widget=forms.TextInput(attrs={'type': 'text'})
    )

    employment_status = forms.ChoiceField(
        choices=[
            ('Employed', 'Employed'),
            ('Self-Employed', 'Self-Employed'),
            ('Unemployed', 'Unemployed'),
            ('Retired', 'Retired'),
        ]
    )

    goal_type = forms.ChoiceField(
        choices=[
            ('Retirement', 'Retirement'),
            ('Education', 'Education'),
            ('Wealth Accumulation', 'Wealth Accumulation'),
        ]
    )

    maximum_tolerable_drawdown = forms.DecimalField(
        max_digits=5,
        decimal_places=2,
        widget=forms.NumberInput(attrs={'step': '0.01'})
    )

    monthly_contribution_amount = forms.DecimalField(
        max_digits=10,
        decimal_places=2
    )

    liquid_assets = forms.DecimalField(
        max_digits=12,
        decimal_places=2
    )

    debt = forms.DecimalField(
        max_digits=12,
        decimal_places=2
    )

    class Meta:
        model = InvestorProfile
        fields = '__all__'

    def __init__(self, *args, **kwargs):
        user = kwargs.pop("user", None)
        super().__init__(*args, **kwargs)

        if user and user.is_authenticated:
            self.fields["username"].initial = user.username
            self.fields["email"].initial = user.email


class BotStrategyForm(forms.ModelForm):
    """Grok Bot > Strategies: add or edit a bot strategy. Prefixes are typed comma- or space-separated;
    a prefix another strategy shares (or that starts with another's) is allowed, with a warning on the page."""
    prefixes_text = forms.CharField(
        label="Client order ID prefixes", max_length=300,
        help_text='Comma-separated, any capitalization - e.g. "eng" matches eng-..., ENG-....',
        widget=forms.TextInput(attrs={"placeholder": "e.g. eng", "autocomplete": "off"}))

    class Meta:
        model = BotStrategy
        fields = ["name", "color", "status", "started_at", "allocated_capital", "notes"]
        labels = {"started_at": "Started", "allocated_capital": "Allocated capital ($)", "notes": "Description / notes"}
        widgets = {"color": forms.RadioSelect(attrs={"class": "strategy-color-choice"}),
                   "started_at": forms.DateInput(attrs={"type": "date"}),
                   "allocated_capital": forms.NumberInput(attrs={"step": "0.01", "min": "0", "placeholder": "Optional"}),
                   "notes": forms.Textarea(attrs={"rows": 2})}

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user or self.instance.user
        if self.instance.pk and not self.is_bound:
            self.fields["prefixes_text"].initial = ", ".join(self.instance.prefixes)

    def clean_name(self):
        name = self.cleaned_data["name"].strip()
        taken = BotStrategy.objects.filter(user=self.user, name__iexact=name).exclude(pk=self.instance.pk)
        if taken.exists():
            raise forms.ValidationError("You already have a strategy with this name.")
        return name

    def clean_prefixes_text(self):
        from .strategy_attribution import PREFIX_RE, parse_prefixes
        prefixes = parse_prefixes(self.cleaned_data["prefixes_text"])
        if not prefixes:
            raise forms.ValidationError("Give at least one prefix.")
        bad = [p for p in prefixes if not PREFIX_RE.match(p) or len(p) > 48]
        if bad:
            raise forms.ValidationError(f"Use letters, numbers, dots, dashes, underscores or colons: {', '.join(bad)}")
        return prefixes

    def save(self, commit=True):
        strategy = super().save(commit=False)
        strategy.user = self.user
        strategy.prefixes = self.cleaned_data["prefixes_text"]
        if commit:
            strategy.save()
        return strategy
