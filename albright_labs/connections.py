"""Connections - who you can pick when sharing with a person (albright_labs.sharing).

You're connected to someone when any of these hold:
  - you're both ACTIVE members of the same circle;
  - there's a visible (not hidden) person ShareGrant either way;
  - one of you invited the other to the site (an Invitation they signed up with);
  - there's an explicit Connection row ACCEPTED (they accepted a request - People > Find people, the Bot API).
A BLOCKED or REMOVED row undoes the last three (circle-mates stay connected until one leaves the circle).

Remove (My account > People) writes a REMOVED Connection and hides person ShareGrants between you,
without deleting them. You disappear from each other's pickers and lose access, both ways. Joining
a circle together reconnects you and restores those shares. Slice 2 will also restore when a
connection request is accepted. Circle-mates can't be removed here: leave the circle to disconnect.

The share type-ahead and search endpoint only ever look at this connected set (and your shareable
circles). The one directory search is find() (People > Find people): an exact email (case-insensitive) or a
username / display name that starts with 3+ characters - never a partial email, never a list of everyone - and it
skips people who chose "Nobody" (or "Only by username" for the email and name matches) in My account > Privacy,
and anyone either of you blocked. Results never include an email address. A request is a PENDING row; the other
person gets a bell notification (and an email if they want one) and Accepts or Declines.
"""
import re
from collections import defaultdict

from django.contrib.auth import get_user_model
from django.db.models import Q
from django.db.models.signals import post_save
from django.urls import reverse
from django.utils import timezone

from .models import Circle, CircleMembership, Connection, ShareGrant

ACTIVE = CircleMembership.ACTIVE
REMOVED, CONNECTED = Connection.REMOVED, Connection.CONNECTED
PENDING, DECLINED, BLOCKED = Connection.PENDING, Connection.DECLINED, Connection.BLOCKED

# How a 1:1 share is described on People and in the picker ("Shared notes with you").
SHARE_PHRASE = {"planner": "Planner items", "notes": "notes", "meals": "recipes", "fitness": "workouts"}
SEARCH_LIMIT = 50
SEARCH_QUERY_MAX = 80


class ConnectionError(Exception):
    pass


def _ordered(a, b):
    return (a, b) if a.pk < b.pk else (b, a)


def pair(a, b):
    """The Connection row for this pair, or None."""
    if a is None or b is None or a.pk == b.pk:
        return None
    low, high = _ordered(a, b)
    return Connection.objects.filter(user_a=low, user_b=high).first()


def is_removed(a, b):
    """True when one of you has clicked Remove and you don't share an active circle (circle-mates stay connected)."""
    if share_a_circle(a, b):
        return False
    row = pair(a, b)
    return bool(row and row.status == REMOVED)


def share_a_circle(a, b):
    """True when both are ACTIVE members of the same circle."""
    return bool(shared_circles(a, b))


def shared_circles(a, b):
    """Circles both are ACTIVE in, by name."""
    if a is None or b is None or a.pk == b.pk:
        return []
    mine = set(CircleMembership.objects.filter(user=a, status=ACTIVE).values_list("circle_id", flat=True))
    if not mine:
        return []
    theirs = set(CircleMembership.objects.filter(user=b, status=ACTIVE, circle_id__in=mine).values_list("circle_id", flat=True))
    if not theirs:
        return []
    return list(Circle.objects.filter(pk__in=theirs).order_by("name", "pk"))


def circle_mate_ids(user):
    my = CircleMembership.objects.filter(user=user, status=ACTIVE).values_list("circle_id", flat=True)
    return set(CircleMembership.objects.filter(circle_id__in=my, status=ACTIVE)
               .exclude(user=user).values_list("user_id", flat=True))


def _visible_share_ids(user):
    """User ids on the other side of a visible person ShareGrant (either way)."""
    given = set(ShareGrant.objects.filter(created_by=user, user__isnull=False, hidden_at__isnull=True)
                .values_list("user_id", flat=True))
    received = set(ShareGrant.objects.filter(user=user, circle__isnull=True, hidden_at__isnull=True)
                   .exclude(created_by=None).exclude(created_by=user).values_list("created_by_id", flat=True))
    # Grants whose created_by is missing: still count the object's owner (rare; historical rows).
    return given | received | _owned_share_ids(user)


