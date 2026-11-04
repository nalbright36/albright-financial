"""Monthly budgets: per category, planned vs spent and what's left - with rollover - and the month's income
(planned vs received) and "left to budget" (planned income minus planned spending).

  available = planned + carried in (rollover categories only)
  remaining = available - spent
  carried in = last month's available - last month's spent (unspent adds, overspent takes away), counted from the
               scope's first budgeted month - months with no budget plan 0.
  bar: green under 90% of available, amber from 90% to 100%, red over.
  "left this month" (the dashboard's "safe to spend", the launcher and Home): the remaining across every
  spending category of the space's budget and the user's personal one, this month.
"""
from datetime import date, timedelta
from decimal import Decimal

from django.db.models.functions import TruncMonth
from django.db.models import Sum
from django.utils import timezone

from . import money
from .models import ZERO, Budget, BudgetLine, Split, Transaction

NEAR = Decimal("0.9")


def month_start(day):
    return day.replace(day=1)


def add_months(day, n):
    m = day.month - 1 + n
    return date(day.year + m // 12, m % 12 + 1, 1)


def month_end(month):
    return add_months(month, 1) - timedelta(days=1)


def this_month():
    return month_start(timezone.localdate())


def get_budget(owner, space, month):
    return Budget.objects.filter(money.scope_filter(owner, space), month=month).first()


def _planned_by_month(owner, space, first, last):
    """{(month, category pk): planned} for the scope's budgets from first to last."""
    lines = BudgetLine.objects.filter(budget__in=Budget.objects.filter(money.scope_filter(owner, space),
                                                                       month__gte=first, month__lte=last))
    return {(line.budget.month, line.category_id): line.planned for line in lines.select_related("budget")}


def _net_by_month(categories, first, last_day):
    """{(month, category pk): net amount} - whole transactions and splits, no transfers."""
    ids = [c.pk for c in categories]
    out = {}
    if not ids:
        return out
    whole = Transaction.objects.filter(is_transfer=False, category_id__in=ids, date__gte=first, date__lte=last_day) \
        .exclude(splits__isnull=False).annotate(m=TruncMonth("date")).values_list("m", "category") \
        .annotate(total=Sum("amount"))
    parts = Split.objects.filter(transaction__is_transfer=False, category_id__in=ids, transaction__date__gte=first,
                                 transaction__date__lte=last_day) \
        .annotate(m=TruncMonth("transaction__date")).values_list("m", "category").annotate(total=Sum("amount"))
    for m, pk, total in list(whole) + list(parts):
        key = (m.date() if hasattr(m, "date") else m, pk)
        out[key] = out.get(key, ZERO) + (total or ZERO)
    return out


def status(spent, available):
    """"under" (green), "near" (amber, 90-100%), "over" (red)."""
    if spent > available:
        return "over"
    if available > 0 and spent >= available * NEAR:
        return "near"
    return "under"


def bar(spent, available):
    """The progress bar's width (0-100)."""
    if spent <= 0:
        return 0
    if spent >= available:
        return 100
    return int(spent / available * 100)


def month_view(owner, space, month):
    """The budget page's numbers for one scope and month."""
    categories = list(money.ensure_categories(owner, space))
    budget = get_budget(owner, space, month)
    first_budget = Budget.objects.filter(money.scope_filter(owner, space)).order_by("month") \
        .values_list("month", flat=True).first()
    start = min(first_budget, month) if first_budget else month
    planned = _planned_by_month(owner, space, start, month)
    net = _net_by_month(categories, start, month_end(month))
    rows, income = [], []
    for c in categories:
        this_planned = planned.get((month, c.pk), ZERO)
        this_spent = money.spent(c, net.get((month, c.pk), ZERO))
        if c.is_income:
            income.append({"category": c, "planned": this_planned, "received": this_spent})
            continue
        carry = ZERO
        if c.rollover:
            m = start
            while m < month:
                carry = planned.get((m, c.pk), ZERO) + carry - money.spent(c, net.get((m, c.pk), ZERO))
                m = add_months(m, 1)
        available = this_planned + carry
        remaining = available - this_spent
        rows.append({"category": c, "planned": this_planned, "carry": carry, "available": available,
                     "spent": this_spent, "remaining": remaining, "status": status(this_spent, available),
                     "pct": bar(this_spent, available)})
    income_planned = sum((r["planned"] for r in income), ZERO)
    planned_spending = sum((r["planned"] for r in rows), ZERO)
    return {
        "month": month, "budget": budget, "rows": rows, "income": income,
        "income_planned": income_planned, "income_received": sum((r["received"] for r in income), ZERO),
        "planned_spending": planned_spending, "left_to_budget": income_planned - planned_spending,
        "available": sum((r["available"] for r in rows), ZERO), "spent": sum((r["spent"] for r in rows), ZERO),
        "remaining": sum((r["remaining"] for r in rows), ZERO),
    }


def save_plan(owner, space, month, planned):
    """Store the month's plan: {category pk: Decimal}. Returns the budget."""
    budget, _ = Budget.objects.get_or_create(space=space, month=month, defaults={"owner": owner})
    allowed = {c.pk for c in money.ensure_categories(owner, space)}
    for pk, amount in planned.items():
        if pk in allowed:
            BudgetLine.objects.update_or_create(budget=budget, category_id=pk, defaults={"planned": amount})
    return budget


def copy_last_month(owner, space, month):
    """Copy last month's plan into this month (categories already planned this month keep theirs). Returns how
    many lines were copied, or None if last month has no budget."""
    last = get_budget(owner, space, add_months(month, -1))
    if last is None:
        return None
    current = {line.category_id for line in BudgetLine.objects.filter(budget=get_budget(owner, space, month))}
    plan = {line.category_id: line.planned for line in last.lines.all() if line.category_id not in current}
    save_plan(owner, space, month, plan)
    return len(plan)


def budgets_for(user, month):
    """[(label, space, month_view)] for the spaces with a budget this month."""
    out = []
    for label, space in money.scopes(user):
        if get_budget(user, space, month):
            out.append((label, space, month_view(user, space, month)))
    return out


def left_this_month(user):
    """What's left this month across the spending categories of the user's budgets (space and personal), or
    None when there's no budget this month."""
    views = budgets_for(user, this_month())
    if not views:
        return None
    return sum((v["remaining"] for _label, _space, v in views), ZERO)
