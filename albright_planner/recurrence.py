"""Repeating items: the "Repeat" choices <-> an RFC 5545 RRULE, a plain-English
summary, and the occurrences in a date range (python-dateutil's rrule - only
ever for the dates on screen).

The rule is stored without its end (ScheduleItem.recurrence); the end is
recurrence_until or recurrence_count. Choices (the form never shows a raw rule):
  none          Does not repeat
  daily         FREQ=DAILY
  every_n_days  FREQ=DAILY;INTERVAL=N
  weekdays      FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR
  weekly        FREQ=WEEKLY;BYDAY=<chosen days>
  every_n_weeks FREQ=WEEKLY;INTERVAL=N;BYDAY=<chosen days>
  monthly_day   FREQ=MONTHLY;BYMONTHDAY=X
  monthly_nth   FREQ=MONTHLY;BYDAY=2TU   (the Nth weekday; -1 = the last)
  yearly        FREQ=YEARLY
"""
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

from dateutil.relativedelta import relativedelta
from dateutil.rrule import rrulestr

WEEKDAYS = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"]          # date.weekday() order
DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
LONG_DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
ORDINALS = {1: "1st", 2: "2nd", 3: "3rd", 4: "4th", 5: "5th", -1: "last"}
REPEATS = [("none", "Does not repeat"), ("daily", "Daily"), ("every_n_days", "Every N days"),
           ("weekdays", "Every weekday (Mon-Fri)"),
           ("weekly", "Weekly on chosen days"), ("every_n_weeks", "Every N weeks"),
           ("monthly_day", "Monthly on day X"), ("monthly_nth", "Monthly on the Nth weekday"), ("yearly", "Yearly")]
NTH_CHOICES = [(1, "1st"), (2, "2nd"), (3, "3rd"), (4, "4th"), (-1, "last")]
MAX_SCAN_YEARS = 50


# ---------------------------------------------------------------------------
# Choices <-> rule
# ---------------------------------------------------------------------------

def build(repeat, start, days=(), interval=1, month_day=None, nth=None, nth_weekday=None):
    """The RRULE (no end) for a "Repeat" choice. start: the item's date - the default day/weekday."""
    days = [d for d in WEEKDAYS if d in set(days)] or [WEEKDAYS[start.weekday()]]
    if repeat == "daily":
        return "FREQ=DAILY"
    if repeat == "every_n_days":
        n = max(1, int(interval or 1))
        return "FREQ=DAILY" if n == 1 else f"FREQ=DAILY;INTERVAL={n}"
    if repeat == "weekdays":
        return "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR"
    if repeat == "weekly":
        return "FREQ=WEEKLY;BYDAY=" + ",".join(days)
    if repeat == "every_n_weeks":
        return f"FREQ=WEEKLY;INTERVAL={max(1, int(interval or 1))};BYDAY=" + ",".join(days)
    if repeat == "monthly_day":
        return f"FREQ=MONTHLY;BYMONTHDAY={int(month_day or start.day)}"
    if repeat == "monthly_nth":
        nth = int(nth or nth_of(start))
        weekday = nth_weekday if nth_weekday in WEEKDAYS else WEEKDAYS[start.weekday()]
        return f"FREQ=MONTHLY;BYDAY={nth}{weekday}"
    if repeat == "yearly":
        return "FREQ=YEARLY"
    return ""


def nth_of(day):
    """Which weekday-of-the-month this is: 1-4, or -1 (the last) for a 5th."""
    n = (day.day - 1) // 7 + 1
    return -1 if n >= 5 else n


def parts(rule):
    """{"FREQ": "WEEKLY", "BYDAY": "MO,WE", ...} of a stored rule."""
    out = {}
    for piece in (rule or "").split(";"):
        if "=" in piece:
            key, value = piece.split("=", 1)
            out[key.strip().upper()] = value.strip().upper()
    return out


def describe(rule):
    """The form's choices for a stored rule: {repeat, days, interval, month_day, nth, nth_weekday}."""
    p = parts(rule)
    freq, byday, interval = p.get("FREQ"), [d for d in p.get("BYDAY", "").split(",") if d], int(p.get("INTERVAL", 1))
    out = {"repeat": "none", "days": [], "interval": interval, "month_day": None, "nth": None, "nth_weekday": None}
    if freq == "DAILY":
        out["repeat"] = "daily" if interval == 1 else "every_n_days"
    elif freq == "WEEKLY":
        if interval == 1 and byday == WEEKDAYS[:5]:
            out["repeat"] = "weekdays"
        else:
            out["repeat"] = "weekly" if interval == 1 else "every_n_weeks"
        out["days"] = byday
    elif freq == "MONTHLY" and "BYMONTHDAY" in p:
        out.update(repeat="monthly_day", month_day=int(p["BYMONTHDAY"]))
    elif freq == "MONTHLY" and byday:
        out.update(repeat="monthly_nth", nth=int(byday[0][:-2]), nth_weekday=byday[0][-2:])
    elif freq == "YEARLY":
        out["repeat"] = "yearly"
    return out


