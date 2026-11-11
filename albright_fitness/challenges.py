"""Circle fitness challenges: a date-range contest in a Circle, with point rules per
activity and measure, logged entries (optional photo), comments, and a live leaderboard
(challenge_views).

Access: only active members of the challenge's circle who opted into Fitness for that
circle (and can open Fitness) - the same four rules as albright_labs.circles.

The creator, or a circle owner, can add, edit and remove rules. A rule is one activity
plus one measure (for example walking by steps, or walking by miles). Changing a rule
recalculates stored points on existing entries so the leaderboard stays in sync. Photos
are stored as image bytes on ChallengePhoto (no files on disk).
"""
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models, transaction
from django.urls import reverse
from django.utils import timezone

from albright_labs import activity, circles


# ---------------------------------------------------------------------------
# Catalog: each activity can score by one or more measures. The stored key is
# "{kind}_{measure}" (walk_steps, run_miles). Suggested points and an optional
# daily cap are defaults for the add-rule form; a challenge can override them,
# including the unit label (slice 2).
# ---------------------------------------------------------------------------

def _d(value):
    return None if value is None else Decimal(str(value))


# (kind, label, category, [(measure, measure_label, unit, points, per_units, daily_cap), ...])
# The first measure is the default for that activity.
CATALOG = [
    ("walk", "Walking", "Cardio", [
        ("steps", "steps", "steps", "1", "1000", "20"),
        ("miles", "miles", "mi", "3", "1", "15"),
        ("minutes", "minutes", "min", "1", "10", "12"),
    ]),
    ("run", "Running", "Cardio", [
        ("miles", "miles", "mi", "10", "1", "50"),
        ("km", "km", "km", "6", "1", "80"),
        ("minutes", "minutes", "min", "1", "1", "60"),
    ]),
    ("bike", "Outdoor biking", "Cardio", [
        ("miles", "miles", "mi", "3", "1", "30"),
        ("minutes", "minutes", "min", "1", "5", "24"),
    ]),
    ("cycle", "Indoor cycling / Peloton", "Cardio", [
        ("minutes", "minutes", "min", "1", "5", "24"),
        ("miles", "miles", "mi", "3", "1", "30"),
        ("calories", "calories", "kcal", "1", "50", "20"),
    ]),
    ("hike", "Hiking", "Cardio", [
        ("miles", "miles", "mi", "8", "1", "40"),
        ("minutes", "minutes", "min", "1", "10", "18"),
        ("elevation", "elevation", "ft", "1", "100", "20"),
    ]),
    ("swim", "Swimming", "Cardio", [
        ("laps", "laps", "laps", "1", "5", "20"),
        ("yards", "yards", "yd", "1", "100", "20"),
        ("meters", "meters", "m", "1", "100", "20"),
        ("minutes", "minutes", "min", "1", "5", "24"),
    ]),
    ("row", "Rowing", "Cardio", [
        ("meters", "meters", "m", "1", "500", "20"),
        ("minutes", "minutes", "min", "1", "5", "24"),
    ]),
    ("elliptical", "Elliptical / stair climber", "Cardio", [
        ("minutes", "minutes", "min", "1", "10", "12"),
        ("floors", "floors", "floors", "1", "10", "15"),
    ]),
    ("jumprope", "Jump rope", "Cardio", [
        ("minutes", "minutes", "min", "1", "5", "12"),
        ("jumps", "jumps", "jumps", "1", "200", "15"),
    ]),
    ("weights", "Weight training", "Strength and training", [
        ("minutes", "minutes", "min", "1", "10", "12"),
        ("sessions", "sessions", "sessions", "10", "1", "20"),
    ]),
    ("bodyweight", "Bodyweight", "Strength and training", [
        ("reps", "reps", "reps", "1", "25", "20"),
    ]),
    ("plank", "Plank", "Strength and training", [
        ("minutes", "minutes", "min", "2", "1", "10"),
    ]),
    ("hiit", "HIIT / CrossFit", "Strength and training", [
        ("minutes", "minutes", "min", "1", "5", "12"),
        ("sessions", "sessions", "sessions", "10", "1", "20"),
    ]),
    ("class", "Workout class", "Strength and training", [
        ("sessions", "sessions", "sessions", "10", "1", "20"),
        ("minutes", "minutes", "min", "1", "10", "12"),
    ]),
    ("yoga", "Yoga", "Mind and body", [
        ("minutes", "minutes", "min", "1", "10", "12"),
        ("sessions", "sessions", "sessions", "8", "1", "16"),
    ]),
    ("pilates", "Pilates", "Mind and body", [
        ("minutes", "minutes", "min", "1", "10", "12"),
        ("sessions", "sessions", "sessions", "8", "1", "16"),
    ]),
    ("stretch", "Stretching / mobility", "Mind and body", [
        ("minutes", "minutes", "min", "1", "10", "6"),
    ]),
    ("meditate", "Meditation", "Mind and body", [
        ("minutes", "minutes", "min", "1", "10", "6"),
    ]),
    ("sports", "Sports", "Sports and outdoors", [
        ("minutes", "minutes", "min", "1", "10", "18"),
        ("games", "games", "games", "10", "1", "20"),
    ]),
    ("ski", "Skiing / snowboarding", "Sports and outdoors", [
        ("minutes", "minutes", "min", "1", "15", "16"),
        ("runs", "runs", "runs", "2", "1", "20"),
    ]),
    ("kayak", "Kayaking / paddleboarding", "Sports and outdoors", [
        ("minutes", "minutes", "min", "1", "10", "12"),
        ("miles", "miles", "mi", "5", "1", "25"),
    ]),
    ("dance", "Dance", "Sports and outdoors", [
        ("minutes", "minutes", "min", "1", "10", "12"),
    ]),
    ("water", "Water", "Habits", [
        ("glasses", "glasses", "glasses", "1", "1", "8"),
    ]),
    ("sleep", "Sleep", "Habits", [
        ("hours", "hours", "hours", "1", "1", "8"),
    ]),
    ("active", "Active minutes", "Habits", [
        ("minutes", "minutes", "min", "1", "10", "15"),
    ]),
]

