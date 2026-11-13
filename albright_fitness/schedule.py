"""Schedule: putting an exercise, a program or one program day on my Planner myself (the Schedule form on the
exercise and program pages), and how those Planner tasks tie back into logging.

  - Each Schedule is a ScheduledSession with its own Planner task: mine, private, source "fitness", source_id
    "session-<pk>" - made, changed and removed only through the Planner's service helpers. A date, an optional
    time, an optional repeat (the Planner's own choices, e.g. weekly on Mon/Wed/Fri) and an optional target
    (sets × reps). A program day's task gets its exercises as the checklist; a whole program's lists every day's.
  - These are things I chose to schedule, not an automatic source: they're made even with every automatic
    source off, and a deleted one never blocks the next (each has a fresh source_id). Deleting the task in the
    Planner deletes that one schedule; Remove here deletes the task.
  - Log now (the Planner's check-off prompt, the view panel's Start workout): the start page for that occurrence,
    pre-filled with the scheduled exercises and targets; finishing the workout checks the occurrence off.
    Logging the same exercise / program day that day (a quick log, the exercise page's Log card, any finished
    workout) checks it off too - complete_matching().
"""
from django import forms
from django.db import transaction
from django.db.models.signals import post_delete
from django.dispatch import receiver
from django.urls import reverse

from albright_planner import recurrence

from . import training
from .models import ScheduledSession

SOURCE = "fitness"
REPEAT_KEYS = ("none", "daily", "weekdays", "weekly", "every_n_weeks", "monthly_day")
REPEATS = [(k, label) for k, label in recurrence.REPEATS if k in REPEAT_KEYS]
DAY_CHOICES = list(zip(recurrence.WEEKDAYS, recurrence.DAY_NAMES))


class ScheduleForm(forms.Form):
    date = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}))
    time = forms.TimeField(required=False, widget=forms.TimeInput(attrs={"type": "time"}, format="%H:%M"))
    repeat = forms.ChoiceField(choices=REPEATS, initial="none", required=False)
    days = forms.MultipleChoiceField(choices=DAY_CHOICES, required=False, widget=forms.CheckboxSelectMultiple)
    interval = forms.IntegerField(required=False, min_value=2, max_value=12, initial=2)
    until = forms.DateField(required=False, widget=forms.DateInput(attrs={"type": "date"}))
    sets = forms.IntegerField(required=False, min_value=1, max_value=20)
    reps = forms.IntegerField(required=False, min_value=1, max_value=100)

    def clean(self):
        data = super().clean()
        data["repeat"] = data.get("repeat") or "none"
        if data["repeat"] != "none" and data.get("until") and data.get("date") and data["until"] < data["date"]:
            self.add_error("until", "The end date is before the first date.")
        if data.get("reps") and not data.get("sets"):
            self.add_error("sets", "Add the number of sets too.")
        return data

    def rule(self):
        d = self.cleaned_data
        if d["repeat"] == "none":
            return ""
        return recurrence.build(d["repeat"], d["date"], days=d.get("days") or (), interval=d.get("interval") or 2)

    @classmethod
    def initial_for(cls, session):
        item = session.planner_item
        rule = recurrence.describe(item.recurrence) if item and item.recurrence else {"repeat": "none", "days": [], "interval": 2}
        repeat = rule["repeat"] if rule["repeat"] in REPEAT_KEYS else "none"
        return {"date": item.date if item else None, "time": item.start_time if item else None,
                "repeat": repeat, "days": rule.get("days") or [], "interval": max(2, rule.get("interval") or 2),
                "until": item.recurrence_until if item else None, "sets": session.target_sets, "reps": session.target_reps}


# ---------------------------------------------------------------------------
# What a schedule holds
# ---------------------------------------------------------------------------

def _target_text(session, pe=None):
    if session.target_sets:
        return session.target_text
    return pe.target_text if pe is not None else ""


def rows_for(session):
    """[{"exercise", "sets", "reps", "seconds", "distance", "target", "day"}] - the scheduled exercises in order
    (a program: every day's, each exercise once), with the schedule's target over the program's."""
    if session.exercise_id:
        return [{"exercise": session.exercise, "sets": session.target_sets, "reps": session.target_reps,
                 "seconds": None, "distance": None, "target": session.target_text, "day": None}]
    days = [session.program_day] if session.program_day_id else list(session.program.days.all())
    out, seen = [], set()
    for day in days:
        for pe in day.exercises.select_related("exercise"):
            if session.program_id and pe.exercise_id in seen:
                continue
            seen.add(pe.exercise_id)
            out.append({"exercise": pe.exercise, "sets": session.target_sets or pe.target_sets,
                        "reps": session.target_reps if session.target_sets else pe.target_reps,
                        "seconds": None if session.target_sets else pe.target_seconds,
                        "distance": None if session.target_sets else pe.target_distance,
                        "target": _target_text(session, pe), "day": day})
    return out


def checklist_for(session):
    """The task's checklist: a program day's exercises ("Barbell Back Squat - 3 × 5"), a whole program's (each
    with its day when there are several); one exercise has none - its target is in the view panel."""
    if session.exercise_id:
        return []
    if session.program_day_id:
        day = session.program_day
        return [f"{pe.exercise.name} - {_target_text(session, pe)}" for pe in day.exercises.select_related("exercise")]
    days = list(session.program.days.prefetch_related("exercises__exercise"))
    lines = []
    for day in days:
        for pe in day.exercises.all():
            text = f"{pe.exercise.name} - {_target_text(session, pe)}"
            lines.append(f"{day.name}: {text}" if len(days) > 1 else text)
    return lines


def start_url(session):
    return reverse("albright_fitness:start") + f"?session={session.pk}"


