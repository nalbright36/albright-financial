"""What a roundup says: build(user, scope, start, end, sections) -> a JSON-safe payload (stored on RoundupIssue).

Only what `user` can see goes in: Planner items they own or that are shared with them (planner.occurrences /
planner.overdue use ScheduleItem.objects.visible_to), Meals / Money / Fitness only for apps they can open
(sources.can_open), and notifications already filtered by activity.notifications. Nothing here writes.

payload = {
  "v": 1, "scope", "start", "end", "title", "range", "sections",
  "days": [{"date", "dow", "num", "label", "weather": {...} | None, "items": [...], "meals": [...]}],
  "repeats": [{"title", "every"}], "overdue": [{"title", "due", "url"}],
  "groceries": int | None, "money": {"left", "text"} | None, "shared": {"count", "lines"} | None,
  "counts": {"events", "tasks", "workouts", "overdue"}, "rain": [...], "temps": [lo, hi] | None,
}
item = {"title", "time", "kind": event | task | workout | weigh_in, "url", "by", "place", "repeat"}
"""
import logging
from collections import defaultdict
from datetime import timedelta
from decimal import Decimal

from django.urls import reverse

from albright_planner import planner
from albright_planner import sources
from albright_planner.models import ScheduleItem

from .models import SECTIONS, TODAY, WEEK
from .schedule import DAY_NAMES, clock

log = logging.getLogger(__name__)
MAX_ITEMS = 120
RAIN_POP = 50


def _iso(d):
    return d.isoformat()


def _range(start, end):
    if start == end:
        return f"{DAY_NAMES[start.weekday()]} {start:%b} {start.day}"
    return f"{DAY_NAMES[start.weekday()]} {start:%b} {start.day} – {DAY_NAMES[end.weekday()]} {end:%b} {end.day}"


def title_for(scope, start, today):
    if scope == TODAY:
        return "Your day" if start == today else ("Tomorrow" if start == today + timedelta(days=1) else "Your day")
    return "Your week ahead" if scope == WEEK else "Your month ahead"


def _kind(item):
    if item.kind == ScheduleItem.EVENT:
        return "event"
    if item.source_app == "fitness":
        return "weigh_in" if item.is_weigh_in else "workout"
    return "task"


def _item_url(occ):
    return reverse("albright_planner:item", args=[occ.item.pk]) + f"?date={occ.occurrence_date.isoformat()}"


def _row(occ, user):
    item = occ.item
    return {"title": occ.title, "time": clock(occ.start_time), "kind": _kind(item), "url": _item_url(occ),
            "by": "" if item.owner_id == user.pk else planner.person(item.owner),
            "place": item.location_label or "", "repeat": bool(item.recurrence)}


def _weather(user, days):
    from albright_planner import weather
    try:
        found = weather.attach(user, [], days)
    except Exception:  # noqa: BLE001 - weather never breaks a roundup
        log.exception("Roundup weather failed")
        return {}
    return {d: {"high": w.high, "low": w.low, "label": w.label, "icon": w.icon, "pop": w.pop}
            for d, w in found.items()}


def _money(user):
    from albright_money import budget
    left = budget.left_this_month(user)
    if left is None:
        return None
    left = Decimal(left).quantize(Decimal("1"))
    text = f"${left:,.0f} left in this month's budget" if left >= 0 else f"${-left:,.0f} over this month's budget"
    return {"left": str(left), "text": text}


def _shared(user):
    from albright_labs import activity
    prefs = activity.prefs_for(user)
    qs = activity.unread(user, activity.notifications(user, prefs), prefs).exclude(app="roundup")
    count = qs.count()
    if not count:
        return None
    return {"count": count, "lines": [activity.text(e) for e in qs.select_related("actor")[:3]]}


def _groceries(user):
    from albright_labs.home_tiles import grocery_left
    return grocery_left(user)


