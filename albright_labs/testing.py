"""Test helpers shared by the apps' tests: the header's app launcher (templates/index.html), and
giving a test user apps (new accounts start with none)."""
import re

TILE = re.compile(r'<a href="([^"]*)" class="(app-launcher__tile[^"]*)" role="menuitem" tabindex="-1" '
                  r'data-app-key="(\w+)" data-app-target="(\w+)".*?<span class="app-launcher__label">([^<]+)</span>', re.S)


def launcher_panel(content):
    """The launcher panel's tiles markup, or "" when the header has no launcher."""
    if 'id="app-launcher-panel"' not in content:
        return ""
    return content.split('id="app-launcher-panel"', 1)[1].split('class="app-launcher__customize"', 1)[0]


def launcher_tiles(content):
    """[(data-app-target, label), ...] of the launcher's tiles, in order."""
    return [(target, label) for _href, _cls, _key, target, label in TILE.findall(launcher_panel(content))]


def launcher_links(content):
    """{label: href} of the launcher's tiles."""
    return {label: href for href, _cls, _key, _target, label in TILE.findall(launcher_panel(content))}


def current_tiles(content):
    """data-app-target of the highlighted (current app) tile(s)."""
    return [target for _href, cls, _key, target, _label in TILE.findall(launcher_panel(content)) if "is-current" in cls]


PICK = re.compile(r'<li class="labs-pick([^"]*)" data-key="(\w+)"')


def home_tiles(content, include_hidden=False):
    """The app keys of Home's (the app picker's) tiles, in page order - Favorites first. Hidden apps' tiles are
    in the page for Edit mode; they're left out unless include_hidden."""
    return [key for cls, key in PICK.findall(content) if include_hidden or "is-off" not in cls]


def favorite_tiles(content):
    """The app keys in Home's Favorites row."""
    if 'data-list="favorites"' not in content:
        return []
    return home_tiles(content.split('data-list="favorites"', 1)[1].split('data-list="others"', 1)[0])


def allow_api_keys(user, on=True):
    """Admin > Users "Allow API keys" for a test user (superusers always may). Returns the user."""
    from .models import UserPreference
    UserPreference.objects.update_or_create(user=user, defaults={"api_keys_allowed": on})
    return user


def with_apps(user, keys=None):
    """Give a test user every app (or just these keys) - new accounts start with none, so a test
    that signs in a plain user to use an app gives them access first, as Admin > Users would.
    Returns the user."""
    from . import apps_registry
    from .models import UserPreference
    keys = apps_registry.grantable_keys() if keys is None else list(keys)
    UserPreference.objects.update_or_create(user=user, defaults={"allowed_apps": keys})
    return user


def kitchen(user, *others, name="Test circle", apps=("meals", "money")):
    """A circle sharing Meals and Money (by default) with `user` as owner and `others` as members - everyone active
    and opted in - like a household that became a circle. Returns the circle's Space (where shared data lives).
    Called again for the same owner and name, it adds the others to that circle."""
    from django.utils import timezone

    from .models import Circle, CircleMembership, Space
    circle = Circle.objects.filter(name=name, memberships__user=user, memberships__role="owner").first()
    if circle is None:
        circle = Circle.objects.create(name=name, created_by=user, enabled_apps=list(apps))
        CircleMembership.objects.create(circle=circle, user=user, role="owner", status="active", joined_at=timezone.now(),
                                        apps=list(apps))
    for other in others:
        CircleMembership.objects.update_or_create(circle=circle, user=other, defaults={
            "status": "active", "role": "member", "joined_at": timezone.now(), "apps": list(apps)})
    space = Space.objects.get_or_create(circle=circle)[0]
    for person in (user,) + others:                              # the circle's space is what they look at
        from .circles import remember
        for app in apps:
            remember(person, app, space.pk)
    return space


def personal(user):
    """The user's Personal space."""
    from .circles import personal_space
    return personal_space(user)


def share_with(obj, user=None, permission="view", circle=None, by=None):
    """Share a Planner item / note / recipe / workout with a person (or a circle) straight in the database - the
    one Share system (albright_labs.sharing). Returns the ShareGrant."""
    from django.contrib.contenttypes.models import ContentType

    from .models import ShareGrant
    target = {"circle": circle} if circle is not None else {"user": user}
    owner = getattr(obj, "owner", None) or getattr(obj, "created_by", None)
    return ShareGrant.objects.update_or_create(content_type=ContentType.objects.get_for_model(type(obj)), object_id=obj.pk,
                                               **target, defaults={"permission": permission, "created_by": by or owner})[0]


def grants_of(obj):
    """[(user id or None, permission)] - who a thing is shared with (person grants first, by user id)."""
    from django.contrib.contenttypes.models import ContentType

    from .models import ShareGrant
    rows = ShareGrant.objects.filter(content_type=ContentType.objects.get_for_model(type(obj)), object_id=obj.pk)
    return sorted(((g.user_id, g.permission) for g in rows), key=lambda r: (r[0] is None, r[0] or 0))
