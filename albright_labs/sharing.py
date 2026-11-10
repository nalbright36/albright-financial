"""Sharing - one system for Planner items, notes, recipes and workouts (ShareGrant). Every app asks here: can_view,
can_edit, visible_queryset / editable_queryset; and shares, unshares and applies defaults through here.

Who sees a shared thing:
  - its owner (Planner items, notes, workouts: the owner; a recipe: everyone who sees its space - circles.can_see);
  - a person it's shared with - while they can open the app;
  - a circle's ACTIVE members who opted in to the app for that circle, while the circle has the app enabled
    (albright_labs.circles) - leaving or opting out removes access at once.
Edit: the owner, or a grant with "edit". Only the owner shares, changes or removes shares (and deletes the thing).
Never shareable: the food diary, targets, body metrics, weight - they aren't registered here.
"""
from dataclasses import dataclass
from typing import Callable

from django.apps import apps as django_apps
from django.contrib.contenttypes.models import ContentType
from django.db.models import Q
from django.db.models.signals import post_delete
from django.urls import reverse

from . import circles
from .models import Circle, ShareGrant, SharingDefault

VIEW, EDIT = ShareGrant.VIEW, ShareGrant.EDIT
PERMISSIONS = dict(ShareGrant.PERMISSIONS)


class ShareError(Exception):
    pass


@dataclass(frozen=True)
class Kind:
    label: str                       # "albright_planner.ScheduleItem"
    app: str                         # apps_registry key: planner / notes / meals / fitness
    noun: str                        # "task", "note", "recipe", "workout"
    owner_q: Callable                # user -> Q of the things they own
    is_owner: Callable               # (user, obj) -> bool
    owner_of: Callable               # obj -> the user shown as its owner
    title: Callable                  # obj -> str
    url: Callable                    # obj -> its page
    can_see_verb: str                # "can see and check off this task" (edit) / "can see" (view)

    @property
    def model(self):
        return django_apps.get_model(self.label)


def _space_ids(user):
    return circles.space_ids(user, "meals")


KINDS = {
    "albright_planner.ScheduleItem": Kind(
        "albright_planner.ScheduleItem", "planner", "item", lambda u: Q(owner=u), lambda u, o: o.owner_id == u.pk,
        lambda o: o.owner, lambda o: o.title, lambda o: reverse("albright_planner:item", args=[o.pk]),
        "check off and change this item"),
    "albright_notes.Note": Kind(
        "albright_notes.Note", "notes", "note", lambda u: Q(owner=u), lambda u, o: o.owner_id == u.pk,
        lambda o: o.owner, lambda o: o.title, lambda o: reverse("albright_notes:note", args=[o.pk]),
        "edit this note"),
    "albright_meals.Recipe": Kind(
        "albright_meals.Recipe", "meals", "recipe", lambda u: Q(space_id__in=_space_ids(u)),
        lambda u, o: o.space_id in _space_ids(u), lambda o: o.created_by, lambda o: o.title,
        lambda o: reverse("albright_meals:recipe", args=[o.pk]), "edit this recipe"),
    "albright_fitness.Workout": Kind(
        "albright_fitness.Workout", "fitness", "workout", lambda u: Q(owner=u), lambda u, o: o.owner_id == u.pk,
        lambda o: o.owner, lambda o: o.title, lambda o: reverse("albright_fitness:summary", args=[o.pk]),
        "change this workout"),
}
APPS = {k.app: k for k in KINDS.values()}       # one shareable kind per app


def kind_of(obj_or_model):
    model = obj_or_model if isinstance(obj_or_model, type) else type(obj_or_model)
    return KINDS.get(model._meta.label)


def _ct(model):
    return ContentType.objects.get_for_model(model)


def _authed(user):
    return bool(user and user.is_authenticated and user.is_active)


# ---------------------------------------------------------------------------
# Who sees what
# ---------------------------------------------------------------------------

def circle_ids(user, app):
    """Circles whose shares count for this person in this app (active, opted in, app enabled)."""
    return [m.circle_id for m in circles._shared_memberships(user, app)]