# Pairs (or more) of measures that score the same effort two ways. Enabling more
# than one in a group shows a warning on the rules page.
OVERLAP_GROUPS = [
    ("walk_steps", "walk_miles", "walk_minutes"),
    ("run_miles", "run_km", "run_minutes"),
    ("bike_miles", "bike_minutes"),
    ("cycle_minutes", "cycle_miles", "cycle_calories"),
    ("hike_miles", "hike_minutes"),
    ("swim_laps", "swim_yards", "swim_meters", "swim_minutes"),
    ("row_meters", "row_minutes"),
    ("elliptical_minutes", "elliptical_floors"),
    ("jumprope_minutes", "jumprope_jumps"),
    ("weights_minutes", "weights_sessions"),
    ("hiit_minutes", "hiit_sessions"),
    ("class_sessions", "class_minutes"),
    ("yoga_minutes", "yoga_sessions"),
    ("pilates_minutes", "pilates_sessions"),
    ("sports_minutes", "sports_games"),
    ("ski_minutes", "ski_runs"),
    ("kayak_minutes", "kayak_miles"),
]


def _plain(value):
    """Decimal as a plain string for form values (no commas)."""
    if value is None:
        return ""
    return format(Decimal(value).normalize(), "f")


def _index_catalog():
    labels, units, names, measures, kinds, suggested, choices = {}, {}, {}, {}, {}, {}, []
    groups, kind_rows, current_cat, cat_opts = [], {}, None, None
    defaults = {}
    for kind, name, category, measure_rows in CATALOG:
        recs = []
        for i, (meas, meas_label, unit, pts, per, cap) in enumerate(measure_rows):
            key = f"{kind}_{meas}"
            pts, per, cap = _d(pts), _d(per), _d(cap)
            # "Active minutes (minutes)" would be redundant.
            label = name if name.lower().endswith(meas_label.lower()) else f"{name} ({meas_label})"
            labels[key] = label
            units[key] = unit
            names[key] = name
            measures[key] = meas_label
            kinds[key] = kind
            suggested[key] = (pts, per, cap)
            choices.append((key, label))
            rec = {"key": key, "measure": meas, "label": meas_label, "unit": unit,
                   "points": pts, "per_units": per, "daily_cap": cap}
            recs.append(rec)
            option = (key, f"{name} - {meas_label}", unit, _plain(pts), _plain(per), _plain(cap), kind, meas)
            if current_cat != category:
                if cat_opts is not None:
                    groups.append((current_cat, cat_opts))
                current_cat = category
                cat_opts = []
            cat_opts.append(option)
            if i == 0:
                defaults[kind] = key
        kind_rows[kind] = {"key": kind, "label": name, "category": category, "measures": recs}
    if cat_opts is not None:
        groups.append((current_cat, cat_opts))
    return labels, units, names, measures, kinds, suggested, choices, groups, kind_rows, defaults


