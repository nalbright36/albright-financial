"""The one Share sheet's space-backed items (Money plans, later accounts and meal ideas).

ShareGrant things (notes, planner items, recipes, workouts, programs, visuals) keep using
albright_labs.sharing.control. This module adds models that live in a Space: who may see them,
who may move them, and the Personal / circle radios. Moving is owner-only (a circle member
must not be able to walk a household plan or account into their Personal space). A space the
person cannot see in that app is rejected and the item is left alone.
"""
from dataclasses import dataclass
from typing import Callable

from django.apps import apps as django_apps
from django.http import Http404
from django.urls import reverse

from . import circles, sharing
from .models import Space


class SheetError(sharing.ShareError):
    pass


@dataclass(frozen=True)
class SpaceKind:
    label: str
    app: str
    noun: str
    is_mover: Callable          # (user, obj) -> bool
    can_view: Callable          # (user, obj) -> bool
    owner_of: Callable          # obj -> user
    title: Callable
    url: Callable
    space_of: Callable          # obj -> Space
    assign: Callable            # (obj, space) -> None

    @property
    def model(self):
        return django_apps.get_model(self.label)


def _plan_kind():
    from albright_money import budget
    from albright_money.models import BudgetDraft

    def can_view(user, obj):
        return budget.visible_plan(user, obj.pk) is not None

    def assign(obj, space):
        if obj.space_id != space.pk:
            obj.space = space
            obj.save(update_fields=["space"])

    return SpaceKind(
        "albright_money.BudgetDraft", "money", "plan",
        lambda u, o: o.owner_id == u.pk,
        can_view,
        lambda o: o.owner,
        lambda o: o.name,
        lambda o: budget.plan_url(o),
        lambda o: o.space,
        assign,
    )


def _account_kind():
    from albright_money.models import Account

    def can_view(user, obj):
        return Account.objects.visible_to(user).filter(pk=obj.pk).exists()

    def assign(obj, space):
        was = obj.space_id
        if was == space.pk:
            return
        obj.space = space
        obj.save(update_fields=["space"])
        from albright_money import money
        from albright_money.views import _rescope
        money.forget_account_activity(obj)
        if obj.transactions.exists():
            _rescope(obj)

    def url(obj):
        return reverse("albright_money:account", args=[obj.pk])

    return SpaceKind(
        "albright_money.Account", "money", "account",
        lambda u, o: o.owner_id == u.pk,
        can_view,
        lambda o: o.owner,
        lambda o: o.name,
        url,
        lambda o: o.space,
        assign,
    )


def _recipe_kind():
    from albright_meals import scope
    from albright_meals.models import Recipe

    def can_view(user, obj):
        return scope.readable(user).filter(pk=obj.pk).exists()

    def assign(obj, space):
        if obj.space_id != space.pk:
            obj.space = space
            obj.save(update_fields=["space", "updated_at"])

    return SpaceKind(
        "albright_meals.Recipe", "meals", "recipe",
        lambda u, o: o.created_by_id == u.pk,
        can_view,
        lambda o: o.created_by,
        lambda o: o.title,
        lambda o: reverse("albright_meals:recipe", args=[o.pk]),
        lambda o: o.space,
        assign,
    )


def _idea_kind():
    from albright_meals import scope
    from albright_meals.models import MealIdea

    def can_view(user, obj):
        return obj.space_id in scope.space_ids(user)

    def assign(obj, space):
        if obj.space_id != space.pk:
            obj.space = space
            obj.save(update_fields=["space", "updated_at"])

    return SpaceKind(
        "albright_meals.MealIdea", "meals", "idea",
        lambda u, o: o.added_by_id == u.pk,
        can_view,
        lambda o: o.added_by,
        lambda o: o.title,
        lambda o: reverse("albright_meals:ideas"),
        lambda o: o.space,
        assign,
    )


# Budget plan (e1); Account, Recipe (space + grants), MealIdea (e2).
SPACE_KINDS = {}


def _ensure():
    if not SPACE_KINDS:
        for factory in (_plan_kind, _account_kind, _recipe_kind, _idea_kind):
            kind = factory()
            SPACE_KINDS[kind.label] = kind


def space_kind_of(obj):
    _ensure()
    if obj is None:
        return None
    return SPACE_KINDS.get(type(obj)._meta.label)


def resolve(label, pk):
    """(obj, share-kind or None, space-kind or None). Missing / unknown label -> (None, None, None)."""
    _ensure()
    kind = sharing.KINDS.get(label)
    space_kind = SPACE_KINDS.get(label)
    model = (kind or space_kind) and (kind.model if kind else space_kind.model)
    if model is None:
        return None, None, None
    obj = model.objects.filter(pk=pk).first()
    return obj, kind, space_kind


