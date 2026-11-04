"""Circles - who sees whose data, for every app. Apps never check membership themselves: they ask this module.

A circle's data in an app shows to a person only when ALL of these hold (visible_spaces / can_see):
  1. their membership is ACTIVE (they accepted the invite - not invited, left or removed);
  2. they've opted in to that app for that circle (CircleMembership.apps - off until they turn it on);
  3. the circle has the app enabled (Circle.enabled_apps);
  4. they can open the app on the site (Admin > Users - albright_labs.app_access).
Leaving, being removed or opting out takes effect at once: the next request no longer includes that circle's space.
Everyone also has a Personal space (created on first use) that only they ever see - owners included.

Owners rename the circle, choose its apps, invite, remove members and hand over ownership. A circle always has an
owner: the last one must hand over ownership (or delete the circle) before leaving.
"""
from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .models import CIRCLE_APPS, Circle, CircleInvite, CircleMembership, Space, UserPreference, hash_token

ACTIVE, OWNER = CircleMembership.ACTIVE, CircleMembership.OWNER
APP_SHARES = {   # one line each, for the accept screen and the circle page
    "planner": "Planner items you choose to share with the circle",
    "meals": "The circle's recipes, meal plan, grocery list and ideas",
    "money": "The circle's accounts, transactions and budget",
    "notes": "Notes you choose to share with the circle",
    "fitness": "Workouts you choose to share with the circle",
}


class CircleError(Exception):
    pass


def can_use_app(user, app):
    from . import app_access
    return bool(user and user.is_authenticated and user.is_active) and app_access.can_use(user, UserPreference.for_user(user), app)


# ---------------------------------------------------------------------------
# Spaces - what each person sees
# ---------------------------------------------------------------------------

def personal_space(user):
    """The user's Personal space (made the first time it's needed)."""
    return Space.objects.get_or_create(user=user)[0]


def _shared_memberships(user, app):
    return (CircleMembership.objects.filter(user=user, status=ACTIVE, apps__contains=[app], circle__enabled_apps__contains=[app])
            .select_related("circle"))


def visible_spaces(user, app):
    """[Space] this person sees in this app: Personal first, then each circle that passes every rule, by name."""
    if not can_use_app(user, app):
        return []
    circles = [m.circle for m in _shared_memberships(user, app)]
    shared = list(Space.objects.filter(circle__in=circles).select_related("circle").order_by("circle__name", "pk"))
    for circle in circles:                                     # a circle's space appears the first time it's needed
        if not any(s.circle_id == circle.pk for s in shared):
            shared.append(Space.objects.get_or_create(circle=circle)[0])
    return [personal_space(user)] + shared


def space_ids(user, app):
    return {s.pk for s in visible_spaces(user, app)}


def can_see(user, app, space):
    """The one check every app uses: may this person see this space in this app?"""
    if space is None or not can_use_app(user, app):
        return False
    if space.user_id:
        return space.user_id == user.pk
    return _shared_memberships(user, app).filter(circle_id=space.circle_id).exists()


def space_members(space, app):
    """Who sees a space in an app: its owner (Personal), or the circle's active members who opted in (and can open
    the app)."""
    User = get_user_model()
    if space.user_id:
        return User.objects.filter(pk=space.user_id)
    ids = [m.user_id for m in CircleMembership.objects.filter(circle_id=space.circle_id, status=ACTIVE, apps__contains=[app])
           .select_related("user") if can_use_app(m.user, app)]
    return User.objects.filter(pk__in=ids)


def current_space(user, app, requested=None):
    """The space an app shows: `requested` (an id) when the user may see it, else the one they last picked, else
    Personal. Remembers a valid pick per app."""
    spaces = visible_spaces(user, app)
    if not spaces:
        return None
    by_id = {s.pk: s for s in spaces}
    pref = UserPreference.objects.filter(user=user).first()
    remembered = (pref.current_spaces or {}).get(app) if pref else None
    for candidate in (requested, remembered):
        try:
            key = int(candidate)
        except (TypeError, ValueError):
            continue
        if key in by_id:
            if candidate == requested and key != remembered:
                remember(user, app, key)
            return by_id[key]
    return spaces[0]


def remember(user, app, space_id):
    pref, _created = UserPreference.objects.get_or_create(user=user)
    spaces = dict(pref.current_spaces or {})
    spaces[app] = space_id
    pref.current_spaces = spaces
    pref.save(update_fields=["current_spaces", "updated_at"])


# ---------------------------------------------------------------------------
# Circles and memberships
# ---------------------------------------------------------------------------

def membership(user, circle):
    return CircleMembership.objects.filter(user=user, circle=circle).first()


def my_circles(user):
    """[(circle, my membership)] for the circles I'm active in, by name."""
    rows = CircleMembership.objects.filter(user=user, status=ACTIVE).select_related("circle").order_by("circle__name")
    return [(m.circle, m) for m in rows]


