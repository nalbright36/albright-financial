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
from .models import ZERO, Budget, BudgetDraft, BudgetLine, BudgetPlanItem, BudgetPlanLine, Category, Split, Transaction

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


def _space_already_used_on_note(user, note):
    """If this note already links plans and they all live in one Money space the writer can still see,
    return that space (keep later @budget lines with the earlier ones). Otherwise None."""
    if note is None or not getattr(note, "pk", None):
        return None
    ids = list(BudgetDraft.objects.visible_to(user).filter(note=note).values_list("space_id", flat=True).distinct())
    if len(ids) != 1:
        return None
    from albright_labs.circles import default_create_space
    return default_create_space(user, "money", inherit=ids[0])


def create_draft(user, text, note=None, space=None):
    """A budget plan from a note's line. The amount (if any) is the cap. Starts blank (no lines) so the
    person can pick a template or add lines. Lives in `space` when given, else in the same space as this
    note's existing plans when they're all in one space the writer can see, else Personal — not the
    last-picked Money switcher (that was putting @budget items in a circle). Returns the plan."""
    from albright_labs.circles import default_create_space, personal_space
    space = space or _space_already_used_on_note(user, note) or default_create_space(user, "money") or personal_space(user)
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
    """A new plan in `space` (when given), else in the same space as `note`'s existing plans when they all
    live in one space the writer can see, else Personal. With starter lines from `template`."""
    from albright_labs.circles import default_create_space, personal_space
    space = space or _space_already_used_on_note(user, note) or default_create_space(user, "money") or personal_space(user)
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
    if existing and any((line.spent and line.spent > 0) or line.items.exists() for line in existing):
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



def parse_line_text(text):
    """ "Mom 50", "Mom $50", or "Mom: scarf 30" -> (name, amount, label), or None when there is no
    amount or no name. The amount is the first or last word ($ and commas allowed). A colon splits
    the person or event from the item's optional label."""
    from .quickadd import AMOUNT
    words = " ".join((text or "").split()).split()
    if not words:
        return None
    raw = None
    if AMOUNT.match(words[-1]):
        raw, words = words[-1], words[:-1]
    elif AMOUNT.match(words[0]):
        raw, words = words[0], words[1:]
    amount = money.to_decimal(raw.lstrip("+-")) if raw else None
    if amount is None or amount <= 0:
        return None
    rest = " ".join(words).strip(" -–")
    if not rest:
        return None
    if ":" in rest:
        name, _, label = rest.partition(":")
        name, label = name.strip(" -–"), label.strip(" -–")
    else:
        name, label = rest, ""
    name = name[:80]
    if not name:
        return None
    return name, amount, (label[:80] if label else "")


def find_group(plan, name):
    """The plan line whose name matches `name` case-insensitively, or None."""
    key = (name or "").strip().lower()
    if not key:
        return None
    return next((line for line in plan.plan_lines.all() if line.name.lower() == key), None)


def _next_order(qs):
    return (qs.order_by("-order").values_list("order", flat=True).first() or 0) + 1


def _items(line):
    """The line's items from the database (not a stale prefetch cache)."""
    return list(BudgetPlanItem.objects.filter(line=line).order_by("order", "pk"))


def spent_of(line):
    """The group's spent: the sum of its items, or the stored spent when it has none (older rows)."""
    items = _items(line)
    if items:
        return sum((item.amount or ZERO) for item in items)
    return line.spent or ZERO


def sync_spent(line):
    """Write the sum of items onto line.spent (0 when there are no items). Returns the new spent."""
    items = _items(line)
    spent = sum((item.amount or ZERO) for item in items) if items else ZERO
    if line.spent != spent:
        line.spent = spent
        line.save(update_fields=["spent"])
    return spent


def add_item(plan, name, amount, label=""):
    """Add an item under the group named `name` (case-insensitive), creating the group if needed.
    Planned follows Spent while line.planned_auto is True (a new group's first item sets both;
    editing Planned in Money turns planned_auto off). Returns (line, item)."""
    amount = amount if amount is not None else ZERO
    if amount < 0:
        raise ValueError("amount must be 0 or more")
    line = find_group(plan, name)
    if line is None:
        line = BudgetPlanLine.objects.create(
            plan=plan, name=(name or "").strip()[:80] or "Person or event",
            planned=ZERO, spent=ZERO, planned_auto=True, order=_next_order(plan.plan_lines))
    item = BudgetPlanItem.objects.create(
        line=line, label=(label or "").strip()[:80], amount=amount, order=_next_order(line.items))
    spent = sync_spent(line)
    if line.planned_auto:
        line.planned = spent
        line.save(update_fields=["planned"])
    return line, item


def add_item_from_text(plan, text):
    """Parse a Notes line ('Mom 50' / 'Mom: scarf 30') and add it. Returns (line, item), or None if the text
    has no name or amount."""
    parsed = parse_line_text(text)
    if not parsed:
        return None
    name, amount, label = parsed
    return add_item(plan, name, amount, label)


def update_item(item, amount=None, label=None):
    """Change an item's amount and/or label. Planned follows Spent while planned_auto is True."""
    line = item.line
    fields = []
    if amount is not None:
        if amount < 0:
            raise ValueError("amount must be 0 or more")
        item.amount = amount
        fields.append("amount")
    if label is not None:
        item.label = (label or "").strip()[:80]
        fields.append("label")
    if fields:
        item.save(update_fields=fields)
    spent = sync_spent(line)
    if line.planned_auto:
        line.planned = spent
        line.save(update_fields=["planned"])
    return item


