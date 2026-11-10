"""Albright Money's forms. Choices are always limited to what the user can see (accounts, categories), and a
transaction's category must come from its account's own space's set - checked on the server."""
from decimal import Decimal

from django import forms
from django.utils import timezone

from . import imports, money
from .models import Account, Category, Rule, Transaction

INPUT = {"class": "ledger-input"}
MONEY_INPUT = {"class": "ledger-input", "inputmode": "decimal", "step": "0.01"}
MAX_AMOUNT = Decimal("99999999.99")


def _amount_field(**kw):
    return forms.DecimalField(max_digits=12, decimal_places=2, max_value=MAX_AMOUNT, min_value=-MAX_AMOUNT,
                              widget=forms.NumberInput(attrs=MONEY_INPUT), **kw)


class AccountForm(forms.ModelForm):
    """Never a full account number: last 4 digits only. A credit card's starting balance is entered as what's
    owed (a positive number) and stored as a negative balance."""
    space = forms.ChoiceField(label="Space", required=False,
                              help_text="Who sees it: just you (Personal), or a circle that shares Money.")
    starting_balance = _amount_field(initial=Decimal("0.00"),
                                     help_text="For a credit card: what you owed on the starting date.")

    class Meta:
        model = Account
        fields = ["name", "kind", "institution", "last4", "starting_balance", "starting_date", "archived"]
        widgets = {"name": forms.TextInput(attrs={**INPUT, "maxlength": 80}),
                   "kind": forms.Select(attrs=INPUT),
                   "institution": forms.TextInput(attrs={**INPUT, "maxlength": 80, "placeholder": "e.g. Chase"}),
                   "last4": forms.TextInput(attrs={**INPUT, "inputmode": "numeric", "pattern": r"\d{4}",
                                                   "maxlength": 4, "autocomplete": "off", "placeholder": "1234"}),
                   "starting_date": forms.DateInput(attrs={**INPUT, "type": "date"}, format="%Y-%m-%d")}

    def __init__(self, *args, spaces=(), current=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.spaces = {s.pk: s for s in spaces}
        self.current = self.instance.space if self.instance.pk else current
        self.fields["space"].choices = [(s.pk, s.name) for s in spaces]
        self.fields["space"].widget.attrs.update(INPUT)
        self.fields["space"].initial = self.instance.space_id if self.instance.pk else (current.pk if current else None)
        if len(self.spaces) < 2:
            self.fields["space"].widget = forms.HiddenInput()
        if self.instance.pk and self.instance.is_credit:
            self.initial["starting_balance"] = -self.instance.starting_balance
        if not self.instance.pk:
            del self.fields["archived"]
            self.fields["starting_date"].initial = timezone.localdate()

    def clean_space(self):
        """One of the spaces the user sees in Money - never anyone else's."""
        value = self.cleaned_data["space"]
        if value in (None, ""):                                  # not sent: the one being shown (or its own)
            value = self.current.pk if self.current else ""
        space = self.spaces.get(int(value)) if str(value).isdigit() else None
        if space is None:
            raise forms.ValidationError("Pick one of your spaces.")
        return space

    def clean_starting_balance(self):
        value = self.cleaned_data["starting_balance"]
        return -abs(value) if self.cleaned_data.get("kind") == Account.CREDIT else value


class TransactionForm(forms.ModelForm):
    amount = _amount_field(help_text="Money in is positive, money out negative.")

    class Meta:
        model = Transaction
        fields = ["date", "amount", "payee", "account", "category", "notes", "tags", "is_transfer", "needs_review"]
        widgets = {"date": forms.DateInput(attrs={**INPUT, "type": "date"}, format="%Y-%m-%d"),
                   "payee": forms.TextInput(attrs={**INPUT, "maxlength": 200}),
                   "account": forms.Select(attrs=INPUT), "category": forms.Select(attrs=INPUT),
                   "notes": forms.Textarea(attrs={**INPUT, "rows": 3, "maxlength": 2000}),
                   "tags": forms.TextInput(attrs={**INPUT, "maxlength": 200, "placeholder": "e.g. vacation, tax"})}
        labels = {"is_transfer": "Transfer between my accounts (not spending)", "needs_review": "Needs review"}

    def __init__(self, *args, user, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["account"].queryset = Account.objects.visible_to(user).filter(archived=False) | \
            Account.objects.visible_to(user).filter(pk=self.instance.account_id or 0)
        self.fields["category"].queryset = Category.objects.visible_to(user).filter(archived=False)
        self.fields["category"].required = False
        # Transfers are created only on the Transfer form. On edit: show the flag
        # read-only when this row is paired; hide it otherwise so a normal spend
        # cannot be marked "transfer" (which would drop it from the budget).
        if self.instance.pk and self.instance.transfer_pair_id:
            self.fields["is_transfer"].disabled = True
            self.fields["is_transfer"].help_text = (
                "Both sides of a transfer stay linked. Delete and re-enter to undo.")
        else:
            self.fields.pop("is_transfer", None)

    def clean(self):
        data = super().clean()
        account, category = data.get("account"), data.get("category")
        if account and category and not category.same_scope(account):
            self.add_error("category", "Pick one of this account's categories "
                                       f"({account.space.name}'s set).")
        if self.instance.transfer_pair_id and account and account.pk != self.instance.account_id:
            self.add_error("account",
                           "A transfer's account can't change here - delete it and make a new transfer.")
        return data


class SplitRows:
    """The form's split rows: split_category / split_amount / split_note, as many as were posted."""

    def __init__(self, post, account, amount):
        self.rows, self.errors = [], []
        cats = list(post.getlist("split_category"))
        amounts = list(post.getlist("split_amount"))
        notes = list(post.getlist("split_note"))
        allowed = {c.pk: c for c in money.categories_for_account(account)} if account else {}
        for i, raw in enumerate(amounts):
            if not raw.strip():
                continue
            value = money.to_decimal(raw)
            if value is None:
                self.errors.append(f"Split {i + 1}: that amount isn't a number.")
                continue
            pk = int(cats[i]) if i < len(cats) and cats[i].isdigit() else None
            if pk is not None and pk not in allowed:
                self.errors.append(f"Split {i + 1}: pick one of this account's categories.")
                continue
            self.rows.append({"category": allowed.get(pk), "amount": value, "note": (notes[i] if i < len(notes) else "")[:200]})
        if len(self.rows) == 1:
            self.errors.append("A split needs at least two parts.")
        if self.rows and amount is not None and sum(r["amount"] for r in self.rows) != amount:
            self.errors.append(f"The splits add up to {sum(r['amount'] for r in self.rows)}, not {amount}.")


class TransferForm(forms.Form):
    from_account = forms.ModelChoiceField(queryset=Account.objects.none(), widget=forms.Select(attrs=INPUT), label="From")
    to_account = forms.ModelChoiceField(queryset=Account.objects.none(), widget=forms.Select(attrs=INPUT), label="To")
    amount = forms.DecimalField(max_digits=12, decimal_places=2, min_value=Decimal("0.01"), max_value=MAX_AMOUNT,
                                widget=forms.NumberInput(attrs=MONEY_INPUT))
    date = forms.DateField(widget=forms.DateInput(attrs={**INPUT, "type": "date"}, format="%Y-%m-%d"))
    note = forms.CharField(required=False, max_length=200, widget=forms.TextInput(attrs=INPUT))

    def __init__(self, *args, user, **kwargs):
        super().__init__(*args, **kwargs)
        accounts = Account.objects.visible_to(user).filter(archived=False)
        self.fields["from_account"].queryset = accounts
        self.fields["to_account"].queryset = accounts
        self.fields["date"].initial = timezone.localdate()

    def clean(self):
        data = super().clean()
        if data.get("from_account") and data.get("from_account") == data.get("to_account"):
            raise forms.ValidationError("Pick two different accounts.")
        return data


class CategoryForm(forms.ModelForm):
    class Meta:
        model = Category
        fields = ["name", "kind", "rollover", "archived"]
        widgets = {"name": forms.TextInput(attrs={**INPUT, "maxlength": 60}), "kind": forms.Select(attrs=INPUT)}
        labels = {"rollover": "Roll over unspent / overspent into next month"}


class RuleForm(forms.ModelForm):
    min_amount = forms.DecimalField(required=False, max_digits=12, decimal_places=2, min_value=Decimal("0"),
                                    widget=forms.NumberInput(attrs=MONEY_INPUT), label="From $")
    max_amount = forms.DecimalField(required=False, max_digits=12, decimal_places=2, min_value=Decimal("0"),
                                    widget=forms.NumberInput(attrs=MONEY_INPUT), label="To $")

    class Meta:
        model = Rule
        fields = ["payee_contains", "category", "min_amount", "max_amount", "account"]
        widgets = {"payee_contains": forms.TextInput(attrs={**INPUT, "maxlength": 100, "placeholder": "e.g. publix"}),
                   "category": forms.Select(attrs=INPUT), "account": forms.Select(attrs=INPUT)}
        labels = {"payee_contains": "Payee contains", "account": "Only for account"}

    def __init__(self, *args, owner, space, **kwargs):
        super().__init__(*args, **kwargs)
        scope = money.scope_filter(owner, space)
        self.fields["category"].queryset = money.ensure_categories(owner, space)
        self.fields["account"].queryset = Account.objects.filter(scope).filter(archived=False)
        self.fields["account"].required = False

    def clean(self):
        data = super().clean()
        low, high = data.get("min_amount"), data.get("max_amount")
        if low is not None and high is not None and low > high:
            self.add_error("max_amount", "The top of the range is below the bottom.")
        return data


class UploadForm(forms.Form):
    account = forms.ModelChoiceField(queryset=Account.objects.none(), widget=forms.Select(attrs=INPUT))
    file = forms.FileField(label="Bank CSV file", widget=forms.ClearableFileInput(attrs={"accept": ".csv,text/csv"}))

    def __init__(self, *args, user, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["account"].queryset = Account.objects.visible_to(user).filter(archived=False)


class MappingForm(forms.Form):
    """Which column is which - saved on the account for next time."""
    date = forms.ChoiceField(widget=forms.Select(attrs=INPUT))
    description = forms.ChoiceField(widget=forms.Select(attrs=INPUT))
    style = forms.ChoiceField(choices=[("amount", "One amount column"), ("split", "Separate debit and credit columns")],
                              widget=forms.RadioSelect, initial="amount")
    amount = forms.ChoiceField(required=False, widget=forms.Select(attrs=INPUT))
    negate = forms.BooleanField(required=False, label="Money out is positive in this file (flip the signs)")
    debit = forms.ChoiceField(required=False, widget=forms.Select(attrs=INPUT), label="Debit (money out)")
    credit = forms.ChoiceField(required=False, widget=forms.Select(attrs=INPUT), label="Credit (money in)")
    date_format = forms.ChoiceField(choices=imports.DATE_FORMATS, widget=forms.Select(attrs=INPUT))

    def __init__(self, *args, headers, **kwargs):
        super().__init__(*args, **kwargs)
        columns = [(h, h or f"(column {i + 1})") for i, h in enumerate(headers)]
        for name in ("date", "description"):
            self.fields[name].choices = columns
        for name in ("amount", "debit", "credit"):
            self.fields[name].choices = [("", "-")] + columns

    def clean(self):
        data = super().clean()
        if data.get("style") == "amount" and not data.get("amount"):
            self.add_error("amount", "Which column has the amount?")
        if data.get("style") == "split" and not (data.get("debit") and data.get("credit")):
            self.add_error("debit", "Pick both the debit and the credit column.")
        return data

    def mapping(self):
        d = self.cleaned_data
        out = {"date": d["date"], "description": d["description"], "date_format": d["date_format"]}
        if d["style"] == "amount":
            out.update(amount=d["amount"], negate=bool(d.get("negate")))
        else:
            out.update(debit=d["debit"], credit=d["credit"])
        return out
