"""Who may hold Bot API keys, and which scopes each person can use.

A token always acts as its owner (BotToken.owner): what it reads is what the owner can see, what it writes is the
owner's. On top of the token's own scopes, every request checks the owner right now (auth.bot_endpoint):
  - API keys allowed: superusers always; anyone else only with "Allow API keys" on (Admin > Users,
    UserPreference.api_keys_allowed). Turned off: their keys stop working.
  - The scope's app: schedule:* needs the Planner, meals:* Meals, notes:* Notes, fitness:* Fitness (APP_SCOPES);
    activity:read works for anyone allowed keys (OPEN_SCOPES - it only shows what they can see). Everything else
    (trading, reseller, opportunities, ideas, site suggestions) is admin-only - superusers. So taking an app away
    from someone stops their key working for that app at once (403 naming the scope).
  - An archived app (albright_labs.archive): its scopes (ARCHIVABLE) are allowed to no one - not offered on the key
    forms, left out of the docs, OpenAPI and MCP tools - and its endpoints answer 404 (auth.bot_endpoint). Keys keep
    the scopes they hold, so they work again when the app is restored.
"""
from albright_labs import app_access, apps_registry, archive

from .models import SCOPE_KEYS, SCOPES

# Scope -> the app (apps_registry key) its owner must be able to open.
APP_SCOPES = {"schedule:read": "planner", "schedule:write": "planner", "meals:read": "meals", "meals:write": "meals",
              "notes:read": "notes", "notes:write": "notes", "fitness:read": "fitness", "fitness:write": "fitness",
              "fitness:food:read": "fitness", "fitness:food:write": "fitness"}
# What each app scope does, in plain words - "My API keys" (non-admins).
PLAIN = {
    "schedule:read": "See my Planner - my items and ones shared with me",
    "schedule:write": "Add and check off tasks in my Planner",
    "meals:read": "See the meal plan, grocery list, meal ideas and recipes",
    "meals:write": "Add to the meal plan, grocery list, meal ideas and recipes",
    "notes:read": "List my notes",
    "notes:write": "Save notes for me, and add to notes I can edit",
    "fitness:read": "See my Fitness day and weekly summary (workouts, food vs targets)",
    "fitness:write": "Log workouts, food and weigh-ins for me",
    "fitness:food:read": "Read your food log",
    "fitness:food:write": "Add and edit food entries",
    "activity:read": "See my notifications - what others changed in things shared with me and in my circles",
}
# Scopes anyone allowed API keys can use (not tied to one app).
OPEN_SCOPES = {"activity:read"}
# The admin-only scopes that belong to an app that can be archived (albright_labs.archive).
ADMIN_APP_SCOPES = {"trading:notes": "trade", "reseller:read": "resell", "reseller:suggest": "resell",
                    "opportunities:read": "opps", "opportunities:suggest": "opps", "opportunities:write": "opps"}


def scope_app(scope):
    """The app (registry key) a scope belongs to, or None (notifications, ideas, site suggestions)."""
    return APP_SCOPES.get(scope) or ADMIN_APP_SCOPES.get(scope)


def is_archived(scope):
    """Whether this scope's app is archived - then nobody can use it, and it's never shown."""
    app = scope_app(scope)
    return app is not None and archive.is_app_archived(app)


def _pref(user):
    from albright_labs.models import UserPreference
    return UserPreference.for_user(user)


def keys_allowed(user):
    """Whether this person may have (and use) API keys."""
    if not (user and user.is_authenticated and user.is_active):
        return False
    return user.is_superuser or bool(_pref(user).api_keys_allowed)


def allowed(user, scope):
    """Whether this person can use this scope now: admin-only scopes need a superuser, app scopes the app."""
    if not (user and user.is_active) or scope not in SCOPE_KEYS or is_archived(scope):
        return False
    if user.is_superuser:
        return True
    if scope in OPEN_SCOPES:
        return True
    app = APP_SCOPES.get(scope)
    return app is not None and app_access.can_use(user, _pref(user), app)


