"""Weigh-in schedules: "Weigh-in" tasks on my Planner, logged with a weight right from the task.

  - A WeighInSchedule (daily, weekly on chosen days, every N days, or monthly on a day - a shorter month uses its
    last day) has ONE repeating Planner task, made the way Fitness's exercise schedules are (schedule.py): mine,
    private, Fitness yellow, source "fitness", source_id "weighin-<pk>", through albright_planner.services. With no
    end date it shows one occurrence at a time; times are the Planner's (my zone).
  - Editing, pausing or deleting never touches a completed weigh-in: the task is ended after its last completed
    day (today included when today's is done) and a fresh one starts after that; a task nothing was completed on is
    simply replaced. Weights are never deleted.
  - Logging from the task (Planner rows, the view panel, Home's Today panel) is body.log_weight - the Body page's
    own - for that occurrence's date; it replaces a weight already logged that day (after asking). Any weigh-in
    logged elsewhere (the Body page, the dashboard, the Bot API) checks off that day's weigh-in too: after_weight().
  - "Skip today" checks it off with no weight (WeighInLog.skipped, shown greyed). Unchecking reopens it and keeps
    the weight - "Weight kept - edit it on the Body page."
  - Done <=> weight logged, the same pattern as workouts (workout_sync): checked off anywhere without a weight, that
    day's weight counts (or it's a skip) - on_toggle(); a weight removed on the Body page reopens what it checked off.
"""
from datetime import date, time, timedelta
from decimal import Decimal

from django import forms
from django.db import transaction
from django.db.models.signals import post_delete, pre_delete
from django.dispatch import receiver
from django.urls import reverse

from . import body
from .models import NutritionProfile, WeighInLog, WeighInSchedule, WeightEntry

SOURCE = "fitness"
TITLE = "Weigh-in"
DEFAULT_TIME = time(7, 0)
DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
RRULE_DAYS = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"]
KEPT_NOTE = "Weight kept - edit it on the Body page."
LIMITS = {"lb": (Decimal(45), Decimal(880)), "kg": (Decimal(20), Decimal(400))}   # the Body page's (body_views)


def ordinal(n):
    return f"{n}{'th' if 11 <= n % 100 <= 13 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def clock(t):
    return f"{t.hour % 12 or 12}:{t.minute:02d} {'AM' if t.hour < 12 else 'PM'}"


# ---------------------------------------------------------------------------
# The form
# ---------------------------------------------------------------------------

