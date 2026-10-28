"""Who can open which app (access - superusers set it in Admin > Users) and
which apps show in a user's header and on Home, in what order (visibility -
each user's own choice, My account). Both come from UserPreference and the
app list in apps_registry.

  - Access: AppAccessMiddleware 404s every view of an app the user can't
    open (pages, AJAX and JSON alike), as if it didn't exist. Superusers can
    open everything. Anonymous visitors are left to each view (sign in first).
    Home, the public pages ("/", /resume/) and the Bot API belong to no app,
    so they're never checked here.
  - Visibility only changes the header buttons and Home's tiles: a hidden app
    still opens by URL (with a "Hidden from your nav · Show it" note).
"""
from django.conf import settings
from django.http import Http404

from . import apps_registry

_CACHE_ATTR = "_labs_preference"


def preference(request):
    """The user's UserPreference (or an unsaved default), read once per request."""
    from .models import UserPreference
    pref = getattr(request, _CACHE_ATTR, None)
    if pref is None:
        pref = UserPreference.for_user(request.user)
        setattr(request, _CACHE_ATTR, pref)
    return pref


def forget(request):
    """Drop the per-request copy after saving a change."""
    if hasattr(request, _CACHE_ATTR):
        delattr(request, _CACHE_ATTR)


def allowed_keys(user, pref):
    """Apps this user may open, in registry order."""
    if not (user and user.is_authenticated and user.is_active):
        return []
    if user.is_superuser:
        return apps_registry.all_keys()
    granted = set(pref.allowed_apps or [])
    return [app.key for app in apps_registry.APPS if not app.superuser_only and app.key in granted]


def can_use(user, pref, key):
    return key in allowed_keys(user, pref)


def ordered_keys(user, pref):
    """The apps this user may open, in their chosen order (any not in it yet - say a
    newly granted app - go at the end, in registry order)."""
    allowed = allowed_keys(user, pref)
    order = [key for key in (pref.app_order or []) if key in allowed]
    seen = set(order)
    return order + [key for key in allowed if key not in seen]


def nav_keys(user, pref):
    """The apps in this user's header and Home: allowed and shown, in their order. At
    least one always shows while they can open any (if access to every shown app was
    removed, the first one they still have)."""
    ordered = ordered_keys(user, pref)
    visible = set(pref.visible_apps or [])
    shown = [key for key in ordered if key in visible]
    return shown or ordered[:1]


def clean_nav(user, pref, visible, order):
    """Validate a "Show in my navigation" form: (visible, order) as saved, or ValueError."""
    allowed = ordered_keys(user, pref)
    visible = [key for key in visible if key in allowed]
    if allowed and not visible:
        raise ValueError("Keep at least one app in your navigation.")
    order = list(dict.fromkeys(key for key in order if key in allowed))
    # Apps the user can't open right now keep their place at the end, ready if access comes back.
    rest = [key for key in (pref.app_order or []) if key not in order]
    hidden_elsewhere = [key for key in (pref.visible_apps or []) if key not in allowed]
    return visible + hidden_elsewhere, order + rest


def grant(pref, keys):
    """Set a user's allowed apps (Admin > Users). A newly granted app shows in their nav."""
    keys = [key for key in apps_registry.grantable_keys() if key in set(keys)]
    added = [key for key in keys if key not in (pref.allowed_apps or [])]
    pref.allowed_apps = keys
    pref.visible_apps = list(pref.visible_apps or []) + [k for k in added if k not in (pref.visible_apps or [])]
    pref.app_order = list(pref.app_order or []) + [k for k in added if k not in (pref.app_order or [])]
    return added


def app_for_request(request):
    """The registry app the resolved view belongs to, or None (Home, public pages, the Bot API)."""
    match = getattr(request, "resolver_match", None)
    if match is None:
        return None
    app = apps_registry.app_for_namespace(match.app_name)
    if app is None or match.url_name in app.ungated_url_names:
        return None
    return app


class AppAccessMiddleware:
    """404 for any view of an app the signed-in user can't open - before the view runs,
    so a redirect, a JSON answer or a POST never gives the app away."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)

    def process_view(self, request, view_func, view_args, view_kwargs):
        user = getattr(request, "user", None)
        if user is None or not user.is_authenticated:
            return None
        if request.path.startswith(settings.STATIC_URL) or request.path.startswith(settings.MEDIA_URL):
            return None
        app = app_for_request(request)
        if app is not None and not can_use(user, preference(request), app.key):
            raise Http404
        return None


def nav_item(app, current_accent):
    return {"key": app.key, "short_label": app.short_label, "label": app.label, "brand": app.brand,
            "accent": app.accent, "color": app.color, "url_name": app.landing_url_name,
            "current": app.accent == current_accent}


def app(key):
    return apps_registry.BY_KEY.get(key)
