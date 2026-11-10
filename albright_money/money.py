"""Albright Money's core: the spaces a person sees, each space's categories (with the default set), rules,
balances, and spending / income totals (splits counted by their own categories, transfers never).

Categories, rules and budgets live in a space (someone's Personal space or a circle's - albright_labs.circles decides
who sees which); an account's transactions use its space's categories. Functions still take (owner, space): the
space is what counts; the owner is who new rows are made by.
"""
import re
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db.models import Q, Sum

from .models import ZERO, Account, Category, Rule, Split, Transaction

APP = "money"

DEFAULT_CATEGORIES = ("Housing", "Utilities", "Groceries", "Dining", "Transportation", "Health", "Insurance",
                      "Subscriptions", "Shopping", "Entertainment", "Travel", "Gifts", "Pets", "Personal care",
                      "Income", "Savings", "Other")


# ---------------------------------------------------------------------------
# People and spaces
# ---------------------------------------------------------------------------

def money_users():
    """Everyone who can open Money (Admin > Users) - who can be in a space."""
    return get_user_model().objects.filter(is_active=True).filter(
        Q(is_superuser=True) | Q(labs_preference__allowed_apps__contains=["money"])).order_by("username")


def can_open(user):
    return money_users().filter(pk=user.pk).exists()


def person(user):
    return (user.first_name or user.get_username()).strip() if user else ""


def members(space):
    """Who sees this space in Money."""
    from albright_labs.circles import space_members
    return list(space_members(space, APP)) if space else []


def record_activity(space, verb, summary, actor=None, group="", url="", obj=None):
    """Money activity - only in a circle's space (budget changes, imports, big transactions); a Personal space never
    records anything. obj (a transaction or account) is stored on the event so forget_activity can remove it."""
    if space is None or not space.circle_id:
        return None
    from albright_labs import activity
    return activity.record_activity(APP, verb, summary, obj=obj, audience=activity.CIRCLE, circle=space.circle,
                                    actor=actor, group=group, url=url)


def forget_activity(*objs):
    """Drop Money events about these transactions / accounts (deleted, or moved out of the circle's space) so circle
    members stop seeing their payee and amount at once - not just when the event ages out."""
    from django.contrib.contenttypes.models import ContentType
    from albright_labs.models import ActivityEvent
    for obj in objs:
        if obj is not None and obj.pk:
            ActivityEvent.objects.filter(app=APP, content_type=ContentType.objects.get_for_model(obj),
                                         object_id=obj.pk).delete()


def forget_account_activity(account):
    """The account and every transaction in it."""
    from django.contrib.contenttypes.models import ContentType
    from albright_labs.models import ActivityEvent
    ActivityEvent.objects.filter(app=APP, content_type=ContentType.objects.get_for_model(Transaction),
                                 object_id__in=list(account.transactions.values_list("pk", flat=True))).delete()
    forget_activity(account)


def record_big_transaction(t, actor=None):
    """One entered transaction at or over its circle's threshold (Circle.money_threshold, default $100), in or out.
    Transfers and smaller ones never show; imports show as one "imported N transactions" event instead."""
    space = t.account.space
    if t.is_transfer or not space.circle_id or abs(t.amount) < space.circle.money_threshold:
        return None
    from django.urls import reverse
    word = "received" if t.amount > 0 else "spent"
    return record_activity(space, "transaction", f"{word} ${abs(t.amount):,.2f}" + (f" – {t.payee}" if t.payee else ""),
                           actor=actor, url=reverse("albright_money:transaction", args=[t.pk]), obj=t)


def visible_spaces(user):
    from albright_labs.circles import visible_spaces as spaces
    return spaces(user, APP)


def current_space(user, requested=None):
    from albright_labs.circles import current_space as current
    return current(user, APP, requested)


# ---------------------------------------------------------------------------
# Scopes and categories
# ---------------------------------------------------------------------------

def scope_filter(owner, space):
    """Rows in this space (owner is unused - kept so callers read the same)."""
    return Q(space=space)


def ensure_categories(owner, space=None):
    """The space's categories - the default set the first time it has none. No space: the owner's Personal one."""
    if space is None:
        from albright_labs.circles import personal_space
        space = personal_space(owner)
    qs = Category.objects.filter(scope_filter(owner, space))
    if not qs.exists():
        Category.objects.bulk_create([
            Category(owner=owner, space=space, name=name, order=i,
                     kind=Category.INCOME if name == "Income" else Category.EXPENSE)
            for i, name in enumerate(DEFAULT_CATEGORIES)])
    return qs.filter(archived=False)


def categories_for_account(account):
    """The categories a transaction on this account can use: the account's own scope's."""
    return ensure_categories(account.owner, account.space)


def scopes(user):
    """[(label, space)] for every space the user sees in Money - Personal first."""
    return [(s.name, s) for s in visible_spaces(user)]


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------