class ScheduleForm(forms.Form):
    frequency = forms.ChoiceField(choices=WeighInSchedule.FREQUENCIES, initial=WeighInSchedule.WEEKLY,
                                  widget=forms.RadioSelect)
    days_of_week = forms.TypedMultipleChoiceField(choices=list(enumerate(DAY_NAMES)), coerce=int, required=False,
                                                  widget=forms.CheckboxSelectMultiple)
    interval_n = forms.IntegerField(required=False, min_value=2, max_value=60, initial=3)
    day_of_month = forms.IntegerField(required=False, min_value=1, max_value=31, initial=1)
    time_of_day = forms.TimeField(required=False, initial=DEFAULT_TIME,
                                  widget=forms.TimeInput(attrs={"type": "time", "id": "fx-wis-time", "class": "ledger-input"},
                                                           format="%H:%M"))
    start_date = forms.DateField(required=False, widget=forms.DateInput(attrs={"type": "date"}))
    end_date = forms.DateField(required=False, widget=forms.DateInput(attrs={"type": "date"}))

    def __init__(self, *args, today=None, **kwargs):
        self.today = today or date.today()
        super().__init__(*args, **kwargs)

    def clean(self):
        data = super().clean()
        freq = data.get("frequency")
        if freq == WeighInSchedule.WEEKLY and not data.get("days_of_week"):
            self.add_error("days_of_week", "Pick at least one day.")
        if freq == WeighInSchedule.EVERY_N_DAYS and not data.get("interval_n"):
            self.add_error("interval_n", "How many days apart? (2 to 60)")
        if freq == WeighInSchedule.MONTHLY and not data.get("day_of_month"):
            self.add_error("day_of_month", "Which day of the month? (1 to 31)")
        data["start_date"] = data.get("start_date") or self.today
        if data.get("end_date") and data["end_date"] < data["start_date"]:
            self.add_error("end_date", "The end date is before the start date.")
        return data

    def apply(self, schedule):
        """Copy the cleaned choices onto a WeighInSchedule (unsaved); fields that don't apply are cleared."""
        d = self.cleaned_data
        schedule.frequency = d["frequency"]
        schedule.days_of_week = sorted(set(d.get("days_of_week") or [])) if d["frequency"] == WeighInSchedule.WEEKLY else []
        schedule.interval_n = d.get("interval_n") if d["frequency"] == WeighInSchedule.EVERY_N_DAYS else None
        schedule.day_of_month = d.get("day_of_month") if d["frequency"] == WeighInSchedule.MONTHLY else None
        schedule.time_of_day = d.get("time_of_day")
        schedule.start_date = d["start_date"]
        schedule.end_date = d.get("end_date")
        return schedule

    @classmethod
    def initial_for(cls, schedule, today):
        if schedule is None:
            return {"frequency": WeighInSchedule.WEEKLY, "days_of_week": [today.weekday()], "interval_n": 3,
                    "day_of_month": today.day, "time_of_day": DEFAULT_TIME, "start_date": today}
        return {"frequency": schedule.frequency, "days_of_week": schedule.days_of_week,
                "interval_n": schedule.interval_n or 3, "day_of_month": schedule.day_of_month or today.day,
                "time_of_day": schedule.time_of_day, "start_date": schedule.start_date, "end_date": schedule.end_date}


# ---------------------------------------------------------------------------
# Rules, dates and plain words
# ---------------------------------------------------------------------------

def rule_for(schedule):
    """The Planner RRULE (no end) for a schedule. Monthly on 29-31 falls back to a shorter month's last day."""
    freq = schedule.frequency
    if freq == WeighInSchedule.DAILY:
        return "FREQ=DAILY"
    if freq == WeighInSchedule.EVERY_N_DAYS:
        return f"FREQ=DAILY;INTERVAL={max(2, int(schedule.interval_n or 2))}"
    if freq == WeighInSchedule.WEEKLY:
        days = sorted({int(d) for d in schedule.days_of_week or [] if 0 <= int(d) <= 6}) or [schedule.start_date.weekday()]
        return "FREQ=WEEKLY;BYDAY=" + ",".join(RRULE_DAYS[d] for d in days)
    day = int(schedule.day_of_month or schedule.start_date.day)
    if day >= 31:
        return "FREQ=MONTHLY;BYMONTHDAY=-1"
    if day >= 29:
        return f"FREQ=MONTHLY;BYMONTHDAY={day},-1;BYSETPOS=1"
    return f"FREQ=MONTHLY;BYMONTHDAY={day}"


def _series(schedule):
    """An unsaved ScheduleItem carrying the schedule's rule from its start date (for working out dates)."""
    from albright_planner.models import ScheduleItem
    return ScheduleItem(date=schedule.start_date, recurrence=rule_for(schedule), recurrence_until=schedule.end_date)


def dates(schedule, start, limit=3):
    """The next `limit` weigh-in dates on or after `start` (the schedule's rule, start and end dates)."""
    from albright_planner import recurrence
    out = []
    for day in recurrence.dates_from(_series(schedule), max(start, schedule.start_date)):
        out.append(day)
        if len(out) >= limit:
            break
    return out