GROUP_ORDER = ["planner", "meals", "notes", "fitness"]


def grouped_choices(user):
    """[(group label, [(scope, description)])] - the key forms group them by app ("Fitness", ...); the rest last."""
    groups = {}
    for key, label in choices(user):
        app = APP_SCOPES.get(key)
        name = apps_registry.BY_KEY[app].label if app in apps_registry.BY_KEY else ("Notifications" if key in OPEN_SCOPES else "Admin")
        groups.setdefault((GROUP_ORDER.index(app) if app in GROUP_ORDER else 99, name), []).append((key, label))
    return [(name, rows) for (_order, name), rows in sorted(groups.items(), key=lambda kv: kv[0])]


def available(user):
    """The scopes this person can grant their keys, in SCOPES order."""
    return [key for key in SCOPE_KEYS if allowed(user, key)]


def choices(user):
    """[(scope, description)] for this person's key forms - plain words for app scopes."""
    labels = dict(SCOPES)
    return [(key, PLAIN.get(key, labels[key])) for key in available(user)]


def denial(user, scope):
    """Why this person can't use a scope now - for the 403."""
    app = APP_SCOPES.get(scope)
    if app is None:
        return f"{scope} is for site admins only."
    return f"Your account doesn't have access to {apps_registry.BY_KEY[app].label}, so {scope} doesn't work."


def usable(token, scope):
    """The token has this scope AND its owner can use it now."""
    return scope in token.scopes and allowed(token.owner, scope)


# --- The key form's three choices (My account > API keys) and how a key's access reads in its list. ---
FULL, READ_ONLY, CUSTOM = "full", "read", "custom"
ACCESS_CHOICES = [
    (FULL, "Full access", "Read and change everything in the apps you can open"),
    (READ_ONLY, "Read only", "See everything, change nothing"),
    (CUSTOM, "Custom", "Pick exactly what it may do, app by app"),
]


def is_admin_only(scope):
    """Admin-only scopes (trading, reseller, opportunities, ideas, site suggestions) - not tied to an app anyone
    else can open. Never part of Full access or Read only, even for an admin: tick them under Custom."""
    return scope not in APP_SCOPES and scope not in OPEN_SCOPES


def is_read(scope):
    return scope.endswith(":read")


def preset(user, access):
    """The scopes Full access / Read only give this person: their apps' scopes (and notifications), never admin-only
    ones. None for Custom (the person picks)."""
    if access not in (FULL, READ_ONLY):
        return None
    return [key for key in available(user) if not is_admin_only(key) and (access == FULL or is_read(key))]


def access_of(user, scope_list):
    """Which of the three choices a key's scopes match now - for ticking the right one in Edit access."""
    held = set(scope_list)
    for access in (FULL, READ_ONLY):
        if held and held == set(preset(user, access)):
            return access
    return CUSTOM


def summary(user, scope_list):
    """One line for the key list: "Full access", "Read only", or "Planner (read and change), Meals (read)". An
    archived app's scopes (kept on the key) aren't mentioned."""
    scope_list = [key for key in scope_list if not is_archived(key)]
    access = access_of(user, scope_list)
    if access == FULL:
        return "Full access"
    if access == READ_ONLY:
        return "Read only"
    return ", ".join(f"{name} ({kind})" for name, kind in by_app(scope_list)) or "Nothing"


def by_app(scope_list):
    """[(app label, "read" | "change" | "read and change")] - for the key list and GET me."""
    kinds_by_app = {}
    for key in scope_list:
        if is_archived(key):
            continue
        app = APP_SCOPES.get(key)
        name = apps_registry.BY_KEY[app].label if app in apps_registry.BY_KEY else (
            "Notifications" if key in OPEN_SCOPES else "Admin")
        kinds_by_app.setdefault(name, set()).add("read" if is_read(key) else "change")
    return [(name, "read and change" if kinds == {"read", "change"} else kinds.pop())
            for name, kinds in kinds_by_app.items()]
