"""Planner logic shared by the views, Home's Today panel and the launcher:
what's on in a date range, overdue tasks, check-off, and changing a repeating
item for "this occurrence only", "this and following" or "all occurrences".

One at a time: a repeating task that never ends (ScheduleItem.one_at_a_time)
shows only its current occurrence - the earliest not done or skipped - plus
the ones already done, on their days. A past one stays on Today as overdue
(no new copy appears). Checking it off or skipping it moves it on:
  - on schedule (default): the first scheduled date after the later of its
    date and today - missed ones are skipped, not stacked up;
  - repeat_from_completion: today plus the repeat interval (step_from), and
    the chain counts on from each check-off (repeat_anchor).
Unchecking puts it back. next_due is on the item, so a check-off on a
shared task moves it on for everyone."""
from datetime import time, timedelta

from django.db import transaction
from django.db.models import F, Prefetch, Q
from django.utils import timezone

from . import recurrence
from .models import DeletedSourceItem, ItemCompletion, ItemException, ItemShare, ScheduleItem

THIS, FOLLOWING, ALL = "this", "following", "all"
# The views' "Show" filter: everything I can see, only mine, or only what others shared with me.
EVERYONE, MINE, SHARED = "", "mine", "shared"
WHO = [(EVERYONE, "All"), (MINE, "Mine"), (SHARED, "Shared with me")]
SCOPES = [(THIS, "This occurrence only"), (FOLLOWING, "This and following"), (ALL, "All occurrences")]
SERIES_FIELDS = ("notes", "kind", "checkable", "priority", "color", "end_time")
MAX_PAST_SCAN_DAYS = 3660          # how far back a never-checked one-at-a-time task looks for its current one


def today():
    return timezone.localdate()


def week_start(day):
    return day - timedelta(days=day.weekday())


# ---------------------------------------------------------------------------
# What's on
# ---------------------------------------------------------------------------

def _candidates(user, start, end):
    """Items that could have an occurrence between start and end."""
    one_off = Q(recurrence="", date__gte=start, date__lte=end)
    repeating = ~Q(recurrence="") & Q(date__lte=end) & (Q(recurrence_until__isnull=True) | Q(recurrence_until__gte=start))
    moved_in = Q(exceptions__action=ItemException.MOVE, exceptions__new_date__gte=start, exceptions__new_date__lte=end)
    ids = ScheduleItem.objects.visible_to(user).filter(archived=False).filter(one_off | repeating | moved_in) \
        .values_list("pk", flat=True).distinct()
    return ScheduleItem.objects.filter(pk__in=list(ids)).select_related("owner").prefetch_related(
        "exceptions", "shares__user", Prefetch("completions", queryset=ItemCompletion.objects.select_related("completed_by")))


def _by_owner(items, user, who):
    if who == MINE:
        return [i for i in items if i.owner_id == user.pk]
    if who == SHARED:
        return [i for i in items if i.owner_id != user.pk]
    return items


def _mark_sharing(occ, user):
    """shared_in: someone else's item (shown with their initial); shared_out: mine, shared with others."""
    occ.shared_in = occ.item.owner_id != user.pk
    occ.shared_out = not occ.shared_in and bool(occ.item.shares.all())
    occ.shared = occ.shared_in or occ.shared_out          # shows "Done by <name>" on it and its subtasks


def occurrences(user, start, end, kind="", show_done=True, shared_only=False, who=EVERYONE, expand=False):
    """Every occurrence the user can see - their own items and ones shared with them - between start and end
    (inclusive), sorted by day and time. A check-off is the item's, so it shows for everyone who can see it, with
    who did it (occ.done_by). A one-at-a-time task shows only its current occurrence (and the done ones) unless
    expand (the Bot API's ?expand=all): every scheduled one."""
    items = list(_candidates(user, start, end))
    if kind in (ScheduleItem.TASK, ScheduleItem.EVENT):
        items = [i for i in items if i.kind == kind]
    items = _by_owner(items, user, SHARED if shared_only else who)
    editable = set(ScheduleItem.objects.editable_by(user).filter(pk__in=[i.pk for i in items]).values_list("pk", flat=True))
    out = []
    for item in items:
        completions = {c.occurrence_date: c for c in item.completions.all()}
        exceptions = {e.occurrence_date: e for e in item.exceptions.all()}
        if item.one_at_a_time and not expand:
            found = _one_at_a_time(item, start, end, completions, exceptions)
        else:
            found = recurrence.occurrences(item, start, end, set(completions), exceptions)
        for occ in found:
            occ.can_edit = item.pk in editable
            completion = completions.get(occ.occurrence_date)
            occ.done_by = completion.completed_by if completion else None
            _mark_sharing(occ, user)
            if show_done or not occ.done:
                out.append(occ)
    out.sort(key=lambda o: (o.date, o.start_time is not None, o.start_time or time.min, o.title.lower()))
    return out