def describe(schedule):
    """ "Every Mon & Thu at 7:00 AM", "Every 3 days", "Monthly on the 31st (or the last day) at 7:00 AM"."""
    freq = schedule.frequency
    if freq == WeighInSchedule.DAILY:
        text = "Every day"
    elif freq == WeighInSchedule.EVERY_N_DAYS:
        text = f"Every {schedule.interval_n} days"
    elif freq == WeighInSchedule.WEEKLY:
        names = [DAY_NAMES[d] for d in sorted(schedule.days_of_week or [])]
        text = "Every " + (", ".join(names[:-1]) + " & " + names[-1] if len(names) > 1 else (names[0] if names else "week"))
    else:
        day = schedule.day_of_month or 1
        text = f"Monthly on the {ordinal(day)}" + (" (or the last day)" if day >= 29 else "")
    if schedule.time_of_day:
        text += f" at {clock(schedule.time_of_day)}"
    if schedule.end_date:
        text += f" until {schedule.end_date:%b} {schedule.end_date.day}, {schedule.end_date.year}"
    return text


def current(user):
    """The person's schedule (the active one, else their latest paused one), or None."""
    return WeighInSchedule.objects.filter(owner=user).order_by("-active", "-pk").select_related("planner_item").first()


def today_row(user):
    """Today's weigh-in for Fitness Today: {"item", "date", "occ", "wi"} or None if nothing is scheduled today."""
    from albright_planner import planner, recurrence
    schedule = WeighInSchedule.objects.filter(owner=user, active=True, planner_item__isnull=False) \
        .select_related("planner_item").first()
    if schedule is None:
        return None
    item, day = schedule.planner_item, _today(user)
    has = item.completions.filter(occurrence_date=day).exists()
    if item.one_at_a_time:
        due = planner.current_occurrence(item)
        if not has and due != day:
            return None
    elif not recurrence.is_scheduled(item, day) and not has:
        return None
    if not planner.valid_occurrence(item, day):
        return None
    occ = planner.occurrence_for(item, day, user)
    return {"item": item, "date": day, "occ": occ, "wi": box(item, day, occ.done, user)}


# ---------------------------------------------------------------------------
# The Planner task: make, end, replace - only through albright_planner.services
# ---------------------------------------------------------------------------

def _today(user):
    from albright_planner import planner
    return planner.today(user)


def _retire(item, today):
    """Stop a weigh-in task without touching what was completed: removed outright when nothing was, else ended
    after its last completed day (today's included) and unlinked. Returns the last day it keeps, or None."""
    from albright_planner import services
    # Unlink first: deleting the task in the Planner deletes its schedule (CASCADE) - not when we replace it here.
    WeighInSchedule.objects.filter(planner_item=item).update(planner_item=None)
    done = set(item.completions.values_list("occurrence_date", flat=True))
    if not done:
        services.delete_scheduled_task(item)
        return None
    keep = max(today if today in done else today - timedelta(days=1), max(done))
    item.recurrence_until, item.recurrence_count = keep, None
    item.source_id = f"{item.source_id}-to-{keep:%Y%m%d}-{item.pk}"[:64]       # frees "weighin-<pk>" for the next
    item.save(update_fields=["recurrence_until", "recurrence_count", "source_id", "updated_at"])
    return keep


def _start_task(schedule, after=None):
    """Make the schedule's task from its first date on or after max(start, today, after + 1) - or none when the
    schedule has ended by then."""
    from albright_planner import services
    today = _today(schedule.owner)
    floor = max(schedule.start_date, today, (after + timedelta(days=1)) if after else today)
    first = next(iter(dates(schedule, floor, limit=1)), None)
    if first is None:
        schedule.planner_item = None
    else:
        schedule.planner_item = services.create_scheduled_task(
            schedule.owner, SOURCE, schedule.source_id, TITLE, first, schedule.time_of_day, rule=rule_for(schedule),
            until=schedule.end_date, url=reverse("albright_fitness:body") + "#fx-weigh-in-schedule")
    schedule.save(update_fields=["planner_item"])
    return schedule.planner_item


def _plan(schedule):
    return (rule_for(schedule), schedule.time_of_day, schedule.start_date, schedule.end_date)