def norm(text):
    """Lower case, punctuation as spaces: "SQ *BLUE BOTTLE" -> "sq blue bottle", "NETFLIX.COM" -> "netflix com"."""
    return " ".join(re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).split())


def rule_key(payee):
    """What "Always categorize <payee>" matches on: the payee's words before the first store number or reference
    code ("PUBLIX #1234 TAMPA FL" -> "publix"; "SQ *BLUE BOTTLE 2231" -> "sq blue bottle"), so another branch
    or another day matches too."""
    words = []
    for word in norm(payee).split():
        if any(ch.isdigit() for ch in word):
            break
        words.append(word)
    return " ".join(words[:4]) or norm(payee)[:100]


def rule_for(transaction, rules=None):
    """The rule that categorizes this transaction, or None: its scope's rules whose text is in the payee (any
    case), within the amount range (by size) and for the account if they name one - the longest text wins."""
    account = transaction.account
    if rules is None:
        rules = Rule.objects.filter(scope_filter(account.owner, account.space)).select_related("category")
    payee, size = norm(transaction.payee), abs(transaction.amount)
    best = None
    for rule in rules:
        key = norm(rule.payee_contains)
        if not key or key not in payee or rule.category.archived:
            continue
        if rule.account_id and rule.account_id != account.pk:
            continue
        if rule.min_amount is not None and size < rule.min_amount:
            continue
        if rule.max_amount is not None and size > rule.max_amount:
            continue
        if best is None or (len(key), rule.pk) > (len(norm(best.payee_contains)), best.pk):
            best = rule
    return best


def categorize(transaction, rules=None):
    """Give an uncategorized transaction its rule's category. Still uncategorized: "needs review"."""
    if transaction.category_id is None and not transaction.is_transfer:
        rule = rule_for(transaction, rules)
        if rule:
            transaction.category = rule.category
    transaction.needs_review = transaction.category_id is None and not transaction.is_transfer
    return transaction


def create_rule_from(transaction, category, user):
    """"Always categorize <payee> as <category>": a rule in the account's scope, and the same category for the
    scope's other uncategorized (or needs-review) transactions it matches. Returns (rule, how many others)."""
    account = transaction.account
    rule, _created = Rule.objects.get_or_create(
        owner=user, space=account.space,
        payee_contains=rule_key(transaction.payee), account=None, min_amount=None, max_amount=None,
        defaults={"category": category})
    if rule.category_id != category.pk:
        rule.category = category
        rule.save(update_fields=["category"])
    accounts = Account.objects.filter(scope_filter(account.owner, account.space))
    others = Transaction.objects.filter(account__in=accounts, is_transfer=False).filter(
        Q(category__isnull=True) | Q(needs_review=True)).exclude(pk=transaction.pk).exclude(splits__isnull=False)
    changed = 0
    for other in others.select_related("account"):
        if rule_for(other, [rule]):
            other.category, other.needs_review = category, False
            other.save(update_fields=["category", "needs_review"])
            changed += 1
    return rule, changed


# ---------------------------------------------------------------------------
# Balances and totals
# ---------------------------------------------------------------------------

def balances(accounts):
    """{account pk: current balance}."""
    return {a.pk: balance(a) for a in accounts}


def _since(account):
    return Transaction.objects.filter(account=account, date__gte=account.starting_date)         .aggregate(total=Sum("amount"))["total"] or ZERO


def balance(account):
    """Its starting balance plus every transaction from its starting date on."""
    return account.starting_balance + _since(account)


def shown_balance(account, value):
    """(label, amount) for the page: a credit card shows what's owed (a positive number), else the balance."""
    if account.is_credit:
        return ("Owed", -value) if value <= 0 else ("Credit", value)
    return ("Balance", value)


def category_totals(categories, start, end):
    """{category pk: net amount} between start and end (inclusive): each transaction in its category, or a split
    transaction's parts in theirs - transfers left out. Negative = money out."""
    ids = [c.pk for c in categories]
    out = {}
    if not ids:
        return out
    whole = Transaction.objects.filter(is_transfer=False, category_id__in=ids, date__gte=start, date__lte=end) \
        .exclude(splits__isnull=False).values_list("category").annotate(total=Sum("amount"))
    parts = Split.objects.filter(transaction__is_transfer=False, category_id__in=ids, transaction__date__gte=start,
                                 transaction__date__lte=end).values_list("category").annotate(total=Sum("amount"))
    for pk, total in list(whole) + list(parts):
        out[pk] = out.get(pk, ZERO) + (total or ZERO)
    return out


def spent(category, net):
    """Spending in a spending category = money out (refunds bring it down); income = money in."""
    return net if category.is_income else -net


def to_decimal(value):
    try:
        return Decimal(str(value).replace(",", "").replace("$", "").strip()).quantize(Decimal("0.01"))
    except Exception:  # noqa: BLE001 - any bad input is just "no amount"
        return None
