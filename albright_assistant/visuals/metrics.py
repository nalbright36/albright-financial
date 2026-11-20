"""What can be charted, and the numbers - every query runs as the person, over their own data and data shared with
them (Meals and Money: the spaces they see, albright_labs.circles), only for apps they can open that aren't archived
(app_access.allowed_keys already leaves archived apps out).

A metric is a daily series: {date: value}. get_series buckets days into day / week (Monday) / month and aggregates
them (sum, avg of the days that have a value, or last). Never shareable data (weight, food) stays the owner's: a
Visual someone shares is drawn from the owner's numbers only because the owner chose to share that Visual.
"""
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Callable

from django.db.models import Count, F, Sum
from django.db.models.functions import TruncDate

from albright_labs import app_access, apps_registry
from albright_labs.models import UserPreference

MAX_DAYS = 731                # two years
MAX_ROWS = 5000
MAX_METRICS = 6
GRANULARITIES = ("day", "week", "month")
AGGREGATIONS = ("sum", "avg", "last")
KG_TO_LB = Decimal("2.20462")


class MetricError(ValueError):
    pass


@dataclass(frozen=True)
class Metric:
    key: str
    label: str
    app: str                  # apps_registry key: access, and the chart color
    unit: str
    aggregation: str          # the sensible default
    daily: Callable           # (user, start, end) -> {date: float}
    description: str = ""

    @property
    def color(self):
        return apps_registry.BY_KEY[self.app].color

    def info(self):
        return {"key": self.key, "label": self.label, "app": apps_registry.BY_KEY[self.app].label, "unit": self.unit,
                "default_aggregation": self.aggregation, "description": self.description}


def _zone(user):
    from albright_planner.planner import user_zone
    try:
        return user_zone(user)
    except Exception:  # noqa: BLE001
        from django.utils import timezone
        return timezone.get_current_timezone()


def _rows(qs, date_field, value):
    """{date: float} from a queryset grouped by a date field."""
    return {row["d"]: float(row["v"] or 0) for row in qs.values(d=F(date_field)).annotate(v=value).order_by()}


# --- Fitness -----------------------------------------------------------------------------------------------------------

def _weight(user, start, end):
    from albright_fitness.models import NutritionProfile, WeightEntry
    lb = NutritionProfile.load(user).weight_unit == "lb"
    out = {}
    for d, kg in WeightEntry.objects.filter(owner=user, date__range=(start, end)).order_by("date", "created_at") \
            .values_list("date", "weight_kg"):
        out[d] = float((kg * KG_TO_LB) if lb else kg)        # the last weigh-in of a day
    return out


def _food(field):
    def daily(user, start, end):
        from albright_fitness.models import FoodLogEntry
        return _rows(FoodLogEntry.objects.filter(owner=user, date__range=(start, end)), "date", Sum(field))
    return daily


def _workouts(user, start, end):
    from albright_fitness.models import Workout
    qs = Workout.objects.filter(owner=user, date__range=(start, end), finished_at__isnull=False)
    return _rows(qs, "date", Count("pk"))


def _volume(user, start, end):
    from albright_fitness.models import WorkoutSet
    qs = WorkoutSet.objects.filter(workout_exercise__workout__owner=user, done=True, weight__isnull=False,
                                   reps__isnull=False, workout_exercise__workout__date__range=(start, end))
    return _rows(qs, "workout_exercise__workout__date", Sum(F("weight") * F("reps")))


def _weight_unit(user):
    from albright_fitness.models import NutritionProfile
    return NutritionProfile.load(user).weight_unit


# --- Planner -----------------------------------------------------------------------------------------------------------

def _tasks_done(user, start, end):
    from albright_planner.models import ItemCompletion, ScheduleItem
    qs = ItemCompletion.objects.filter(item__owner=user, item__kind=ScheduleItem.TASK,
                                       occurrence_date__range=(start, end))
    return _rows(qs, "occurrence_date", Count("pk"))


# --- Meals -------------------------------------------------------------------------------------------------------------

def _grocery_added(user, start, end):
    from albright_labs.circles import space_ids
    from albright_meals.models import GroceryItem
    qs = GroceryItem.objects.filter(space_id__in=space_ids(user, "meals")) \
        .annotate(day=TruncDate("created_at", tzinfo=_zone(user))).filter(day__range=(start, end))
    return _rows(qs, "day", Count("pk"))