def _grants_for(user, kind, permission=None):
    """ShareGrants that reach this person for this kind (none when they can't open the app)."""
    if not _authed(user) or not circles.can_use_app(user, kind.app):
        return ShareGrant.objects.none()
    grants = ShareGrant.objects.filter(content_type=_ct(kind.model)).filter(
        Q(user=user) | Q(circle_id__in=circle_ids(user, kind.app)))
    return grants.filter(permission=EDIT) if permission == EDIT else grants


def grants_reaching(user, model, permission=None):
    """The ShareGrants of this kind that reach this person (their own, and their circles' - Phase A rules)."""
    return _grants_for(user, kind_of(model), permission)


def shared_ids(user, model, permission=None):
    return _grants_for(user, kind_of(model), permission).values("object_id")


def visible_queryset(user, model, queryset=None):
    """Everything of this kind the person may see: their own and what's shared with them."""
    kind = kind_of(model)
    qs = queryset if queryset is not None else kind.model.objects.all()
    if not _authed(user):
        return qs.none()
    return qs.filter(kind.owner_q(user) | Q(pk__in=shared_ids(user, kind.model)))


def editable_queryset(user, model, queryset=None):
    kind = kind_of(model)
    qs = queryset if queryset is not None else kind.model.objects.all()
    if not _authed(user):
        return qs.none()
    return qs.filter(kind.owner_q(user) | Q(pk__in=shared_ids(user, kind.model, EDIT)))


def shared_with_queryset(user, model, queryset=None):
    """Only what others shared with this person (not their own)."""
    kind = kind_of(model)
    qs = queryset if queryset is not None else kind.model.objects.all()
    return qs.filter(pk__in=shared_ids(user, kind.model)).exclude(kind.owner_q(user))


def is_owner(user, obj):
    kind = kind_of(obj)
    return _authed(user) and kind is not None and kind.is_owner(user, obj)


def can_view(user, obj):
    kind = kind_of(obj)
    if kind is None or not _authed(user):
        return False
    return kind.is_owner(user, obj) or _grants_for(user, kind).filter(object_id=obj.pk).exists()


def can_edit(user, obj):
    kind = kind_of(obj)
    if kind is None or not _authed(user):
        return False
    return kind.is_owner(user, obj) or _grants_for(user, kind, EDIT).filter(object_id=obj.pk).exists()


def permission_for(user, obj):
    """"owner", "edit", "view" or None."""
    if is_owner(user, obj):
        return "owner"
    if can_edit(user, obj):
        return EDIT
    return VIEW if can_view(user, obj) else None


# ---------------------------------------------------------------------------
# Sharing (owner only)
# ---------------------------------------------------------------------------

def grants(obj):
    return list(ShareGrant.objects.filter(content_type=_ct(type(obj)), object_id=obj.pk)
                .select_related("user", "circle").order_by("circle__name", "user__username", "pk"))


def shareable_circles(user, app):
    """Circles this person can share with in this app: active in, opted in, app enabled."""
    return [m.circle for m in circles._shared_memberships(user, app)]


def _owner_only(user, obj):
    if not is_owner(user, obj):
        raise ShareError("Only its owner can share it.")


def resolve_target(user, kind, circle_id=None, who=""):
    """{"circle": ...} or {"user": ...} this owner may share with (one of their circles that shares the app, or a person
    by exact username / email who can open it) - else ShareError."""
    if circle_id not in (None, ""):
        circle = next((c for c in shareable_circles(user, kind.app) if str(c.pk) == str(circle_id)), None)
        if circle is None:
            raise ShareError("You can share only with circles that share this app and that you've joined it in.")
        return {"circle": circle}
    person = circles.find_person(who)
    if person is None or not circles.can_use_app(person, kind.app):
        raise ShareError("No one who can open this app matches that exactly - check their username or email.")
    if person.pk == user.pk:
        raise ShareError("That's you.")
    return {"user": person}


def share(user, obj, permission=VIEW, circle_id=None, who="", by=None):
    """Share with a circle (id) or a person (exact username / email). Returns the grant (updated if it existed)."""
    _owner_only(user, obj)
    kind = kind_of(obj)
    if permission not in PERMISSIONS:
        raise ShareError("Pick View or Edit.")
    target = resolve_target(user, kind, circle_id, who)
    if "user" in target and kind.is_owner(target["user"], obj):
        raise ShareError("They already see it - it's theirs too.")
    grant, _created = ShareGrant.objects.update_or_create(
        content_type=_ct(type(obj)), object_id=obj.pk, **target,
        defaults={"permission": permission, "created_by": by or user})
    return grant