def _one_at_a_time(item, start, end, completions, exceptions):
    """A one-at-a-time task's occurrences between start and end: the done ones, and its current one."""
    key = current_occurrence(item, set(completions), _skipped(exceptions))
    keys = sorted(set(completions)) + ([key] if key and key not in completions else [])
    occs = [recurrence.make(item, d, exceptions.get(d), d in completions) for d in keys]
    return [o for o in occs if start <= o.date <= end]


def _skipped(exceptions):
    return {d for d, e in exceptions.items() if e.action == ItemException.SKIP}


def current_occurrence(item, completed=None, skipped=None):
    """A one-at-a-time task's current occurrence: the date it was scheduled for (its completions and exceptions are
    keyed by it) - the earliest one not done or skipped, from where it last moved on (next_due). Never moved on
    here: from after its latest check-off, else its first date. completed / skipped: sets of dates (else queried)."""
    if completed is None:
        completed = set(item.completions.values_list("occurrence_date", flat=True))
    if skipped is None:
        skipped = set(item.exceptions.filter(action=ItemException.SKIP).values_list("occurrence_date", flat=True))
    if item.repeat_from_completion and item.next_due and item.next_due not in completed | skipped:
        return item.next_due                         # off the rule's dates: due N days/weeks/months after the last
    floor = item.next_due
    if floor is None:
        floor = max(completed) + timedelta(days=1) if completed else item.date
    floor = max(floor, item.date, today() - timedelta(days=MAX_PAST_SCAN_DAYS))
    for day in recurrence.dates_from(item, floor):
        if day not in completed and day not in skipped:
            return day
    return None


def _move_on(item, occurrence_date, on_day):
    """After the current occurrence was checked off or skipped on `on_day`: where the next one is."""
    if item.repeat_from_completion:
        item.repeat_anchor = on_day
        item.next_due = recurrence.step_from(item, on_day)
    else:
        later = max(occurrence_date, on_day)
        item.next_due = recurrence.first_after(item, later) or later + timedelta(days=1)
    item.save(update_fields=["next_due", "repeat_anchor", "updated_at"])


def _put_back(item, occurrence_date):
    """Unchecking: that occurrence is the current one again (the next one it made goes away)."""
    item.next_due = occurrence_date
    if item.repeat_from_completion:
        last = item.completions.order_by("-completed_at").first()
        item.repeat_anchor = timezone.localdate(last.completed_at) if last else None
    item.save(update_fields=["next_due", "repeat_anchor", "updated_at"])


def valid_occurrence(item, day):
    """Whether `day` names one of the item's occurrences: scheduled that day, or (one at a time) its current one or
    one already done."""
    if recurrence.is_scheduled(item, day):
        return True
    if item.one_at_a_time:
        return day == current_occurrence(item) or item.completions.filter(occurrence_date=day).exists()
    return False


def current_or_first(item):
    """The occurrence the item page opens on when none is given: a one-at-a-time task's current one."""
    return (current_occurrence(item) if item.one_at_a_time else None) or item.date


def occurrence_for(item, day, user):
    """One occurrence for the item's view panel: after its exception (moved / retimed / retitled), whether it's
    done and by whom, overdue (an unchecked one-off or one-at-a-time task from before today), and how it's shared
    with `user`."""
    exc = item.exceptions.filter(occurrence_date=day).first() if item.recurrence else None
    completion = item.completions.select_related("completed_by").filter(occurrence_date=day).first()
    occ = recurrence.make(item, day, exc, completion is not None)
    occ.done_by = completion.completed_by if completion else None
    occ.can_edit = item.can_edit(user)
    occ.overdue = not occ.done and item.kind == ScheduleItem.TASK and item.checkable and occ.date < today() and \
        (not item.recurrence or item.one_at_a_time)
    _mark_sharing(occ, user)
    return occ