def is_owner(user, circle):
    return CircleMembership.objects.filter(user=user, circle=circle, status=ACTIVE, role=OWNER).exists()


def active_members(circle):
    return CircleMembership.objects.filter(circle=circle, status=ACTIVE).select_related("user")


def _clean_apps(apps, allowed=CIRCLE_APPS):
    return [a for a in CIRCLE_APPS if a in set(apps or []) and a in allowed]


@transaction.atomic
def create(user, name, apps=(), color=""):
    name = " ".join((name or "").split())[:100]
    if not name:
        raise CircleError("Give the circle a name.")
    circle = Circle.objects.create(name=name, color=color[:7] if (color or "").startswith("#") else "", created_by=user,
                                   enabled_apps=_clean_apps(apps))
    CircleMembership.objects.create(circle=circle, user=user, role=OWNER, status=ACTIVE, joined_at=timezone.now(),
                                    apps=list(circle.enabled_apps))
    Space.objects.get_or_create(circle=circle)
    return circle


def _owner_only(user, circle):
    if not is_owner(user, circle):
        raise CircleError("Only the circle's owners can do that.")


def rename(user, circle, name):
    _owner_only(user, circle)
    name = " ".join((name or "").split())[:100]
    if not name:
        raise CircleError("Give the circle a name.")
    circle.name = name
    circle.save(update_fields=["name"])


def set_enabled_apps(user, circle, apps):
    """Owners choose the circle's apps. Turning one off hides it from everyone at once (members' opt-ins stay, so
    turning it back on brings back exactly who had opted in)."""
    _owner_only(user, circle)
    circle.enabled_apps = _clean_apps(apps)
    circle.save(update_fields=["enabled_apps"])


def set_participation(user, circle, app, on):
    """A member turns one app on or off for themselves in this circle - takes effect at once."""
    m = CircleMembership.objects.filter(user=user, circle=circle, status=ACTIVE).first()
    if m is None:
        raise CircleError("You're not in that circle.")
    if app not in CIRCLE_APPS or (on and app not in circle.enabled_apps):
        raise CircleError("That app isn't shared in this circle.")
    apps = set(m.apps or [])
    (apps.add if on else apps.discard)(app)
    m.apps = _clean_apps(apps)
    m.save(update_fields=["apps"])
    return m


def _owners(circle):
    return CircleMembership.objects.filter(circle=circle, status=ACTIVE, role=OWNER)


def leave(user, circle):
    m = CircleMembership.objects.filter(user=user, circle=circle, status=ACTIVE).first()
    if m is None:
        raise CircleError("You're not in that circle.")
    if m.role == OWNER and _owners(circle).count() == 1:
        raise CircleError("You're its only owner - make someone else an owner first, or delete the circle.")
    m.status, m.role = CircleMembership.LEFT, CircleMembership.MEMBER
    m.save(update_fields=["status", "role"])


def remove(owner, circle, user):
    _owner_only(owner, circle)
    if owner.pk == user.pk:
        raise CircleError("To leave, use Leave circle.")
    m = CircleMembership.objects.filter(user=user, circle=circle, status=ACTIVE).first()
    if m is None:
        raise CircleError("They're not in this circle.")
    m.status, m.role = CircleMembership.REMOVED, CircleMembership.MEMBER
    m.save(update_fields=["status", "role"])


def transfer(owner, circle, user, keep_owner=False):
    """Make an active member an owner; the giver steps down to member unless keep_owner."""
    _owner_only(owner, circle)
    m = CircleMembership.objects.filter(user=user, circle=circle, status=ACTIVE).first()
    if m is None:
        raise CircleError("They're not an active member.")
    m.role = OWNER
    m.save(update_fields=["role"])
    if not keep_owner and owner.pk != user.pk:
        CircleMembership.objects.filter(user=owner, circle=circle).update(role=CircleMembership.MEMBER)


def delete(owner, circle):
    """Owners can delete a circle - its shared Meals and Money data goes with it (members' personal data stays)."""
    _owner_only(owner, circle)
    circle.delete()


# ---------------------------------------------------------------------------
# Invites
# ---------------------------------------------------------------------------

def find_person(text):
    """An account by its exact username or email (any case) - never a list, never a guess."""
    text = (text or "").strip()
    if not text:
        return None
    User = get_user_model()
    found = list(User.objects.filter(is_active=True).filter(Q(username__iexact=text) | Q(email__iexact=text))[:2])
    return found[0] if len(found) == 1 else None


