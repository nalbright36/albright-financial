"""Bot API v1 - Planner (/api/bot/v1/schedule...): a bot reads and adds to its owner's schedule.

Like Notes, these write straight to the Planner's own table. An item a bot creates belongs to the token's
owner and is labeled with the bot's name (ScheduleItem.created_by / from_bot). schedule:read lists the
occurrences in a date range (with whether each is checked off, and each one's checklist); schedule:write
creates items (with a checklist) and checks occurrences and their subtasks off (or back on) - only items the
owner may edit. If the owner can't open the Planner
(Admin > Users), every call is a 403. Every call is in the Activity log (auth.bot_endpoint)."""
from datetime import date, datetime, time, timedelta

from dateutil.rrule import rrulestr
from django.urls import reverse
from django.views.decorators.csrf import csrf_exempt

from .auth import ApiError, bot_endpoint, json_body

MAX_RANGE_DAYS = 366
DEFAULT_RANGE_DAYS = 7
FREQS = ("DAILY", "WEEKLY", "MONTHLY", "YEARLY")
RULE_KEYS = {"FREQ", "INTERVAL", "BYDAY", "BYMONTHDAY", "BYMONTH", "BYSETPOS", "WKST", "UNTIL", "COUNT"}


def _require_planner_access(token):
    from albright_labs import app_access
    from albright_labs.models import UserPreference
    if not app_access.can_use(token.owner, UserPreference.for_user(token.owner), "planner"):
        raise ApiError(403, "The token's owner can't open the Planner.", "rejected: owner has no Planner access")


def _date(value, key, required=False):
    if value in (None, ""):
        if required:
            raise ApiError(400, f'"{key}" is required (YYYY-MM-DD).', f"rejected: missing {key}")
        return None
    try:
        return date.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        pass
    raise ApiError(400, f'"{key}" must be a date (YYYY-MM-DD).', f"rejected: bad {key}")


def _time(value, key):
    if value in (None, ""):
        return None
    try:
        if isinstance(value, str):
            return time.fromisoformat(value).replace(second=0, microsecond=0, tzinfo=None)
    except ValueError:
        pass
    raise ApiError(400, f'"{key}" must be a time (HH:MM, 24-hour).', f"rejected: bad {key}")


def _str(data, key, limit, required=False):
    value = data.get(key)
    if value in (None, ""):
        if required:
            raise ApiError(400, f'"{key}" is required.', f"rejected: missing {key}")
        return ""
    if not isinstance(value, str):
        raise ApiError(400, f'"{key}" must be a string.', f"rejected: {key} not a string")
    value = value.strip()
    if len(value) > limit:
        raise ApiError(400, f'"{key}" is longer than {limit} characters.', f"rejected: {key} too long")
    if required and not value:
        raise ApiError(400, f'"{key}" is required.', f"rejected: missing {key}")
    return value


def _rrule(text, start):
    """A bot's RRULE -> (rule without its end, until, count). Only the simple parts the Planner uses."""
    rule = text.strip()
    if rule.upper().startswith("RRULE:"):
        rule = rule[6:]
    parts = {}
    for piece in rule.split(";"):
        key, sep, value = piece.partition("=")
        key = key.strip().upper()
        if not sep or key not in RULE_KEYS or not value.strip():
            raise ApiError(400, f'"rrule" may only use {", ".join(sorted(RULE_KEYS))} (KEY=VALUE;...).',
                           "rejected: bad rrule")
        parts[key] = value.strip().upper()
    if parts.get("FREQ") not in FREQS:
        raise ApiError(400, f'"rrule" needs FREQ={"|".join(FREQS)}.', "rejected: bad rrule freq")
    until = count = None
    if "UNTIL" in parts:
        raw = parts.pop("UNTIL")[:8]
        try:
            until = date(int(raw[:4]), int(raw[4:6]), int(raw[6:8]))
        except ValueError as exc:
            raise ApiError(400, '"rrule" UNTIL must be a date (YYYYMMDD).', "rejected: bad rrule until") from exc
    if "COUNT" in parts:
        raw = parts.pop("COUNT")
        if not raw.isdigit() or not 1 <= int(raw) <= 1000:
            raise ApiError(400, '"rrule" COUNT must be 1-1000.', "rejected: bad rrule count")
        count = int(raw)
    if until and count:
        raise ApiError(400, '"rrule" can have UNTIL or COUNT, not both.', "rejected: until and count")
    rule = ";".join(f"{k}={v}" for k, v in parts.items())
    if len(rule) > 500:
        raise ApiError(400, '"rrule" is too long.', "rejected: rrule too long")
    try:
        next(iter(rrulestr("RRULE:" + rule, dtstart=datetime.combine(start, time()))), None)
    except (ValueError, TypeError) as exc:
        raise ApiError(400, '"rrule" isn\'t a valid recurrence rule.', "rejected: invalid rrule") from exc
    return rule, until, count