def _owned_share_ids(user):
    """User ids on visible person grants of things this person owns, when created_by is empty."""
    from .sharing import KINDS
    ids = set()
    orphan = ShareGrant.objects.filter(circle__isnull=True, hidden_at__isnull=True, created_by__isnull=True)
    if not orphan.exists():
        return ids
    for kind in KINDS.values():
        owned = set(kind.model.objects.filter(kind.owner_q(user)).values_list("pk", flat=True))
        if not owned:
            continue
        from django.contrib.contenttypes.models import ContentType
        ids.update(orphan.filter(content_type=ContentType.objects.get_for_model(kind.model), object_id__in=owned)
                   .exclude(user=user).values_list("user_id", flat=True))
        # Things others own, shared with me, created_by empty:
        incoming = list(orphan.filter(content_type=ContentType.objects.get_for_model(kind.model), user=user))
        if incoming:
            objects = kind.model.objects.in_bulk([g.object_id for g in incoming])
            for g in incoming:
                obj = objects.get(g.object_id)
                owner = kind.owner_of(obj) if obj is not None else None
                if owner and owner.pk != user.pk:
                    ids.add(owner.pk)
    return ids


def _explicit_ids(user, status):
    rows = Connection.objects.filter(status=status).filter(Q(user_a=user) | Q(user_b=user))
    return {(r.user_b_id if r.user_a_id == user.pk else r.user_a_id) for r in rows}


def invite_ids(user):
    """People this person invited to the site who signed up, and whoever invited them."""
    from .models import Invitation
    sent = Invitation.objects.filter(created_by=user, used_by__isnull=False).values_list("used_by_id", flat=True)
    got = Invitation.objects.filter(used_by=user, created_by__isnull=False).values_list("created_by_id", flat=True)
    return set(sent) | set(got)


def connected_ids(user):
    """Pks of people this person is connected to (active accounts only are applied by connected_people)."""
    if user is None or not getattr(user, "is_authenticated", False):
        return set()
    mates = circle_mate_ids(user)
    ids = mates | _visible_share_ids(user) | _explicit_ids(user, CONNECTED) | invite_ids(user)
    ids -= ((_explicit_ids(user, REMOVED) | _explicit_ids(user, BLOCKED)) - mates)
    ids.discard(user.pk)
    return ids


def is_connected(user, other):
    return bool(other and other.pk in connected_ids(user))


def person_if_connected(user, user_id):
    """The connected, active account with this id, or None. Never looks up an id outside the connected set."""
    try:
        pk = int(user_id)
    except (TypeError, ValueError):
        return None
    if pk not in connected_ids(user):
        return None
    return get_user_model().objects.filter(pk=pk, is_active=True).first()


def connected_people(user):
    """Active connected accounts, by first name then username."""
    ids = connected_ids(user)
    if not ids:
        return []
    return list(get_user_model().objects.filter(pk__in=ids, is_active=True).order_by("first_name", "username", "pk"))


def _name(user):
    from .sharing import person_name
    return person_name(user)


def _initials(user):
    from .sharing import initials
    return initials(user)


def _join(words):
    words = [w for w in words if w]
    if not words:
        return ""
    if len(words) == 1:
        return words[0]
    if len(words) == 2:
        return f"{words[0]} and {words[1]}"
    return ", ".join(words[:-1]) + f", and {words[-1]}"


def _share_apps_between(user, other):
    """({apps I shared with them}, {apps they shared with me}) - visible person grants only."""
    from .sharing import KINDS
    from django.contrib.contenttypes.models import ContentType
    mine, theirs = set(), set()
    for kind in KINDS.values():
        ct = ContentType.objects.get_for_model(kind.model)
        if ShareGrant.objects.filter(content_type=ct, user=other, created_by=user, circle__isnull=True,
                                     hidden_at__isnull=True).exists():
            mine.add(kind.app)
        if ShareGrant.objects.filter(content_type=ct, user=user, created_by=other, circle__isnull=True,
                                     hidden_at__isnull=True).exists():
            theirs.add(kind.app)
    return mine, theirs