# ---------------------------------------------------------------------------
# Create / change / remove - the Planner task only through albright_planner.services
# ---------------------------------------------------------------------------

@transaction.atomic
def create(user, form, exercise=None, program=None, program_day=None):
    from albright_planner import services
    d = form.cleaned_data
    session = ScheduledSession.objects.create(owner=user, exercise=exercise, program=program, program_day=program_day,
                                              target_sets=d.get("sets"), target_reps=d.get("reps") if d.get("sets") else None)
    rule = form.rule()
    session.planner_item = services.create_scheduled_task(
        user, SOURCE, session.source_id, session.title, d["date"], d.get("time"), rule=rule,
        until=d.get("until") if rule else None, checklist=checklist_for(session), url=start_url(session))
    session.save(update_fields=["planner_item"])
    return session


@transaction.atomic
def update(session, form):
    from albright_planner import services
    d = form.cleaned_data
    session.target_sets = d.get("sets")
    session.target_reps = d.get("reps") if d.get("sets") else None
    session.save(update_fields=["target_sets", "target_reps"])
    rule = form.rule()
    services.update_scheduled_task(session.planner_item, session.title, d["date"], d.get("time"), rule=rule,
                                   until=d.get("until") if rule else None, checklist=checklist_for(session),
                                   url=start_url(session))
    return session


def remove(session):
    """Remove one schedule: its Planner task goes (and with it, this session)."""
    from albright_planner import services
    if session.planner_item_id:
        services.delete_scheduled_task(session.planner_item)
    ScheduledSession.objects.filter(pk=session.pk).delete()


@receiver(post_delete, sender=ScheduledSession, dispatch_uid="fitness_session_deleted")
def _session_deleted(sender, instance, **kwargs):
    """A program, program day or exercise deleted takes its schedules with it - and their Planner tasks."""
    from albright_planner import services
    if instance.planner_item_id:
        services.delete_scheduled_task(instance.planner_item_id)


def upcoming(user, today, *where, **target):
    """[(session, [next dates])] for this exercise / program / program day, soonest first - only ones still due."""
    from albright_planner import services
    out = []
    for session in ScheduledSession.objects.filter(*where, owner=user, planner_item__isnull=False, **target) \
            .select_related("planner_item", "exercise", "program", "program_day__program"):
        dates = services.upcoming_dates(session.planner_item, today)
        if dates:
            out.append((session, dates))
    out.sort(key=lambda pair: pair[1][0])
    return out


def session_for_item(item):
    if item is None:
        return None
    return ScheduledSession.objects.filter(planner_item=item).select_related(
        "exercise", "program", "program_day__program").first()


# ---------------------------------------------------------------------------
# Logging ties: the occurrence a workout is for, and checking off what was logged
# ---------------------------------------------------------------------------

def _matches(session, workout, logged, partial=False):
    if session.exercise_id:
        return session.exercise_id in logged
    if session.program_day_id:
        if workout.program_day_id == session.program_day_id and not partial:
            return True
        wanted = set(session.program_day.exercises.values_list("exercise_id", flat=True))
        return bool(wanted) and wanted <= logged
    if workout.program_day_id and workout.program_day.program_id == session.program_id and not partial:
        return True
    for day in session.program.days.all():
        wanted = set(day.exercises.values_list("exercise_id", flat=True))
        if wanted and wanted <= logged:
            return True
    return False


def complete_matching(workout, exercises=None):
    """Check off the scheduled occurrences on the workout's date that it covers: an exercise logged, the program
    day done (or all its exercises logged). exercises: only these were just logged (the exercise page's Log card,
    into a workout still in progress - its other, pre-filled exercises don't count yet). Returns the Planner items
    checked off."""
    from albright_planner import services
    if exercises is not None:
        logged = {e.pk for e in exercises}
    else:
        logged = set(workout.exercises.filter(sets__done=True).values_list("exercise_id", flat=True))
    if not logged and not (exercises is None and workout.program_day_id):      # a program day's workout counts as such
        return []
    done = []
    for session in ScheduledSession.objects.filter(owner=workout.owner, planner_item__isnull=False) \
            .select_related("planner_item", "program_day", "program"):
        item = session.planner_item
        if not services.is_scheduled(item, workout.date) or services.is_done(item, workout.date):
            continue
        if _matches(session, workout, logged, partial=exercises is not None):
            services.complete_occurrence(item, workout.date, workout.owner)
            done.append(item)
    return done


# ---------------------------------------------------------------------------
# The Planner's view panel for a Fitness task
# ---------------------------------------------------------------------------

def panel(item, user, day):
    """What the view panel shows for a Fitness task: the exercises with their targets and last time, and Start
    workout (the start page for this occurrence). None for anything else."""
    if item is None or item.source_app != SOURCE:
        return None
    rows = []
    session = session_for_item(item)
    if session is not None:
        rows = rows_for(session)
    elif item.source_id.startswith("day-"):                     # "Schedule this program" (programs.schedule)
        from .models import ProgramDay
        found = ProgramDay.objects.filter(pk=item.source_id[4:] if item.source_id[4:].isdigit() else 0,
                                          program__owner=item.owner).first()
        if found:
            rows = [{"exercise": pe.exercise, "target": pe.target_text}
                    for pe in found.exercises.select_related("exercise")]
    owner = item.owner
    out = []
    for row in rows:
        tip = training.suggest(owner, row["exercise"])
        out.append({"exercise": row["exercise"], "target": row["target"],
                    "last": ", ".join(training.set_text(s) for s in tip.last) if tip else ""})
    start = item.source_url
    if start and day:
        start += ("&" if "?" in start else "?") + f"date={day.isoformat()}"
    return {"rows": out, "start_url": start if item.owner_id == getattr(user, "pk", None) else ""}