def _repeat(data, start):
    """The same simple choices as the Planner's form ("repeat" + its options), or an "rrule" -> (rule, until, count)."""
    from albright_planner import recurrence
    if data.get("rrule") and data.get("repeat") not in (None, "", "none"):
        raise ApiError(400, 'Send "repeat" or "rrule", not both.', "rejected: repeat and rrule")
    if data.get("rrule"):
        if not isinstance(data["rrule"], str):
            raise ApiError(400, '"rrule" must be a string.', "rejected: bad rrule")
        rule, until, count = _rrule(data["rrule"], start)
    else:
        repeat = data.get("repeat") or "none"
        choices = [k for k, _ in recurrence.REPEATS]
        if repeat not in choices:
            raise ApiError(400, f'"repeat" must be one of: {", ".join(choices)}.', "rejected: bad repeat")
        days = data.get("days") or []
        if not isinstance(days, list) or any(d not in recurrence.WEEKDAYS for d in days):
            raise ApiError(400, f'"days" must be a list of {", ".join(recurrence.WEEKDAYS)}.', "rejected: bad days")
        nums = {}
        for key, low, high in (("interval", 1, 52), ("month_day", 1, 31)):
            value = data.get(key)
            if value is not None and (not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high):
                raise ApiError(400, f'"{key}" must be a whole number {low}-{high}.', f"rejected: bad {key}")
            nums[key] = value
        nth = data.get("nth")
        if nth is not None and nth not in (1, 2, 3, 4, -1):
            raise ApiError(400, '"nth" must be 1, 2, 3, 4 or -1 (the last).', "rejected: bad nth")
        weekday = data.get("nth_weekday")
        if weekday is not None and weekday not in recurrence.WEEKDAYS:
            raise ApiError(400, f'"nth_weekday" must be one of {", ".join(recurrence.WEEKDAYS)}.', "rejected: bad nth_weekday")
        rule = recurrence.build(repeat, start, days=days, interval=nums["interval"] or 1, month_day=nums["month_day"],
                                nth=nth, nth_weekday=weekday)
        until = count = None
    if not rule:
        if data.get("until") or data.get("count"):
            raise ApiError(400, '"until" and "count" need "repeat" or "rrule".', "rejected: end without repeat")
        return "", None, None
    if data.get("until") is not None or data.get("count") is not None:
        if until or count:
            raise ApiError(400, 'Give the end in the rrule or in "until"/"count", not both.', "rejected: two ends")
        until = _date(data.get("until"), "until")
        count = data.get("count")
        if count is not None and (not isinstance(count, int) or isinstance(count, bool) or not 1 <= count <= 1000):
            raise ApiError(400, '"count" must be a whole number 1-1000.', "rejected: bad count")
        if until and count:
            raise ApiError(400, 'Send "until" or "count", not both.', "rejected: until and count")
    if until and until < start:
        raise ApiError(400, '"until" is before "date".', "rejected: until before date")
    return rule, until, count


def _occurrence_json(o):
    return {"item_id": o.item.pk, "title": o.title, "date": o.date.isoformat(),
            "occurrence_date": o.occurrence_date.isoformat(), "start_time": o.start_time.strftime("%H:%M") if o.start_time else None,
            "end_time": o.end_time.strftime("%H:%M") if o.end_time else None, "kind": o.item.kind,
            "checkable": o.item.checkable, "done": o.done, "repeats": o.item.repeats,
            "one_at_a_time": o.item.one_at_a_time, "created_by": o.item.created_by,
            "owner": _person(o.item.owner), "mine": not getattr(o, "shared_in", False),
            "done_by": _person(o.done_by) if getattr(o, "done_by", None) else None,
            "checklist": [{"id": r.pk, "text": r.text, "done": r.checked,
                           "done_by": _person(r.done_by) if r.checked and r.done_by else None}
                          for r in getattr(o, "checklist", [])]}


