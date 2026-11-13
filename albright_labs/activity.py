"""Activity - what changed in shared things and circle spaces, and who is told about it (ActivityEvent).

Apps call record_activity(...) (or one of the helpers below it) after a change. Nothing is recorded for something
only the actor can see: a Planner item, note or workout needs a share (albright_labs.sharing); a recipe a share or a
circle's kitchen; a grocery line, meal plan, rating, budget or transaction a circle's space. Repeats by the same
person on the same kind of thing within GROUP_WINDOW are merged ("Kate added 5 items to the grocery list"); a note's
edits within NOTE_WINDOW are one event. A bot's actions say "via <bot name>".

Who sees an event is decided when they look (visible), never stored:
  - audience OBJECT: whoever can view the object right now (sharing.can_view - unsharing hides it at once);
  - audience CIRCLE: the circle's ACTIVE members who opted in to the app (all active members for "circles") -
    leaving or opting out hides it at once;
  - recipient set: only that person (a circle invite is audience CIRCLE plus recipient, so the invitee sees it
    before they join).
Notifications (the bell, the digest, the Bot API) are those events minus the person's own actions, filtered by
their preferences (My account > Notifications: per app and per circle All / Only things that involve me / Off).
A circle-invite notification is not hidden if Circles is Off.
Quiet hours hide the badge count and hold the digest. Events are kept KEEP_DAYS (manage.py prune_activity).

Who did it comes from acting(...): ActivityMiddleware sets the signed-in person for each request, and the Bot API
sets the token's owner and name (via) - so signals and deep helpers know without being passed a user.
"""
import contextvars
from contextlib import contextmanager
from datetime import timedelta

from django.contrib.contenttypes.models import ContentType
from django.db.models import Exists, OuterRef, Q
from django.utils import timezone

from . import circles, sharing
from .models import CIRCLE_APPS, ActivityEvent, ActivityRead, CircleMembership, NotificationSettings, ShareGrant

GROUP_WINDOW = timedelta(minutes=15)
NOTE_WINDOW = timedelta(minutes=10)
KEEP_DAYS = 90
OBJECT, CIRCLE = ActivityEvent.OBJECT, ActivityEvent.CIRCLE
ALL, MINE, OFF = NotificationSettings.ALL, NotificationSettings.MINE, NotificationSettings.OFF
APP_LABELS = {"planner": "Planner", "meals": "Meals", "money": "Money", "notes": "Notes", "fitness": "Fitness",
              "circles": "Circles"}

_acting = contextvars.ContextVar("albright_activity_acting", default=None)


# ---------------------------------------------------------------------------
# Who is acting
# ---------------------------------------------------------------------------

@contextmanager
def acting(user, via=""):
    """Everything recorded inside is by this person (and, from the Bot API, via this bot)."""
    token = _acting.set((user, via or ""))
    try:
        yield
    finally:
        _acting.reset(token)


def current():
    """(user, via) - who is acting now, or (None, "")."""
    return _acting.get() or (None, "")


