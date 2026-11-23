"""Smart links inside a note: due dates (optionally a linked Planner task), grocery list lines and mentions.

They live in the note's Markdown as small tokens, so notes stay Markdown (the Bot API reads and writes them too):

    @[date:2026-12-03]                    a due date
    @[date:2026-12-03T17:00|task:12]      ... at 5 PM, sent to the Planner as task 12
    @[grocery]                            a grocery tag not yet sent (the editor sends it when you leave the line)
    @[grocery:45,46]                      the line's items on the grocery list (lines 45 and 46)
    @[grocery:45|already]                 it was already on the list - nothing was added
    @[person:7|Sam Lee]                   a mention
    @[budget]                             a budget tag not yet sent (sent when you leave the line, like @grocery)
    @[budget:9]                           the line became Money's budget draft 9 (albright_money.BudgetDraft) - a
                                          draft until it's finished on the Budget page; it links back to the note

The editor (editor_src/src/note_editor.js: smartChip) shows each as a chip; render.py does the same for readers.

Two-way sync (a "link" is a date chip with a task, or a grocery chip with lines):
  - a note save (notes.save -> reconcile) pushes what the person changed to the task / grocery lines - ticking the
    line, moving the date - and takes everything they didn't change from them (the task was checked off in the
    Planner meanwhile, say). "What they changed" is against the link states the editor last had from the server
    (link_base), or, for the Bot API, against the saved note;
  - checking off / moving / deleting the task, or checking a grocery line, rewrites just that line of the note
    (sync_write - signals.py). That bumps the version but not human_version, so the editor's next save isn't
    a conflict: it's merged (views.note_save)."""
import re
import threading
import uuid
from contextlib import contextmanager
from datetime import date as date_cls, time as time_cls

from django.db import transaction
from django.db.models import F
from django.urls import reverse
from django.utils import timezone

CHIP = re.compile(r"@\[(date|grocery|person|budget)(?::([^\]|\n]*))?((?:\|[^\]|\n]*)*)\]")
FENCE = re.compile(r"^[ \t]*(```|~~~)")
CHECK_LINE = re.compile(r"^((?:[ \t]*>)*[ \t]*[-*+][ \t]+)\[([ xX])\](?=[ \t]|$)")
MARKERS = re.compile(r"^\s*(?:>\s*)*(?:[-*+]\s+(?:\[[ xX]\]\s+)?|\d+[.)]\s+|#{1,6}\s+|-#\s+)?")
TASK_SOURCE = "notes_item"             # ScheduleItem.source_app of a task made from a dated note item
MAX_GROCERIES = 30

_local = threading.local()


@contextmanager
def quiet():
    """While a note save pushes its changes to tasks and grocery lines, their signals don't write back."""
    _local.quiet = getattr(_local, "quiet", 0) + 1
    try:
        yield
    finally:
        _local.quiet -= 1


def is_quiet():
    return getattr(_local, "quiet", 0) > 0


# ---------------------------------------------------------------------------
# Reading tokens
# ---------------------------------------------------------------------------

def parse(match):
    kind, value, rest = match.group(1), (match.group(2) or "").strip(), match.group(3) or ""
    parts = [p for p in rest.split("|")[1:]]
    chip = {"kind": kind, "token": match.group(0)}
    if kind == "date":
        day, _sep, at = value.partition("T")
        chip["date"], chip["time"] = day or None, at[:5] or None
        task = next((p[5:] for p in parts if p.startswith("task:")), "")
        chip["task"] = int(task) if task.isdigit() else None
    elif kind == "grocery":
        chip["ids"] = [int(i) for i in value.split(",") if i.strip().isdigit()]
        chip["already"] = "already" in parts
    elif kind == "budget":
        chip["draft"] = int(value) if value.isdigit() else None
    else:
        chip["pid"] = int(value) if value.isdigit() else None
        chip["name"] = (parts[0] if parts else "").strip()
    return chip


def date_token(day, at=None, task=None):
    value = day.isoformat() if hasattr(day, "isoformat") else str(day)
    if at:
        value += "T" + (at.strftime("%H:%M") if hasattr(at, "strftime") else str(at)[:5])
    return f"@[date:{value}" + (f"|task:{task}" if task else "") + "]"


def grocery_token(ids, already=False):
    return "@[grocery" + (":" + ",".join(str(i) for i in ids) if ids else "") + ("|already" if already else "") + "]"


def budget_token(draft_id=None):
    return "@[budget" + (f":{draft_id}" if draft_id else "") + "]"