def _cooked(user, start, end):
    from albright_labs.circles import space_ids
    from albright_meals.models import MealPlanEntry
    qs = MealPlanEntry.objects.filter(space_id__in=space_ids(user, "meals"), status="cooked", date__range=(start, end))
    return _rows(qs, "date", Count("pk"))


# --- Money -------------------------------------------------------------------------------------------------------------

def _money(kind=None, category_id=None):
    """Spending (positive numbers) or income, from transactions and splits - transfers never count."""
    def daily(user, start, end):
        from albright_money.models import Category, Split, Transaction
        tx = Transaction.objects.visible_to(user).filter(date__range=(start, end), is_transfer=False)
        whole = tx.filter(category__isnull=False)
        parts = Split.objects.filter(transaction__in=tx, category__isnull=False)
        if category_id is not None:
            whole, parts = whole.filter(category_id=category_id), parts.filter(category_id=category_id)
            which = Category.objects.filter(pk=category_id).values_list("kind", flat=True).first()
        else:
            whole, parts, which = whole.filter(category__kind=kind), parts.filter(category__kind=kind), kind
        out = defaultdict(float)
        for d, v in _rows(whole, "date", Sum("amount")).items():
            out[d] += v
        for d, v in _rows(parts, "transaction__date", Sum("amount")).items():
            out[d] += v
        sign = -1 if which == Category.EXPENSE else 1          # spending is stored negative: chart it positive
        return {d: round(v * sign, 2) for d, v in out.items()}
    return daily


# --- the catalog -------------------------------------------------------------------------------------------------------

def _static():
    return [
        Metric("weight", "Weight", "fitness", "weight unit", "last", _weight, "Weigh-ins (the last one of a day)"),
        Metric("calories", "Calories", "fitness", "kcal", "sum", _food("calories"), "Food logged per day"),
        Metric("protein", "Protein", "fitness", "g", "sum", _food("protein"), "Food logged per day"),
        Metric("carbs", "Carbs", "fitness", "g", "sum", _food("carbs"), "Food logged per day"),
        Metric("fat", "Fat", "fitness", "g", "sum", _food("fat"), "Food logged per day"),
        Metric("fiber", "Fiber", "fitness", "g", "sum", _food("fiber"), "Food logged per day"),
        Metric("workouts", "Workouts", "fitness", "workouts", "sum", _workouts, "Finished workouts"),
        Metric("workout_volume", "Workout volume", "fitness", "weight x reps", "sum", _volume,
               "Weight x reps of every done set"),
        Metric("tasks_completed", "Tasks completed", "planner", "tasks", "sum", _tasks_done, "Tasks checked off"),
        Metric("grocery_items", "Grocery items added", "meals", "items", "sum", _grocery_added,
               "Lines added to the grocery list"),
        Metric("recipes_cooked", "Meals cooked", "meals", "meals", "sum", _cooked, "Plan entries marked cooked"),
        Metric("spending", "Spending", "money", "$", "sum", _money(kind="expense"), "All spending categories"),
        Metric("income", "Income", "money", "$", "sum", _money(kind="income"), "All income categories"),
    ]


def available(user):
    """{key: Metric} this person can chart now (their apps, none archived) - money categories included."""
    pref = UserPreference.for_user(user)
    allowed = set(app_access.allowed_keys(user, pref))
    out = {}
    for m in _static():
        if m.app in allowed:
            if m.key == "weight":
                m = Metric(m.key, m.label, m.app, _weight_unit(user), m.aggregation, m.daily, m.description)
            out[m.key] = m
    if "money" in allowed:
        from albright_money.models import Category
        for c in Category.objects.visible_to(user).filter(archived=False).order_by("name")[:60]:
            key = f"money_category_{c.pk}"
            out[key] = Metric(key, f"Money: {c.name}", "money", "$", "sum", _money(category_id=c.pk),
                              "Spending in this category" if c.kind == Category.EXPENSE else "Income in this category")
    return out


def list_metrics(user):
    return [m.info() for m in available(user).values()]