def build(user, scope, start, end, sections=None):
    sections = [s for s in (sections if sections is not None else SECTIONS) if s in SECTIONS]
    today = planner.today(user)
    days = [start + timedelta(days=n) for n in range((end - start).days + 1)]
    occs = [o for o in planner.occurrences(user, start, end, show_done=False, expand=True)][:MAX_ITEMS * 3]
    if "fitness" not in sections or not sources.can_open(user, "fitness"):
        occs = [o for o in occs if o.item.source_app != "fitness"]

    # A repeat on every day of a 3+ day period is one line ("Every day: Walk Brutus"), not N rows.
    repeats, hits = [], defaultdict(set)
    for o in occs:
        if o.item.recurrence:
            hits[o.item.pk].add(o.date)
    every = {pk for pk, ds in hits.items() if len(days) >= 3 and len(ds) >= len(days)}
    if "repeats" in sections:
        seen = set()
        for o in occs:
            if o.item.pk in every and o.item.pk not in seen:
                seen.add(o.item.pk)
                repeats.append({"title": o.item.title, "every": "Every day",
                                "time": clock(o.item.start_time)})
    occs = [o for o in occs if o.item.pk not in every]

    by_day = defaultdict(list)
    for o in occs[:MAX_ITEMS]:
        by_day[o.date].append(_row(o, user))

    meals = defaultdict(list)
    if "meals" in sections and sources.can_open(user, "meals"):
        for m in sources.meals(user, start, end, show=True):
            meals[m.date].append({"slot": m.slot, "title": m.title})

    wx = _weather(user, days) if "weather" in sections else {}

    out_days = []
    for d in days:
        out_days.append({"date": _iso(d), "dow": DAY_NAMES[d.weekday()], "num": d.day,
                         "label": f"{DAY_NAMES[d.weekday()]} {d:%b} {d.day}", "today": d == today,
                         "weather": wx.get(d), "items": by_day.get(d, []) if "days" in sections else [],
                         "meals": meals.get(d, [])})

    overdue = []
    if "attention" in sections:
        for o in planner.overdue(user, today)[:5]:
            overdue.append({"title": o.title, "due": f"{DAY_NAMES[o.date.weekday()]} {o.date:%b} {o.date.day}",
                            "url": _item_url(o)})

    rows = [r for d in out_days for r in d["items"]]
    counts = {"events": sum(r["kind"] == "event" for r in rows), "tasks": sum(r["kind"] == "task" for r in rows),
              "workouts": sum(r["kind"] in ("workout", "weigh_in") for r in rows), "overdue": len(overdue)}
    temps = [(w["low"], w["high"]) for w in wx.values() if w]
    payload = {
        "v": 1, "scope": scope, "start": _iso(start), "end": _iso(end), "title": title_for(scope, start, today),
        "range": _range(start, end), "sections": sections, "days": out_days, "repeats": repeats,
        "overdue": overdue, "counts": counts,
        "rain": [d["dow"] for d in out_days if d["weather"] and (d["weather"]["pop"] or 0) >= RAIN_POP][:3],
        "temps": [min(t[0] for t in temps), max(t[1] for t in temps)] if temps else None,
        "groceries": _safe(_groceries, user) if "meals" in sections and sources.can_open(user, "meals") else None,
        "money": _safe(_money, user) if "money" in sections and sources.can_open(user, "money") else None,
        "shared": _safe(_shared, user) if "shared" in sections else None,
    }
    payload["empty"] = not (rows or repeats or overdue or any(d["meals"] for d in out_days))
    return payload


def _safe(fn, user):
    try:
        return fn(user)
    except Exception:  # noqa: BLE001 - one section never breaks the roundup
        log.exception("Roundup section %s failed", fn.__name__)
        return None


# ---------------------------------------------------------------------------------------------------------------------
# Words: the rule-based summary (the default; no AI) and the push line
# ---------------------------------------------------------------------------------------------------------------------

def _join(parts):
    parts = [p for p in parts if p]
    if len(parts) <= 1:
        return "".join(parts)
    return ", ".join(parts[:-1]) + " and " + parts[-1]


