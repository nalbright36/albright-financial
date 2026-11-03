"""Logging help: "last time" for an exercise and the suggested target, copying a set, the quick text log, and
personal records.

Suggested target: if every working set last time reached its reps (the program's target when there was one,
else the first working set's), the same reps with a little more weight - 5 lb (2.5 kg) for lower-body
exercises, 2.5 lb (1.25 kg) for upper body; otherwise repeat last time. Bodyweight and cardio: repeat.

Personal records (on finishing a workout): heaviest weight, best estimated 1-rep max (Epley: weight × (1 +
reps / 30); one rep is the weight itself), most reps at a weight, and for cardio the longest distance, the longest
time and the fastest pace. Weights compare in pounds, distances in miles. An exercise's first ever session sets
the baseline - records start from the second.
"""
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from django.db.models import Max

from .models import KG, LB, MI, Exercise, PersonalRecord, WorkoutExercise, WorkoutSet

STEP = {(True, LB): Decimal("5"), (False, LB): Decimal("2.5"), (True, KG): Decimal("2.5"), (False, KG): Decimal("1.25")}
TWO = Decimal("0.01")


# ---------------------------------------------------------------------------
# Last time and the suggested target
# ---------------------------------------------------------------------------

def last_session(user, exercise, before=None):
    """The exercise's sets from the user's last finished workout (before `before`, a Workout), or None."""
    qs = WorkoutExercise.objects.filter(workout__owner=user, exercise=exercise, workout__finished_at__isnull=False)
    if before is not None:
        qs = qs.exclude(workout=before).filter(workout__date__lte=before.date)
    we = qs.select_related("workout").order_by("-workout__date", "-workout__finished_at", "-pk").first()
    return we


@dataclass
class Suggestion:
    reps: int = None
    weight: Decimal = None
    unit: str = LB
    sets: int = 0
    increase: bool = False
    text: str = ""
    last: list = field(default_factory=list)       # last time's sets, for showing


def suggest(user, exercise, before=None, target_reps=None, target_sets=None):
    """Last time and what to aim for now (a Suggestion), or None if it's the first time. A program's target sets /
    reps, when there are some, set the shape; last time's weight sets the load."""
    we = last_session(user, exercise, before)
    if we is None:
        return None
    sets = list(we.sets.all())
    working = [s for s in sets if s.set_type == WorkoutSet.WORKING and s.done]
    out = Suggestion(last=sets)
    if exercise.is_cardio or exercise.kind == Exercise.MOBILITY or not working:
        out.text = "Repeat last time"
        out.sets = len(working) or len(sets)
        return out
    goal = target_reps or we.target_reps or working[0].reps
    top = max((s for s in working if s.weight is not None), key=lambda s: s.weight, default=None)
    out.sets, out.reps = target_sets or len(working), goal
    hit = goal is not None and all((s.reps or 0) >= goal for s in working)
    if top is None:                                   # bodyweight
        out.text = f"{out.sets} × {goal}" + (" - add a rep" if hit else " - repeat")
        out.increase = hit
        return out
    out.unit = top.unit
    if hit:
        out.weight = top.weight + STEP[(exercise.is_lower_body, top.unit)]
        out.increase = True
        out.text = f"{out.sets} × {goal} @ {fmt_weight(out.weight)} {top.unit} (+{fmt_weight(STEP[(exercise.is_lower_body, top.unit)])})"
    else:
        out.weight = top.weight
        out.text = f"{out.sets} × {goal} @ {fmt_weight(out.weight)} {top.unit} - repeat"
    return out


