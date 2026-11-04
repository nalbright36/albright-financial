"""Who may hold Bot API keys, and which scopes each person can use.

A token always acts as its owner (BotToken.owner): what it reads is what the owner can see, what it writes is the
owner's. On top of the token's own scopes, every request checks the owner right now (auth.bot_endpoint):
  - API keys allowed: superusers always; anyone else only with "Allow API keys" on (Admin > Users,
    UserPreference.api_keys_allowed). Turned off: their keys stop working.
  - The scope's app: schedule:* needs the Planner, meals:* Meals, notes:* Notes, fitness:* Fitness (APP_SCOPES). Everything else
    (trading, reseller, opportunities, ideas, site suggestions) is admin-only - superusers. So taking an app away
    from someone stops their key working for that app at once (403 naming the scope).
"""
from albright_labs import app_access, apps_registry

from .models import SCOPE_KEYS, SCOPES

# Scope -> the app (apps_registry key) its owner must be able to open.
APP_SCOPES = {"schedule:read": "planner", "schedule:write": "planner", "meals:read": "meals", "meals:write": "meals",
              "notes:read": "notes", "notes:write": "notes", "fitness:read": "fitness", "fitness:write": "fitness"}
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
}


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
    if not (user and user.is_active) or scope not in SCOPE_KEYS:
        return False
    if user.is_superuser:
        return True
    app = APP_SCOPES.get(scope)
    return app is not None and app_access.can_use(user, _pref(user), app)


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