def _date_text(day, today=None):
    today = today or date.today()
    return f"{day:%b} {day.day}" + (f", {day.year}" if day.year != today.year else "")


def summary(rule, start, until=None, count=None, today=None):
    """ "Every Mon, Wed, Fri until Dec 31" - or "" for a one-off."""
    if not rule:
        return ""
    d = describe(rule)
    days = ", ".join(DAY_NAMES[WEEKDAYS.index(x)] for x in d["days"] if x in WEEKDAYS)
    text = {
        "daily": "Every day",
        "every_n_days": f"Every {d['interval']} days",
        "weekdays": "Every weekday",
        "weekly": f"Every {days}",
        "every_n_weeks": f"Every {d['interval']} weeks on {days}",
        "monthly_day": f"Monthly on day {d['month_day']}",
        "monthly_nth": f"Monthly on the {ORDINALS.get(d['nth'], d['nth'])} "
                       f"{LONG_DAY_NAMES[WEEKDAYS.index(d['nth_weekday'])] if d['nth_weekday'] in WEEKDAYS else ''}",
        "yearly": f"Every year on {start:%b} {start.day}",
    }.get(d["repeat"], "Repeats")
    if until:
        text += f" until {_date_text(until, today)}"
    elif count:
        text += f", {count} time{'s' if count != 1 else ''}"
    return text


# ---------------------------------------------------------------------------
# Occurrences
# ---------------------------------------------------------------------------

def _rule(item):
    rule = rrulestr("RRULE:" + item.recurrence, dtstart=datetime.combine(item.date, time()))
    if item.recurrence_until:
        rule = rule.replace(until=datetime.combine(item.recurrence_until, time(23, 59, 59)), count=None)
    elif item.recurrence_count:
        rule = rule.replace(count=item.recurrence_count, until=None)
    return rule


def scheduled_dates(item, start, end):
    """The dates the item is scheduled for between start and end (inclusive), before any exception."""
    if not item.recurrence:
        return [item.date] if start <= item.date <= end else []
    return [d.date() for d in _rule(item).between(datetime.combine(start, time()), datetime.combine(end, time(23, 59, 59)),
                                                   inc=True)]


def dates_from(item, day, limit=5000):
    """The scheduled dates on or after `day`, in order (a generator - stops after `limit`)."""
    if not item.recurrence:
        if item.date >= day:
            yield item.date
        return
    rule = _rule(item)
    current = rule.after(datetime.combine(max(day, item.date), time()), inc=True)
    while current is not None and limit > 0:
        yield current.date()
        limit -= 1
        current = rule.after(current, inc=False)


def first_after(item, day):
    """The first scheduled date after `day` (None if the series has ended)."""
    return next(dates_from(item, day + timedelta(days=1)), None)


def step_from(item, day):
    """"Repeat from completion date": the next date after something done on `day` - `day` plus the repeat interval
    (every 3 days -> 3 days later; weekly -> 7 days later; monthly -> the same day next month, or the month's
    last day). A weekly rule on several days (e.g. every weekday) -> the next of those days."""
    p = parts(item.recurrence)
    n = max(1, int(p.get("INTERVAL", 1) or 1))
    freq = p.get("FREQ")
    if freq == "WEEKLY":
        days = [d for d in p.get("BYDAY", "").split(",") if d in WEEKDAYS]
        if len(days) > 1:
            return next(day + timedelta(days=i) for i in range(1, 8)
                        if WEEKDAYS[(day + timedelta(days=i)).weekday()] in days)
        return day + timedelta(weeks=n)
    if freq == "MONTHLY":
        return day + relativedelta(months=n)
    if freq == "YEARLY":
        return day + relativedelta(years=n)
    return day + timedelta(days=n)


def from_completion_text(rule):
    """"every 3 days" - how far apart "Repeat from completion date" puts them."""
    p = parts(rule)
    n = max(1, int(p.get("INTERVAL", 1) or 1))
    freq = p.get("FREQ")
    days = [d for d in p.get("BYDAY", "").split(",") if d in WEEKDAYS]
    if freq == "WEEKLY" and len(days) > 1:
        return "on the next of " + ", ".join(DAY_NAMES[WEEKDAYS.index(d)] for d in days)
    unit = {"DAILY": "day", "WEEKLY": "week", "MONTHLY": "month", "YEARLY": "year"}.get(freq, "day")
    return f"every {unit}" if n == 1 else f"every {n} {unit}s"