def fmt_weight(value):
    """225.00 -> "225", 232.50 -> "232.5"."""
    if value is None:
        return ""
    text = f"{Decimal(value):f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def prefill(workout_exercise, user, target_sets=None, target_reps=None, target_seconds=None, target_distance=None):
    """New sets for an exercise just added to a workout: from the suggestion (or the program's target)."""
    tip = suggest(user, workout_exercise.exercise, workout_exercise.workout, target_reps, target_sets)
    count = target_sets or (tip.sets if tip and tip.sets else (1 if workout_exercise.exercise.is_cardio else 3))
    rows = []
    for n in range(count):
        s = WorkoutSet(workout_exercise=workout_exercise, order=n)
        if workout_exercise.exercise.is_cardio:
            last = tip.last[0] if tip and tip.last else None
            s.duration_seconds = target_seconds or (last.duration_seconds if last else None)
            s.distance = target_distance or (last.distance if last else None)
            s.distance_unit = last.distance_unit if last else MI
        else:
            s.reps = (tip.reps if tip and tip.reps else None) or target_reps
            if tip and tip.weight is not None:
                s.weight, s.unit = tip.weight, tip.unit
        rows.append(s)
    WorkoutSet.objects.bulk_create(rows)
    return rows


def copy_set(previous, order):
    """"+ Set": a new set like the one before it (not done yet)."""
    return WorkoutSet.objects.create(
        workout_exercise=previous.workout_exercise, order=order, reps=previous.reps, weight=previous.weight,
        unit=previous.unit, duration_seconds=previous.duration_seconds, distance=previous.distance,
        distance_unit=previous.distance_unit, rpe=None, set_type=previous.set_type, done=False)


def next_order(workout_exercise):
    top = workout_exercise.sets.aggregate(m=Max("order"))["m"]
    return 0 if top is None else top + 1


# ---------------------------------------------------------------------------
# The quick text log: "squat 225x5x3, bench 185x8,8,7, run 3.1mi 28:30"
# ---------------------------------------------------------------------------

ALIASES = {"squat": "Barbell Back Squat", "squats": "Barbell Back Squat", "back squat": "Barbell Back Squat",
           "front squat": "Barbell Front Squat", "bench": "Barbell Bench Press", "bench press": "Barbell Bench Press",
           "incline bench": "Incline Barbell Bench Press", "deadlift": "Deadlift", "dl": "Deadlift",
           "rdl": "Romanian Deadlift", "ohp": "Overhead Press", "press": "Overhead Press", "row": "Barbell Row",
           "rows": "Barbell Row", "curl": "Barbell Curl", "curls": "Dumbbell Curl", "pullup": "Pull-Up",
           "pullups": "Pull-Up", "pull-ups": "Pull-Up", "pull ups": "Pull-Up", "chinup": "Chin-Up", "chinups": "Chin-Up",
           "pushup": "Push-Up", "pushups": "Push-Up", "push-ups": "Push-Up", "dips": "Dip", "lunges": "Walking Lunge",
           "plank": "Plank", "run": "Running", "ran": "Running", "jog": "Running", "walk": "Walking", "bike": "Cycling",
           "cycle": "Cycling", "ride": "Cycling", "erg": "Rowing Machine", "rower": "Rowing Machine", "swim": "Swimming",
           "elliptical": "Elliptical", "treadmill": "Treadmill Run", "leg press": "Leg Press", "pulldown": "Lat Pulldown",
           "lat pulldown": "Lat Pulldown", "hip thrust": "Barbell Hip Thrust", "kb swing": "Kettlebell Swing",
           "swings": "Kettlebell Swing"}
WEIGHT_SETS = re.compile(r"^(?P<w>\d+(?:\.\d+)?)\s*(?P<u>lbs?|kgs?)?\s*[x×*]\s*(?P<rest>[\d,\sx×*]+)$", re.I)
DIST = re.compile(r"(?P<d>\d+(?:\.\d+)?)\s*(?P<u>mi|miles?|km|k|m)\b", re.I)
TIME = re.compile(r"(?:(?P<h>\d+):)?(?P<m>\d{1,2}):(?P<s>\d{2})\b|(?P<min>\d+(?:\.\d+)?)\s*(?:min|mins|minutes)\b|(?P<sec>\d+)\s*(?:s|sec|secs|seconds)\b", re.I)


@dataclass
class Parsed:
    text: str
    exercise: object = None
    sets: list = field(default_factory=list)       # [{"reps", "weight", "unit", "duration_seconds", "distance", "distance_unit"}]
    problem: str = ""


