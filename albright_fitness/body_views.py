"""Fitness pages for body metrics (/fitness/body/) and Insights (/fitness/insights/). Body metrics are their owner's
alone - never shared, even within a circle: every lookup is through owner=request.user (anyone else: 404)."""
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST

from . import body, insights
from .models import Measurement, NutritionProfile, WeightEntry

WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
CHART_DAYS = 90


def _day(text):
    try:
        on = date.fromisoformat(text or "")
    except ValueError:
        return timezone.localdate()
    today = timezone.localdate()
    return min(on, today) if on >= today - timedelta(days=3650) else today


def _number(text, low, high):
    try:
        value = Decimal(str(text).strip())
    except (InvalidOperation, ValueError, TypeError):
        return None
    return value if value.is_finite() and low <= value <= high else None


def _back(request, default):
    nxt = request.POST.get("next") or ""
    if nxt and url_has_allowed_host_and_scheme(nxt, {request.get_host()}, require_https=request.is_secure()):
        return redirect(nxt)
    return redirect(default)


WEIGHT_LIMITS = {"lb": (Decimal(45), Decimal(880)), "kg": (Decimal(20), Decimal(400))}


@login_required
@never_cache
def body_page(request):
    profile = NutritionProfile.load(request.user)
    unit = profile.weight_unit
    points = body.series(request.user)
    recent = [p for p in points if p[0] > timezone.localdate() - timedelta(days=CHART_DAYS)]
    entries = WeightEntry.objects.filter(owner=request.user).order_by("-date")[:14]
    length_unit = profile.height_unit
    return render(request, "albright_fitness/body.html", {
        "profile": profile, "unit": unit, "length_unit": length_unit, "chart": body.weight_chart(recent, unit),
        "latest": {"weight": body.shown(points[-1][1], unit), "average": body.shown(points[-1][2], unit), "date": points[-1][0]} if points else None,
        "changes": [c for c in (body.change(points, 2, unit), body.change(points, 4, unit)) if c],
        "projection": body.projection(request.user, profile), "goal": body.shown(profile.goal_weight_kg, unit),
        "entries": [(e, body.shown(e.weight_kg, unit)) for e in entries], "parts": Measurement.PARTS,
        "measurements": body.measurement_charts(request.user, length_unit), "weekdays": list(enumerate(WEEKDAYS)),
        "today": timezone.localdate()})


@login_required
@require_POST
def log_weight(request):
    profile = NutritionProfile.load(request.user)
    unit = request.POST.get("unit") if request.POST.get("unit") in WEIGHT_LIMITS else profile.weight_unit
    value = _number(request.POST.get("weight"), *WEIGHT_LIMITS[unit])
    if value is None:
        messages.error(request, f"Enter a weight in {unit}.")
    else:
        on = _day(request.POST.get("date"))
        _entry, created = body.log_weight(request.user, on, value, unit, " ".join(request.POST.get("note", "").split()))
        messages.success(request, f"Logged {value.normalize():f} {unit}" + ("." if created else f" (replaced {on:%b} {on.day}'s)."))
    return _back(request, reverse("albright_fitness:body"))


@login_required
@require_POST
def delete_weight(request, pk):
    entry = get_object_or_404(WeightEntry, pk=pk, owner=request.user)
    entry.delete()
    messages.success(request, "Removed that weigh-in.")
    return redirect("albright_fitness:body")


@login_required
@require_POST
def log_measurements(request):
    profile = NutritionProfile.load(request.user)
    values = {}
    for key, _label in Measurement.PARTS:
        raw = request.POST.get(key, "").strip()
        if raw:
            number = _number(raw, Decimal(1), Decimal(300))
            if number is None:
                messages.error(request, "Measurements should be numbers.")
                return redirect("albright_fitness:body")
            values[key] = body.length_to_cm(number, profile.height_unit)
    raw = request.POST.get("body_fat", "").strip()
    if raw:
        values["body_fat"] = _number(raw, Decimal(2), Decimal(70))
        if values["body_fat"] is None:
            messages.error(request, "Body fat should be a percentage from 2 to 70.")
            return redirect("albright_fitness:body")
    if not values:
        messages.error(request, "Enter at least one measurement.")
    else:
        Measurement.objects.update_or_create(owner=request.user, date=_day(request.POST.get("date")), defaults=values)
        messages.success(request, "Saved your measurements.")
    return redirect(reverse("albright_fitness:body") + "#fx-measurements")


@login_required
@require_POST
def body_settings(request):
    """Units, goal weight, hiding weight from the dashboard, and the weekly weigh-in reminder."""
    profile = NutritionProfile.load(request.user)
    if request.POST.get("weight_unit") in WEIGHT_LIMITS:
        profile.weight_unit = request.POST["weight_unit"]
    if request.POST.get("height_unit") in ("in", "cm"):
        profile.height_unit = request.POST["height_unit"]
    raw = request.POST.get("goal", "").strip()
    if raw:
        goal = _number(raw, *WEIGHT_LIMITS[profile.weight_unit])
        if goal is None:
            messages.error(request, f"Enter the goal in {profile.weight_unit}.")
            return redirect("albright_fitness:body")
        profile.goal_weight_kg = body.q(body.to_kg(goal, profile.weight_unit))
    else:
        profile.goal_weight_kg = None
    profile.hide_weight = bool(request.POST.get("hide_weight"))
    profile.save()
    weekday = request.POST.get("weigh_in_weekday", "")
    weekday = int(weekday) if weekday.isdigit() and int(weekday) <= 6 else profile.weigh_in_weekday
    reminder = bool(request.POST.get("weigh_in_reminder"))
    if reminder != profile.weigh_in_reminder or (reminder and weekday != profile.weigh_in_weekday):
        body.set_reminder(profile, reminder, weekday)
    messages.success(request, "Saved.")
    return redirect("albright_fitness:body")


@login_required
@never_cache
def insights_page(request):
    today = timezone.localdate()
    weeks = insights.weeks(request.user, 2, today)
    return render(request, "albright_fitness/insights.html", {
        "this_week": weeks[0], "last_week": weeks[1], "volume_text": insights.volume_change_text(weeks[0]),
        "muscles": insights.muscle_weeks(request.user, 8, today), "lifts": [
            {**lift, "chart": body.time_chart(lift["points"])} for lift in insights.lift_trends(request.user, 12, today)],
        "untrained": insights.untrained(request.user, today=today), "food": insights.nutrition(request.user, today),
        "consistency": insights.consistency(request.user, 12, today), "weekday_letters": "MTWTFSS"})
