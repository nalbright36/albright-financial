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

from django.urls import reverse

from . import money
from .models import ZERO, Budget, BudgetDraft, BudgetLine, BudgetPlanLine, Category, Split, Transaction

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
    before = dict(BudgetLine.objects.filter(budget=budget).values_list("category_id", "planned"))
    changed = 0
    for pk, amount in planned.items():
        if pk in allowed:
            BudgetLine.objects.update_or_create(budget=budget, category_id=pk, defaults={"planned": amount})
            changed += before.get(pk) != amount and not (pk not in before and amount == 0)
    if changed:
        money.record_activity(space, "budget", f"updated the {month:%B} budget", group=f"budget:{month:%Y-%m}",
                              url=reverse("albright_money:budget") + f"?month={month:%Y-%m}&space={space.pk}")
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


# ---------------------------------------------------------------------------
# Drafts from notes ("@budget Christmas gifts 300" - albright_notes.smart)
# ---------------------------------------------------------------------------

def split_amount(text):
    """ "Christmas gifts 300" -> ("Christmas gifts", Decimal("300.00")); no amount -> (text, None). The amount is the
    first or last word, as in quick add ("$300", "1,200.50")."""
    from .quickadd import AMOUNT
    words = (text or "").split()
    raw = None
    if words and AMOUNT.match(words[-1]):
        raw, words = words[-1], words[:-1]
    elif words and AMOUNT.match(words[0]):
        raw, words = words[0], words[1:]
    amount = money.to_decimal(raw.lstrip("+-")) if raw else None
    name = " ".join(words).strip(" -–:,") or "Budget item"
    return name[:60], (amount if amount and amount > 0 else None)


def create_draft(user, text, note=None):
    """A budget plan from a note's line, in the writer's current Money space. The amount (if any) is the cap.
    Starts blank (no lines) so the person can pick a template or add lines. Returns the plan."""
    space = money.current_space(user)
    name, planned = split_amount(text)
    return BudgetDraft.objects.create(owner=user, space=space, name=name, planned=planned, note=note,
                                      template=BudgetDraft.BLANK)


def open_drafts(space):
    """The space's drafts not yet finished, newest first."""
    return BudgetDraft.objects.filter(space=space, confirmed_at__isnull=True).select_related("note")


def confirm_draft(draft, user, month, planned, category=None, new_name=""):
    """Finish a draft: its amount goes on `month`'s budget for `category` (or a new spending category named
    new_name / the draft's name) - added to what that category already plans. Returns the BudgetLine."""
    space = draft.space
    if category is None:
        name = (new_name or draft.name).strip()[:60] or draft.name
        category = Category.objects.filter(space=space, name__iexact=name, archived=False).first()
        if category is None:
            category = Category.objects.create(owner=user, space=space, name=name, kind=Category.EXPENSE,
                                               order=Category.objects.filter(space=space).count())
    budget, _ = Budget.objects.get_or_create(space=space, month=month, defaults={"owner": user})
    line, _ = BudgetLine.objects.get_or_create(budget=budget, category=category)
    line.planned = (line.planned or ZERO) + planned
    line.save(update_fields=["planned"])
    draft.line, draft.planned, draft.confirmed_at = line, planned, timezone.now()
    draft.save(update_fields=["line", "planned", "confirmed_at"])
    money.record_activity(space, "budget", f"updated the {month:%B} budget", group=f"budget:{month:%Y-%m}",
                          url=reverse("albright_money:budget") + f"?month={month:%Y-%m}&space={space.pk}")
    return line


def notes_for_lines(user, budget):
    """{category pk: [(note pk, title)]} - the notes this month's budget lines came from (only notes `user` can
    open), for the "From note" links on the Budget page."""
    if budget is None:
        return {}
    from albright_notes.models import Note
    drafts = BudgetDraft.objects.filter(line__budget=budget, note__isnull=False).select_related("line")
    visible = dict(Note.objects.visible_to(user).filter(pk__in=[d.note_id for d in drafts]).values_list("pk", "title"))
    out = {}
    for d in drafts:
        if d.note_id in visible and (d.note_id, visible[d.note_id]) not in out.get(d.line.category_id, []):
            out.setdefault(d.line.category_id, []).append((d.note_id, visible[d.note_id]))
    return out