def is_scheduled(item, day):
    if not item.recurrence:
        return day == item.date
    return bool(scheduled_dates(item, day, day))


def count_before(item, day):
    """How many occurrences fall before `day` (for "this and following" on a counted series)."""
    if day <= item.date:
        return 0
    return len(scheduled_dates(item, item.date, day - timedelta(days=1)))


@dataclass
class Occurrence:
    item: object
    date: date                       # the day it shows on (after a move)
    occurrence_date: date            # the day it was scheduled for (completions and exceptions are keyed by this)
    title: str
    start_time: time = None
    end_time: time = None
    done: bool = False
    exception: object = None
    can_edit: bool = False
    overdue: bool = False
    tags: list = field(default_factory=list)

    @property
    def moved(self):
        return self.date != self.occurrence_date

    @property
    def key(self):
        return f"{self.item.pk}-{self.occurrence_date.isoformat()}"


def _shifted_end(item, new_start):
    if not (item.start_time and item.end_time and new_start):
        return item.end_time if not new_start else None
    span = datetime.combine(date.min, item.end_time) - datetime.combine(date.min, item.start_time)
    end = (datetime.combine(date.min, new_start) + span)
    return end.time() if end.date() == date.min else time(23, 59)


def make(item, original, exc=None, done=False):
    """The occurrence scheduled for `original`, after its exception (moved / retimed / retitled) if any."""
    title, start_time, end_time, day = item.title, item.start_time, item.end_time, original
    if exc is not None:
        if exc.new_title:
            title = exc.new_title
        if exc.new_start_time:
            start_time, end_time = exc.new_start_time, _shifted_end(item, exc.new_start_time)
        if exc.action == "move" and exc.new_date:
            day = exc.new_date
    return Occurrence(item, day, original, title, start_time, end_time, done, exc)


def occurrences(item, start, end, completed=None, exceptions=None):
    """The item's occurrences shown between start and end (inclusive): skipped ones dropped, moved ones on their
    new day (including ones moved INTO the range from outside it), edited ones retitled.
    completed: a set of done occurrence dates; exceptions: {occurrence_date: ItemException} (else queried)."""
    if exceptions is None:
        exceptions = {e.occurrence_date: e for e in item.exceptions.all()}
    if completed is None:
        completed = set(item.completions.values_list("occurrence_date", flat=True))
    out = []

    def add(day, original, exc):
        occ = make(item, original, exc, original in completed)
        occ.date = day
        out.append(occ)

    seen = set()
    for day in scheduled_dates(item, start, end):
        seen.add(day)
        exc = exceptions.get(day)
        if exc is not None and exc.action == "skip":
            continue
        if exc is not None and exc.action == "move" and exc.new_date and exc.new_date != day:
            if start <= exc.new_date <= end:
                add(exc.new_date, day, exc)
            continue
        add(day, day, exc)
    # Occurrences moved in from outside the range.
    for original, exc in exceptions.items():
        if exc.action == "move" and exc.new_date and start <= exc.new_date <= end and original not in seen \
                and not start <= original <= end and is_scheduled(item, original):
            add(exc.new_date, original, exc)
    out.sort(key=lambda o: (o.date, o.start_time or time.min, o.title))
    return out


# ---------------------------------------------------------------------------
# Changing a series
# ---------------------------------------------------------------------------

def shift(rule, old, new):
    """The rule after moving the whole series from `old` to `new`: weekdays and month-days move with it."""
    p = parts(rule)
    if not p:
        return rule
    delta = (new - old).days
    if "BYDAY" in p:
        moved = []
        for day in p["BYDAY"].split(","):
            prefix, code = day[:-2], day[-2:]
            if code in WEEKDAYS:
                code = WEEKDAYS[(WEEKDAYS.index(code) + delta) % 7]
            if prefix and p.get("FREQ") == "MONTHLY":
                prefix = str(nth_of(new))
            moved.append(prefix + code)
        p["BYDAY"] = ",".join(sorted(set(moved), key=lambda d: (d[:-2], WEEKDAYS.index(d[-2:]) if d[-2:] in WEEKDAYS else 9)))
    if "BYMONTHDAY" in p:
        p["BYMONTHDAY"] = str(new.day)
    order = ["FREQ", "INTERVAL", "BYDAY", "BYMONTHDAY"]
    return ";".join(f"{k}={p[k]}" for k in order if k in p) + "".join(f";{k}={v}" for k, v in p.items() if k not in order)