@transaction.atomic
def invite(owner, circle, who):
    """Invite by exact username or email -> (CircleInvite, raw token, site Invitation or None, its raw token).
    Someone with an account sees it under Circles (and the badge); an email with no account gets the site's sign-up
    invite, with the circle invite waiting when they arrive."""
    _owner_only(owner, circle)
    person = find_person(who)
    if person is not None:
        existing = membership(person, circle)
        if existing and existing.status == ACTIVE:
            raise CircleError("They're already in this circle.")
        pending = [i for i in CircleInvite.objects.filter(circle=circle, user=person) if i.status == "pending"]
        if pending:
            raise CircleError("They already have an invite waiting.")
        circle_invite, raw = CircleInvite.create(circle=circle, invited_by=owner, user=person, email=person.email or "")
        CircleMembership.objects.update_or_create(circle=circle, user=person, defaults={
            "status": CircleMembership.INVITED, "role": CircleMembership.MEMBER, "invited_by": owner, "apps": []})
        return circle_invite, raw, None, None
    email = (who or "").strip()
    from django.core.exceptions import ValidationError
    from django.core.validators import validate_email
    try:
        validate_email(email)
    except ValidationError as exc:
        raise CircleError("No account matches that exactly - check their username, or enter their email to invite them.") from exc
    from .models import Invitation
    circle_invite, raw = CircleInvite.create(circle=circle, invited_by=owner, email=email.lower())
    apps = [a for a in circle.enabled_apps if can_use_app(owner, a)]
    site_invite, site_raw = Invitation.create(email=email, apps=apps, created_by=owner, days=14, circle_invite=circle_invite)
    return circle_invite, raw, site_invite, site_raw


def attach_signup(user, site_invite):
    """A new account from a site invite that came with a circle invite: the invite is now theirs to accept."""
    ci = site_invite.circle_invite
    if ci is None or ci.status != "pending":
        return
    ci.user = user
    ci.save(update_fields=["user"])
    CircleMembership.objects.get_or_create(circle=ci.circle, user=user, defaults={
        "status": CircleMembership.INVITED, "invited_by": ci.invited_by})


def pending_invites(user):
    """Invites waiting for this person (not expired, accepted, declined or revoked)."""
    return [i for i in CircleInvite.objects.filter(user=user, accepted_at__isnull=True, declined_at__isnull=True,
                                                    revoked_at__isnull=True).select_related("circle", "invited_by")
            if i.status == "pending"]


def invite_for(user, pk=None, raw=None):
    """One of this person's pending invites - by id, or by the raw token from a link. None otherwise."""
    qs = CircleInvite.objects.filter(user=user).select_related("circle", "invited_by")
    if raw:
        if len(raw) > 100:
            return None
        found = qs.filter(token_hash=hash_token(raw)).first()
    else:
        found = qs.filter(pk=pk).first()
    return found if found and found.status == "pending" else None


@transaction.atomic
def accept(user, circle_invite, apps):
    """Join with the apps they chose (off unless chosen)."""
    if circle_invite.user_id != user.pk or circle_invite.status != "pending":
        raise CircleError("That invite isn't available any more.")
    circle = circle_invite.circle
    CircleMembership.objects.update_or_create(circle=circle, user=user, defaults={
        "status": ACTIVE, "role": CircleMembership.MEMBER, "joined_at": timezone.now(),
        "invited_by": circle_invite.invited_by, "apps": _clean_apps(apps, circle.enabled_apps)})
    circle_invite.accepted_at = timezone.now()
    circle_invite.save(update_fields=["accepted_at"])
    Space.objects.get_or_create(circle=circle)


def decline(user, circle_invite):
    if circle_invite.user_id != user.pk or circle_invite.status != "pending":
        raise CircleError("That invite isn't available any more.")
    circle_invite.declined_at = timezone.now()
    circle_invite.save(update_fields=["declined_at"])
    CircleMembership.objects.filter(circle=circle_invite.circle, user=user, status=CircleMembership.INVITED).delete()


def revoke(owner, circle_invite):
    _owner_only(owner, circle_invite.circle)
    if circle_invite.status != "pending":
        raise CircleError("That invite isn't pending.")
    circle_invite.revoked_at = timezone.now()
    circle_invite.save(update_fields=["revoked_at"])
    if circle_invite.user_id:
        CircleMembership.objects.filter(circle=circle_invite.circle, user_id=circle_invite.user_id,
                                        status=CircleMembership.INVITED).delete()
    for site in circle_invite.signups.filter(used_at__isnull=True):
        site.revoked = True
        site.save(update_fields=["revoked"])


def resend(owner, circle_invite):
    """A fresh token and 14 more days. Returns the raw token."""
    _owner_only(owner, circle_invite.circle)
    if circle_invite.accepted_at or circle_invite.declined_at or circle_invite.revoked_at:
        raise CircleError("That invite is closed.")
    return circle_invite.renew()


def pending_count(user):
    """For the account menu's badge."""
    if not (user and user.is_authenticated):
        return 0
    return len(pending_invites(user))