def find_exercise(user, name):
    """An exercise by what someone typed: an alias, the exact name, or the shortest name containing every word."""
    name = " ".join(name.lower().replace("_", " ").split())
    visible = Exercise.objects.visible_to(user)
    target = ALIASES.get(name)
    if target:
        found = visible.filter(name__iexact=target).first()
        if found:
            return found
    exact = visible.filter(name__iexact=name).first()
    if exact:
        return exact
    words = [w.rstrip("s") for w in name.split() if w]
    candidates = [e for e in visible if all(w in e.name.lower() for w in words)]
    return min(candidates, key=lambda e: (len(e.name), e.owner_id is None), default=None)


def parse_quick(text, user):
    """[Parsed] - one per exercise in "squat 225x5x3, bench 185x8,8,7, run 3.1mi 28:30"."""
    out = []
    for chunk in re.split(r",\s*(?=[A-Za-z])|;|\n", text or ""):
        chunk = chunk.strip()
        if not chunk:
            continue
        m = re.match(r"^(?P<name>[A-Za-z][A-Za-z \-']*?)\s+(?P<rest>[\d(].*)$", chunk)
        if not m:
            out.append(Parsed(chunk, problem="no numbers after the exercise"))
            continue
        item = Parsed(chunk, exercise=find_exercise(user, m.group("name")))
        if item.exercise is None:
            item.problem = f"couldn't find an exercise called “{m.group('name').strip()}”"
            out.append(item)
            continue
        rest = m.group("rest").strip()
        weighted = WEIGHT_SETS.match(rest)
        if item.exercise.is_cardio or DIST.search(rest) or (TIME.search(rest) and not weighted):
            dist, time = DIST.search(rest), TIME.search(rest)
            row = {}
            if dist:
                unit = dist.group("u").lower()
                value = Decimal(dist.group("d"))
                if unit == "m":
                    value, unit = (value / 1000).quantize(TWO), "km"
                row.update(distance=value, distance_unit="km" if unit in ("km", "k") else MI)
            if time:
                row["duration_seconds"] = _seconds(time)
            if not row:
                item.problem = "couldn't read a distance or time"
            else:
                item.sets.append(row)
        elif weighted:
            weight = Decimal(weighted.group("w"))
            unit = KG if (weighted.group("u") or "").lower().startswith("kg") else LB
            for reps in _rep_list(weighted.group("rest")):
                item.sets.append({"reps": reps, "weight": weight, "unit": unit})
        else:                                         # bodyweight: "pullups 10,8,6" / "pushups 20x3"
            reps = _rep_list(rest)
            if not reps:
                item.problem = "couldn't read the sets"
            item.sets = [{"reps": r} for r in reps]
        if not item.sets and not item.problem:
            item.problem = "couldn't read the sets"
        out.append(item)
    return out


def _rep_list(text):
    """"5x3" -> [5, 5, 5]; "8,8,7" -> [8, 8, 7]; "5" -> [5]."""
    text = text.replace(" ", "").replace("×", "x").replace("*", "x").lower()
    if re.fullmatch(r"\d+x\d+", text):
        reps, sets = (int(p) for p in text.split("x"))
        return [reps] * min(sets, 50) if 0 < reps <= 1000 else []
    parts = [p for p in text.split(",") if p]
    return [int(p) for p in parts if p.isdigit() and 0 < int(p) <= 1000][:50] if all(p.isdigit() for p in parts) else []


def _seconds(match):
    if match.group("m") is not None:
        return int(match.group("h") or 0) * 3600 + int(match.group("m")) * 60 + int(match.group("s"))
    if match.group("min") is not None:
        return int(Decimal(match.group("min")) * 60)
    return int(match.group("sec"))


def save_quick(workout, parsed):
    """Add parsed exercises (and their sets, done) to a workout."""
    order = (workout.exercises.aggregate(m=Max("order"))["m"] or -1) + 1
    for item in parsed:
        if item.problem or not item.exercise:
            continue
        we = WorkoutExercise.objects.create(workout=workout, exercise=item.exercise, order=order)
        order += 1
        WorkoutSet.objects.bulk_create([WorkoutSet(workout_exercise=we, order=n, done=True, **row)
                                        for n, row in enumerate(item.sets)])


# ---------------------------------------------------------------------------
# Personal records
# ---------------------------------------------------------------------------

