"""Body metrics: weigh-ins and measurements - private to their owner, never shared.

- Weight: one weigh-in a day, stored in kg and shown in the person's unit. Daily weigh-ins swing with water and
  food, so what matters is the 7-day moving average (the mean of the weigh-ins in the 7 days up to each one); the
  changes over 2 and 4 weeks compare those averages, in neutral words ("down 1.2 lb over 4 weeks").
- A goal weight gets a projected date only when the recent trend supports one: at least 5 weigh-ins over at least
  14 of the last 28 days, the average moving toward the goal by at least 0.1 lb a week, and the date within 2 years.
- Measurements (cm, shown in the person's height unit) and body fat %, each with its own trend.
- An optional weekly "Weigh in" task on the Planner (off by default).
"""
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal

from django.urls import reverse
from django.utils import timezone

from .models import KG_TO_LB, LB, Measurement, NutritionProfile, WeightEntry

WINDOW = 7
TREND_DAYS = 28
MIN_POINTS = 5
MIN_SPAN_DAYS = 14
MIN_RATE_LB_WEEK = Decimal("0.1")
MAX_PROJECTION_DAYS = 730
CM_PER_IN = Decimal("2.54")
STEADY = Decimal("0.1")                 # under this (in the person's unit) is "about the same"
REMINDER_SOURCE, REMINDER_ID = "fitness", "weigh-in"


def q(value, places="0.1"):
    return Decimal(value).quantize(Decimal(places), rounding=ROUND_HALF_UP)


def to_kg(value, unit):
    value = Decimal(str(value))
    return q(value / KG_TO_LB, "0.01") if unit == LB else q(value, "0.01")


def shown(kg, unit):
    """kg -> the person's unit (0.1)."""
    if kg is None:
        return None
    return q(Decimal(kg) * KG_TO_LB) if unit == LB else q(kg)


def length_shown(cm, unit):
    if cm is None:
        return None
    return q(Decimal(cm) / CM_PER_IN) if unit == NutritionProfile.IN else q(cm)


def length_to_cm(value, unit):
    value = Decimal(str(value))
    return q(value * CM_PER_IN) if unit == NutritionProfile.IN else q(value)


def log_weight(user, day, value, unit, note=""):
    """Record (or replace) the day's weigh-in -> (entry, created)."""
    return WeightEntry.objects.update_or_create(owner=user, date=day,
                                                defaults={"weight_kg": to_kg(value, unit), "note": (note or "")[:200]})


# ---------------------------------------------------------------------------
# The trend
# ---------------------------------------------------------------------------

def moving_average(entries, window=WINDOW):
    """[(date, weight_kg, average_kg)] oldest first: each weigh-in with the mean of the weigh-ins in the `window`
    days up to and including it."""
    entries = sorted(entries, key=lambda e: e.date)
    out = []
    for i, e in enumerate(entries):
        recent = [x.weight_kg for x in entries[:i + 1] if (e.date - x.date).days < window]
        out.append((e.date, e.weight_kg, sum(recent) / len(recent)))
    return out


def series(user, days=None, today=None):
    today = today or timezone.localdate()
    rows = WeightEntry.objects.filter(owner=user, date__lte=today)
    if days:
        rows = rows.filter(date__gt=today - timedelta(days=days + WINDOW))   # the first averages need earlier days
    return moving_average(list(rows))


def _average_on(points, day):
    """The moving average at the latest weigh-in on or before `day`, or None."""
    earlier = [p for p in points if p[0] <= day]
    return earlier[-1][2] if earlier else None


def change(points, weeks, unit):
    """The 7-day average's change over `weeks` -> {"weeks", "amount" (signed, person's unit), "text"} or None
    when there isn't a weigh-in that far back."""
    if not points:
        return None
    last_day, _w, now = points[-1]
    then = _average_on(points, last_day - timedelta(weeks=weeks))
    if then is None:
        return None
    amount = shown(now - then, unit)                     # the difference, converted once (no double rounding)
    period = f"{weeks} week{'s' if weeks != 1 else ''}"
    if abs(amount) < STEADY:
        text = f"about the same over {period}"
    else:
        text = f"{'down' if amount < 0 else 'up'} {abs(amount)} {unit} over {period}"
    return {"weeks": weeks, "amount": amount, "text": text}


def slope_per_day(points):
    """Least-squares slope of the moving average over time (kg a day), or None with too little to go on."""
    if len(points) < 2:
        return None
    first = points[0][0]
    xs = [Decimal((p[0] - first).days) for p in points]
    ys = [p[2] for p in points]
    mean_x, mean_y = sum(xs) / len(xs), sum(ys) / len(ys)
    spread = sum((x - mean_x) ** 2 for x in xs)
    if not spread:
        return None
    return sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys)) / spread


