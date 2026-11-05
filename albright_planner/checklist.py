"""Task checklists (subtasks): models.TaskChecklistItem is a subtask of a task; whether it's ticked is per
occurrence (models.ChecklistItemCompletion), so each occurrence of a repeating task starts with every subtask
open, and a one-off task has just its one occurrence.

A row linked to a grocery line (a grocery run made from Meals' grocery list) has no ticks of its own: it IS the
line's checked box, read and written straight through (GroceryItem.checked), so checking it in the task checks it
on the grocery list and the other way round.

Editing a repeating task's checklist applies from its current occurrence on (edit_from): a new subtask starts
there (starts_on), a removed one ends there (ends_on), so earlier occurrences keep the list they had - and a tick
keeps the subtask's text as it read then, so the history survives a rename or delete.

What follows a tick, wherever it happens (the task, the grocery list, the Bot API): an occurrence whose subtasks
are all ticked checks itself off, and unticking one reopens it - when the task's owner has "Complete task when
all items are checked" on (PlannerSettings, default on). signals.py runs that for grocery lines.
"""
from dataclasses import dataclass, field

from django.db import transaction
from django.utils import timezone

from .models import NOTES_MAX, ChecklistItemCompletion, ItemCompletion, PlannerSettings, ScheduleItem, TaskChecklistItem

GROCERY_TITLE = "Grocery run"
MAX_ITEMS = 100


@dataclass
class Row:
    """One subtask as one occurrence shows it."""
    item: TaskChecklistItem
    text: str
    checked: bool
    done_by: object = None
    tick: object = field(default=None, repr=False)

    @property
    def pk(self):
        return self.item.pk

    @property
    def grocery_item(self):
        return self.item.grocery_item

    @property
    def grocery_item_id(self):
        return self.item.grocery_item_id


def _today():
    from .planner import today
    return today()


def edit_from(task):
    """The first occurrence a checklist edit applies to: None for a one-off task (its only occurrence); today for a
    repeating one - or a one-at-a-time task's current occurrence, if that's earlier (overdue)."""
    if not task.recurrence:
        return None
    day = _today()
    if task.one_at_a_time:
        from .planner import current_occurrence
        current = current_occurrence(task)
        if current and current < day:
            return current
    return day


def occurrence_for_groceries(task):
    """Which occurrence a grocery line's check counts for: a one-off task's date; a one-at-a-time task's current
    occurrence; another repeating task's today (if it's on today)."""
    if not task.recurrence:
        return task.date
    from . import planner, recurrence
    if task.one_at_a_time:
        return planner.current_occurrence(task)
    day = _today()
    return day if recurrence.is_scheduled(task, day) else None


def rows_for(items):
    """{task pk: [its subtasks (every one, any date), grocery line loaded]} for these items (or occurrences)."""
    ids = {getattr(i, "item", i).pk for i in items}
    out = {}
    for row in TaskChecklistItem.objects.filter(task_id__in=ids).select_related("grocery_item"):
        out.setdefault(row.task_id, []).append(row)
    return out


def _rows(definitions, day, ticks):
    """The subtasks on `day`, ticked or not. ticks: {checklist item pk: ChecklistItemCompletion} for that day."""
    out = []
    for d in definitions:
        if not d.applies_to(day):
            continue
        if d.grocery_item_id:
            out.append(Row(d, d.text, d.grocery_item.checked, d.grocery_item.checked_by if d.grocery_item.checked else None))
            continue
        tick = ticks.get(d.pk)
        out.append(Row(d, tick.text if tick else d.text, tick is not None, tick.done_by if tick else None, tick))
    return out


def occurrence_rows(task, day):
    """The task's subtasks on one occurrence (`day`: its scheduled date), each ticked or not."""
    ticks = {t.checklist_item_id: t for t in
             ChecklistItemCompletion.objects.filter(task=task, occurrence_date=day).select_related("done_by")}
    return _rows(list(task.checklist.select_related("grocery_item", "grocery_item__checked_by")), day, ticks)


def progress(rows):
    """{"done", "total", "open", "label", "title"}: "2/5", and "2 of 5 bought" (grocery) / "2 of 5 done"."""
    done = sum(1 for r in rows if r.checked)
    word = "bought" if rows and all(r.grocery_item_id for r in rows) else "done"
    return {"done": done, "total": len(rows), "open": len(rows) - done, "label": f"{done}/{len(rows)}",
            "title": f"{done} of {len(rows)} {word}"}