@transaction.atomic
def save(user, form, schedule=None):
    """Create (replacing any other schedule - the form asked first) or edit a schedule, and bring its task up to
    date: future, not-completed weigh-ins follow the new plan; completed ones stay as they were."""
    today = _today(user)
    if schedule is None:
        for old in WeighInSchedule.objects.filter(owner=user):
            remove(old)
        schedule = form.apply(WeighInSchedule(owner=user, active=True))
        schedule.save()
        _start_task(schedule)
        return schedule
    before = _plan(schedule)
    form.apply(schedule)
    schedule.save()
    if schedule.active and (before != _plan(schedule) or schedule.planner_item_id is None):
        after = _retire(schedule.planner_item, today) if schedule.planner_item_id else None
        schedule.planner_item = None
        _start_task(schedule, after)
    return schedule


@transaction.atomic
def pause(schedule):
    """Future not-completed weigh-ins go; completed ones and every weight stay."""
    if schedule.planner_item_id:
        _retire(schedule.planner_item, _today(schedule.owner))
    schedule.active, schedule.planner_item = False, None
    schedule.save(update_fields=["active", "planner_item"])


@transaction.atomic
def resume(schedule):
    WeighInSchedule.objects.filter(owner=schedule.owner, active=True).exclude(pk=schedule.pk).update(active=False)
    schedule.active = True
    schedule.save(update_fields=["active"])
    _start_task(schedule)


@transaction.atomic
def remove(schedule):
    """Delete a schedule: its future weigh-ins go, completed ones and every weight stay."""
    if schedule.planner_item_id:
        _retire(schedule.planner_item, _today(schedule.owner))
    WeighInSchedule.objects.filter(pk=schedule.pk).update(planner_item=None)
    WeighInSchedule.objects.filter(pk=schedule.pk).delete()


# ---------------------------------------------------------------------------
# Logging from the task, and checking off from anywhere else
# ---------------------------------------------------------------------------

def is_weigh_in(item):
    return item is not None and item.is_weigh_in


def unit_of(user):
    return NutritionProfile.load(user).weight_unit


def parse(value, unit):
    """A weight in `unit` within the Body page's limits, to one decimal - or None."""
    from decimal import InvalidOperation
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, ValueError, TypeError):
        return None
    low, high = LIMITS[unit]
    if not number.is_finite() or not low <= number <= high:
        return None
    return body.q(number)


def _record(item, day, entry=None, skipped=False, reason="", note=""):
    """What the occurrence was: a weight, or skipped - the task's status follows (skipped: skip mark, muted)."""
    from albright_planner import services
    WeighInLog.objects.update_or_create(item=item, occurrence_date=day,
                                        defaults={"weight_entry": entry, "skipped": skipped})
    if skipped:
        services.set_skipped(item, day, reason, note)
    else:
        services.clear_skipped(item, day)


def _occurrence_for(item, day):
    """The occurrence of `item` a weigh-in on `day` checks off: that day's, or - one at a time - the current one
    when it's due on or before a scheduled `day`. None if there isn't one."""
    from albright_planner import planner, recurrence
    if item is None or item.archived:
        return None
    if planner.valid_occurrence(item, day):
        return day
    if item.one_at_a_time and recurrence.is_scheduled(item, day):
        now = planner.current_occurrence(item)
        if now is not None and now <= day:
            return now
    return None


def log(item, day, value, unit, user):
    """Log a weight for one weigh-in occurrence (the Body page's body.log_weight for `day`, replacing a weight
    already logged that day) and check it off. Returns the WeightEntry."""
    entry, _created = body.log_weight(user, day, value, unit)      # after_weight() checks off what it matches
    complete(item, day, user, entry=entry)
    return entry


def skip(item, day, user, reason="", note=""):
    complete(item, day, user, skipped=True, reason=reason, note=note)


def complete(item, day, user, entry=None, skipped=False, reason="", note=""):
    from albright_planner import planner

    from .workout_sync import from_fitness
    if not item.completions.filter(occurrence_date=day).exists():
        with from_fitness():
            planner.toggle(item, day, user, done=True)
    _record(item, day, entry, skipped, reason, note)