def reason(user, other):
    """One short line: "In Albright Family · you share Planner items" / "Shared notes with you"."""
    circles = shared_circles(user, other)
    parts = []
    if circles:
        parts.append("In " + _join([c.name for c in circles]))
    mine, theirs = _share_apps_between(user, other)
    order = ("planner", "notes", "meals", "fitness")
    if mine and not theirs:
        parts.append("you share " + _join([SHARE_PHRASE[a] for a in order if a in mine]))
    elif theirs and not mine:
        parts.append("Shared " + _join([SHARE_PHRASE[a] for a in order if a in theirs]) + " with you")
    elif mine or theirs:
        parts.append("you share " + _join([SHARE_PHRASE[a] for a in order if a in (mine | theirs)]))
    if not parts:
        parts.append("You connected")
    return " · ".join(parts)


def circle_meta(circle):
    """"3 members · Planner, Meals, Notes"."""
    from . import apps_registry
    from .models import CIRCLE_APPS
    n = CircleMembership.objects.filter(circle=circle, status=ACTIVE).count()
    members = f"{n} member" if n == 1 else f"{n} members"
    labels = [apps_registry.BY_KEY[a].label for a in CIRCLE_APPS
              if a in (circle.enabled_apps or []) and a in apps_registry.BY_KEY]
    return f"{members} · {', '.join(labels)}" if labels else members


def _already(obj):
    """({user ids already shared with}, {circle ids}) for a thing - visible grants only."""
    if obj is None:
        return set(), set()
    from .sharing import grants
    users, circles = set(), set()
    for g in grants(obj):
        if g.user_id:
            users.add(g.user_id)
        if g.circle_id:
            circles.add(g.circle_id)
    return users, circles


def picker_payload(user, app, obj=None):
    """People (who can open this app) and choices for the Share control's type-ahead and no-JS select."""
    repair_removed_mates(user)
    from . import circles as circles_mod
    from . import sharing
    already_users, already_circles = _already(obj)
    people_rows, people_choices = [], []
    for person in connected_people(user):
        if not circles_mod.can_use_app(person, app):
            continue
        row = {"id": person.pk, "name": _name(person), "initials": _initials(person), "meta": reason(user, person),
               "target": f"user:{person.pk}", "already": person.pk in already_users}
        people_rows.append(row)
        people_choices.append(row)
    circle_choices = []
    for circle in sharing.shareable_circles(user, app):
        circle_choices.append({"id": circle.pk, "name": circle.name, "meta": circle_meta(circle),
                               "target": f"circle:{circle.pk}", "already": circle.pk in already_circles})
    search = reverse("labs:people_search") + f"?app={app}"
    if obj is not None:
        kind = sharing.kind_of(obj)
        search += f"&label={kind.label}&pk={obj.pk}"
    choices = {"circles": circle_choices, "people": people_choices, "presets": [], "search": search}
    return people_rows, choices


def _haystack(name, *extra):
    return " ".join(s for s in (name,) + extra if s).lower()


def _matches(query, name, *extra):
    q = (query or "").strip().lower()
    return (not q) or q in _haystack(name, *extra)


def search(user, query, app, obj=None):
    """Filter the connected set (and this user's shareable circles) by a substring of the name.

    Never looks up accounts outside the connected set - an exact email or username of a
    stranger returns the same empty list as a typo.
    """
    query = (query or "")[:SEARCH_QUERY_MAX]
    _people, choices = picker_payload(user, app, obj=obj)
    people = []
    for p in choices["people"]:
        # Match display name only plus (for connected people) their username; email is matched but
        # never returned. Limited to this already-filtered connected set.
        person = person_if_connected(user, p["id"])
        extra = ()
        if person is not None:
            extra = (person.get_username(), person.first_name, person.last_name, person.email)
        if _matches(query, p["name"], *extra):
            people.append(p)
            if len(people) >= SEARCH_LIMIT:
                break
    circles = [c for c in choices["circles"] if _matches(query, c["name"])][:SEARCH_LIMIT]
    return {"circles": circles, "people": people}


def people_page_rows(user):
    """Rows for My account > People: name, why you're connected, and whether Remove is offered."""
    repair_removed_mates(user)
    rows = []
    for person in connected_people(user):
        circles = shared_circles(user, person)
        rows.append({"user": person, "name": _name(person), "initials": _initials(person),
                     "meta": reason(user, person), "circles": circles, "can_remove": not circles})
    return rows


