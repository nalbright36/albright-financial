"""Task checklists (models.TaskChecklistItem) - so far made from Meals' grocery list ("Create task").

A row linked to a grocery line has no state of its own: it IS the line's checked box, read and written straight
through (GroceryItem.checked), so checking it in the task checks it on the grocery list and the other way round,
with nothing to keep in sync. What does follow a check, wherever it happens (the task, the grocery list, the Bot
API): a one-off task whose rows are all checked checks itself off, and unchecking a row reopens it - when its
owner has "Complete task when all items are checked" on (PlannerSettings, default on). signals.py runs that for
grocery lines; set_checked() for rows here.
"""
from django.db import transaction
from django.utils import timezone

from .models import ItemCompletion, PlannerSettings, ScheduleItem, TaskChecklistItem

GROCERY_TITLE = "Grocery run"


def rows_for(items):
    """{task pk: [its checklist rows, grocery line loaded]} for these items (or occurrences' items)."""
    ids = {getattr(i, "item", i).pk for i in items}
    out = {}
    for row in TaskChecklistItem.objects.filter(task_id__in=ids).select_related("grocery_item"):
        out.setdefault(row.task_id, []).append(row)
    return out


def progress(rows):
    """{"done", "total", "label"}: "3 of 7 bought" for a grocery checklist, "3 of 7 done" otherwise."""
    done = sum(1 for r in rows if r.checked)
    word = "bought" if rows and all(r.grocery_item_id for r in rows) else "done"
    return {"done": done, "total": len(rows), "label": f"{done} of {len(rows)} {word}"}


def attach(occurrences):
    """Give each occurrence with a checklist .checklist (its rows) and .checklist_progress."""
    by_task = rows_for(occurrences)
    for occ in occurrences:
        rows = by_task.get(occ.item.pk)
        if rows:
            occ.checklist, occ.checklist_progress = rows, progress(rows)
    return occurrences


def reevaluate(task):
    """Check the task off when every row is checked, and reopen it when one isn't - a one-off task, and only with
    its owner's "Complete task when all items are checked" on."""
    if task.recurrence or not task.checkable:
        return
    rows = list(task.checklist.select_related("grocery_item"))
    if not rows or not PlannerSettings.for_user(task.owner).complete_when_checklist_done:
        return
    all_done = all(r.checked for r in rows)
    done = ItemCompletion.objects.filter(item=task, occurrence_date=task.date)
    if all_done and not done.exists():
        ItemCompletion.objects.get_or_create(item=task, occurrence_date=task.date)
    elif not all_done and done.exists():
        done.delete()


def set_checked(row, checked, user):
    """Check or uncheck one row. A grocery row writes the grocery line itself (as checking it on the list does)."""
    line = row.grocery_item
    if line is not None:
        if line.checked != checked:
            line.checked = checked
            line.checked_by = user if checked else None
            line.checked_by_bot = False
            line.checked_at = timezone.now() if checked else None
            line.save()                      # signals.grocery_saved re-checks every task this line is in
        return
    if row.done != checked:
        row.done = checked
        row.save(update_fields=["done"])
    reevaluate(row.task)


def grocery_text(line):
    """"1.5 lb chicken thighs" - a grocery line as a checklist row."""
    from albright_meals.groceries import format_quantity
    amount = " ".join(part for part in (format_quantity(line.quantity), line.unit) if part)
    return f"{amount} {line.name}".strip()[:200]


@transaction.atomic
def task_from_groceries(user, lines, title, day, start_time=None, note=""):
    """A Planner task owned by `user`, with one checklist row per grocery line (in list order)."""
    task = ScheduleItem.objects.create(owner=user, title=(title or GROCERY_TITLE)[:200], date=day,
                                       start_time=start_time, notes=(note or "")[:1000], kind=ScheduleItem.TASK,
                                       checkable=True, created_by=user.get_username())
    TaskChecklistItem.objects.bulk_create([
        TaskChecklistItem(task=task, order=n, text=grocery_text(line), grocery_item=line)
        for n, line in enumerate(lines)])
    from .planner import apply_default_shares
    apply_default_shares(task)               # shared as the owner chose by default
    reevaluate(task)                         # already all bought: done from the start
    return task


def tasks_for_lines(lines, user):
    """{grocery line pk: [(task, its date)]} for the tasks this user can see that list these lines - the grocery
    list's "In task: Grocery run (Sat)" chips. Another person's private task is never shown."""
    visible = ScheduleItem.objects.visible_to(user).filter(archived=False)
    out = {}
    rows = TaskChecklistItem.objects.filter(grocery_item_id__in=[line.pk for line in lines], task__in=visible) \
        .select_related("task").order_by("task__date", "task_id")
    for row in rows:
        out.setdefault(row.grocery_item_id, []).append(row.task)
    return out