def can_view(user, obj, kind=None, space_kind=None):
    kind = kind if kind is not None else sharing.kind_of(obj)
    space_kind = space_kind if space_kind is not None else space_kind_of(obj)
    if kind is not None:
        return sharing.can_view(user, obj)
    if space_kind is not None:
        return space_kind.can_view(user, obj)
    return False


def is_mover(user, obj, kind=None, space_kind=None):
    """Who may change sharing / move the item: ShareGrant owner, or the space-kind's mover (plan owner)."""
    kind = kind if kind is not None else sharing.kind_of(obj)
    space_kind = space_kind if space_kind is not None else space_kind_of(obj)
    if kind is not None:
        return sharing.is_owner(user, obj)
    if space_kind is not None:
        return bool(user and user.is_authenticated and space_kind.is_mover(user, obj))
    return False


def set_space(user, obj, target, space_kind=None):
    """Move a space-backed item to Personal or one of the spaces the user can see in its app.

    `target` is "personal" or "space:<id>". Raises ShareError (and does not move) when the person
    isn't the mover or the space isn't one they can see in that app — including a circle they
    left or were never in.
    """
    space_kind = space_kind or space_kind_of(obj)
    if space_kind is None:
        raise SheetError("This item doesn't live in a space.")
    if not space_kind.is_mover(user, obj):
        raise SheetError("Only its owner can change who sees it.")
    spaces = circles.visible_spaces(user, space_kind.app)
    allowed = {s.pk: s for s in spaces}
    if not allowed:
        raise SheetError("You can't open that app.")
    if target in ("personal", "me", ""):
        space = circles.personal_space(user)
    elif str(target).startswith("space:"):
        raw = target.split(":", 1)[1]
        space = allowed.get(int(raw)) if str(raw).isdigit() else None
    else:
        space = None
    if space is None or space.pk not in allowed:
        raise SheetError("Pick Personal or a circle you can use this app in.")
    space_kind.assign(obj, space)
    obj_space = space_kind.space_of(obj)
    if space.circle_id:
        return f"Opened to {obj_space.name}.", space_kind.url(obj)
    return "Only you can see this now.", space_kind.url(obj)


def space_visibility(user, obj, space_kind=None):
    """Radios for a space-backed item: Personal, then each circle space the user sees in that app."""
    space_kind = space_kind or space_kind_of(obj)
    if space_kind is None:
        return []
    current = space_kind.space_of(obj)
    options = [{
        "target": "personal",
        "name": "Personal",
        "meta": f"Only you can see this {space_kind.noun}" if space_kind.is_mover(user, obj)
                else "Only its owner can see this in Personal",
        "on": bool(current and current.user_id and current.user_id == user.pk),
    }]
    # If the plan is in someone else's Personal (shouldn't happen), no radio is on.
    if current and current.circle_id:
        # on-flag set in the loop
        pass
    elif current and current.user_id and current.user_id != user.pk:
        options[0]["on"] = False
    for space in circles.visible_spaces(user, space_kind.app):
        if not space.circle_id:
            continue
        on = bool(current and current.pk == space.pk)
        label = sharing.app_label(space_kind.app)
        noun = space_kind.noun
        options.append({
            "target": f"space:{space.pk}",
            "name": space.name,
            # These radios MOVE the item into the circle's space; they are not a ShareGrant.
            "meta": (f"This {noun} is in this circle's list. Members with {label} on for this circle can see it."
                     if on else
                     f"Move this {noun} into this circle's list. Members with {label} on for this circle can see it."),
            "on": on,
        })
    return options


def sheet(user, obj):
    """Context for albright_labs/_share.html, for a ShareGrant item or a space-backed one."""
    kind = sharing.kind_of(obj)
    space_kind = space_kind_of(obj)
    if kind is not None:
        data = sharing.control(user, obj)
        if space_kind is not None:
            data["visibility"] = space_visibility(user, obj, space_kind)
            data["visibility_action"] = "set_space"
            # Radios move the space (created_by / owner only). People chips still
            # use share.owner (for a recipe: anyone who sees its space).
            data["can_move"] = space_kind.is_mover(user, obj)
        return data
    if space_kind is None:
        return None
    mover = is_mover(user, obj, None, space_kind)
    return {
        "obj": obj, "kind": None, "space_kind": space_kind, "owner": mover, "can_move": mover,
        "rows": [], "people_rows": [], "chip_rows": [],
        "label": space_kind.label, "circles": [], "people": [], "choices": {},
        "default": "",
        "url": reverse("labs:share", args=[space_kind.label, obj.pk]),
        "owner_name": sharing.person_name(space_kind.owner_of(obj)) if not mover else "",
        "visibility": space_visibility(user, obj, space_kind),
        "visibility_action": "set_space",
        "noun": space_kind.noun,
    }