def _lines(body):
    """(index, line) for every line outside fenced code."""
    fenced = False
    for i, line in enumerate((body or "").split("\n")):
        if FENCE.match(line):
            fenced = not fenced
            continue
        if not fenced:
            yield i, line


def checked_of(line):
    m = CHECK_LINE.match(line)
    return None if not m else m.group(2) != " "


def line_text(line):
    """The line as plain text: no list / checkbox / heading marker, no chips."""
    return " ".join(CHIP.sub(" ", MARKERS.sub("", line, count=1)).split())


def chips(body):
    """[{kind, token, line, checked, text, ...}] for every chip, in order."""
    out = []
    for i, line in _lines(body):
        if "@[" not in line:
            continue
        for m in CHIP.finditer(line):
            chip = parse(m)
            chip.update(line=i, checked=checked_of(line), text=line_text(line))
            out.append(chip)
    return out


def key_of(chip):
    if chip["kind"] == "date" and chip.get("task"):
        return f"task:{chip['task']}"
    if chip["kind"] == "grocery" and chip.get("ids"):
        return "grocery:" + ",".join(str(i) for i in chip["ids"])
    return None


def link_states(body):
    """{link key: {"checked", "date", "time"}} - what the editor sends back as link_base."""
    out = {}
    for chip in chips(body):
        key = key_of(chip)
        if key:
            out[key] = {"checked": chip["checked"], "date": chip.get("date"), "time": chip.get("time")}
    return out


def due_items(body):
    return [{"line": c["line"], "text": c["text"], "date": c["date"], "time": c["time"], "task_id": c["task"],
             "done": bool(c["checked"])} for c in chips(body) if c["kind"] == "date" and c.get("date")]


def grocery_items(body):
    return [{"line": c["line"], "text": c["text"], "grocery_ids": c["ids"], "already": c["already"],
             "pending": not c["ids"], "done": bool(c["checked"])} for c in chips(body) if c["kind"] == "grocery"]


def budget_items(body):
    return [{"line": c["line"], "text": c["text"], "draft_id": c["draft"], "pending": not c["draft"]}
            for c in chips(body) if c["kind"] == "budget"]


def _day(text):
    try:
        return date_cls.fromisoformat(text)
    except (TypeError, ValueError):
        return None


def next_due(body, today):
    """(date, overdue) of the soonest due date on a line that isn't ticked off, or None."""
    days = [d for d in (_day(i["date"]) for i in due_items(body) if not i["done"]) if d]
    if not days:
        return None
    soonest = min(days)
    return soonest, soonest < today


def label(day, at=None, today=None):
    """ "Today", "Tomorrow", "Fri", "Dec 3", "Dec 3, 2027" - and " 5 PM" with a time."""
    today = today or timezone.localdate()
    delta = (day - today).days
    if delta == 0:
        text = "Today"
    elif delta == 1:
        text = "Tomorrow"
    elif delta == -1:
        text = "Yesterday"
    elif 1 < delta < 7:
        text = f"{day:%a}"
    elif day.year == today.year:
        text = f"{day:%b} {day.day}"
    else:
        text = f"{day:%b} {day.day}, {day.year}"
    if at:
        try:
            t = time_cls.fromisoformat(at) if isinstance(at, str) else at
            text += " " + (f"{t:%I:%M %p}".lstrip("0").replace(":00 ", " "))
        except ValueError:
            pass
    return text


# ---------------------------------------------------------------------------
# A note save: push the person's changes, take the rest from the task / grocery lines
# ---------------------------------------------------------------------------

def _hhmm(t):
    return t.strftime("%H:%M") if t else None


def _set_checked(line, checked):
    return CHECK_LINE.sub(lambda m: f"{m.group(1)}[{'x' if checked else ' '}]", line, count=1)


def _check_grocery(line_item, checked, actor):
    if line_item.checked == checked:
        return
    line_item.checked = checked
    line_item.checked_by = actor if checked and getattr(actor, "pk", None) else None
    line_item.checked_by_bot = False
    line_item.checked_at = timezone.now() if checked else None
    line_item.save()