(ACTIVITY_LABELS, ACTIVITY_UNITS, ACTIVITY_NAMES, ACTIVITY_MEASURES, ACTIVITY_KINDS,
 SUGGESTED, ACTIVITY_CHOICES, CATALOG_GROUPS, CATALOG_KINDS, DEFAULT_KEYS) = _index_catalog()
ACTIVITY_KEYS = tuple(ACTIVITY_LABELS)
# Slice 1-2 keys after migration 0009: steps, run, bike, lift, class.
STEPS, RUN, BIKE, LIFT, CLASS = "walk_steps", "run_miles", "bike_miles", "weights_sessions", "class_sessions"
# Kept for existing tests and create-challenge defaults.
DEFAULT_STEPS_POINTS = Decimal("1")
DEFAULT_STEPS_PER = Decimal("1000")
TWO = Decimal("0.01")
MAX_AMOUNT = Decimal("1000000")
MAX_POINTS = Decimal("100000")
MAX_PHOTO_BYTES = 2 * 1024 * 1024
PHOTO_TYPES = {"image/jpeg", "image/png", "image/webp"}

# Slice 1-2 activity keys, plus a unit hint when the old rule used a custom unit.
_MINUTES = {"min", "mins", "minute", "minutes"}
_KM = {"km", "kilometer", "kilometers", "kilometre", "kilometres"}
_LEGACY = {
    "steps": "walk_steps",
    "run": "run_miles",
    "bike": "bike_miles",
    "lift": "weights_sessions",
    "class": "class_sessions",
}


def remap_legacy_activity(key, unit=None):
    """Map a slice 1-2 activity key onto the catalog. Already-new keys pass through.

    lift and class (and run/bike) use the stored unit when it clearly picks a
    different measure, so a lifting rule in minutes becomes weights_minutes.
    """
    key = (key or "").strip()
    if key in ACTIVITY_KEYS:
        return key
    unit_l = " ".join((unit or "").split()).lower()
    if key == "run":
        if unit_l in _KM:
            return "run_km"
        if unit_l in _MINUTES:
            return "run_minutes"
        return "run_miles"
    if key == "bike":
        return "bike_minutes" if unit_l in _MINUTES else "bike_miles"
    if key == "lift":
        return "weights_minutes" if unit_l in _MINUTES else "weights_sessions"
    if key == "class":
        return "class_minutes" if unit_l in _MINUTES else "class_sessions"
    return _LEGACY.get(key, key)


def resolve_activity_key(key, unit=None):
    """Accept a catalog key or a slice 1-2 key (steps, run, bike, lift, class)."""
    return remap_legacy_activity(key, unit)


def default_key(kind):
    """The default (first) measure key for an activity, e.g. walk → walk_steps."""
    return DEFAULT_KEYS.get(kind)


def available_options(exclude=None):
    """Grouped catalog options for a <select>, skipping keys already used on a challenge.

    Each option is (key, 'Walking - steps', unit, points, per, cap, kind, measure).
    """
    skip = set(exclude or [])
    groups = []
    for category, opts in CATALOG_GROUPS:
        left = [o for o in opts if o[0] not in skip]
        if left:
            groups.append((category, left))
    return groups


def available_kinds(exclude=None):
    """Activities that still have a free measure, grouped by category, for the JS picker."""
    skip = set(exclude or [])
    by_cat = []
    current, rows = None, None
    for kind, name, category, measure_rows in CATALOG:
        recs = [m for m in CATALOG_KINDS[kind]["measures"] if m["key"] not in skip]
        if not recs:
            continue
        row = {"key": kind, "label": name, "measures": recs}
        if current != category:
            if rows is not None:
                by_cat.append((current, rows))
            current, rows = category, []
        rows.append(row)
    if rows is not None:
        by_cat.append((current, rows))
    return by_cat


_OVERLAP_NOUN = {
    "walk": "walk", "run": "run", "bike": "ride", "cycle": "ride", "hike": "hike",
    "swim": "swim", "row": "row", "elliptical": "session", "jumprope": "jump-rope session",
    "weights": "workout", "hiit": "workout", "class": "class", "yoga": "session",
    "pilates": "session", "sports": "game", "ski": "ski day", "kayak": "paddle",
}