def attach(occurrences):
    """Give each occurrence with subtasks .checklist (its rows, ticked for that occurrence) and
    .checklist_progress."""
    occurrences = list(occurrences) if not isinstance(occurrences, list) else occurrences
    by_task = rows_for(occurrences)
    if not by_task:
        return occurrences
    days = {o.occurrence_date for o in occurrences if o.item.pk in by_task}
    ticks = {}
    for t in ChecklistItemCompletion.objects.filter(task_id__in=list(by_task), occurrence_date__in=days,
                                                    checklist_item__isnull=False).select_related("done_by"):
        ticks.setdefault((t.task_id, t.occurrence_date), {})[t.checklist_item_id] = t
    for occ in occurrences:
        definitions = by_task.get(occ.item.pk)
        if definitions:
            rows = _rows(definitions, occ.occurrence_date, ticks.get((occ.item.pk, occ.occurrence_date), {}))
            if rows:
                occ.checklist, occ.checklist_progress = rows, progress(rows)
    return occurrences


def reevaluate(task, day, user=None):
    """Check the occurrence off when every subtask is ticked, and reopen it when one isn't - only with the task's
    owner's "Complete task when all items are checked" on. day: the occurrence (None: nothing to do)."""
    if day is None or not task.checkable:
        return
    rows = occurrence_rows(task, day)
    if not rows or not PlannerSettings.for_user(task.owner).complete_when_checklist_done:
        return
    from .planner import toggle
    all_done = all(r.checked for r in rows)
    done = ItemCompletion.objects.filter(item=task, occurrence_date=day).exists()
    if all_done and not done:
        toggle(task, day, user, done=True)
    elif not all_done and done:
        toggle(task, day, user, done=False)


def set_checked(row, day, checked, user, check_task=True):
    """Tick or untick one subtask on one occurrence (`row`: the TaskChecklistItem). A grocery row writes the grocery
    line itself, as checking it on the list does (signals.grocery_saved then re-checks its tasks)."""
    line = row.grocery_item
    if line is not None:
        if line.checked != checked:
            line.checked = checked
            line.checked_by = user if checked else None
            line.checked_by_bot = False
            line.checked_at = timezone.now() if checked else None
            line.save()
        return
    if checked:
        ChecklistItemCompletion.objects.get_or_create(
            checklist_item=row, occurrence_date=day,
            defaults={"task_id": row.task_id, "text": row.text, "done_by": user if getattr(user, "pk", None) else None})
    else:
        ChecklistItemCompletion.objects.filter(checklist_item=row, occurrence_date=day).delete()
    if check_task:
        reevaluate(row.task, day, user)


def mark_all(task, day, user):
    """"Mark all subtasks done too?" - Yes: tick every open subtask of that occurrence."""
    for r in occurrence_rows(task, day):
        if not r.checked:
            set_checked(r.item, day, True, user, check_task=False)


# ---------------------------------------------------------------------------
# Editing the list
# ---------------------------------------------------------------------------

def clean_texts(texts):
    """Subtask texts as given (blank ones dropped, each at most 200 characters, at most MAX_ITEMS)."""
    return [" ".join(str(t).split())[:200] for t in texts if str(t).strip()][:MAX_ITEMS]


def create_rows(task, texts):
    """A new task's checklist."""
    TaskChecklistItem.objects.bulk_create([TaskChecklistItem(task=task, order=n, text=text)
                                           for n, text in enumerate(clean_texts(texts))])


def current_rows(task):
    """The subtasks an edit starts from: the ones on the occurrence edits apply to (a one-off task: all of them)."""
    start = edit_from(task)
    return [r for r in task.checklist.select_related("grocery_item") if start is None or r.applies_to(start)]


def add(task, text):
    """Add one subtask at the end (from the occurrence edits apply to)."""
    text = clean_texts([text])
    if not text:
        return None
    last = task.checklist.order_by("-order").values_list("order", flat=True).first()
    row = TaskChecklistItem.objects.create(task=task, text=text[0], order=(last + 1) if last is not None else 0,
                                           starts_on=edit_from(task))
    reevaluate(task, occurrence_for_groceries(task) if task.recurrence else task.date)   # an open one reopens it
    return row


def _remove(row, start):
    if start is None or (row.starts_on is not None and row.starts_on >= start):
        row.delete()                              # never on an earlier occurrence: gone (ticks keep their text)
    else:
        row.ends_on = start                       # earlier occurrences keep it
        row.save(update_fields=["ends_on"])