def after_weight(entry):
    """A weigh-in was logged (the Body page, the dashboard, the Bot API, a task): check off that day's weigh-in
    task, if the person has one and it isn't done. Returns the Planner items checked off."""
    from albright_planner import planner
    done = []
    for schedule in WeighInSchedule.objects.filter(owner=entry.owner, active=True, planner_item__isnull=False) \
            .select_related("planner_item"):
        item = schedule.planner_item
        day = _occurrence_for(item, entry.date)
        if day is None:
            continue
        if not item.completions.filter(occurrence_date=day).exists():
            from .workout_sync import from_fitness
            with from_fitness():
                if planner.toggle(item, day, entry.owner, done=True) is None:
                    continue
            done.append(item)
        _record(item, day, entry)
    return done


def on_toggle(item, day, done, user):
    """The Planner / Home / Bot API checked a weigh-in off or reopened it (workout_sync.on_planner_toggle). Checked
    off with no weight saved through it: that day's weight counts if there is one, else it's a skip (greyed).
    Reopened: the _reopened receiver drops the record - the weight itself always stays."""
    if done and not WeighInLog.objects.filter(item=item, occurrence_date=day).exists():
        entry = WeightEntry.objects.filter(owner=item.owner, date=day).first()
        _record(item, day, entry, skipped=entry is None)


@receiver(pre_delete, sender=WeightEntry, dispatch_uid="fitness_weigh_in_weight_deleted")
def _weight_deleted(sender, instance, **kwargs):
    """A weight removed (the Body page): the weigh-in it checked off is open again - done means a weight is logged."""
    from albright_planner import planner

    from .workout_sync import from_fitness
    for logged in WeighInLog.objects.filter(weight_entry=instance).select_related("item"):
        with from_fitness():
            planner.toggle(logged.item, logged.occurrence_date, instance.owner, done=False)


@receiver(post_delete, sender="albright_planner.ItemCompletion", dispatch_uid="fitness_weigh_in_reopened")
def _reopened(sender, instance, **kwargs):
    """Unchecking a weigh-in drops what was recorded for it - never the weight itself."""
    WeighInLog.objects.filter(item_id=instance.item_id, occurrence_date=instance.occurrence_date).delete()


# ---------------------------------------------------------------------------
# What a weigh-in task shows
# ---------------------------------------------------------------------------

def _previous(entry):
    return WeightEntry.objects.filter(owner=entry.owner, date__lt=entry.date).order_by("-date").first()


def result_text(entry, unit):
    """ "182.0 lb (−0.4 since last)" - the change from the weigh-in before it, when there is one."""
    shown = body.shown(entry.weight_kg, unit)
    before = _previous(entry)
    if before is None:
        return f"{shown} {unit}"
    diff = shown - body.shown(before.weight_kg, unit)
    sign = "+" if diff > 0 else "−" if diff < 0 else "±"
    return f"{shown} {unit} ({sign}{abs(diff)} since last)"


def last_weight(user, day):
    entry = WeightEntry.objects.filter(owner=user, date__lte=day).order_by("-date").first()
    return entry


def box(item, day, done, user):
    """What a weigh-in occurrence's "Log weight" control shows - or None (not a weigh-in, or not the viewer's)."""
    if not is_weigh_in(item) or item.owner_id != getattr(user, "pk", None):
        return None
    unit = unit_of(user)
    low, high = LIMITS[unit]
    out = {"item": item, "date": day, "unit": unit, "min": low, "max": high, "done": done, "skipped": False,
           "result": "", "url": reverse("albright_fitness:weigh_in_log", args=[item.pk]), "future": day > _today(user)}
    if done:
        logged = WeighInLog.objects.filter(item=item, occurrence_date=day).select_related("weight_entry").first()
        entry = logged.weight_entry if logged and logged.weight_entry_id else None
        if logged and logged.skipped:
            out.update(skipped=True, result="Skipped")
        else:
            entry = entry or WeightEntry.objects.filter(owner=user, date=day).first()
            out["result"] = result_text(entry, unit) if entry else "Done"
        return out
    last = last_weight(user, day)
    out["placeholder"] = f"Last: {body.shown(last.weight_kg, unit)} {unit}" if last else unit
    return out