def overlap_warnings(keys):
    """Human warnings when a challenge scores the same effort two ways."""
    have = set(keys or [])
    warnings = []
    for group in OVERLAP_GROUPS:
        hit = [k for k in group if k in have]
        if len(hit) < 2:
            continue
        labels = [ACTIVITY_LABELS[k] for k in hit]
        if len(labels) == 2:
            joined = f"{labels[0]} and {labels[1]}"
        else:
            joined = ", ".join(labels[:-1]) + f", and {labels[-1]}"
        noun = _OVERLAP_NOUN.get(ACTIVITY_KINDS[hit[0]], ACTIVITY_NAMES[hit[0]].lower())
        warnings.append(f"{joined} count the same {noun} twice.")
    return warnings


def log_label(rule):
    """Label for the log form: 'Walking (miles)', or the custom unit when one is set."""
    key, unit = rule.activity, rule.unit
    name = ACTIVITY_NAMES.get(key) or ACTIVITY_LABELS.get(key, key)
    default_unit = ACTIVITY_UNITS.get(key, "")
    if unit and default_unit and unit != default_unit:
        return f"{name} ({unit})"
    measure = ACTIVITY_MEASURES.get(key)
    if measure:
        return f"{name} ({measure})"
    return ACTIVITY_LABELS.get(key, key)


class ChallengeError(Exception):
    pass


class Challenge(models.Model):
    """A dated contest in one circle. created_by may be any participating member."""
    circle = models.ForeignKey("albright_labs.Circle", on_delete=models.CASCADE, related_name="fitness_challenges")
    name = models.CharField(max_length=100)
    start_date = models.DateField()
    end_date = models.DateField()
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name="fitness_challenges_created")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-start_date", "-pk"]

    def __str__(self):
        return self.name

    def clean(self):
        from django.core.exceptions import ValidationError
        if self.start_date and self.end_date and self.end_date < self.start_date:
            raise ValidationError("The end date has to be on or after the start date.")

    @property
    def is_active(self):
        today = timezone.localdate()
        return self.start_date <= today <= self.end_date

    def covers(self, day):
        return self.start_date <= day <= self.end_date


class ChallengeRule(models.Model):
    """How one activity+measure scores: `points` per `per_units` of `unit`, optional daily point cap."""
    challenge = models.ForeignKey(Challenge, on_delete=models.CASCADE, related_name="rules")
    activity = models.CharField(max_length=32, choices=ACTIVITY_CHOICES)
    unit = models.CharField(max_length=20)
    points = models.DecimalField(max_digits=10, decimal_places=2, validators=[MinValueValidator(0)])
    per_units = models.DecimalField(max_digits=12, decimal_places=2, default=1,
                                    validators=[MinValueValidator(Decimal("0.01"))])
    daily_cap = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True,
                                    validators=[MinValueValidator(0)])

    class Meta:
        ordering = ["pk"]
        constraints = [models.UniqueConstraint(fields=["challenge", "activity"], name="fitness_one_rule_per_activity")]

    def __str__(self):
        return f"{self.challenge}: {self.activity}"

    def text(self):
        """'1 pt per 1,000 steps' (and a cap, if set)."""
        pts, per = _fmt(self.points), _fmt(self.per_units)
        word = "pt" if self.points == 1 else "pts"
        unit = self.unit or ACTIVITY_UNITS.get(self.activity, self.activity)
        cap = f", cap {_fmt(self.daily_cap)} pts/day" if self.daily_cap is not None else ""
        return f"{pts} {word} per {per} {unit}{cap}"


class ChallengeEntry(models.Model):
    """One person's amount for one activity+measure on one day. points is computed from the rule and stored."""
    challenge = models.ForeignKey(Challenge, on_delete=models.CASCADE, related_name="entries")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="fitness_challenge_entries")
    date = models.DateField()
    activity = models.CharField(max_length=32, choices=ACTIVITY_CHOICES)
    amount = models.DecimalField(max_digits=12, decimal_places=2, validators=[MinValueValidator(Decimal("0.01")),
                                                                               MaxValueValidator(MAX_AMOUNT)])
    note = models.CharField(max_length=500, blank=True)
    points = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-date", "pk"]
        constraints = [models.UniqueConstraint(fields=["challenge", "user", "date", "activity"],
                                               name="fitness_one_entry_per_day_activity")]

    def __str__(self):
        return f"{self.user} {self.date} {self.activity}: {self.amount}"