# ---------------------------------------------------------------------------
# Plans (BudgetDraft + BudgetPlanLine): templates, listing, totals, the plan URL
# ---------------------------------------------------------------------------

TEMPLATE_LINES = {
    BudgetDraft.BLANK: [],
    BudgetDraft.GIFTS: ["Person or event"],
    BudgetDraft.TRIP: ["Flights", "Hotel", "Food", "Activities", "Transport", "Other"],
    BudgetDraft.EVENT: ["Venue", "Food", "Drinks", "Decor", "Favors", "Other"],
    BudgetDraft.HOME: ["Materials", "Labor", "Permits", "Furniture", "Other"],
}


def plan_url(plan):
    """The plan page, in its own space."""
    return reverse("albright_money:budget_plan", args=[plan.pk]) + f"?space={plan.space_id}"


def plans_for(space):
    """The space's plans, newest first, with their note and any reserved monthly line."""
    return BudgetDraft.objects.filter(space=space).select_related("note", "line__category", "line__budget")


def create_plan(user, name, template=BudgetDraft.BLANK, planned=None, due_date=None, note=None, space=None):
    """A new plan in `space` (or the writer's current Money space), with starter lines from `template`."""
    space = space or money.current_space(user)
    key = template if template in dict(BudgetDraft.TEMPLATES) else BudgetDraft.BLANK
    plan = BudgetDraft.objects.create(
        owner=user, space=space, name=(name or "Budget plan").strip()[:60] or "Budget plan",
        planned=planned, template=key, due_date=due_date, note=note)
    apply_template(plan, key, replace=True)
    return plan


def apply_template(plan, template, replace=False):
    """Set `plan.template` and add its starter lines (name only, planned $0, To buy). If the plan already has
    lines, does nothing unless `replace` is True and no line has spent money — then the empty lines are replaced.
    Returns the number of lines added, or None if it was left alone."""
    key = template if template in TEMPLATE_LINES else BudgetDraft.BLANK
    existing = list(plan.plan_lines.all())
    if existing and not replace:
        return None
    if existing and any(line.spent and line.spent > 0 for line in existing):
        return None
    if existing:
        plan.plan_lines.all().delete()
    names = TEMPLATE_LINES[key]
    lines = [BudgetPlanLine(plan=plan, name=name, order=i) for i, name in enumerate(names)]
    if lines:
        BudgetPlanLine.objects.bulk_create(lines)
    if plan.template != key:
        plan.template = key
        plan.save(update_fields=["template"])
    return len(lines)


def plan_totals(plan):
    """Summary numbers for a plan: planned and spent (sum of lines), remaining, % used, bought / to-buy counts,
    days left until due_date, and a weekly pace (remaining / weeks left) when there is a future due date."""
    lines = list(plan.plan_lines.all())
    planned = sum((line.planned or ZERO) for line in lines) if lines else ZERO
    spent = sum((line.spent or ZERO) for line in lines) if lines else ZERO
    remaining = planned - spent
    pct = int(spent / planned * 100) if planned > 0 else (100 if spent > 0 else 0)
    bought = sum(1 for line in lines if line.status == BudgetPlanLine.BOUGHT)
    to_buy = len(lines) - bought
    today = timezone.localdate()
    days_left = (plan.due_date - today).days if plan.due_date else None
    pace = None
    if days_left is not None and days_left > 0 and remaining > 0:
        weeks = Decimal(days_left) / Decimal(7)
        pace = (remaining / weeks).quantize(Decimal("0.01"))
    cap = plan.planned
    cap_over = (planned - cap) if cap and planned > cap else None
    return {
        "lines": lines, "planned": planned, "spent": spent, "remaining": remaining, "pct": min(pct, 100),
        "bought": bought, "to_buy": to_buy, "count": len(lines), "days_left": days_left, "pace": pace,
        "cap": cap, "cap_over": cap_over, "over": remaining < 0,
    }


def visible_plan(user, pk):
    """The plan `user` can see in Money, or None (callers 404)."""
    return BudgetDraft.objects.visible_to(user).filter(pk=pk).select_related(
        "note", "line__category", "line__budget", "space").first()