def _person(user):
    from albright_planner.planner import person
    return person(user)


def _item_json(request, item):
    from albright_planner import recurrence
    return {"id": item.pk, "title": item.title, "kind": item.kind, "date": item.date.isoformat(),
            "start_time": item.start_time.strftime("%H:%M") if item.start_time else None,
            "end_time": item.end_time.strftime("%H:%M") if item.end_time else None, "checkable": item.checkable,
            "notes": item.notes, "rrule": item.recurrence or None,
            "until": item.recurrence_until.isoformat() if item.recurrence_until else None,
            "count": item.recurrence_count,
            "repeats": recurrence.summary(item.recurrence, item.date, item.recurrence_until, item.recurrence_count) or None,
            "one_at_a_time": item.one_at_a_time, "repeat_from_completion": item.repeat_from_completion,
            "created_by": item.created_by,
            "checklist": [{"id": r.pk, "text": r.text} for r in item.checklist.all()],
            "url": request.build_absolute_uri(reverse("albright_planner:item", args=[item.pk]))}


@bot_endpoint("schedule:read")
def schedule_list(request, token):
    from albright_planner import planner
    _require_planner_access(token)
    start = _date(request.GET.get("start"), "start") or planner.today()
    end = _date(request.GET.get("end"), "end") or start + timedelta(days=DEFAULT_RANGE_DAYS - 1)
    if end < start:
        raise ApiError(400, '"end" is before "start".', "rejected: end before start")
    if (end - start).days >= MAX_RANGE_DAYS:
        raise ApiError(400, f"The range can be at most {MAX_RANGE_DAYS} days.", "rejected: range too long")
    expand = request.GET.get("expand", "")
    if expand not in ("", "all"):
        raise ApiError(400, '"expand" must be "all" (or left out).', "rejected: bad expand")
    from albright_planner import checklist
    occs = checklist.attach(planner.occurrences(token.owner, start, end, expand=expand == "all"))
    rows = [_occurrence_json(o) for o in occs]
    return ({"start": start.isoformat(), "end": end.isoformat(), "occurrences": rows}, 200,
            f"listed {len(rows)} occurrences {start}..{end}" + (" (expand=all)" if expand else ""))


@bot_endpoint("schedule:write", methods=("POST",))
def schedule_create(request, token):
    from albright_planner.models import NOTES_MAX, ScheduleItem
    _require_planner_access(token)
    data = json_body(request)
    title = _str(data, "title", 200, required=True)
    start = _date(data.get("date"), "date", required=True)
    start_time, end_time = _time(data.get("start_time"), "start_time"), _time(data.get("end_time"), "end_time")
    if end_time and not start_time:
        raise ApiError(400, '"end_time" needs a "start_time".', "rejected: end without start")
    if start_time and end_time and end_time <= start_time:
        raise ApiError(400, '"end_time" must be after "start_time".', "rejected: end before start")
    kind = data.get("kind") or ScheduleItem.TASK
    if kind not in (ScheduleItem.TASK, ScheduleItem.EVENT):
        raise ApiError(400, '"kind" must be "task" or "event".', "rejected: bad kind")
    checkable = data.get("checkable", kind == ScheduleItem.TASK)
    if not isinstance(checkable, bool):
        raise ApiError(400, '"checkable" must be true or false.', "rejected: bad checkable")
    rule, until, count = _repeat(data, start)
    subtasks = data.get("checklist", [])
    if not isinstance(subtasks, list) or any(not isinstance(t, str) for t in subtasks):
        raise ApiError(400, '"checklist" must be a list of strings.', "rejected: bad checklist")
    if subtasks and kind != ScheduleItem.TASK:
        raise ApiError(400, 'Only a task can have a "checklist".', "rejected: checklist on an event")
    if len(subtasks) > 100 or any(len(t) > 200 for t in subtasks):
        raise ApiError(400, 'A "checklist" can have at most 100 items of at most 200 characters.',
                       "rejected: checklist too long")
    from_completion = data.get("repeat_from_completion", False)
    if not isinstance(from_completion, bool):
        raise ApiError(400, '"repeat_from_completion" must be true or false.', "rejected: bad repeat_from_completion")
    if from_completion and not (rule and kind == ScheduleItem.TASK and not until and not count):
        raise ApiError(400, '"repeat_from_completion" is for a repeating task with no end.',
                       "rejected: repeat_from_completion without an open-ended task")
    item = ScheduleItem.objects.create(
        owner=token.owner, title=title, date=start, start_time=start_time, end_time=end_time, kind=kind,
        checkable=True if kind == ScheduleItem.TASK else checkable, notes=_str(data, "notes", NOTES_MAX),
        recurrence=rule, recurrence_until=until, recurrence_count=count, repeat_from_completion=from_completion,
        created_by=token.name[:150], from_bot=True, bot_token=token)
    from albright_planner.checklist import create_rows
    from albright_planner.planner import apply_default_shares
    create_rows(item, subtasks)
    apply_default_shares(item)                       # shared as the owner chose by default
    return _item_json(request, item), 201, f"item #{item.pk}: {item.title[:80]}" + (f" ({rule})" if rule else "")


