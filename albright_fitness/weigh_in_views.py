"""Weigh-in schedules (the "Weigh-in schedule" card on Fitness > Body) and logging a weight from a weigh-in task
(Planner rows, the view panel, Home's Today panel) - weigh_in.py has the rules. Always the signed-in person's own:
anyone else's schedule or task is a 404."""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_GET, require_POST

from . import body, weigh_in
from .models import WeighInSchedule, WeightEntry

CARD = "#fx-weigh-in-schedule"


def _today(request):
    from albright_planner import planner
    return planner.today(request.user)


def _wants_json(request):
    return request.headers.get("X-Requested-With") == "XMLHttpRequest" or "application/json" in request.headers.get("Accept", "")


def _back(request, default):
    nxt = request.POST.get("next") or ""
    if nxt and url_has_allowed_host_and_scheme(nxt, {request.get_host()}, require_https=request.is_secure()):
        return redirect(nxt)
    return redirect(default)


def card_context(request, form=None, editing=None):
    """The Body page's schedule card: the schedule in plain words, its next 3 dates, and the form."""
    today = _today(request)
    schedule = weigh_in.current(request.user)
    if form is None:
        form = weigh_in.ScheduleForm(initial=weigh_in.ScheduleForm.initial_for(schedule, today), today=today)
    return {"wi_schedule": schedule, "wi_text": weigh_in.describe(schedule) if schedule else "",
            "wi_next": weigh_in.dates(schedule, today) if schedule and schedule.active else [],
            "wi_form": form, "wi_editing": editing if editing is not None else form.is_bound,
            "wi_preview_url": reverse("albright_fitness:weigh_in_preview")}


@login_required
@require_POST
def save(request):
    """Create (replace = the confirmed "this replaces your schedule") or edit the schedule."""
    today = _today(request)
    form = weigh_in.ScheduleForm(request.POST, today=today)
    schedule = weigh_in.current(request.user)
    replacing = request.POST.get("mode") == "new"
    if not form.is_valid():
        from .body_views import body_page
        request._weigh_in_form = form                  # the Body page again, errors next to the fields
        response = body_page(request)
        response.status_code = 400
        return response
    saved = weigh_in.save(request.user, form, None if replacing or schedule is None else schedule)
    messages.success(request, f"Weigh-ins: {weigh_in.describe(saved)}.")
    return redirect(reverse("albright_fitness:body") + CARD)


@login_required
@require_POST
def pause(request, pk):
    schedule = get_object_or_404(WeighInSchedule, pk=pk, owner=request.user)
    if schedule.active:
        weigh_in.pause(schedule)
        messages.success(request, "Weigh-ins paused. Past weigh-ins and weights are kept.")
    else:
        weigh_in.resume(schedule)
        messages.success(request, f"Weigh-ins back on: {weigh_in.describe(schedule)}.")
    return redirect(reverse("albright_fitness:body") + CARD)


@login_required
@require_POST
def delete(request, pk):
    schedule = get_object_or_404(WeighInSchedule, pk=pk, owner=request.user)
    weigh_in.remove(schedule)
    messages.success(request, "Weigh-in schedule deleted. Past weigh-ins and weights are kept.")
    return redirect(reverse("albright_fitness:body") + CARD)


@login_required
@require_GET
def preview(request):
    """The form's live preview: the next 3 dates for the choices so far, or why there aren't any."""
    today = _today(request)
    form = weigh_in.ScheduleForm(request.GET, today=today)
    if not form.is_valid():
        return JsonResponse({"ok": False, "dates": [], "error": next(iter(e for errs in form.errors.values() for e in errs), "")})
    schedule = form.apply(WeighInSchedule(owner=request.user))
    found = weigh_in.dates(schedule, today)
    return JsonResponse({"ok": True, "text": weigh_in.describe(schedule),
                         "dates": [f"{d:%a, %b} {d.day}" + (f" at {weigh_in.clock(schedule.time_of_day)}" if schedule.time_of_day else "")
                                   for d in found]})


@login_required
@require_POST
def log(request, pk):
    """Log a weight for one weigh-in occurrence (POST date, weight; replace=1 after "Replace today's ...?"), or
    skip=1 ("Skip today": done, no weight). JSON for the page's fetch; a normal form post redirects back."""
    from albright_planner import planner
    from albright_planner.models import ScheduleItem
    item = get_object_or_404(ScheduleItem, pk=pk, owner=request.user, archived=False)
    if not item.is_weigh_in:
        raise Http404
    day = _parse_day(request.POST.get("date")) or planner.current_or_first(item)
    already = item.completions.filter(occurrence_date=day).exists()
    if not (planner.valid_occurrence(item, day) or already):
        return _error(request, "That weigh-in isn't on that day.", 400)
    if day > _today(request):
        return _error(request, "You can log this weigh-in on its day.", 400)
    if request.POST.get("skip"):
        weigh_in.skip(item, day, request.user)
        return _done(request, item, day, "Skipped", skipped=True)
    unit = weigh_in.unit_of(request.user)
    value = weigh_in.parse(request.POST.get("weight"), unit)
    if value is None:
        low, high = weigh_in.LIMITS[unit]
        return _error(request, f"Enter a weight from {low} to {high} {unit}.", 400)
    existing = WeightEntry.objects.filter(owner=request.user, date=day).first()
    if existing and request.POST.get("replace") != "1":
        when = "today's" if day == _today(request) else f"{day:%a %b} {day.day}'s"
        question = f"Replace {when} {body.shown(existing.weight_kg, unit)} {unit}?"
        if _wants_json(request):
            return JsonResponse({"ok": False, "confirm": question}, status=409)
        return render(request, "albright_fitness/weigh_in_replace.html", {
            "question": question, "item": item, "day": day, "weight": value, "next": request.POST.get("next", "")})
    entry = weigh_in.log(item, day, value, unit, request.user)
    return _done(request, item, day, weigh_in.result_text(entry, unit))


def _parse_day(text):
    from datetime import date
    try:
        return date.fromisoformat(text or "")
    except ValueError:
        return None


def _error(request, text, status):
    if _wants_json(request):
        return JsonResponse({"ok": False, "error": text}, status=status)
    messages.error(request, text)
    return _back(request, reverse("albright_planner:day"))


def _done(request, item, day, result, skipped=False):
    if _wants_json(request):
        return JsonResponse({"ok": True, "done": True, "result": result, "skipped": skipped,
                             "item_id": item.pk, "date": day.isoformat()})
    messages.success(request, f"Weigh-in: {result}." if not skipped else "Weigh-in skipped.")
    return _back(request, reverse("albright_planner:day"))