def reconcile(note, body, actor, base=None):
    """The body to save, and [{key, checked?, date?, time?}] for what came from the task / grocery side (the editor
    patches its document with it). base: the link states the editor last had (None: the saved note's)."""
    from albright_meals.models import GroceryItem
    from albright_planner import planner, services
    from albright_planner.models import ScheduleItem

    found = chips(body)
    if not any(key_of(c) for c in found):
        return body, []
    base = link_states(note.body) if base is None else base
    lines = body.split("\n")
    tasks = {t.pk: t for t in ScheduleItem.objects.filter(pk__in=[c["task"] for c in found if c.get("task")], note_id=note.pk)}
    lines_by_id = {g.pk: g for g in GroceryItem.objects.filter(pk__in=[i for c in found for i in c.get("ids") or []])}
    sync = []
    with quiet():
        for chip in found:
            key, i = key_of(chip), chip["line"]
            if not key:
                continue
            before = base.get(key)
            if chip["kind"] == "date":
                task = tasks.get(chip["task"])
                if task is None:                                    # deleted in the Planner: just a date now
                    lines[i] = lines[i].replace(chip["token"], date_token(chip["date"], chip["time"]), 1)
                    sync.append({"key": key, "unlink": True})
                    continue
                theirs = (task.date.isoformat(), _hhmm(task.start_time))
                mine = (chip["date"], chip["time"])
                if before and (before.get("date"), before.get("time")) == mine:
                    if theirs != mine:                              # moved in the Planner
                        lines[i] = lines[i].replace(chip["token"], date_token(theirs[0], theirs[1], task.pk), 1)
                        sync.append({"key": key, "date": theirs[0], "time": theirs[1]})
                elif theirs != mine and _day(chip["date"]):
                    task.date = _day(chip["date"])
                    task.start_time = time_cls.fromisoformat(chip["time"]) if chip["time"] else None
                    task.save(update_fields=["date", "start_time", "updated_at"])
                title = (chip["text"] or note.title)[:200]
                if title and task.title != title:
                    ScheduleItem.objects.filter(pk=task.pk).update(title=title)
                if chip["checked"] is not None:
                    done = services.is_done(task, task.date)
                    if before and before.get("checked") == chip["checked"]:
                        if done != chip["checked"]:
                            lines[i] = _set_checked(lines[i], done)
                            sync.append({"key": key, "checked": done})
                    elif done != chip["checked"]:
                        planner.toggle(task, task.date, actor, done=chip["checked"])
            else:
                items = [lines_by_id[g] for g in chip["ids"] if g in lines_by_id]
                if not items or chip["checked"] is None:
                    continue
                done = all(g.checked for g in items)
                if before and before.get("checked") == chip["checked"]:
                    if done != chip["checked"]:
                        lines[i] = _set_checked(lines[i], done)
                        sync.append({"key": key, "checked": done})
                elif done != chip["checked"]:
                    for g in items:
                        _check_grocery(g, chip["checked"], actor)
    return "\n".join(lines), sync


# ---------------------------------------------------------------------------
# The other way: a task or grocery line changed - rewrite those lines of the note
# ---------------------------------------------------------------------------

def sync_write(note_id, mutate):
    """Change some lines of a note from the task / grocery side. mutate(lines) changes the list in place and returns
    whether it did. Bumps the version (the editor's next save merges it) but not human_version or updated_at."""
    from .models import Note
    with transaction.atomic():
        note = Note.objects.select_for_update().filter(pk=note_id).first()
        if note is None:
            return False
        lines = note.body.split("\n")
        if not mutate(lines):
            return False
        Note.objects.filter(pk=note.pk).update(body="\n".join(lines), version=F("version") + 1)
    return True


def _each_chip(lines, kind):
    fenced = False
    for i, line in enumerate(lines):
        if FENCE.match(line):
            fenced = not fenced
            continue
        if fenced or "@[" not in line:
            continue
        for m in CHIP.finditer(line):
            if m.group(1) == kind:
                yield i, parse(m)


def task_changed(task):
    """A dated note item's task was checked off / reopened, moved, or deleted (task=None handled by task_deleted)."""
    from albright_planner import services
    if task.source_app != TASK_SOURCE or not task.note_id:
        return
    done = services.is_done(task, task.date)
    day, at = task.date.isoformat(), _hhmm(task.start_time)

    def mutate(lines):
        changed = False
        for i, chip in list(_each_chip(lines, "date")):
            if chip.get("task") != task.pk:
                continue
            if (chip["date"], chip["time"]) != (day, at):
                lines[i] = lines[i].replace(chip["token"], date_token(day, at, task.pk), 1)
                changed = True
            if checked_of(lines[i]) is not None and checked_of(lines[i]) != done:
                lines[i] = _set_checked(lines[i], done)
                changed = True
        return changed
    sync_write(task.note_id, mutate)


def task_deleted(task):
    if task.source_app != TASK_SOURCE or not task.note_id:
        return

    def mutate(lines):
        changed = False
        for i, chip in list(_each_chip(lines, "date")):
            if chip.get("task") == task.pk:
                lines[i] = lines[i].replace(chip["token"], date_token(chip["date"], chip["time"]), 1)
                changed = True
        return changed
    sync_write(task.note_id, mutate)