class EntryComment(models.Model):
    """Plain-text comment on an entry."""
    entry = models.ForeignKey(ChallengeEntry, on_delete=models.CASCADE, related_name="comments")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="fitness_entry_comments")
    text = models.TextField(max_length=2000)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "pk"]

    def __str__(self):
        return f"Comment by {self.user} on entry {self.entry_id}"


class ChallengePhoto(models.Model):
    """Optional photo on an entry, stored as image bytes (no files on disk; no user-controlled name)."""
    entry = models.OneToOneField(ChallengeEntry, on_delete=models.CASCADE, related_name="photo")
    data = models.BinaryField(editable=False)
    content_type = models.CharField(max_length=20)
    uploaded_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Photo for entry {self.entry_id}"


# ---------------------------------------------------------------------------
# Access, scoring, logging, the board, rules, photos
# ---------------------------------------------------------------------------

def can_participate(user, circle):
    """Active in the circle, opted into Fitness there, the circle has Fitness on, and they can open Fitness."""
    if not circles.can_use_app(user, "fitness"):
        return False
    m = circles.membership(user, circle)
    return bool(m and m.status == circles.ACTIVE and "fitness" in (m.apps or [])
                and "fitness" in (circle.enabled_apps or []))


def can_edit_rules(user, challenge):
    """The person who started the challenge, or a circle owner, if they can still participate."""
    if not can_participate(user, challenge.circle):
        return False
    return (challenge.created_by_id == user.pk) or circles.is_owner(user, challenge.circle)


def participatable_circles(user):
    """Circles this person can create or join a challenge in, by name."""
    if not circles.can_use_app(user, "fitness"):
        return []
    return [c for c, m in circles.my_circles(user)
            if "fitness" in (m.apps or []) and "fitness" in (c.enabled_apps or [])]


def visible(user):
    """Challenges in circles this person participates in (Fitness)."""
    ids = [c.pk for c in participatable_circles(user)]
    return Challenge.objects.filter(circle_id__in=ids).select_related("circle", "created_by")


def participants(circle):
    """Active members who opted into Fitness (and can open it) - the people on a leaderboard."""
    return [m.user for m in circles.active_members(circle)
            if "fitness" in (m.apps or []) and circles.can_use_app(m.user, "fitness")]


def points_for(rule, amount):
    """Points for an amount under this rule, applying the daily cap. 0 if the amount isn't positive."""
    try:
        amount = Decimal(amount)
    except (InvalidOperation, TypeError):
        return Decimal("0.00")
    if amount <= 0 or rule.per_units <= 0:
        return Decimal("0.00")
    pts = (amount / rule.per_units * rule.points).quantize(TWO, rounding=ROUND_HALF_UP)
    if rule.daily_cap is not None and pts > rule.daily_cap:
        pts = rule.daily_cap.quantize(TWO)
    return pts


def _fmt(value):
    value = Decimal(value)
    text = format(value.normalize(), "f")
    if "." in text:
        whole, frac = text.split(".", 1)
        return f"{int(whole):,}" + (("." + frac) if frac else "")
    return f"{int(value):,}"


def display_name(user):
    return (user.first_name or user.get_username()).strip()


def leaderboard(challenge):
    """[{user, name, points, rank, entries}] highest points first; 0-point members included; ties share a rank."""
    people = participants(challenge.circle)
    totals = {u.pk: Decimal("0.00") for u in people}
    counts = {u.pk: 0 for u in people}
    for row in (ChallengeEntry.objects.filter(challenge=challenge, user_id__in=totals)
                .values("user_id").annotate(total=models.Sum("points"), n=models.Count("id"))):
        totals[row["user_id"]] = row["total"] or Decimal("0.00")
        counts[row["user_id"]] = row["n"]
    by_id = {u.pk: u for u in people}
    ordered = sorted(totals, key=lambda pk: (-totals[pk], display_name(by_id[pk]).lower(), pk))
    board, prev, rank = [], None, 0
    for i, pk in enumerate(ordered, start=1):
        pts = totals[pk]
        if prev is None or pts != prev:
            rank = i
            prev = pts
        board.append({"user": by_id[pk], "name": display_name(by_id[pk]), "points": pts, "rank": rank,
                      "entries": counts[pk], "me": False})
    return board