# ---------------------------------------------------------------------------
# Remove and restore
# ---------------------------------------------------------------------------

def _kind_for_grant(grant):
    from .sharing import KINDS
    for kind in KINDS.values():
        if (kind.model._meta.app_label == grant.content_type.app_label
                and kind.model._meta.model_name == grant.content_type.model):
            return kind
    return None


def grants_between(a, b):
    """Person ShareGrants from a to b or b to a (the recipient is one, the owner / created_by is the other)."""
    qs = list(ShareGrant.objects.filter(circle__isnull=True, user_id__in=[a.pk, b.pk]))
    if not qs:
        return []
    by_kind = defaultdict(list)
    for g in qs:
        kind = _kind_for_grant(g)
        if kind is not None:
            by_kind[kind].append(g)
    found = []
    for kind, grants in by_kind.items():
        objects = kind.model.objects.in_bulk([g.object_id for g in grants])
        for g in grants:
            other_id = b.pk if g.user_id == a.pk else a.pk if g.user_id == b.pk else None
            if other_id is None:
                continue
            if g.created_by_id == other_id:
                found.append(g)
                continue
            obj = objects.get(g.object_id)
            if obj is None:
                continue
            owner = kind.owner_of(obj)
            if owner and owner.pk == other_id:
                found.append(g)
    return found


def _set_hidden(a, b, when):
    pks = [g.pk for g in grants_between(a, b)]
    if pks:
        ShareGrant.objects.filter(pk__in=pks).update(hidden_at=when)


def remove(actor, other):
    """Hide this connection both ways. Raises ConnectionError for circle-mates, yourself, or a stranger."""
    if other is None or other.pk == actor.pk or not other.is_active:
        raise ConnectionError("They're not in your people.")
    if not is_connected(actor, other):
        raise ConnectionError("They're not in your people.")
    if share_a_circle(actor, other):
        raise ConnectionError("Leave the circle to disconnect.")
    low, high = _ordered(actor, other)
    row, _created = Connection.objects.get_or_create(user_a=low, user_b=high, defaults={"status": REMOVED})
    row.status = REMOVED
    row.removed_by = actor
    row.removed_at = timezone.now()
    row.save(update_fields=["status", "removed_by", "removed_at"])
    _set_hidden(actor, other, timezone.now())
    return row


def reconnect(a, b):
    """Clear a REMOVED row and restore hidden person shares both ways. A no-op if you weren't removed."""
    if a is None or b is None or a.pk == b.pk:
        return
    row = pair(a, b)
    if row is not None and row.status == REMOVED:
        row.delete()
    _set_hidden(a, b, None)


def restore_with_mates(user, circle):
    """Joining a circle reconnects you with its other active members and restores hidden shares."""
    mates = (CircleMembership.objects.filter(circle=circle, status=ACTIVE).exclude(user=user)
             .select_related("user"))
    for m in mates:
        reconnect(user, m.user)


def repair_removed_mates(user):
    """If a REMOVED row was left in place for someone you share a circle with, restore (defense in depth)."""
    stale = circle_mate_ids(user) & _explicit_ids(user, REMOVED)
    if not stale:
        return
    for other in get_user_model().objects.filter(pk__in=stale):
        reconnect(user, other)


def connect_signals():
    """An ACTIVE membership restores hidden shares with the circle's other members (AppConfig.ready)."""
    post_save.connect(_on_membership, sender=CircleMembership, dispatch_uid="labs-connections-membership")


def _on_membership(sender, instance, **kwargs):
    if instance.status == ACTIVE and instance.user_id:
        restore_with_mates(instance.user, instance.circle)


# ---------------------------------------------------------------------------
# Find people and connection requests
# ---------------------------------------------------------------------------

FIND_MIN = 3
FIND_LIMIT = 10
EMAIL_SHAPE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def blocked_ids(user):
    """Everyone this person blocked, and everyone who blocked them - they never find or request each other."""
    return _explicit_ids(user, BLOCKED)


