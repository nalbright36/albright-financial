"""get_stats: the numbers the assistant quotes about two metrics - computed here, never by the model.

Days where both metrics have a value are paired (metric B shifted by lag_days: lag 1 pairs today's A with
tomorrow's B). Pearson (linear) and Spearman (rank, ties averaged) correlations, the number of paired days, and a
least-squares trend for each metric over its own days (change per week). Too few pairs (< MIN_PAIRS) or a metric
that never changes gives null correlations with a reason.
"""
import math
from datetime import timedelta

from . import metrics

MIN_PAIRS = 5


def pearson(xs, ys):
    n = len(xs)
    if n < 2:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx == 0 or syy == 0:
        return None
    return sxy / math.sqrt(sxx * syy)


def ranks(values):
    """Ranks 1..n, ties get the average of their ranks."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    out = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg = (i + j) / 2 + 1
        for k in range(i, j + 1):
            out[order[k]] = avg
        i = j + 1
    return out


def spearman(xs, ys):
    return pearson(ranks(xs), ranks(ys))


def trend(daily):
    """Least-squares slope of a {date: value} series, per week, with the first and last values and the direction."""
    days = sorted(daily)
    if len(days) < 2:
        return {"days_with_data": len(days), "per_week": None, "direction": "not enough data"}
    x0 = days[0]
    xs = [(d - x0).days for d in days]
    ys = [daily[d] for d in days]
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx if sxx else 0.0
    per_week = slope * 7
    scale = max(abs(my), 1e-9)
    direction = "flat" if abs(per_week) / scale < 0.005 else ("up" if per_week > 0 else "down")
    return {"days_with_data": n, "per_week": round(per_week, 3), "direction": direction,
            "first": {"date": days[0].isoformat(), "value": round(ys[0], 2)},
            "last": {"date": days[-1].isoformat(), "value": round(ys[-1], 2)},
            "mean": round(my, 2)}


def strength(r):
    if r is None:
        return "none"
    a = abs(r)
    word = "very weak" if a < 0.1 else "weak" if a < 0.3 else "moderate" if a < 0.5 else "strong" if a < 0.7 \
        else "very strong"
    return f"{word} {'positive' if r > 0 else 'negative'}" if a >= 0.1 else word


def get_stats(user, metric_a, metric_b, start=None, end=None, lag_days=0):
    from albright_planner.planner import today
    try:
        lag = int(lag_days or 0)
    except (TypeError, ValueError):
        raise metrics.MetricError("lag_days is a whole number of days.") from None
    if abs(lag) > 60:
        raise metrics.MetricError("lag_days is at most 60.")
    end_d = metrics._parse_day(end, today(user))
    start_d = metrics._parse_day(start, end_d - timedelta(days=89))
    if start_d > end_d or (end_d - start_d).days + 1 > metrics.MAX_DAYS:
        raise metrics.MetricError("Use a range of up to 2 years, start before end.")
    a, ma = metrics.daily_values(user, metric_a, start_d, end_d)
    b, mb = metrics.daily_values(user, metric_b, start_d + timedelta(days=lag), end_d + timedelta(days=lag))
    pairs = [(a[d], b[d + timedelta(days=lag)]) for d in sorted(a) if d + timedelta(days=lag) in b]
    xs, ys = [p[0] for p in pairs], [p[1] for p in pairs]
    out = {"metric_a": {"key": metric_a, "label": ma.label, "unit": ma.unit},
           "metric_b": {"key": metric_b, "label": mb.label, "unit": mb.unit},
           "start": start_d.isoformat(), "end": end_d.isoformat(), "lag_days": lag,
           "paired_days": len(pairs), "trend_a": trend(a), "trend_b": trend(b)}
    if len(pairs) < MIN_PAIRS:
        out.update(pearson=None, spearman=None, strength="none",
                   note=f"Only {len(pairs)} day(s) have both - at least {MIN_PAIRS} are needed for a correlation.")
        return out
    r, rho = pearson(xs, ys), spearman(xs, ys)
    out.update(pearson=None if r is None else round(r, 3), spearman=None if rho is None else round(rho, 3),
               strength=strength(r))
    if r is None:
        out["note"] = "One of the metrics doesn't change over these days, so there's no correlation to measure."
    out["reminder"] = ("Say how many days this is based on (paired_days), and that a correlation doesn't show that "
                       "one causes the other.")
    return out