def plain_repeat(item):
    """The view panel's repeat line in plain words: "Every Monday", "Every 3 days after it's done" - or ""."""
    if not item.recurrence:
        return ""
    if item.one_at_a_time and item.repeat_from_completion:
        text = recurrence.from_completion_text(item.recurrence) + " after it's done"
        return text[:1].upper() + text[1:]
    return recurrence.summary(item.recurrence, item.date, item.recurrence_until, item.recurrence_count, long_days=True)


@transaction.atomic
def duplicate(item, user):
    """A copy of the item owned by `user`: the same series and details, its current checklist (nothing ticked),
    shared as `user` shares new items. Completions, skips and moves stay with the original; a copy of an item
    another app keeps up to date is a plain item."""
    from . import checklist
    copy = ScheduleItem.objects.get(pk=item.pk)
    copy.pk = copy.id = None
    copy.owner, copy.created_by = user, user.get_username()
    copy.source_app = copy.source_id = copy.source_url = ""
    copy.from_bot, copy.bot_token, copy.archived = False, None, False
    if copy.note_id is not None:
        from albright_notes.models import Note
        if not Note.objects.visible_to(user).filter(pk=copy.note_id).exists():
            copy.note = None                              # a note the new owner can't see isn't linked
    copy.save()
    checklist.create_rows(copy, [r.text for r in checklist.current_rows(item)])
    apply_default_shares(copy)
    return copy


def repeat_text(item):
    """A one-at-a-time task's line on its page: "Repeats every Mon · next appears after this one is done"."""
    if item.repeat_from_completion:
        how = recurrence.from_completion_text(item.recurrence) + " after it's done"
    else:
        text = recurrence.summary(item.recurrence, item.date)
        how = text[:1].lower() + text[1:]
    return f"Repeats {how} \u00b7 next appears after this one is done"


def overdue(user, day=None, kind="", shared_only=False, who=EVERYONE):
    """One-off tasks from before `day` that aren't checked off - they roll into Today until done or moved - and
    one-at-a-time tasks whose current occurrence is before `day` (shown once, "due Mon, Nov 3"). Includes tasks
    shared with the user."""
    day = day or today()
    if kind == ScheduleItem.EVENT:
        return []
    qs = ScheduleItem.objects.visible_to(user).filter(archived=False, recurrence="", kind=ScheduleItem.TASK,
                                                      checkable=True, date__lt=day) \
        .exclude(completions__occurrence_date=F("date")).select_related("owner").prefetch_related("shares__user") \
        .order_by("date", "start_time", "pk")
    who = SHARED if shared_only else who
    if who == SHARED:
        qs = qs.exclude(owner=user)
    elif who == MINE:
        qs = qs.filter(owner=user)
    series = ScheduleItem.objects.visible_to(user).filter(
        archived=False, kind=ScheduleItem.TASK, checkable=True, date__lt=day, recurrence_until__isnull=True,
        recurrence_count__isnull=True).exclude(recurrence="").select_related("owner").prefetch_related(
        "shares__user", "exceptions", "completions")
    if who == SHARED:
        series = series.exclude(owner=user)
    elif who == MINE:
        series = series.filter(owner=user)
    occs = [recurrence.Occurrence(item, item.date, item.date, item.title, item.start_time, item.end_time)
            for item in qs]
    for item in series:
        exceptions = {e.occurrence_date: e for e in item.exceptions.all()}
        key = current_occurrence(item, {c.occurrence_date for c in item.completions.all()}, _skipped(exceptions))
        if key is not None:
            occ = recurrence.make(item, key, exceptions.get(key))
            if occ.date < day:
                occs.append(occ)
    editable = set(ScheduleItem.objects.editable_by(user).filter(pk__in=[o.item.pk for o in occs])
                   .values_list("pk", flat=True))
    for occ in occs:
        occ.overdue, occ.can_edit, occ.done_by = True, occ.item.pk in editable, None
        _mark_sharing(occ, user)
    occs.sort(key=lambda o: (o.date, o.start_time is not None, o.start_time or time.min, o.item.pk))
    return occs


def open_tasks_today(user):
    """Today's unchecked tasks (and overdue ones) - the launcher's and Home's number."""
    day = today()
    todays = [o for o in occurrences(user, day, day, kind=ScheduleItem.TASK) if o.item.checkable and not o.done]
    return todays, overdue(user, day)


def is_completed(occ):
    """A task occurrence that's finished: checked off, or every checklist row ticked (its owner may have "Complete
    task when all items are checked" off, so the box itself can still be open)."""
    progress = getattr(occ, "checklist_progress", None)
    return bool(occ.done or (progress and progress["total"] and not progress["open"]))