def find(user, query):
    """People > Find people: [{"id", "name", "username", "initials", "relation"}] - never an email address.
    An exact email (case-insensitive) finds someone who lets anyone find them; otherwise the username or display
    name must START with the query (3+ characters). "Only by username" people match only on their username; "Nobody"
    people never match. Never anyone either of you blocked, inactive accounts or yourself."""
    from .models import UserPreference
    q = " ".join((query or "").split())[:SEARCH_QUERY_MAX]
    if not q or user is None or not user.is_authenticated:
        return []
    User = get_user_model()
    base = User.objects.filter(is_active=True).exclude(pk=user.pk).exclude(pk__in=blocked_ids(user))
    nobody = UserPreference.objects.filter(findable=UserPreference.FIND_NOBODY).values("user_id")
    username_only = UserPreference.objects.filter(findable=UserPreference.FIND_USERNAME).values("user_id")
    if "@" in q:
        if not EMAIL_SHAPE.match(q):
            return []
        found = base.filter(email__iexact=q).exclude(pk__in=nobody).exclude(pk__in=username_only)
    else:
        if len(q) < FIND_MIN:
            return []
        handle = q.lstrip("@")
        by_username = Q(username__istartswith=handle)
        by_name = Q(first_name__istartswith=q) | Q(last_name__istartswith=q)
        if " " in q:
            first, _, last = q.partition(" ")
            by_name |= Q(first_name__iexact=first, last_name__istartswith=last)
        found = base.exclude(pk__in=nobody).filter(by_username | (by_name & ~Q(pk__in=username_only)))
    people = list(found.order_by("username")[:FIND_LIMIT])
    return [{"id": p.pk, "name": _name(p), "username": p.get_username(), "initials": _initials(p),
             "relation": relation(user, p)} for p in people]


def findable_person(user, pk):
    """The account behind a Find people result (an id from find()) - only if find() could have shown it: active, not
    yourself, not blocked either way, and not "Nobody". Otherwise None, the same as an id that doesn't exist."""
    from .models import UserPreference
    try:
        pk = int(pk)
    except (TypeError, ValueError):
        return None
    if pk == getattr(user, "pk", None) or pk in blocked_ids(user):
        return None
    other = get_user_model().objects.filter(pk=pk, is_active=True).first()
    if other is None:
        return None
    pref = UserPreference.objects.filter(user=other).first()
    if pref is not None and pref.findable == UserPreference.FIND_NOBODY and pk not in connected_ids(user) \
            and relation(user, other) != "incoming":
        return None
    return other


def relation(user, other):
    """What the Find people button says: "connected", "sent" (a request out, or one they declined - never told),
    "incoming" (they asked you: Accept), "blocked" (you blocked them), or "none" (Connect)."""
    row = pair(user, other)
    if row is not None:
        if row.status == BLOCKED:
            return "blocked" if row.blocked_by_id == user.pk else "none"
        if row.status == PENDING:
            return "sent" if row.from_user_id == user.pk else "incoming"
        if row.status == DECLINED and row.from_user_id == user.pk:
            return "sent"
    if other.pk in connected_ids(user):
        return "connected"
    return "none"


def _clear_bell(asker, asked):
    from .models import ActivityEvent
    ActivityEvent.objects.filter(app="circles", verb="connect", actor=asker, recipient=asked).delete()


def _notify_request(row):
    """The bell ("<name> wants to connect") for the person asked - and an email when they want one."""
    from . import activity, emails
    from .models import ActivityEvent, UserPreference
    asker, asked = row.from_user, row.to_user
    url = reverse("labs:people") + "#requests"
    _clear_bell(asker, asked)
    activity.record_activity("circles", "connect", "wants to connect", actor=asker, recipient=asked,
                             audience=ActivityEvent.CIRCLE, url=url)
    pref = UserPreference.objects.filter(user=asked).first()
    if asked.email and (pref is None or pref.email_connect_requests):
        emails.send("connect_request", asked.email, {
            "name": _name(asker), "username": asker.get_username(), "recipient": (asked.first_name or "").strip(),
            "link": emails.site_url(url), "button": "See the request"})