def set_permission(user, obj, grant_id, permission):
    _owner_only(user, obj)
    if permission not in PERMISSIONS:
        raise ShareError("Pick View or Edit.")
    ShareGrant.objects.filter(content_type=_ct(type(obj)), object_id=obj.pk, pk=grant_id).update(permission=permission)


def unshare(user, obj, grant_id):
    _owner_only(user, obj)
    ShareGrant.objects.filter(content_type=_ct(type(obj)), object_id=obj.pk, pk=grant_id).delete()


def copy_grants(source, target, by=None):
    """A copy of something (a Planner fork) gets the same shares."""
    for g in grants(source):
        ShareGrant.objects.get_or_create(content_type=_ct(type(target)), object_id=target.pk, user=g.user, circle=g.circle,
                                         defaults={"permission": g.permission, "created_by": by or g.created_by})


def _delete_grants(sender, instance, **kwargs):
    ShareGrant.objects.filter(content_type=_ct(sender), object_id=instance.pk).delete()


def connect_signals():
    """Deleting a shared thing deletes its grants (AppConfig.ready)."""
    for kind in KINDS.values():
        post_delete.connect(_delete_grants, sender=kind.model, dispatch_uid=f"share-grants-{kind.label}")


# ---------------------------------------------------------------------------
# Defaults for new things
# ---------------------------------------------------------------------------

def default_for(user, app):
    return SharingDefault.objects.filter(user=user, app=app).select_related("circle").first()


def apply_defaults(obj, owner, by=None):
    """Share a new thing the way its owner shares new things in its app (on the site, quick add and bots)."""
    kind = kind_of(obj)
    d = default_for(owner, kind.app)
    if d is None or d.mode == SharingDefault.ONLY_ME:
        return []
    made = []
    try:
        if d.mode == SharingDefault.CIRCLE and d.circle_id:
            made.append(share(owner, obj, d.permission, circle_id=d.circle_id, by=by))
        elif d.mode == SharingDefault.PEOPLE:
            User = django_apps.get_model("auth", "User")
            for person in User.objects.filter(pk__in=[int(k) for k in d.people if str(k).isdigit()]):
                try:
                    made.append(share(owner, obj, d.people.get(str(person.pk), VIEW), who=person.username, by=by))
                except ShareError:
                    continue                          # someone who lost access: skipped, the rest still apply
    except ShareError:
        pass                                          # a circle they've since left: only them
    _record_created(obj, by or owner)
    return made


def _record_created(obj, actor):
    """A new Planner item or note others can now see shows in their activity (albright_labs.activity)."""
    from . import activity
    activity.created(obj, actor)


def apply_posted(request, obj, prefix="share"):
    """The Share control in a "New" form: share_mode = "default" (or missing) | "me" | "circle:<id>" | "person",
    with share_perm and share_who. Applies it to the just-created thing."""
    mode = request.POST.get(f"{prefix}_mode", "default")
    perm = request.POST.get(f"{prefix}_perm", VIEW)
    if mode == "default":
        return apply_defaults(obj, request.user)
    if mode == "me":
        return []
    made = []
    if mode.startswith("circle:"):
        made = [share(request.user, obj, perm, circle_id=mode.split(":", 1)[1])]
    elif mode == "person" and request.POST.get(f"{prefix}_who", "").strip():
        made = [share(request.user, obj, perm, who=request.POST[f"{prefix}_who"])]
    _record_created(obj, request.user)
    return made


def save_default(user, app, mode, circle_id=None, permission=VIEW, people=None):
    if app not in APPS:
        raise ShareError("That app has no sharing.")
    if permission not in PERMISSIONS:
        permission = VIEW
    circle = None
    if mode == SharingDefault.CIRCLE:
        circle = next((c for c in shareable_circles(user, app) if str(c.pk) == str(circle_id)), None)
        if circle is None:
            raise ShareError("Pick one of your circles that shares this app.")
    elif mode == SharingDefault.PEOPLE:
        people = {str(k): (v if v in PERMISSIONS else VIEW) for k, v in (people or {}).items()}
        if not people:
            mode = SharingDefault.ONLY_ME
    elif mode != SharingDefault.ONLY_ME:
        raise ShareError("Pick who new items are shared with.")
    return SharingDefault.objects.update_or_create(user=user, app=app, defaults={
        "mode": mode, "circle": circle, "permission": permission, "people": people if mode == SharingDefault.PEOPLE else {}})[0]