def grocery_changed(line_item):
    """A grocery line was checked or unchecked: every note line tagged with it follows."""
    from albright_meals.models import GroceryItem

    from .models import Note
    pattern = rf"@\[grocery:([0-9]+,)*{line_item.pk}(,[0-9]+)*[]|]"
    for note_id in Note.objects.filter(body__regex=pattern).values_list("pk", flat=True):
        def mutate(lines):
            changed = False
            for i, chip in list(_each_chip(lines, "grocery")):
                if line_item.pk not in chip["ids"] or checked_of(lines[i]) is None:
                    continue
                done = all(GroceryItem.objects.filter(pk__in=chip["ids"]).values_list("checked", flat=True)) \
                    if len(chip["ids"]) > 1 else line_item.checked
                if checked_of(lines[i]) != done:
                    lines[i] = _set_checked(lines[i], done)
                    changed = True
            return changed
        sync_write(note_id, mutate)


# ---------------------------------------------------------------------------
# Adding: grocery lines, Planner tasks (the editor's endpoints and the Bot API)
# ---------------------------------------------------------------------------

class NoGroceries(Exception):
    """This person can't open Meals (no grocery list)."""


def split_items(text):
    """ "milk, eggs, bread" -> ["milk", "eggs", "bread"]."""
    return [" ".join(part.split())[:120] for part in (text or "").split(",") if part.strip()][:MAX_GROCERIES]


def _space_already_used_for_groceries(user, note):
    """If this note already links grocery lines and they all live in one Meals space the writer can still
    see, return that space. Otherwise None."""
    if note is None or not getattr(note, "pk", None):
        return None
    from albright_labs import circles
    from albright_meals.models import GroceryItem
    ids = []
    for row in grocery_items(getattr(note, "body", "") or ""):
        ids.extend(row.get("grocery_ids") or [])
    if not ids:
        return None
    space_ids = set(GroceryItem.objects.filter(pk__in=ids, space_id__in=circles.space_ids(user, "meals"))
                    .values_list("space_id", flat=True).distinct())
    if len(space_ids) != 1:
        return None
    return circles.default_create_space(user, "meals", inherit=space_ids.pop())


def add_groceries(user, texts, token=None, note=None, space=None):
    """Put these items on a grocery list. Uses `space` when given, else the same Meals space as this note's
    existing grocery chips when they're all in one space the writer can see, else Personal — not the
    last-picked Meals switcher. An item already on the list (unchecked, the same thing - groceries.name_key)
    isn't added again. Returns [(line, "added"|"already")]."""
    from albright_labs import circles
    from albright_meals import groceries
    from albright_meals.models import GroceryItem
    space = space or _space_already_used_for_groceries(user, note) or circles.default_create_space(user, "meals")
    if space is None:
        raise NoGroceries()
    listed = {groceries.name_key(g.name): g for g in GroceryItem.objects.active().filter(space=space, checked=False).order_by("pk")}
    out = []
    for text in texts:
        parsed = groceries.parse_line(text)
        name = parsed["name"] or text
        existing = listed.get(groceries.name_key(name))
        if existing is not None:
            out.append((existing, "already"))
            continue
        item, _status = groceries.add_item_detail(space, name, parsed["quantity"], parsed["unit"], note=parsed["note"],
                                                  user=user, token=token)
        listed[groceries.name_key(name)] = item
        out.append((item, "added"))
    return out


def remove_groceries(user, ids):
    """Take these lines off the grocery list (Undo, or "Remove from groceries") - only in spaces this person sees."""
    from albright_labs import circles
    from albright_meals.models import GroceryItem
    spaces = circles.space_ids(user, "meals")
    return GroceryItem.objects.filter(pk__in=ids, space_id__in=spaces).delete()[0]


class NoMoney(Exception):
    """This person can't open Money (no budget to put a draft in)."""


def add_budget_draft(note, user, text):
    """ "@budget Christmas gifts 300": a draft budget item in Money — Personal, or the same space as this
    note's existing plans when they all live in one space the writer can see. Named from the line; its
    amount (the first or last word) is the optional cap. Linked to this note. A shared note does not
    move the plan into a circle (the owner can open it to a circle from the plan). Returns the draft."""
    from albright_money import budget
    from albright_money.money import can_open
    if not can_open(user):
        raise NoMoney()
    return budget.create_draft(user, " ".join((text or "").split())[:200], note=note)