def today_panel(user, show_completed=False):
    """Home's Today panel: the open tasks - overdue ones first, then today's - and today's events. show_completed
    (Home's "Show completed tasks"): today's finished tasks too, at the bottom. "open" / "overdue" / "done": the
    count line's numbers for what's shown."""
    from . import checklist
    day = today()
    late = overdue(user, day)
    occs = checklist.attach(occurrences(user, day, day) + late)
    todays = [o for o in occs if o.item.kind == ScheduleItem.TASK and not getattr(o, "overdue", False)]
    open_tasks = late + [o for o in todays if not is_completed(o)]
    done = [o for o in todays if is_completed(o)] if show_completed else []
    for o in done:
        o.done = True                                  # struck through, even if only its checklist is finished
    return {"date": day, "tasks": open_tasks + done, "events": [o for o in occs if o.item.kind == ScheduleItem.EVENT],
            "open": len(open_tasks), "overdue": len(late), "done": len(done), "show_completed": show_completed}


# ---------------------------------------------------------------------------
# Check-off
# ---------------------------------------------------------------------------

@transaction.atomic
def toggle(item, occurrence_date, user, done=None):
    """Check off (or undo) one occurrence. Returns whether it's now done, or None if it isn't scheduled that day.
    A one-at-a-time task's current occurrence moves it on to the next; unchecking one puts it back."""
    if item.one_at_a_time:
        return _toggle_one_at_a_time(item, occurrence_date, user, done)
    if not recurrence.is_scheduled(item, occurrence_date):
        return None
    existing = ItemCompletion.objects.filter(item=item, occurrence_date=occurrence_date)
    want = (not existing.exists()) if done is None else done
    if want:
        ItemCompletion.objects.get_or_create(item=item, occurrence_date=occurrence_date,
                                             defaults={"completed_by": user})
    else:
        existing.delete()
    return want


def _toggle_one_at_a_time(item, occurrence_date, user, done):
    completed = set(item.completions.values_list("occurrence_date", flat=True))
    current = current_occurrence(item, completed)
    is_done = occurrence_date in completed
    if not (is_done or occurrence_date == current or recurrence.is_scheduled(item, occurrence_date)):
        return None
    want = (not is_done) if done is None else done
    if want and not is_done:
        ItemCompletion.objects.get_or_create(item=item, occurrence_date=occurrence_date, defaults={"completed_by": user})
        if occurrence_date == current:
            _move_on(item, occurrence_date, today())
    elif not want and is_done:
        ItemCompletion.objects.filter(item=item, occurrence_date=occurrence_date).delete()
        _put_back(item, occurrence_date)
    return want


# ---------------------------------------------------------------------------
# Changing items
# ---------------------------------------------------------------------------

def _apply(item, changes):
    for name in ("title", "notes", "kind", "start_time", "end_time", "checkable", "priority", "color",
                 "repeat_from_completion"):
        if name in changes:
            setattr(item, name, changes[name])
    if item.kind == ScheduleItem.TASK:
        item.checkable = True


def _set_rule(item, changes, moved_from=None):
    """The repeat settings from the form (if sent), else the old rule moved along with the series."""
    if "recurrence" in changes:
        item.recurrence = changes["recurrence"]
        item.recurrence_until = changes.get("recurrence_until")
        item.recurrence_count = None if item.recurrence_until else changes.get("recurrence_count")
    elif moved_from is not None and moved_from != item.date and item.recurrence:
        item.recurrence = recurrence.shift(item.recurrence, moved_from, item.date)
    if not item.recurrence:
        item.recurrence_until = item.recurrence_count = None