def default_text(user, app):
    """"Only me" / "Albright Family (can edit)" / "Sam, Kim" - for settings and the New forms."""
    d = default_for(user, app)
    if d is None or d.mode == SharingDefault.ONLY_ME:
        return "Only me"
    if d.mode == SharingDefault.CIRCLE and d.circle_id:
        return f"{d.circle.name} ({'can edit' if d.permission == EDIT else 'can view'})"
    User = django_apps.get_model("auth", "User")
    names = [(u.first_name or u.username) for u in User.objects.filter(pk__in=[int(k) for k in d.people if str(k).isdigit()])]
    return ", ".join(names) or "Only me"


# ---------------------------------------------------------------------------
# Words and the control
# ---------------------------------------------------------------------------

def person_name(user):
    return (user.first_name or user.get_username()).strip() if user else "Someone"


def initials(user):
    name = (user.get_full_name() or user.get_username()).strip().split()
    return ((name[0][:1] + (name[1][:1] if len(name) > 1 else "")) if name else "?").upper()


def explain(kind, grant):
    """"Albright Family members who use Planner can see and check off this item." - one line per grant."""
    from . import apps_registry
    app = apps_registry.BY_KEY[kind.app].label
    verb = f"can see and {kind.can_see_verb}" if grant.permission == EDIT else f"can see this {kind.noun}"
    if grant.circle_id:
        return f"{grant.circle.name} members who use {app} {verb}."
    return f"{person_name(grant.user)} {verb}."


def control(user, obj=None, app=None):
    """Everything the Share control (albright_labs/_share.html) shows, for an existing thing or a "New" form."""
    kind = kind_of(obj) if obj is not None else APPS[app]
    owner = obj is None or kind.is_owner(user, obj)
    rows = []
    if obj is not None:
        for g in grants(obj):
            rows.append({"grant": g, "is_circle": bool(g.circle_id),
                         "name": g.circle.name if g.circle_id else person_name(g.user),
                         "initials": "" if g.circle_id else initials(g.user), "explain": explain(kind, g)})
    return {"obj": obj, "kind": kind, "owner": owner, "rows": rows, "label": kind.label,
            "circles": shareable_circles(user, kind.app) if owner else [],
            "default": default_text(user, kind.app) if obj is None else "",
            "url": reverse("labs:share", args=[kind.label, obj.pk]) if obj is not None else "",
            "owner_name": person_name(kind.owner_of(obj)) if obj is not None and not owner else ""}


# ---------------------------------------------------------------------------
# "Shared with me"
# ---------------------------------------------------------------------------

def shared_with_me(user, app=None, circle_id=None):
    """Everything others shared with this person, newest first: [{"obj", "kind", "grant", "via"}] - one row per thing
    (the strongest grant; a person grant before a circle one)."""
    out = []
    for kind in KINDS.values():
        if app and kind.app != app:
            continue
        grants_qs = _grants_for(user, kind).select_related("circle", "created_by")
        if circle_id:
            grants_qs = grants_qs.filter(circle_id=circle_id)
        best = {}
        for g in grants_qs:
            current = best.get(g.object_id)
            if current is None or (g.permission == EDIT and current.permission != EDIT) or (g.user_id and not current.user_id):
                best[g.object_id] = g
        objects = kind.model.objects.in_bulk(list(best))
        for pk, g in best.items():
            obj = objects.get(pk)
            if obj is None or kind.is_owner(user, obj):
                continue
            out.append({"obj": obj, "kind": kind, "grant": g, "title": kind.title(obj), "url": kind.url(obj),
                        "shared_by": person_name(g.created_by) if g.created_by_id else person_name(kind.owner_of(obj)),
                        "circle": g.circle})
    return sorted(out, key=lambda r: r["grant"].created_at, reverse=True)


def circles_for_filter(user):
    return list(Circle.objects.filter(memberships__user=user, memberships__status="active").distinct().order_by("name"))