def send_request(actor, other, notify=True):
    """Ask to connect. Returns (row, outcome): "sent", "already_sent", "accepted" (they'd asked you - now
    connected) or "connected" (already). Raises ConnectionError with the same vague message for anyone you can't
    reach (yourself, inactive, blocked either way) so nothing is revealed."""
    if other is None or other.pk == actor.pk or not other.is_active:
        raise ConnectionError("No one found.")
    row = pair(actor, other)
    if row is not None and row.status == BLOCKED:
        raise ConnectionError("No one found.")
    if row is not None and row.status == PENDING:
        if row.from_user_id == actor.pk:
            return row, "already_sent"
        return accept(actor, row), "accepted"
    if row is not None and row.status == DECLINED and row.from_user_id == actor.pk:
        return row, "already_sent"                       # they said no; asking again doesn't bother them
    if other.pk in connected_ids(actor):
        return row, "connected"
    low, high = _ordered(actor, other)
    row, _created = Connection.objects.update_or_create(user_a=low, user_b=high, defaults={
        "status": PENDING, "from_user": actor, "to_user": other, "responded_at": None, "blocked_by": None})
    if notify:
        _notify_request(row)
    return row, "sent"


def incoming(user):
    return list(Connection.objects.filter(status=PENDING, to_user=user, from_user__is_active=True)
                .select_related("from_user").order_by("-created_at"))


def outgoing(user):
    return list(Connection.objects.filter(status__in=(PENDING, DECLINED), from_user=user, to_user__is_active=True)
                .select_related("to_user").order_by("-created_at"))


def blocked_by_me(user):
    rows = Connection.objects.filter(status=BLOCKED, blocked_by=user).select_related("user_a", "user_b")
    return [(r.user_b if r.user_a_id == user.pk else r.user_a) for r in rows]


def request_for(user, pk):
    """A request this person may act on (theirs to answer, or theirs to cancel), or None."""
    try:
        pk = int(pk)
    except (TypeError, ValueError):
        return None
    return Connection.objects.filter(pk=pk, status__in=(PENDING, DECLINED)).filter(
        Q(to_user=user) | Q(from_user=user)).select_related("from_user", "to_user").first()


def accept(actor, row):
    if row.status != PENDING or row.to_user_id != actor.pk:
        raise ConnectionError("That request isn't waiting for you.")
    row.status, row.responded_at = CONNECTED, timezone.now()
    row.save(update_fields=["status", "responded_at"])
    _set_hidden(row.from_user, row.to_user, None)          # shares hidden by an earlier Remove come back
    _clear_bell(row.from_user, actor)
    from . import activity
    from .models import ActivityEvent
    activity.record_activity("circles", "connected", "accepted your request to connect", actor=actor,
                             recipient=row.from_user, audience=ActivityEvent.CIRCLE, url=reverse("labs:people"))
    return row


def decline(actor, row):
    if row.status != PENDING or row.to_user_id != actor.pk:
        raise ConnectionError("That request isn't waiting for you.")
    row.status, row.responded_at = DECLINED, timezone.now()
    row.save(update_fields=["status", "responded_at"])
    _clear_bell(row.from_user, actor)
    return row


def cancel(actor, row):
    if row.from_user_id != actor.pk or row.status not in (PENDING, DECLINED):
        raise ConnectionError("That isn't your request.")
    _clear_bell(actor, row.to_user)
    row.delete()


def block(actor, other):
    """They can't find or request you again; nothing is sent. Person shares between you are hidden. A circle you're
    both in still connects you until one of you leaves it."""
    if other is None or other.pk == actor.pk:
        raise ConnectionError("No one found.")
    low, high = _ordered(actor, other)
    row, _created = Connection.objects.update_or_create(user_a=low, user_b=high, defaults={
        "status": BLOCKED, "blocked_by": actor, "responded_at": timezone.now()})
    _clear_bell(other, actor)
    if not share_a_circle(actor, other):
        _set_hidden(actor, other, timezone.now())
    return row


def unblock(actor, other):
    """Unblock: back to not connected (they can find you and ask again)."""
    row = pair(actor, other)
    if row is not None and row.status == BLOCKED and row.blocked_by_id == actor.pk:
        row.status, row.removed_by, row.removed_at = REMOVED, actor, timezone.now()
        row.save(update_fields=["status", "removed_by", "removed_at"])
