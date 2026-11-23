"""Albright Money: accounts, transactions, categories and rules, monthly budgets - each owned by a space
(albright_labs.Space): someone's Personal space, or a circle's when the circle shares Money.

Who sees what (the managers' visible_to - every view goes through them; anything else is a 404): a space's rows show
to whoever albright_labs.circles says can see that space in Money (Personal: its person; a circle: its active
members who opted in to Money). A transaction: whoever can see its account. `owner` is who made the row.

Never stored: bank logins or passwords, full account or card numbers (last 4 digits only), uploaded files (CSV
imports are read in memory and dropped - imports.py). Every amount is a Decimal.
"""
from decimal import Decimal

from django.conf import settings
from django.core.validators import RegexValidator
from django.db import models

ZERO = Decimal("0.00")
MONEY = {"max_digits": 12, "decimal_places": 2}


class ScopedQuerySet(models.QuerySet):
    """Rows in the spaces a person can see in Money (albright_labs.circles), or in one space."""

    def visible_to(self, user):
        from albright_labs.circles import space_ids
        return self.filter(space_id__in=space_ids(user, "money"))

    def in_space(self, space):
        return self.filter(space=space) if space else self.none()


class Scoped(models.Model):
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")
    space = models.ForeignKey("albright_labs.Space", on_delete=models.CASCADE, related_name="+")

    objects = ScopedQuerySet.as_manager()

    class Meta:
        abstract = True

    @property
    def is_shared(self):
        """In a circle's space (not someone's Personal one)."""
        return self.space.circle_id is not None

    def same_scope(self, other):
        """Rows go together only within one space."""
        return self.space_id == other.space_id


class Account(Scoped):
    CHECKING, SAVINGS, CREDIT, CASH, OTHER = "checking", "savings", "credit", "cash", "other"
    KINDS = [(CHECKING, "Checking"), (SAVINGS, "Savings"), (CREDIT, "Credit card"), (CASH, "Cash"), (OTHER, "Other")]
    name = models.CharField(max_length=80)
    kind = models.CharField(max_length=10, choices=KINDS, default=CHECKING)
    institution = models.CharField(max_length=80, blank=True)
    # The last 4 digits only - never a full account or card number.
    last4 = models.CharField(max_length=4, blank=True, validators=[RegexValidator(r"^\d{4}$", "Exactly 4 digits.")],
                             verbose_name="Last 4 digits")
    # Signed like every amount: a credit card's balance owed is negative (the page shows it as "owed").
    starting_balance = models.DecimalField(default=ZERO, **MONEY)
    starting_date = models.DateField()
    archived = models.BooleanField(default=False)
    # The CSV column mapping, saved on the first import so the next one is a single step (imports.py).
    csv_mapping = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["archived", "name", "pk"]

    def __str__(self):
        return self.name + (f" ··{self.last4}" if self.last4 else "")

    @property
    def is_credit(self):
        return self.kind == self.CREDIT


class Category(Scoped):
    """A budget category - in a space's set. "income" categories are money
    in (planned vs received on the budget); everything else is spending."""
    EXPENSE, INCOME = "expense", "income"
    KINDS = [(EXPENSE, "Spending"), (INCOME, "Income")]
    name = models.CharField(max_length=60)
    kind = models.CharField(max_length=10, choices=KINDS, default=EXPENSE)
    # Unspent (or overspent) money carries into next month's budget for this category.
    rollover = models.BooleanField(default=False)
    order = models.PositiveIntegerField(default=0)
    archived = models.BooleanField(default=False)

    class Meta:
        ordering = ["archived", "order", "name", "pk"]
        verbose_name_plural = "categories"

    def __str__(self):
        return self.name

    @property
    def is_income(self):
        return self.kind == self.INCOME


class TransactionQuerySet(models.QuerySet):
    def visible_to(self, user):
        return self.filter(account__in=Account.objects.visible_to(user))