def _parse_day(value, default):
    if not value:
        return default
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        raise MetricError(f"Dates are YYYY-MM-DD (got {value!r}).") from None


def bucket(d, granularity):
    if granularity == "week":
        return d - timedelta(days=d.weekday())
    if granularity == "month":
        return d.replace(day=1)
    return d


def clean_query(user, raw):
    """A get_series query, checked: {"metrics", "start", "end", "granularity", "aggregation"} - or MetricError."""
    from albright_planner.planner import today
    if not isinstance(raw, dict):
        raise MetricError("A query is an object with metrics, start, end, granularity and aggregation.")
    metrics = raw.get("metrics") or []
    if isinstance(metrics, str):
        metrics = [metrics]
    if not metrics or not isinstance(metrics, list) or len(metrics) > MAX_METRICS:
        raise MetricError(f"Ask for 1 to {MAX_METRICS} metrics.")
    known = available(user)
    unknown = [m for m in metrics if m not in known]
    if unknown:
        raise MetricError(f"Unknown or unavailable metric(s): {', '.join(map(str, unknown))} - see list_metrics.")
    end = _parse_day(raw.get("end"), today(user))
    start = _parse_day(raw.get("start"), end - timedelta(days=89))
    if start > end:
        raise MetricError("start is after end.")
    if (end - start).days + 1 > MAX_DAYS:
        raise MetricError("At most 2 years at a time.")
    granularity = raw.get("granularity") or "day"
    if granularity not in GRANULARITIES:
        raise MetricError("granularity is day, week or month.")
    aggregation = raw.get("aggregation") or ""
    if aggregation and aggregation not in AGGREGATIONS:
        raise MetricError("aggregation is sum, avg or last.")
    return {"metrics": list(dict.fromkeys(metrics)), "start": start.isoformat(), "end": end.isoformat(),
            "granularity": granularity, "aggregation": aggregation}


def series(user, query, owner=None):
    """Tidy rows [{"date", "metric" (label), "key", "value"}] for a clean_query()ed query, oldest first. owner: whose
    data (a shared Visual is drawn from its owner's)."""
    who = owner or user
    known = available(who)
    start, end = date.fromisoformat(query["start"]), date.fromisoformat(query["end"])
    rows = []
    for key in query["metrics"]:
        metric = known.get(key)
        if metric is None:                    # lost access to the app since: leave it out
            continue
        daily = metric.daily(who, start, end)
        how = query.get("aggregation") or metric.aggregation
        buckets = defaultdict(list)
        for d in sorted(daily):
            buckets[bucket(d, query["granularity"])].append(daily[d])
        for b in sorted(buckets):
            values = buckets[b]
            value = sum(values) if how == "sum" else (values[-1] if how == "last" else sum(values) / len(values))
            rows.append({"date": b.isoformat(), "metric": metric.label, "key": key, "value": round(value, 2)})
        if len(rows) > MAX_ROWS:
            raise MetricError(f"That's more than {MAX_ROWS} rows - use week or month, or a shorter range.")
    return rows


def daily_values(user, key, start, end):
    """{date: value} for one metric (stats.py) - MetricError if it isn't available."""
    metric = available(user).get(key)
    if metric is None:
        raise MetricError(f"Unknown or unavailable metric: {key} - see list_metrics.")
    return metric.daily(user, start, end), metric


def _shade(hex_color, step):
    """The app color, then lighter / darker variants of it, so two metrics of one app still look different."""
    if step == 0:
        return hex_color
    r, g, b = (int(hex_color.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4))
    amount = (0.45, -0.35, 0.7, -0.55, 0.25)[(step - 1) % 5]
    target = 255 if amount > 0 else 0
    mix = abs(amount)
    r, g, b = (round(c + (target - c) * mix) for c in (r, g, b))
    return f"#{r:02X}{g:02X}{b:02X}"


def colors(user, keys):
    """{label: color} for these metric keys: the app's color (Fitness yellow, Meals orange ...) for its first metric,
    shades of it for the next ones from the same app."""
    known = available(user)
    out, used = {}, defaultdict(int)
    for k in dict.fromkeys(keys):
        if k in known and known[k].label not in out:
            m = known[k]
            out[m.label] = _shade(m.color, used[m.app])
            used[m.app] += 1
    return out