def _set_done(request, token, item_id, done):
    from albright_planner import planner
    from albright_planner.models import ScheduleItem
    _require_planner_access(token)
    item = ScheduleItem.objects.editable_by(token.owner).filter(pk=item_id, archived=False).first()
    if item is None:
        raise ApiError(404, f"No item {item_id} the owner can check off.", "rejected: item not found or not editable")
    if not item.checkable:
        raise ApiError(400, "That item can't be checked off.", "rejected: not checkable")
    data = json_body(request)
    occurrence = _date(data.get("occurrence_date"), "occurrence_date", required=item.repeats) or item.date
    result = planner.toggle(item, occurrence, token.owner, done=done)
    if result is None:
        raise ApiError(400, f"Item {item_id} isn't scheduled on {occurrence}.", "rejected: not scheduled that day")
    return ({"item_id": item.pk, "occurrence_date": occurrence.isoformat(), "done": result}, 200,
            f"{'completed' if done else 'uncompleted'} item #{item.pk} on {occurrence}")


def _set_subtask(request, token, item_id, row_id, done):
    from albright_planner import checklist, planner, sources
    from albright_planner.models import ScheduleItem
    _require_planner_access(token)
    item = ScheduleItem.objects.editable_by(token.owner).filter(pk=item_id, archived=False).first()
    if item is None:
        raise ApiError(404, f"No item {item_id} the owner can edit.", "rejected: item not found or not editable")
    row = item.checklist.select_related("grocery_item").filter(pk=row_id).first()
    if row is None:
        raise ApiError(404, f"Item {item_id} has no checklist item {row_id}.", "rejected: no such checklist item")
    if row.grocery_item_id and not sources.can_open(token.owner, "meals"):
        raise ApiError(403, "That checklist item is a grocery list line - the owner can't open Meals.",
                       "rejected: grocery row without Meals access")
    data = json_body(request)
    occurrence = _date(data.get("occurrence_date"), "occurrence_date", required=item.repeats) or item.date
    if not planner.valid_occurrence(item, occurrence) or not row.applies_to(occurrence):
        raise ApiError(400, f"Checklist item {row_id} isn't on item {item_id}'s {occurrence} occurrence.",
                       "rejected: not on that occurrence")
    checklist.set_checked(row, occurrence, done, token.owner)
    rows = checklist.occurrence_rows(item, occurrence)
    return ({"item_id": item.pk, "checklist_item_id": row.pk, "occurrence_date": occurrence.isoformat(),
             "done": next(r.checked for r in rows if r.pk == row.pk), "progress": checklist.progress(rows)["label"],
             "task_done": item.completions.filter(occurrence_date=occurrence).exists()}, 200,
            f"{'ticked' if done else 'unticked'} item #{item.pk} subtask #{row.pk} on {occurrence}")


@bot_endpoint("schedule:write", methods=("POST",))
def schedule_subtask_complete(request, token, item_id, row_id):
    return _set_subtask(request, token, item_id, row_id, True)


@bot_endpoint("schedule:write", methods=("POST",))
def schedule_subtask_uncomplete(request, token, item_id, row_id):
    return _set_subtask(request, token, item_id, row_id, False)


@bot_endpoint("schedule:write", methods=("POST",))
def schedule_complete(request, token, item_id):
    return _set_done(request, token, item_id, True)


@bot_endpoint("schedule:write", methods=("POST",))
def schedule_uncomplete(request, token, item_id):
    return _set_done(request, token, item_id, False)


@csrf_exempt
def schedule_collection(request):
    """GET lists (schedule:read); POST creates (schedule:write)."""
    return schedule_create(request) if request.method == "POST" else schedule_list(request)