class Transaction(models.Model):
    """Positive amount = money in, negative = money out. A split transaction has no category of its own - its
    splits carry the categories. A transfer (between the user's own accounts) never counts as spending or income."""
    MANUAL, IMPORT, BOT = "manual", "import", "bot"
    SOURCES = [(MANUAL, "Entered"), (IMPORT, "Imported"), (BOT, "Bot")]
    account = models.ForeignKey(Account, on_delete=models.CASCADE, related_name="transactions")
    date = models.DateField()
    amount = models.DecimalField(**MONEY)
    payee = models.CharField(max_length=200, blank=True)
    category = models.ForeignKey(Category, on_delete=models.SET_NULL, null=True, blank=True, related_name="transactions")
    notes = models.TextField(blank=True, max_length=2000)
    tags = models.CharField(max_length=200, blank=True, help_text="Comma-separated.")
    entered_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name="+")
    source = models.CharField(max_length=10, choices=SOURCES, default=MANUAL)
    needs_review = models.BooleanField(default=False)
    is_transfer = models.BooleanField(default=False)
    # The other side of a transfer made on the Transfer form (out of one account, into another).
    transfer_pair = models.OneToOneField("self", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    objects = TransactionQuerySet.as_manager()

    class Meta:
        ordering = ["-date", "-pk"]
        indexes = [models.Index(fields=["account", "date"])]

    def __str__(self):
        return f"{self.date} {self.payee} {self.amount}"

    @property
    def tag_list(self):
        return [t.strip() for t in self.tags.split(",") if t.strip()]


class Split(models.Model):
    """Part of a split transaction: its own category and amount (the parts add up to the transaction)."""
    transaction = models.ForeignKey(Transaction, on_delete=models.CASCADE, related_name="splits")
    category = models.ForeignKey(Category, on_delete=models.SET_NULL, null=True, blank=True, related_name="splits")
    amount = models.DecimalField(**MONEY)
    note = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["pk"]


class Rule(Scoped):
    """Auto-categorizing: a payee containing `payee_contains` (any case), optionally within an amount range
    (compared as a positive size) and/or for one account, gets `category`. The longest match wins (rules.py)."""
    payee_contains = models.CharField(max_length=100)
    min_amount = models.DecimalField(null=True, blank=True, **MONEY)
    max_amount = models.DecimalField(null=True, blank=True, **MONEY)
    account = models.ForeignKey(Account, on_delete=models.CASCADE, null=True, blank=True, related_name="rules")
    category = models.ForeignKey(Category, on_delete=models.CASCADE, related_name="rules")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["payee_contains", "pk"]

    def __str__(self):
        return f"{self.payee_contains} → {self.category}"


class Budget(Scoped):
    """One month's budget for a space. month: its first day."""
    month = models.DateField()

    class Meta:
        constraints = [models.UniqueConstraint(fields=["space", "month"], name="money_one_budget_a_month")]


class BudgetLine(models.Model):
    budget = models.ForeignKey(Budget, on_delete=models.CASCADE, related_name="lines")
    category = models.ForeignKey(Category, on_delete=models.CASCADE, related_name="budget_lines")
    planned = models.DecimalField(default=ZERO, **MONEY)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["budget", "category"], name="money_one_line_per_category")]


class BudgetDraft(Scoped):
    """A budget plan: a named list of lines (people, trip costs, …) that can span months. "@budget Christmas gifts 300"
    on a note (albright_notes.smart) makes one in the writer's current Money space; the amount is an optional cap
    (`planned`), not a monthly category line. The plan lives under Money > Budget > Plans and is what the note's chip
    opens. It does not change a month's category budget unless someone reserves it (budget.confirm_draft / reserve_plan):
    then `line` points at that BudgetLine and `confirmed_at` is set, and the plan stays as the breakdown and the link
    back to the note. `template` is which starter lines were applied (Gifts & holiday, Trip, …)."""
    BLANK, GIFTS, TRIP, EVENT, HOME = "blank", "gifts", "trip", "event", "home"
    TEMPLATES = [
        (BLANK, "Start blank"),
        (GIFTS, "Gifts & holiday"),
        (TRIP, "Trip"),
        (EVENT, "Event / party"),
        (HOME, "Home project"),
    ]
    name = models.CharField(max_length=60)
    planned = models.DecimalField(null=True, blank=True, **MONEY)   # optional cap (e.g. the amount from the note)
    template = models.CharField(max_length=20, choices=TEMPLATES, default=BLANK)
    due_date = models.DateField(null=True, blank=True)
    note = models.ForeignKey("albright_notes.Note", on_delete=models.SET_NULL, null=True, blank=True,
                             related_name="budget_drafts")
    line = models.ForeignKey(BudgetLine, on_delete=models.SET_NULL, null=True, blank=True, related_name="drafts")
    created_at = models.DateTimeField(auto_now_add=True)
    confirmed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-pk"]

    def __str__(self):
        return self.name

    @property
    def is_draft(self):
        """Not yet reserved in a monthly category."""
        return self.confirmed_at is None

    @property
    def is_reserved(self):
        return self.confirmed_at is not None


class BudgetPlanLine(models.Model):
    """One row on a budget plan: a person, a trip cost, etc. `spent` is entered by hand (linking Money
    transactions is a later step). Status is To buy or Bought."""
    TO_BUY, BOUGHT = "to-buy", "bought"
    STATUSES = [(TO_BUY, "To buy"), (BOUGHT, "Bought")]
    plan = models.ForeignKey(BudgetDraft, on_delete=models.CASCADE, related_name="plan_lines")
    name = models.CharField(max_length=80)
    planned = models.DecimalField(default=ZERO, **MONEY)
    spent = models.DecimalField(default=ZERO, **MONEY)
    status = models.CharField(max_length=10, choices=STATUSES, default=TO_BUY)
    notes = models.CharField(max_length=200, blank=True)
    order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["order", "pk"]

    def __str__(self):
        return self.name
