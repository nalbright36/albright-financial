"""Who can open which app (access - superusers set it in Admin > Users) and
which apps show in a user's header and on Home, in what order (visibility -
each user's own choice, My account). Both come from UserPreference and the
app list in apps_registry.

  - Access: AppAccessMiddleware 404s every view of an app the user can't
    open (pages, AJAX and JSON alike), as if it didn't exist. Superusers can
    open everything. Anonymous visitors are left to each view (sign in first).
    Home, the public pages ("/") and the Bot API belong to no app,
    so they're never checked here.
  - Archived apps (albright_labs.archive, settings.ARCHIVED_APPS) are never
    in allowed_keys - not even a superuser's - so nothing lists or links them;
    their pages are a 404 "This app is archived" page for everyone but
    superusers, who still open them (with a banner). The stored allowed_apps
    entries stay, so restoring the app brings each person's access back.
  - Visibility only changes the header buttons and Home's tiles: a hidden app
    still opens by URL (with a "Hidden from your nav · Show it" note).
"""
from django.conf import settings
from django.http import Http404

from . import apps_registry, archive

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
    """Apps this user may open, in registry order - never an archived one (archive.py)."""
    if not (user and user.is_authenticated and user.is_active):
        return []
    if user.is_superuser:
        return archive.live_keys(apps_registry.all_keys())
    granted = set(pref.allowed_apps or [])
    return archive.live_keys([app.key for app in apps_registry.APPS if not app.superuser_only and app.key in granted])


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
    # An app the user has never arranged (not in their saved order - e.g. one added to
    # the registry after they last saved) shows until they choose otherwise.
    arranged = set(pref.app_order or [])
    visible = set(pref.visible_apps or []) | {key for key in ordered if key not in arranged}
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
    """Set a user's allowed apps (Admin > Users). A newly granted app shows in their nav. Archived apps aren't on
    the form, so the ones this person already had are kept - restoring the app brings their access back."""
    kept = [key for key in (pref.allowed_apps or []) if key in archive.archived_keys()]
    keys = [key for key in apps_registry.grantable_keys() if key in set(keys) | set(kept)]
    added = [key for key in keys if key not in (pref.allowed_apps or [])]
    pref.allowed_apps = keys
    pref.visible_apps = list(pref.visible_apps or []) + [k for k in added if k not in (pref.visible_apps or [])]
    pref.app_order = list(pref.app_order or []) + [k for k in added if k not in (pref.app_order or [])]
    return added


def new_user_preference(user, keys):
    """A new account's saved preference with exactly these apps (Admin > Users "Add a user", an
    invite's sign-up). Someone given one app with pages lands on its first page, not Home."""
    from .models import UserPreference
    pref = UserPreference(user=user)
    pref.allowed_apps = []
    grant(pref, keys)
    only = apps_registry.BY_KEY[pref.allowed_apps[0]] if len(pref.allowed_apps) == 1 else None
    if only and only.pages:
        pref.landing, pref.landing_page = "page", only.pages[0][0]
    pref.save()
    return pref


def app_for_request(request):
    """The registry app the resolved view belongs to, or None (Home, public pages, the Bot API): by its URL namespace,
    or - for views with none, like the trading app's - by the package its view is in (so the icons, service worker and
    password pages, which have no namespace either, never count as Trading). archive.app_for_view."""
    return archive.app_for_view(getattr(request, "resolver_match", None))


class AppAccessMiddleware:
    """404 for any view of an app the signed-in user can't open - before the view runs,
    so a redirect, a JSON answer or a POST never gives the app away."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)

    def process_view(self, request, view_func, view_args, view_kwargs):
        if request.path.startswith(settings.STATIC_URL) or request.path.startswith(settings.MEDIA_URL):
            return None
        user = getattr(request, "user", None)
        archived = archive.archived_app_for(request)
        if archived is not None:
            # Everyone - signed in or not - gets "This app is archived" (404); superusers still open it, with a banner.
            if user is not None and user.is_authenticated and user.is_active and user.is_superuser:
                request.archived_app = archived
                return None
            return archive.blocked_response(request, archived)
        if user is None or not user.is_authenticated:
            return None
        app = app_for_request(request)
        if app is not None and not can_use(user, preference(request), app.key):
            raise Http404
        return None

    def process_exception(self, request, exception):
        # A superuser on an archived app's page that tried to use its saved credentials (AlpacaCredentials raise
        # ArchivedAppError): say so instead of a server error. Nothing was decrypted or sent.
        if isinstance(exception, archive.ArchivedAppError):
            return archive.blocked_response(request, exception.app, status=200, needs_secrets=True)
        return None


def nav_item(app, current_accent):
    return {"key": app.key, "short_label": app.short_label, "label": app.label, "brand": app.brand,
            "accent": app.accent, "color": app.color, "url_name": app.landing_url_name,
            "current": app.accent == current_accent}


def app(key):
    return apps_registry.BY_KEY.get(key)