@transaction.atomic
def create_challenge(user, circle, name, start, end, steps_points=None, steps_per=None, steps_cap=None,
                     activity_key=None, unit=None):
    """Create a challenge with one scoring rule (steps by default). Raises ChallengeError on bad input or no access."""
    if not can_participate(user, circle):
        raise ChallengeError("You can only start a challenge in a circle where you've joined Fitness.")
    name = " ".join((name or "").split())[:100]
    if not name:
        raise ChallengeError("Give the challenge a name.")
    start, end = _date(start, "start date"), _date(end, "end date")
    if end < start:
        raise ChallengeError("The end date has to be on or after the start date.")
    activity_key = resolve_activity_key(activity_key or STEPS, unit)
    if activity_key not in ACTIVITY_KEYS:
        raise ChallengeError("Pick a scoring activity.")
    default_pts, default_per, _default_cap = SUGGESTED[activity_key]
    if activity_key == STEPS:
        default_pts, default_per = DEFAULT_STEPS_POINTS, DEFAULT_STEPS_PER
    points = _decimal(steps_points, default_pts, "points")
    per = _decimal(steps_per, default_per, "units per point")
    cap = _decimal(steps_cap, None, "daily cap", allow_empty=True)
    if points < 0 or per <= 0:
        raise ChallengeError("Points have to be 0 or more, and the unit count has to be more than 0.")
    if cap is not None and cap < 0:
        raise ChallengeError("The daily cap has to be 0 or more.")
    unit = " ".join((unit or "").split())[:20] or ACTIVITY_UNITS[activity_key]
    challenge = Challenge.objects.create(circle=circle, name=name, start_date=start, end_date=end, created_by=user)
    ChallengeRule.objects.create(challenge=challenge, activity=activity_key, unit=unit,
                                 points=points, per_units=per, daily_cap=cap)
    activity.record_activity(
        "fitness", "challenge", f"started the challenge {activity.quote(name)}",
        circle=circle, actor=user, audience=activity.CIRCLE,
        url=reverse("albright_fitness:challenge", args=[challenge.pk]))
    return challenge


def sniff_image(data):
    """Content type from magic bytes, or None if this isn't a JPEG, PNG or WebP."""
    if not data:
        return None
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data[:4] == b"RIFF" and len(data) >= 12 and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def read_photo(upload):
    """Return (bytes, content_type) or None if nothing was attached. Raises ChallengeError if invalid."""
    if not upload:
        return None
    if isinstance(upload, (bytes, bytearray, memoryview)):
        data = bytes(upload)
    else:
        size = getattr(upload, "size", None)
        if size == 0:
            return None
        if size is not None and size > MAX_PHOTO_BYTES:
            raise ChallengeError("Photos have to be 2 MB or smaller.")
        data = upload.read()
    if not data:
        return None
    if len(data) > MAX_PHOTO_BYTES:
        raise ChallengeError("Photos have to be 2 MB or smaller.")
    ctype = sniff_image(data)
    if not ctype:
        raise ChallengeError("Use a JPEG, PNG or WebP image.")
    return data, ctype


def _attach_photo(entry, parsed):
    data, ctype = parsed
    ChallengePhoto.objects.update_or_create(entry=entry, defaults={"data": data, "content_type": ctype})


@transaction.atomic
def log_entry(challenge, user, *, day, activity_key, amount, note="", photo=None):
    """Create or replace this user's entry for that day and activity. Returns the entry.
    photo is optional (an uploaded file or raw bytes); a new photo replaces the previous one."""
    if not can_participate(user, challenge.circle):
        raise ChallengeError("You're not in this challenge.")
    day = _date(day, "date")
    today = timezone.localdate()
    if not challenge.covers(day):
        raise ChallengeError("That date isn't in this challenge.")
    if day > today:
        raise ChallengeError("You can't log a future date.")
    activity_key = resolve_activity_key(activity_key)
    if activity_key not in ACTIVITY_KEYS:
        raise ChallengeError("Pick an activity this challenge scores.")
    rule = challenge.rules.filter(activity=activity_key).first()
    if rule is None:
        raise ChallengeError("This challenge doesn't score that activity yet.")
    amount = _decimal(amount, None, "amount")
    if amount is None or amount <= 0:
        raise ChallengeError("Enter an amount greater than 0.")
    if amount > MAX_AMOUNT:
        raise ChallengeError("That amount is too large.")
    note = " ".join((note or "").split())[:500]
    parsed = read_photo(photo)
    points = points_for(rule, amount)
    entry, created = ChallengeEntry.objects.select_for_update().get_or_create(
        challenge=challenge, user=user, date=day, activity=activity_key,
        defaults={"amount": amount, "note": note, "points": points})
    if not created:
        if entry.user_id != user.pk:
            raise ChallengeError("You can only edit your own entries.")
        entry.amount, entry.note, entry.points = amount, note, points
        entry.save(update_fields=["amount", "note", "points", "updated_at"])
    if parsed:
        _attach_photo(entry, parsed)
    return entry