def projection(user, profile, today=None):
    """The date the goal weight would be reached on the recent trend -> {"date", "rate" (person's unit a week)},
    or None when the trend doesn't support a projection (too few or too recent weigh-ins, flat, heading away from
    the goal, or more than 2 years out). Already at the goal -> None too."""
    if profile.goal_weight_kg is None:
        return None
    today = today or timezone.localdate()
    points = [p for p in series(user, TREND_DAYS, today) if p[0] > today - timedelta(days=TREND_DAYS)]
    if len(points) < MIN_POINTS or (points[-1][0] - points[0][0]).days < MIN_SPAN_DAYS:
        return None
    slope = slope_per_day(points)
    if slope is None:
        return None
    current, goal = points[-1][2], Decimal(profile.goal_weight_kg)
    gap = goal - current
    weekly_lb = abs(slope * 7 * KG_TO_LB)
    if weekly_lb < MIN_RATE_LB_WEEK or gap == 0 or (gap > 0) != (slope > 0):
        return None
    days = int((gap / slope).to_integral_value(ROUND_HALF_UP))
    if days <= 0 or days > MAX_PROJECTION_DAYS:
        return None
    rate = shown(abs(slope) * 7, profile.weight_unit)
    return {"date": points[-1][0] + timedelta(days=days), "rate": rate}


# ---------------------------------------------------------------------------
# Charts (fitness.css .fx-chart: an SVG line stretched to the box, true-size HTML dots and labels on top)
# ---------------------------------------------------------------------------

def time_chart(rows, line_key=None, dot_key="value", ticks=3):
    """rows: [{"date", dot_key, (line_key), "label"}] oldest first -> geometry in percent of the plot box: x by date,
    y by value; "dots" (every row), "line" (an SVG polyline of line_key, or of the dots without one), y-axis ticks.
    None with fewer than 2 rows."""
    if len(rows) < 2:
        return None
    keys = [dot_key] + ([line_key] if line_key else [])
    values = [float(r[k]) for r in rows for k in keys if r.get(k) is not None]
    low, high = min(values), max(values)
    span = (high - low) or max(abs(high) * 0.02, 1)
    low, high = low - span * 0.15, high + span * 0.15
    first, last = rows[0]["date"], rows[-1]["date"]
    days = max((last - first).days, 1)

    def y(v):
        return round(100 - (float(v) - low) / (high - low) * 100, 2)

    dots = [{**r, "x": round(100 * (r["date"] - first).days / days, 2), "y": y(r[dot_key])} for r in rows]
    line_rows = [d for d in dots if not line_key or d.get(line_key) is not None]
    line = " ".join(f"{d['x']},{y(d[line_key]) if line_key else d['y']}" for d in line_rows)
    tick_rows = [{"y": round(100 - 100 * n / ticks, 2), "label": f"{low + (high - low) * n / ticks:,.1f}"} for n in range(ticks + 1)]
    return {"dots": dots, "line": line, "ticks": tick_rows, "first": first, "last": last}


def weight_chart(points, unit):
    rows = [{"date": d, "value": shown(w, unit), "avg": shown(a, unit),
             "label": f"{shown(w, unit)} {unit} (7-day average {shown(a, unit)})"} for d, w, a in points]
    return time_chart(rows, line_key="avg")


def measurement_charts(user, unit):
    """One small trend per measurement that has at least one value: [{"key", "label", "unit", "latest", "change",
    "chart"}]."""
    rows = list(Measurement.objects.filter(owner=user).order_by("date"))
    out = []
    for key, label in Measurement.PARTS + [("body_fat", "Body fat")]:
        values = [(m.date, getattr(m, key)) for m in rows if getattr(m, key) is not None]
        if not values:
            continue
        shown_unit = "%" if key == "body_fat" else unit
        conv = (lambda v: q(v)) if key == "body_fat" else (lambda v: length_shown(v, unit))
        series_rows = [{"date": d, "value": conv(v), "label": f"{conv(v)} {shown_unit}"} for d, v in values]
        diff = series_rows[-1]["value"] - series_rows[0]["value"] if len(series_rows) > 1 else None
        out.append({"key": key, "label": label, "unit": shown_unit, "latest": series_rows[-1], "change": diff,
                    "chart": time_chart(series_rows)})
    return out


# ---------------------------------------------------------------------------
# The weekly weigh-in reminder (the Planner)
# ---------------------------------------------------------------------------

def set_reminder(profile, on, weekday=None, today=None):
    """Turn the weekly "Weigh in" task on (or move it to another weekday) or off. Off ends it after today."""
    from albright_planner import services
    today = today or timezone.localdate()
    if weekday is not None:
        profile.weigh_in_weekday = weekday
    profile.weigh_in_reminder = on
    profile.save(update_fields=["weigh_in_reminder", "weigh_in_weekday", "updated_at"])
    if on:
        services.upsert_weekly_source_item(profile.user, REMINDER_SOURCE, REMINDER_ID, "Weigh in", today,
                                           [profile.weigh_in_weekday], url=reverse("albright_fitness:body"))
    else:
        services.end_source_items(profile.user, REMINDER_SOURCE, [REMINDER_ID], today)