@transaction.atomic
def change(item, occurrence_date, scope, changes):
    """Edit or move an item. changes: any of title, notes, kind, date (the occurrence's new day), start_time,
    end_time, checkable, priority, color, and (for a series) recurrence / recurrence_until / recurrence_count.
    Returns the item now holding that occurrence."""
    new_date = changes.get("date") or occurrence_date
    if not item.recurrence:
        _apply(item, changes)
        item.date = new_date
        _set_rule(item, changes)
        item.save()
        return item

    if scope == THIS:
        exc, _ = ItemException.objects.get_or_create(item=item, occurrence_date=occurrence_date,
                                                     defaults={"action": ItemException.EDIT})
        title = changes.get("title", exc.new_title or item.title)
        exc.new_title = "" if title == item.title else title[:200]
        start = changes.get("start_time", exc.new_start_time or item.start_time)
        exc.new_start_time = None if start == item.start_time else start
        exc.new_date = None if new_date == occurrence_date else new_date
        exc.action = ItemException.MOVE if exc.new_date or exc.new_start_time else ItemException.EDIT
        if exc.action == ItemException.EDIT and not exc.new_title:
            exc.delete()                                  # back to the plain occurrence
        else:
            exc.save()
        return item

    delta = new_date - occurrence_date
    if scope == FOLLOWING and occurrence_date > item.date:
        before = recurrence.count_before(item, occurrence_date)
        new = ScheduleItem.objects.get(pk=item.pk)
        new.pk = new.id = None
        new.source_app = new.source_id = ""          # one item per source record: the copy isn't it
        new.date = occurrence_date + delta
        if item.recurrence_count:
            new.recurrence_count = max(1, item.recurrence_count - before)
        item.recurrence_until, item.recurrence_count = occurrence_date - timedelta(days=1), None
        item.save()
        _apply(new, changes)
        _set_rule(new, changes, moved_from=occurrence_date)
        new.save()
        for share in item.shares.all():
            ItemShare.objects.create(item=new, user=share.user, permission=share.permission)
        for row in list(item.completions.filter(occurrence_date__gte=occurrence_date)) + \
                list(item.exceptions.filter(occurrence_date__gte=occurrence_date)):
            row.item, row.occurrence_date = new, row.occurrence_date + delta
            row.save()
        from . import checklist
        checklist.move_with_series(item, new, occurrence_date, delta)
        return new

    # ALL (or "following" from the first occurrence): the whole series, its start moved by the same amount.
    old_start = item.date
    _apply(item, changes)
    item.date = item.date + delta
    if item.next_due:
        item.next_due = item.next_due + delta
    _set_rule(item, changes, moved_from=old_start)
    item.save()
    if delta:
        for row in list(item.completions.all()) + list(item.exceptions.all()):
            row.occurrence_date = row.occurrence_date + delta
            row.save()
        if item.recurrence:
            from . import checklist
            checklist.shift(item, delta)
    return item


@transaction.atomic
def remove(item, occurrence_date, scope):
    """Delete a one-off item; for a series skip this occurrence, end it before this one, or delete it all."""
    if not item.recurrence or scope == ALL or (scope == FOLLOWING and occurrence_date <= item.date):
        DeletedSourceItem.record(item)                    # a source item never comes back once deleted
        item.delete()
        return
    if scope == THIS:
        current = current_occurrence(item) if item.one_at_a_time else None
        ItemException.objects.update_or_create(item=item, occurrence_date=occurrence_date, defaults={
            "action": ItemException.SKIP, "new_date": None, "new_start_time": None, "new_title": ""})
        ItemCompletion.objects.filter(item=item, occurrence_date=occurrence_date).delete()
        if current is not None and occurrence_date == current:
            _move_on(item, occurrence_date, today())        # "Skip this one": on to the next, by the same rules
        return
    item.recurrence_until, item.recurrence_count = occurrence_date - timedelta(days=1), None
    item.save()
    item.completions.filter(occurrence_date__gte=occurrence_date).delete()
    item.exceptions.filter(occurrence_date__gte=occurrence_date).delete()


def planner_users():
    """Everyone who can open the Planner (Admin > Users) - who an item can be shared with."""
    from django.contrib.auth import get_user_model
    return get_user_model().objects.filter(is_active=True).filter(
        Q(is_superuser=True) | Q(labs_preference__allowed_apps__contains=["planner"])).order_by("username")


# ---------------------------------------------------------------------------
# Sharing: "Share with" on the new / edit forms and quick add, and the remembered default
# ---------------------------------------------------------------------------

def person(user):
    """"Kim" - the first name, else the username."""
    return (user.first_name or user.get_username()).strip() if user else ""


def share_people(owner):
    """Who the owner can share with: everyone else who can open the Planner."""
    return list(planner_users().exclude(pk=owner.pk))


def default_shares(user):
    """The user's remembered "Share with" choice, {user pk: "view" | "edit"} - only people who still can open
    the Planner."""
    from .models import PlannerSettings
    saved = PlannerSettings.for_user(user).default_shares or {}
    people = {str(p.pk) for p in share_people(user)}
    return {int(pk): perm for pk, perm in saved.items() if pk in people and perm in dict(ItemShare.PERMISSIONS)}