def epley(weight, reps):
    """Estimated 1-rep max: weight × (1 + reps / 30); a single is the weight itself."""
    if weight is None or not reps:
        return None
    weight = Decimal(weight)
    return weight if reps == 1 else (weight * (1 + Decimal(reps) / 30)).quantize(TWO)


def _bests(sets):
    """{kind: value} (and {"reps": {weight: reps}}) from done sets."""
    out = {"reps": {}}
    for s in sets:
        if not s.done or s.set_type == WorkoutSet.WARMUP:
            continue
        w = s.weight_lb
        if w is not None and w > 0 and s.reps:
            out["weight"] = max(out.get("weight", 0), w)
            out["e1rm"] = max(out.get("e1rm", 0), epley(w, s.reps))
            out["reps"][w] = max(out["reps"].get(w, 0), s.reps)
        miles = s.miles
        if miles:
            out["distance"] = max(out.get("distance", 0), miles)
        if s.duration_seconds:
            out["duration"] = max(out.get("duration", 0), Decimal(s.duration_seconds))
        if miles and s.duration_seconds:
            pace = (Decimal(s.duration_seconds) / miles).quantize(TWO)
            out["pace"] = min(out.get("pace", pace), pace)
    return out


def detect_records(workout):
    """Store this finished workout's new personal records and return them."""
    found = []
    for we in workout.exercises.select_related("exercise").prefetch_related("sets"):
        now = _bests(we.sets.all())
        earlier = WorkoutSet.objects.filter(workout_exercise__exercise=we.exercise, workout_exercise__workout__owner=workout.owner,
                                            workout_exercise__workout__finished_at__isnull=False) \
            .exclude(workout_exercise__workout=workout).filter(workout_exercise__workout__date__lte=workout.date)
        if not earlier.exists():
            continue                                   # the first time: the baseline, not a record
        before = _bests(earlier)

        def add(kind, value, previous, at=None):
            found.append(PersonalRecord.objects.create(owner=workout.owner, exercise=we.exercise, kind=kind, value=value,
                                                       previous=previous, at_weight=at, workout=workout,
                                                       achieved_on=workout.date))
        for kind in ("weight", "e1rm", "distance", "duration"):
            if kind in now and now[kind] > before.get(kind, 0):
                add(kind, now[kind], before.get(kind))
        if "pace" in now and "pace" in before and now["pace"] < before["pace"]:
            add("pace", now["pace"], before["pace"])
        for weight, reps in now["reps"].items():
            if weight in before["reps"] and reps > before["reps"][weight]:
                add("reps", Decimal(reps), Decimal(before["reps"][weight]), at=weight)
    return found


def record_text(record):
    """"Heaviest weight: 235 lb (was 225)" etc."""
    def show(kind, v):
        if v is None:
            return ""
        if kind in ("weight", "e1rm"):
            return f"{fmt_weight(v)} lb"
        if kind == "reps":
            return f"{int(v)} reps"
        if kind == "distance":
            return f"{fmt_weight(v)} mi"
        if kind == "duration":
            return clock(int(v))
        return f"{clock(int(v))}/mi"
    label = dict(PersonalRecord.KINDS)[record.kind]
    text = f"{label}: {show(record.kind, record.value)}"
    if record.kind == "reps":
        text = f"Most reps at {fmt_weight(record.at_weight)} lb: {int(record.value)}"
    if record.previous is not None:
        text += f" (was {show(record.kind, record.previous)})"
    return text


def clock(seconds):
    if seconds is None:
        return ""
    h, rest = divmod(int(seconds), 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def parse_clock(text):
    """"28:30" -> 1710, "1:05:00" -> 3900, "90" -> 90 (seconds); "" -> None."""
    text = (text or "").strip()
    if not text:
        return None
    try:
        parts = [int(p) for p in text.split(":")]
    except ValueError:
        return None
    seconds = 0
    for p in parts:
        seconds = seconds * 60 + p
    return seconds if 0 <= seconds < 100 * 3600 else None


def to_decimal(text, places=2):
    try:
        value = Decimal(str(text).strip().replace(",", ""))
    except (InvalidOperation, ValueError):
        return None
    return value.quantize(Decimal(1).scaleb(-places)) if value.is_finite() else None