def delete_item(item):
    """Remove an item and resync its group's spent (and planned, when planned_auto is still on). Returns the line."""
    line = item.line
    item.delete()
    spent = sync_spent(line)
    if line.planned_auto:
        line.planned = spent
        line.save(update_fields=["planned"])
    return line


def set_spent(line, spent):
    """Set a group's spent when the form still posts a single spent field: create or update one unlabeled
    item if the group has no items or only one unlabeled item; leave a multi-item group alone and return
    the sum of its items."""
    spent = spent or ZERO
    if spent < 0:
        raise ValueError("amount must be 0 or more")
    items = _items(line)
    if len(items) > 1 or (len(items) == 1 and items[0].label):
        return sync_spent(line)
    if spent == ZERO:
        if items:
            items[0].delete()
        sync_spent(line)
    elif items:
        items[0].amount = spent
        items[0].save(update_fields=["amount"])
        sync_spent(line)
    else:
        BudgetPlanItem.objects.create(line=line, amount=spent, label="", order=0)
        sync_spent(line)
    # Custom spent from the Money form is an explicit amount, not a request to retarget Planned.
    return line.spent


def plan_payload(plan):
    """JSON for the Notes budget block and the Notes API: groups with items, plus spent / planned totals.
    `cap` is the plan's optional overall cap (BudgetDraft.planned), not the sum of group planned amounts."""
    totals = plan_totals(plan)
    groups = []
    for row in totals["rows"]:
        line = row["line"]
        items = [{"id": item.pk, "label": item.label, "amount": str(item.amount)} for item in row["items"]]
        groups.append({
            "id": line.pk, "name": line.name, "planned": str(line.planned or ZERO),
            "spent": str(row["spent"]), "left": str(row["left"]), "status": row["status"],
            "item_count": len(items), "items": items,
        })
    return {
        "id": plan.pk, "name": plan.name, "url": plan_url(plan),
        "groups": groups, "spent": str(totals["spent"]), "group_planned": str(totals["planned"]),
        "remaining": str(totals["remaining"]), "count": totals["count"],
        "item_count": sum(g["item_count"] for g in groups),
        "cap": str(totals["cap"]) if totals["cap"] is not None else None,
    }


def plan_totals(plan):
    """Summary numbers for a plan: planned and spent (sum of groups; spent is the sum of each group's items,
    falling back to the line's spent when it has no items yet), remaining, % used, bought / to-buy counts,
    days left until due_date, and a weekly pace (remaining / weeks left) when there is a future due date."""
    lines = list(plan.plan_lines.prefetch_related("items").all())
    planned = sum((line.planned or ZERO) for line in lines) if lines else ZERO
    spent = sum((spent_of(line) for line in lines), ZERO) if lines else ZERO
    remaining = planned - spent
    pct = int(spent / planned * 100) if planned > 0 else (100 if spent > 0 else 0)
    bought = sum(1 for line in lines if line.status == BudgetPlanLine.BOUGHT)
    to_buy = len(lines) - bought
    today = timezone.localdate()
    days_left = (plan.due_date - today).days if plan.due_date else None
    pace = None
    if days_left is not None and days_left > 0 and remaining > 0:
        weeks = Decimal(days_left) / Decimal(7)
        pace = (remaining / weeks).quantize(Decimal("1"))
    cap = plan.planned
    cap_over = (planned - cap) if cap and planned > cap else None
    rows = []
    for line in lines:
        line_spent = spent_of(line)
        leftover = (line.planned or ZERO) - line_spent
        if line_spent > (line.planned or ZERO):
            st = "over"
        elif line.status == BudgetPlanLine.BOUGHT:
            st = "under"
        elif line.planned > 0 and line_spent >= line.planned * NEAR:
            st = "near"
        else:
            st = "under"
        lpct = 0 if line.planned <= 0 else min(100, int(line_spent / line.planned * 100))
        if line.planned <= 0 and line_spent > 0:
            lpct = 100
        rows.append({"line": line, "spent": line_spent, "items": list(line.items.all()),
                     "left": leftover, "status": st, "pct": lpct})
    return {
        "lines": lines, "rows": rows, "planned": planned, "spent": spent, "remaining": remaining,
        "pct": min(pct, 100), "bought": bought, "to_buy": to_buy, "count": len(lines),
        "days_left": days_left, "pace": pace, "cap": cap, "cap_over": cap_over, "over": remaining < 0,
    }


def reserve_plan(plan, user, month, planned, category=None, new_name=""):
    """Put `planned` on `month`'s budget for `category` (or a new one). The plan keeps its lines and its cap;
    `confirmed_at` / `line` mark it reserved. Returns the BudgetLine."""
    cap = plan.planned
    line = confirm_draft(plan, user, month, planned, category=category, new_name=new_name)
    plan.refresh_from_db()
    if plan.planned != cap:
        plan.planned = cap
        plan.save(update_fields=["planned"])
    return line


def visible_plan(user, pk):
    """The plan `user` can see in Money, or None (callers 404)."""
    return BudgetDraft.objects.visible_to(user).filter(pk=pk).select_related(
        "note", "line__category", "line__budget", "space").first()