def _when(day, row, scope):
    t = row["time"]
    if scope == TODAY:
        return t
    return f"{day['dow']} {t}".strip()


def summary(payload):
    """A few plain sentences from the payload - the "short version" when AI is off (or fails)."""
    scope = payload["scope"]
    days = payload["days"]
    events = [(d, r) for d in days for r in d["items"] if r["kind"] == "event"]
    tasks = [(d, r) for d in days for r in d["items"] if r["kind"] == "task"]
    workouts = [(d, r) for d in days for r in d["items"] if r["kind"] in ("workout", "weigh_in")]
    out = []
    if events:
        bits = [f"{r['title']} ({_when(d, r, scope)})" if _when(d, r, scope) else r["title"] for d, r in events[:4]]
        more = f", plus {len(events) - 4} more" if len(events) > 4 else ""
        out.append(f"Coming up: {_join(bits)}{more}.")
    if tasks:
        bits = [f"{r['title']} ({d['dow']})" if scope != TODAY else r["title"] for d, r in tasks[:4]]
        more = f" and {len(tasks) - 4} more" if len(tasks) > 4 else ""
        out.append(f"To do: {_join(bits)}{more}.")
    lifts = [(d, r) for d, r in workouts if r["kind"] == "workout"]
    weighs = [(d, r) for d, r in workouts if r["kind"] == "weigh_in"]
    if workouts:
        names = sorted({d["dow"] for d, _ in lifts}, key=DAY_NAMES.index) if scope != TODAY else []
        n = len(lifts)
        bit = f"{n} workout{'s' if n != 1 else ''}" + (f" ({', '.join(names)})" if names else "") if n else ""
        if weighs:
            w = f"a weigh-in {weighs[0][0]['dow']}" if scope != TODAY else "a weigh-in"
            bit = f"{bit} and {w}" if bit else w[0].upper() + w[1:]
        out.append(bit + ".")
    if payload.get("repeats"):
        out.append("Every day: " + " · ".join(r["title"] for r in payload["repeats"][:4]) + ".")
    if payload.get("rain"):
        out.append(f"Rain likely {_join(payload['rain'])}.")
    elif scope == TODAY and days and days[0].get("weather"):
        w = days[0]["weather"]
        out.append(f"{w['label']}, {w['high']}° / {w['low']}°.")
    if scope == WEEK:
        free = [d["dow"] for d in days if not d["items"]]
        if 0 < len(free) <= 2:
            out.append(f"{_join(free)} {'is' if len(free) == 1 else 'are'} free.")
    if payload.get("overdue"):
        o = payload["overdue"]
        out.append(f"{len(o)} overdue: {_join([x['title'] for x in o[:2]])}{' and more' if len(o) > 2 else ''}.")
    if not (events or tasks or workouts or payload.get("repeats") or payload.get("overdue")):
        out.insert(0, "Nothing on the calendar." if scope != TODAY else "Nothing scheduled.")
    return " ".join(out)


def push_text(payload, limit=170):
    """The banner body: the first few events, then overdue and rain - "Thanksgiving at Mom's Thu 2 PM · ..."."""
    scope = payload["scope"]
    bits = []
    for d in payload["days"]:
        for r in d["items"]:
            if r["kind"] == "event":
                bits.append(f"{r['title']} {_when(d, r, scope)}".strip())
    bits = bits[:3]
    if not bits:
        tasks = [r["title"] for d in payload["days"] for r in d["items"] if r["kind"] == "task"]
        if tasks:
            bits.append(f"{len(tasks)} task{'s' if len(tasks) != 1 else ''}: {tasks[0]}")
    if payload.get("overdue"):
        bits.append(f"{len(payload['overdue'])} overdue")
    if payload.get("rain"):
        bits.append("Rain " + "/".join(payload["rain"]))
    text = " · ".join(bits) or "Nothing scheduled - enjoy it."
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"