class ActivityMiddleware:
    """Each signed-in request acts as its user (after AuthenticationMiddleware)."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, "user", None)
        if user is not None and user.is_authenticated:
            with acting(user):
                return self.get_response(request)
        return self.get_response(request)


# ---------------------------------------------------------------------------
# Recording
# ---------------------------------------------------------------------------

def _ct(obj):
    return ContentType.objects.get_for_model(type(obj))


def _grants(obj):
    return ShareGrant.objects.filter(content_type=_ct(obj), object_id=obj.pk)


def _space_circle(obj):
    space = getattr(obj, "space", None)
    return space.circle if space is not None and space.circle_id else None


def _object_circle(obj):
    """The circle something is seen in: its space's (a recipe in a circle kitchen), else a circle it's shared with."""
    found = _space_circle(obj)
    if found is not None:
        return found
    grant = _grants(obj).filter(circle__isnull=False).select_related("circle").order_by("pk").first()
    return grant.circle if grant else None


def _seen_by_others(obj):
    """Can anyone but the owner see this? (A share, or a circle's space.)"""
    return _space_circle(obj) is not None or _grants(obj).exists()


def others_can_see(obj):
    """For callers that build a summary first: skip the work when nothing would be recorded."""
    return _seen_by_others(obj)


def _audience_key(obj):
    """Grouping across different objects only merges things shared exactly alike (the same people and circles)."""
    space = getattr(obj, "space_id", None)
    shares = sorted(f"{'c' if c else 'u'}{c or u}" for c, u in _grants(obj).values_list("circle_id", "user_id"))
    return f"s{space}|" + ",".join(shares) if space else ",".join(shares)



def _saved(event):
    """An event was written; queue Web Push (never raises, never blocks a rollback)."""
    try:
        from . import push
        push.schedule(event)
    except Exception:
        pass
    return event


def record_activity(app, verb, summary, *, obj=None, actor=None, via=None, circle=None, audience=None, recipient=None,
                    owner=None, url="", group="", plural="", count=1, window=GROUP_WINDOW):
    """Record (or merge into) one event; returns it, or None when nothing is recorded.
      summary: what follows the actor's name - "checked off “Pack”"; plural: the merged form with {n} -
               "checked off {n} tasks" (used once the count passes 1).
      obj: the thing it's about. A shareable thing (sharing.KINDS) defaults to audience OBJECT; anything else to
           CIRCLE, which needs `circle`.
      group: what counts as "the same kind of thing" for merging (same actor, via, app, verb and circle).
    actor / via default to acting(...); with no actor nothing is recorded (a management command, a migration)."""
    ctx_actor, ctx_via = current()
    if actor is None:
        actor = ctx_actor
    if via is None and ctx_actor is not None and getattr(actor, "pk", None) == ctx_actor.pk:
        via = ctx_via                                       # the same person, acting through a bot
    via = (via or "")[:100]
    if actor is None or not getattr(actor, "pk", None):
        return None
    kind = sharing.kind_of(obj) if obj is not None else None
    if audience is None:
        audience = OBJECT if kind is not None else CIRCLE
    if audience == OBJECT:
        if kind is None or not (recipient or _seen_by_others(obj)):
            return None                                     # only the actor can see it
        if circle is None and recipient is None:
            circle = _object_circle(obj)
        owner = owner or kind.owner_of(obj)
        url = url or kind.url(obj)
        if group:
            group = f"{group}|{_audience_key(obj)}"
    elif circle is None:
        return None                                         # a Personal space: only its person sees it
    now = timezone.now()
    fields = {"content_type": _ct(obj) if obj is not None else None, "object_id": obj.pk if obj is not None else None}
    if group:
        existing = (ActivityEvent.objects.filter(actor=actor, via=via, app=app, verb=verb, group_key=group[:120],
                                                 circle=circle, audience=audience, recipient=recipient,
                                                 updated_at__gte=now - window).order_by("-updated_at").first())
        if existing is not None:
            existing.count += count
            existing.summary = (plural.format(n=existing.count) if plural and existing.count > 1 else summary)[:300]
            existing.updated_at = now
            existing.url = url or existing.url
            for name, value in fields.items():
                setattr(existing, name, value)
            existing.save(update_fields=["count", "summary", "updated_at", "url", "content_type", "object_id"])
            return _saved(existing)
    text = plural.format(n=count) if plural and count > 1 else summary
    return _saved(ActivityEvent.objects.create(actor=actor, via=via, app=app, verb=verb, summary=text[:300], count=count,
                                        group_key=group[:120], audience=audience, circle=circle, recipient=recipient,
                                        owner=owner, url=(url or "")[:300], created_at=now, updated_at=now, **fields))


def quote(title, limit=60):
    title = " ".join(str(title or "").split())
    return f"“{title[:limit - 1] + '…' if len(title) > limit else title}”"


def created(obj, actor=None):
    """A new Planner item or note, once its sharing is applied: recorded when someone else can see it (once)."""
    kind = sharing.kind_of(obj)
    if kind is None or kind.app not in ("planner", "notes"):
        return None
    if ActivityEvent.objects.filter(verb="created", content_type=_ct(obj), object_id=obj.pk).exists():
        return None
    noun = "note" if kind.app == "notes" else ("event" if getattr(obj, "kind", "") == "event" else "task")
    return record_activity(kind.app, "created", f"added the {noun} {quote(kind.title(obj))}", obj=obj, actor=actor,
                           group=f"created:{kind.app}", plural=f"added {{n}} {noun}s")


def shared(grant, actor=None):
    """The owner shared something: "shared “Chili” with you" (a person) / "... with Albright Family" (a circle)."""
    obj = grant.content_type.model_class().objects.filter(pk=grant.object_id).first()
    if obj is None or sharing.kind_of(obj) is None:
        return None
    kind = sharing.kind_of(obj)
    if grant.user_id:
        return record_activity(kind.app, "shared", f"shared the {kind.noun} {quote(kind.title(obj))} with you", obj=obj,
                               actor=actor, recipient=grant.user)
    return record_activity(kind.app, "shared", f"shared the {kind.noun} {quote(kind.title(obj))} with {grant.circle.name}",
                           obj=obj, actor=actor, circle=grant.circle)


def circle_event(circle, verb, summary, actor=None):
    """Someone joined, left or was removed; an app was turned on. Seen by the circle's active members."""
    from django.urls import reverse
    return record_activity("circles", verb, summary, circle=circle, actor=actor, audience=CIRCLE,
                           url=reverse("labs:circle", args=[circle.pk]))


# ---------------------------------------------------------------------------
# Who sees what
# ---------------------------------------------------------------------------

def _authed(user):
    return bool(user and user.is_authenticated and user.is_active)


def visible(user):
    """Every event this person may see right now (their own included) - newest first."""
    if not _authed(user):
        return ActivityEvent.objects.none()
    q = Q(pk__in=[])
    for kind in sharing.KINDS.values():
        if circles.can_use_app(user, kind.app):
            q |= Q(audience=OBJECT, content_type=ContentType.objects.get_for_model(kind.model),
                   object_id__in=sharing.visible_queryset(user, kind.model).values("pk"))
    for app in CIRCLE_APPS:
        ids = sharing.circle_ids(user, app)
        if ids:
            q |= Q(audience=CIRCLE, app=app, circle_id__in=ids)
    member_of = list(CircleMembership.objects.filter(user=user, status=CircleMembership.ACTIVE).values_list("circle_id", flat=True))
    if member_of:
        q |= Q(audience=CIRCLE, app="circles", circle_id__in=member_of)
    # A direct circle message (an invite) is visible to its recipient even before they join.
    q |= Q(recipient=user, audience=CIRCLE)
    return (ActivityEvent.objects.filter(q).filter(Q(recipient__isnull=True) | Q(recipient=user))
            .select_related("actor", "circle"))


def involves(user):
    """Q: things that involve this person - theirs, or shared with them directly (not just through a circle)."""
    direct = ShareGrant.objects.filter(user=user, content_type=OuterRef("content_type"), object_id=OuterRef("object_id"))
    return Q(owner=user) | Q(recipient=user) | Q(Exists(direct))


def prefs_for(user):
    """Their settings - unsaved defaults when they never changed any (reading never writes a row)."""
    return NotificationSettings.objects.filter(user=user).first() or NotificationSettings(user=user)


def level(prefs, key):
    value = (prefs.levels or {}).get(key, ALL)
    return value if value in (ALL, MINE, OFF) else ALL


def notifications(user, prefs=None):
    """What this person is notified about: visible events by someone else, as their preferences allow."""
    prefs = prefs or prefs_for(user)
    qs = visible(user).exclude(actor=user)
    mine = involves(user)
    # An invite is a request to the person, not circle noise - it stays even if Circles is Off.
    keep_invites = Q(app="circles", verb="invited", recipient=user)
    for key, value in (prefs.levels or {}).items():
        kind, _, ref = key.partition(":")
        if kind == "app":
            match = Q(app=ref)
        elif kind == "circle" and ref.isdigit():
            match = Q(circle_id=int(ref))
        else:
            continue
        if value == OFF:
            qs = qs.exclude(match & ~keep_invites)
        elif value == MINE:
            qs = qs.exclude(match & ~mine)
    return qs


def unread(user, qs, prefs=None):
    """The events in qs this person hasn't read (their own actions are never unread)."""
    prefs = prefs or prefs_for(user)
    qs = qs.exclude(actor=user)
    if prefs.read_all_at:
        qs = qs.filter(updated_at__gt=prefs.read_all_at)
    seen = ActivityRead.objects.filter(user=user, event=OuterRef("pk"), read_at__gte=OuterRef("updated_at"))
    return qs.exclude(Exists(seen))


def in_quiet_hours(prefs, now=None):
    start, end = prefs.quiet_start, prefs.quiet_end
    if start is None or end is None or start == end:
        return False
    t = timezone.localtime(now or timezone.now()).time()
    return start <= t < end if start < end else (t >= start or t < end)


def unread_count(user):
    """The bell's number - 0 during quiet hours."""
    if not _authed(user):
        return 0
    prefs = prefs_for(user)
    if in_quiet_hours(prefs):
        return 0
    return unread(user, notifications(user, prefs), prefs).count()


def mark_read(user, event):
    ActivityRead.objects.update_or_create(user=user, event=event, defaults={"read_at": timezone.now()})


def mark_all_read(user):
    prefs = NotificationSettings.for_user(user)
    prefs.read_all_at = timezone.now()
    prefs.save(update_fields=["read_all_at", "updated_at"])


# ---------------------------------------------------------------------------
# Showing it
# ---------------------------------------------------------------------------

def actor_name(event):
    return sharing.person_name(event.actor) if event.actor_id else "Someone"


def text(event):
    """"Nathan checked off “Pack” via Recipe Bot"."""
    return f"{actor_name(event)} {event.summary}" + (f" via {event.via}" if event.via else "")


def rows(user, events, prefs=None):
    """Template rows: [{"event", "text", "app", "app_label", "initials", "color", "unread", "circle"}]."""
    from albright_planner.templatetags.planner_extras import avatar_color
    events = list(events)
    unread_ids = set()
    if events and _authed(user):
        unread_ids = set(unread(user, ActivityEvent.objects.filter(pk__in=[e.pk for e in events]), prefs)
                         .values_list("pk", flat=True))
    pending = {}
    if events and _authed(user):
        invite_ids = [e.object_id for e in events
                      if e.verb == "invited" and e.object_id and e.recipient_id == user.pk]
        if invite_ids:
            from .models import CircleInvite
            pending = {i.pk: i for i in CircleInvite.objects.filter(pk__in=invite_ids, user=user)
                       if i.status == "pending"}
    return [{"event": e, "text": text(e), "app": e.app, "app_label": APP_LABELS.get(e.app, e.app.title()),
             "initials": sharing.initials(e.actor) if e.actor_id else "?", "color": avatar_color(e.actor),
             "unread": e.pk in unread_ids, "circle": e.circle, "when": when(e.updated_at),
             "invite": pending.get(e.object_id) if e.verb == "invited" else None} for e in events]


def when(moment, now=None):
    """"just now", "5 minutes ago", "3 days ago"."""
    from django.utils.timesince import timesince
    now = now or timezone.now()
    if (now - moment).total_seconds() < 60:
        return "just now"
    return f"{timesince(moment, now, depth=1)} ago"


def as_json(event, user=None):
    """The Bot API's shape."""
    return {"id": event.pk, "app": event.app, "verb": event.verb, "text": text(event), "actor": actor_name(event),
            "via": event.via or None, "summary": event.summary, "count": event.count,
            "circle": {"id": event.circle_id, "name": event.circle.name} if event.circle_id else None,
            "url": event.url or None, "created_at": event.created_at.isoformat(), "updated_at": event.updated_at.isoformat()}


# ---------------------------------------------------------------------------
# Upkeep: the daily digest and pruning
# ---------------------------------------------------------------------------

def prune(now=None):
    """Delete events older than KEEP_DAYS. Returns how many."""
    cutoff = (now or timezone.now()) - timedelta(days=KEEP_DAYS)
    _total, per_model = ActivityEvent.objects.filter(updated_at__lt=cutoff).delete()
    return per_model.get(ActivityEvent._meta.label, 0)


def digest_events(user, prefs, now=None):
    """Unread notifications since the last digest (at most a day back)."""
    now = now or timezone.now()
    since = max(prefs.digest_sent_at or now - timedelta(days=1), now - timedelta(days=1))
    return list(unread(user, notifications(user, prefs), prefs).filter(updated_at__gt=since)[:50])


def send_digests(now=None):
    """The daily email for everyone who turned it on - nothing when there's nothing new or it's their quiet hours.
    Returns (sent, skipped)."""
    from . import emails
    now = now or timezone.now()
    sent = skipped = 0
    for prefs in NotificationSettings.objects.filter(digest=True).select_related("user"):
        user = prefs.user
        if not (user.is_active and user.email) or in_quiet_hours(prefs, now):
            skipped += 1
            continue
        events = digest_events(user, prefs, now)
        if not events:
            skipped += 1
            continue
        by_app = {}
        for row in rows(user, events, prefs):
            by_app.setdefault(row["app_label"], []).append(row["text"])
        ok, _message = emails.send("activity_digest", user.email, {
            "name": sharing.person_name(user), "groups": sorted(by_app.items()), "count": len(events),
            "link": emails.site_url(_reverse("labs:activity") + "?unread=1"), "button": "See what changed",
            "settings_link": emails.site_url(_reverse("labs:notification_settings"))})
        if ok:
            prefs.digest_sent_at = now
            prefs.save(update_fields=["digest_sent_at", "updated_at"])
            sent += 1
        else:
            skipped += 1
    return sent, skipped


def _reverse(name):
    from django.urls import reverse
    return reverse(name)
