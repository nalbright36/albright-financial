"""When a roundup is made and what it covers - all in the owner's Planner zone (planner.user_zone).

  period: a roundup made before noon starts that day; from noon on, the next day (Sunday 6 PM's "Week ahead" is
          Mon-Sun). today = that one day; week = 7 days; month = that day through the same date next month minus one
          (the 1st: the whole month).
  next run: the first send_time after a moment on a day the schedule allows (daily, weekdays Mon-Fri, weekly on its
          weekdays, monthly on month_day). Stored in UTC; DST is handled by the zone.
"""
import calendar
from datetime import datetime, time, timedelta, timezone as dt_tz

from django.utils import timezone

from albright_planner import planner

from .models import DAILY, EMAIL, MONTH, MONTHLY, PUSH, TODAY, WEEK, WEEKDAYS, WEEKLY, Roundup

NOON = time(12, 0)
DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
PRESETS = [  # name, scope, freq, weekdays, month_day, send_time
    ("Week ahead", WEEK, WEEKLY, [6], 1, time(18, 0)),
    ("Today", TODAY, WEEKDAYS, [], 1, time(7, 0)),
    ("Month ahead", MONTH, MONTHLY, [], 1, time(8, 0)),
]
TIME_CHOICES = [time(h, m) for h in range(5, 23) for m in (0, 30)]


def local(user, moment=None):
    return (moment or timezone.now()).astimezone(planner.user_zone(user))


def period(scope, local_dt):
    """(start, end) dates a roundup of this scope made at local_dt covers."""
    start = local_dt.date() if local_dt.time() < NOON else local_dt.date() + timedelta(days=1)
    if scope == TODAY:
        return start, start
    if scope == WEEK:
        return start, start + timedelta(days=6)
    days = calendar.monthrange(start.year, start.month)[1]
    return start, start + timedelta(days=days - 1)


def current_period(roundup, now=None):
    return period(roundup.scope, local(roundup.owner, now))


def runs_on(roundup, day):
    if roundup.freq == DAILY:
        return True
    if roundup.freq == WEEKDAYS:
        return day.weekday() < 5
    if roundup.freq == WEEKLY:
        return day.weekday() in (roundup.weekdays or [6])
    if roundup.freq == MONTHLY:
        return day.day == min(max(roundup.month_day or 1, 1), 28)
    return False


def next_run(roundup, after=None):
    """The next time (UTC) it's due after `after` (default now), or None when the schedule never runs."""
    after = after or timezone.now()
    zone = planner.user_zone(roundup.owner)
    day = after.astimezone(zone).date()
    for offset in range(0, 63):
        d = day + timedelta(days=offset)
        if not runs_on(roundup, d):
            continue
        at = datetime.combine(d, roundup.send_time, tzinfo=zone)
        if at > after:
            return at.astimezone(dt_tz.utc)
    return None


def reschedule(roundup, after=None, save=True):
    roundup.next_run_at = next_run(roundup, after) if roundup.enabled else None
    if save:
        Roundup.objects.filter(pk=roundup.pk).update(next_run_at=roundup.next_run_at)
    return roundup.next_run_at


def ensure_presets(user):
    """The person's roundups - the three presets (all off) the first time. Returns them in order."""
    rows = list(Roundup.objects.filter(owner=user))
    if not rows:
        for n, (name, scope, freq, weekdays, month_day, at) in enumerate(PRESETS):
            Roundup.objects.create(owner=user, name=name, scope=scope, freq=freq, weekdays=weekdays,
                                   month_day=month_day, send_time=at, enabled=False, position=n,
                                   channels=[PUSH])
        rows = list(Roundup.objects.filter(owner=user))
    return rows


def preset(user, scope):
    """The person's roundup for a scope (the first one), creating the presets if needed."""
    rows = ensure_presets(user)
    return next((r for r in rows if r.scope == scope), rows[0])


def clock(t):
    """6:00 PM -> "6 PM", 6:30 PM -> "6:30 PM"."""
    if t is None:
        return ""
    h = t.hour % 12 or 12
    return f"{h}{'' if t.minute == 0 else f':{t.minute:02d}'} {'AM' if t.hour < 12 else 'PM'}"


def when_text(roundup):
    """"Sun 6 PM", "Weekdays 7 AM", "1st, 8 AM", "Daily 9 PM"."""
    at = clock(roundup.send_time)
    if roundup.freq == DAILY:
        return f"Daily {at}"
    if roundup.freq == WEEKDAYS:
        return f"Weekdays {at}"
    if roundup.freq == MONTHLY:
        d = roundup.month_day
        suffix = "th" if 11 <= d <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(d % 10, "th")
        return f"{d}{suffix}, {at}"
    days = "/".join(DAY_NAMES[d] for d in sorted(roundup.weekdays or [6]))
    return f"{days} {at}"


__all__ = ["period", "current_period", "next_run", "reschedule", "ensure_presets", "preset", "clock", "when_text",
           "PUSH", "EMAIL"]