def save_default_shares(user, choices):
    from .models import PlannerSettings
    prefs = PlannerSettings.for_user(user)
    prefs.default_shares = {str(pk): perm for pk, perm in choices.items() if perm}
    prefs.save(update_fields=["default_shares"])


def posted_shares(post, owner):
    """{user pk: "view" | "edit" | ""} from a form's "Share with" selects (share_<pk>); "" = not shared."""
    out = {}
    for p in share_people(owner):
        perm = post.get(f"share_{p.pk}", "")
        out[p.pk] = perm if perm in dict(ItemShare.PERMISSIONS) else ""
    return out


def set_shares(item, choices):
    """Share the item exactly as chosen ({user pk: perm or ""}) - only with people who can open the Planner."""
    allowed = {p.pk for p in share_people(item.owner)}
    for pk, perm in choices.items():
        if pk not in allowed:
            continue
        if perm:
            ItemShare.objects.update_or_create(item=item, user_id=pk, defaults={"permission": perm})
        else:
            ItemShare.objects.filter(item=item, user_id=pk).delete()


def apply_default_shares(item):
    """A new item made outside the form (from a note, the grocery list, the Bot API): shared as its owner chose
    by default."""
    set_shares(item, default_shares(item.owner))


def share_rows(owner, chosen):
    """The "Share with" section's rows: [{"user", "name", "perm"}] for everyone the owner can share with."""
    return [{"user": p, "name": person(p), "perm": chosen.get(p.pk, "")} for p in share_people(owner)]


def share_summary(rows):
    """"Shared with Kim" / "Shared with Kim +1" / "Only me" - quick add's chip."""
    names = [r["name"] for r in rows if r["perm"]]
    if not names:
        return "Only me"
    return f"Shared with {names[0]}" + (f" +{len(names) - 1}" if len(names) > 1 else "")


# ---------------------------------------------------------------------------
# Linked notes (ScheduleItem.note - the note's id; the note lives in Notes)
# ---------------------------------------------------------------------------

def linked_note(item, user, full=False):
    """What an item page shows about its note: {"title", "preview", "url"}; {"hidden": True} for a note this user
    can't see; {"deleted": True} once the note was deleted; None when there's none.
    full (the view panel): also the whole note as safe HTML (albright_notes.render), and - for someone who may
    edit the note and open Notes - tickable checklist boxes that save to the note (Notes' check endpoint, with
    the version it was rendered at)."""
    if item.note_id is None:
        return {"deleted": True} if item.note_deleted else None
    from albright_notes.models import Note
    from albright_notes.render import preview
    from django.urls import reverse
    note = Note.objects.visible_to(user).filter(pk=item.note_id).first()
    if note is None:
        return {"hidden": True}
    out = {"title": note.title, "preview": preview(note.body), "url": reverse("albright_notes:note", args=[note.pk]),
           "id": note.pk}
    if full:
        from albright_notes.render import render
        from .sources import can_open
        can_tick = note.can_edit(user) and can_open(user, "notes")
        out.update({"html": render(note.body, editable=can_tick) if note.body.strip() else "", "can_tick": can_tick,
                    "check_url": reverse("albright_notes:check", args=[note.pk]) if can_tick else "",
                    "version": note.version})
    return out


def attachable_notes(user, limit=200):
    """The notes this user may attach: theirs and ones shared with them, most recently changed first."""
    from albright_notes.models import Note
    return list(Note.objects.visible_to(user).filter(archived=False).order_by("-updated_at", "-pk")
                .only("pk", "title")[:limit])


def scheduled_for_note(note, user):
    """Notes' "Scheduled: <date>" chips: this user's visible items linked to the note, each with its next date
    (a repeating one's next occurrence from today, else its first) and a link to that day in the Planner."""
    from django.urls import reverse
    day = today()
    out = []
    for item in ScheduleItem.objects.visible_to(user).filter(note=note, archived=False).order_by("date", "pk"):
        when = item.date
        if item.recurrence:
            upcoming = recurrence.scheduled_dates(item, day, day + timedelta(days=400))
            when = upcoming[0] if upcoming else item.date
        out.append({"item": item, "date": when, "repeats": item.repeats,
                    "url": reverse("albright_planner:day") + f"?date={when.isoformat()}"})
    return out