def add_comment(entry, user, text):
    if not can_participate(user, entry.challenge.circle):
        raise ChallengeError("You're not in this challenge.")
    text = " ".join((text or "").split())
    if not text:
        raise ChallengeError("Write a comment first.")
    return EntryComment.objects.create(entry=entry, user=user, text=text[:2000])


def _recalculate(challenge, activity_key, rule):
    """Recompute stored points for this activity from the current rule (0 if the rule was removed)."""
    entries = list(challenge.entries.filter(activity=activity_key))
    for entry in entries:
        entry.points = points_for(rule, entry.amount) if rule is not None else Decimal("0.00")
    if entries:
        ChallengeEntry.objects.bulk_update(entries, ["points"])


def _rule_values(points, per_units, daily_cap, unit, activity_key):
    points = _decimal(points, None, "points")
    per = _decimal(per_units, None, "units per point")
    cap = _decimal(daily_cap, None, "daily cap", allow_empty=True)
    if points is None or points < 0 or per is None or per <= 0:
        raise ChallengeError("Points have to be 0 or more, and the unit count has to be more than 0.")
    if cap is not None and cap < 0:
        raise ChallengeError("The daily cap has to be 0 or more.")
    if points > MAX_POINTS or per > MAX_AMOUNT or (cap is not None and cap > MAX_POINTS):
        raise ChallengeError("That number is too large.")
    unit = " ".join((unit or "").split())[:20] or ACTIVITY_UNITS[activity_key]
    return points, per, cap, unit


@transaction.atomic
def set_rule(challenge, user, activity_key, points, per_units, daily_cap=None, unit=None):
    """Create or replace the rule for one activity+measure, then recalculate that activity's stored points."""
    if not can_edit_rules(user, challenge):
        raise ChallengeError("Only the person who started this challenge, or a circle owner, can edit the rules.")
    activity_key = resolve_activity_key(activity_key, unit)
    if activity_key not in ACTIVITY_KEYS:
        raise ChallengeError("Pick a scoring activity.")
    points, per, cap, unit = _rule_values(points, per_units, daily_cap, unit, activity_key)
    rule, _created = ChallengeRule.objects.update_or_create(
        challenge=challenge, activity=activity_key,
        defaults={"unit": unit, "points": points, "per_units": per, "daily_cap": cap})
    _recalculate(challenge, activity_key, rule)
    return rule


@transaction.atomic
def remove_rule(challenge, user, activity_key):
    """Drop a rule and zero that activity's stored points (entries, comments and photos stay)."""
    if not can_edit_rules(user, challenge):
        raise ChallengeError("Only the person who started this challenge, or a circle owner, can edit the rules.")
    activity_key = resolve_activity_key(activity_key)
    rules = challenge.rules.all()
    if not rules.filter(activity=activity_key).exists():
        raise ChallengeError("That rule isn't on this challenge.")
    if rules.count() == 1:
        raise ChallengeError("A challenge needs at least one rule - add another before removing this one.")
    rules.filter(activity=activity_key).delete()
    _recalculate(challenge, activity_key, None)


def _date(value, label):
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat((value or "").strip())
    except (TypeError, ValueError) as exc:
        raise ChallengeError(f"Enter a valid {label}.") from exc


def _decimal(value, default, label, allow_empty=False):
    if value in (None, ""):
        if allow_empty or default is not None:
            return default
        raise ChallengeError(f"Enter the {label}.")
    try:
        number = Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, TypeError) as exc:
        raise ChallengeError(f"Enter a valid {label}.") from exc
    if not number.is_finite():
        raise ChallengeError(f"Enter a valid {label}.")
    return number