def remove_budget_draft(user, draft_id):
    """Undo an "@budget", or "Remove draft": only a draft still unfinished, in a space this person sees in Money."""
    from albright_money.models import BudgetDraft
    return BudgetDraft.objects.visible_to(user).filter(pk=draft_id, confirmed_at__isnull=True).delete()[0]


def budget_draft(user, draft_id):
    """{name, planned, confirmed, category, url} for a chip's popover, or None when it's gone / not theirs to see."""
    from albright_money.models import BudgetDraft
    d = BudgetDraft.objects.visible_to(user).select_related("line__category", "line__budget").filter(pk=draft_id).first()
    if d is None:
        return None
    from albright_money.budget import plan_url
    return {"name": d.name, "planned": str(d.planned) if d.planned is not None else None,
            "confirmed": not d.is_draft, "category": d.line.category.name if d.line_id else None,
            "url": plan_url(d)}


def add_task(note, user, day, at=None, text=""):
    """A Planner task for a dated note item, linked back to the note. source_id is "<note>:<random>": the Planner
    allows one item per (owner, source_app, source_id), and a note can have any number of dated lines."""
    from albright_planner import services
    from albright_planner.models import ScheduleItem
    item = services.create_scheduled_task(user, TASK_SOURCE, f"{note.pk}:{uuid.uuid4().hex[:12]}",
                                          (text or note.title)[:200], day, at,
                                          url=reverse("albright_notes:note", args=[note.pk]))
    ScheduleItem.objects.filter(pk=item.pk).update(note=note)
    item.note_id = note.pk
    return item


def task_for_line(note, user, day, at=None, text=""):
    """The editor's auto-add (a dated line, like "@today call mom"): this line's task, made once. A request sent twice
    (a retry, two tabs) gets the task already made for it - same note, date, time and title, and no line of the
    saved note links it yet - instead of a second one. Returns (task, created)."""
    from albright_planner.models import ScheduleItem
    title = (" ".join((text or note.title or "").split()) or "Untitled")[:200]
    linked = {c["task"] for c in chips(note.body) if c["kind"] == "date" and c.get("task")}
    for task in ScheduleItem.objects.filter(note_id=note.pk, owner=user, source_app=TASK_SOURCE, date=day,
                                            start_time=at, title=title, archived=False).order_by("pk"):
        if task.pk not in linked:
            return task, False
    return add_task(note, user, day, at, title), True


def planner_task(user, task_id):
    from albright_planner.models import ScheduleItem
    return ScheduleItem.objects.filter(pk=task_id, owner=user, source_app=TASK_SOURCE).first()


def auto_planner(user):
    """A dated line always becomes a Planner task (like @grocery) for anyone who can open the Planner.
    (NotesSettings.auto_planner, the old opt-in switch, is no longer read.)"""
    from . import links
    return "planner" in links._allowed_apps(user)


def parse_when(day_text, time_text=None):
    """ISO date and optional "HH:MM" -> (date, time) or ValueError."""
    day_text, _sep, inline = str(day_text or "").strip().partition("T")      # "2026-12-03T17:00" works too
    day = date_cls.fromisoformat(day_text)
    time_text = str(time_text or inline or "").strip()
    at = time_cls.fromisoformat(time_text[:5].zfill(5)) if time_text else None   # "9:00" -> 09:00
    return day, at


def append_items(note, items, user, token=None):
    """The Bot API's {"items": [...]}: lines appended to the note - a checklist item unless "checklist": false, with a
    due date (and a Planner task when the owner has "Always add dated note items to Planner" on) and/or its text put
    on the grocery list ("grocery": true; commas split it). Returns (body, summary dict)."""
    lines, added, tasks = [], 0, 0
    for raw in items:
        text = " ".join(str(raw.get("text") or "").split())[:500]
        if not text:
            continue
        line = ("- [ ] " if raw.get("checklist", True) else "") + text
        if raw.get("due"):
            day, at = parse_when(raw["due"], raw.get("time"))
            task = add_task(note, user, day, at, text).pk if auto_planner(user) else None
            tasks += 1 if task else 0
            line += " " + date_token(day, at, task)
        if raw.get("grocery"):
            made = add_groceries(user, split_items(text), token=token, note=note)
            added += sum(1 for _l, status in made if status == "added")
            line += " " + grocery_token([g.pk for g, _s in made], already=all(s == "already" for _g, s in made))
        lines.append(line)
    body = note.body.rstrip("\n")
    body = (body + "\n\n" if body.strip() else "") + "\n".join(lines)
    return body, {"lines": len(lines), "groceries_added": added, "planner_tasks": tasks}

