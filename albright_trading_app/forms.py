from django import forms
from django.contrib.auth.models import User
from albright_trading_app.models import UserProfileInfo,InvestorProfile
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