@transaction.atomic
def save_rows(task, posted):
    """The form's Checklist section: [(subtask pk or None, text)] in order. Existing ones are renamed and
    reordered, new ones added, missing (or emptied) ones removed - for a repeating task from the occurrence edits
    apply to on. A grocery row keeps its text (it's the grocery line's)."""
    start = edit_from(task)
    existing = {r.pk: r for r in current_rows(task)}
    kept, order = set(), 0
    for pk, text in posted[:MAX_ITEMS]:
        text = " ".join((text or "").split())[:200]
        row = existing.get(pk)
        if not text and not (row and row.grocery_item_id):
            continue
        if row is None:
            TaskChecklistItem.objects.create(task=task, order=order, text=text, starts_on=start)
        else:
            kept.add(pk)
            row.order = order
            if not row.grocery_item_id:
                row.text = text
            row.save(update_fields=["order", "text"])
        order += 1
    for pk, row in existing.items():
        if pk not in kept:
            _remove(row, start)


def posted_rows(post):
    """[(pk or None, text)] from the form's cl_id / cl_text lists."""
    ids, texts = post.getlist("cl_id"), post.getlist("cl_text")
    return [(int(pk) if pk.isdigit() else None, text) for pk, text in zip(ids, texts)]


def move_with_series(old, new, from_day, delta):
    """"This and following" split a series at from_day into `new` (dates moved by delta): its subtasks on that
    occurrence are copied to it, and the ticks from then on move over."""
    copies = {}
    for row in old.checklist.all():
        if row.applies_to(from_day):
            copy = TaskChecklistItem.objects.create(task=new, order=row.order, text=row.text,
                                                    grocery_item=row.grocery_item)
            copies[row.pk] = copy
    for tick in ChecklistItemCompletion.objects.filter(task=old, occurrence_date__gte=from_day):
        tick.task, tick.occurrence_date = new, tick.occurrence_date + delta
        tick.checklist_item = copies.get(tick.checklist_item_id)
        tick.save()


def shift(task, delta):
    """The whole series moved by delta: its ticks and subtask start/end dates move with it."""
    for tick in ChecklistItemCompletion.objects.filter(task=task):
        tick.occurrence_date += delta
        tick.save(update_fields=["occurrence_date"])
    for row in task.checklist.exclude(starts_on__isnull=True, ends_on__isnull=True):
        row.starts_on = row.starts_on + delta if row.starts_on else None
        row.ends_on = row.ends_on + delta if row.ends_on else None
        row.save(update_fields=["starts_on", "ends_on"])


# ---------------------------------------------------------------------------
# Grocery runs (Meals' grocery list "Create task")
# ---------------------------------------------------------------------------

def grocery_text(line):
    """"1.5 lb chicken thighs" - a grocery line as a checklist row."""
    from albright_meals.groceries import format_quantity
    amount = " ".join(part for part in (format_quantity(line.quantity), line.unit) if part)
    return f"{amount} {line.name}".strip()[:200]


@transaction.atomic
def task_from_groceries(user, lines, title, day, start_time=None, note=""):
    """A Planner task owned by `user`, with one checklist row per grocery line (in list order)."""
    task = ScheduleItem.objects.create(owner=user, title=(title or GROCERY_TITLE)[:200], date=day,
                                       start_time=start_time, notes=(note or "")[:NOTES_MAX], kind=ScheduleItem.TASK,
                                       checkable=True, created_by=user.get_username())
    TaskChecklistItem.objects.bulk_create([
        TaskChecklistItem(task=task, order=n, text=grocery_text(line), grocery_item=line)
        for n, line in enumerate(lines)])
    from .planner import apply_default_shares
    apply_default_shares(task)               # shared the way its owner shares new items
    reevaluate(task, task.date, user)        # already all bought: done from the start
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


def keep_grocery_tick(line):
    """A grocery line is being deleted: a checked one becomes a tick (on the occurrence it counted for), so the task
    still shows it bought."""
    if not line.checked:
        return
    for row in TaskChecklistItem.objects.filter(grocery_item=line).select_related("task"):
        day = occurrence_for_groceries(row.task) or row.task.date
        ChecklistItemCompletion.objects.get_or_create(
            checklist_item=row, occurrence_date=day,
            defaults={"task_id": row.task_id, "text": row.text, "done_by": line.checked_by})
