"""Where a signed-in user goes: after signing in (unless a "next" URL was given)
and on "/". "Open after sign-in" is per user (models.UserPreference):

  - App picker (Home) (default): the Labs hub
  - Last used app: from the last_app cookie (albright_trading_proj.middleware)
  - An app: its main page (landing="app", landing_page = the app key)
  - A specific page: one of each app's main nav pages (landing="page", apps_registry, App.pages)

Only apps the user can open and has kept in their navigation are offered or
followed - if the saved choice's app is hidden or no longer allowed, the picker.
Someone who can open just one app never sees the picker: they go straight to it.
"/?picker=1" (PICKER_PARAM) always shows the picker - the launcher's Home tile
and every "Home" link use it (home_url), so it's one click away whatever the
default.
"""
from django.urls import NoReverseMatch, reverse

from albright_trading_proj.middleware import LAST_APP_COOKIE, OPPORTUNITIES_COOKIE_VALUES

from . import app_access, apps_registry

PICKER_PARAM = "picker"
HOME, LAST_APP = "home", "last_app"


def home_url():
    """The app picker, even for someone with a default app."""
    return reverse("labs:root") + f"?{PICKER_PARAM}=1"


def _nav_keys(user, pref=None):
    from .models import UserPreference
    return app_access.nav_keys(user, pref or UserPreference.for_user(user))


def page_choices(user, pref=None):
    """[(app short label, [(key, label), ...]), ...] - the user's nav apps' pages, in their order."""
    return [(apps_registry.BY_KEY[key].short_label, [(page_key, label) for page_key, label, _url
                                                     in apps_registry.BY_KEY[key].pages])
            for key in _nav_keys(user, pref) if apps_registry.BY_KEY[key].pages]


def open_after_choices(user, pref=None):
    """The "Open after sign-in" dropdown: the picker, the last app, then each nav app (its main page, then its
    pages). Values: "home", "last_app", "app:<key>", "page:<page key>"."""
    choices = [(HOME, "App picker (Home)"), (LAST_APP, "Last used app")]
    for key in _nav_keys(user, pref):
        app = apps_registry.BY_KEY[key]
        choices.append((app.label, [(f"app:{key}", f"{app.label} (its main page)")]
                        + [(f"page:{page_key}", f"{app.label} › {label}") for page_key, label, _url in app.pages]))
    return choices


def open_after_value(pref):
    """The dropdown value for a saved preference."""
    if pref.landing in ("app", "page") and pref.landing_page:
        return f"{pref.landing}:{pref.landing_page}"
    return pref.landing if pref.landing == LAST_APP else HOME


def parse_open_after(user, value, pref=None):
    """(landing, landing_page) for a dropdown value, or ValueError if it isn't one of this user's choices."""
    if value in (HOME, LAST_APP):
        return value, ""
    kind, _, key = (value or "").partition(":")
    if kind == "app" and key in _nav_keys(user, pref):
        return "app", key
    if kind == "page" and allowed_page(user, key, pref):
        return "page", key
    raise ValueError("That choice isn't available.")


def allowed_page(user, key, pref=None):
    page = apps_registry.PAGES.get(key)
    return page is not None and page[0].key in _nav_keys(user, pref)


def _reverse(url_name):
    try:
        return reverse(url_name)
    except NoReverseMatch:
        return None


def landing_url(request):
    """The URL to send this signed-in user to, or None for the picker (the hub)."""
    pref = app_access.preference(request)
    nav = _nav_keys(request.user, pref)
    if pref.landing == "page" and allowed_page(request.user, pref.landing_page, pref):
        return _reverse(apps_registry.PAGES[pref.landing_page][2])
    if pref.landing == "app" and pref.landing_page in nav:
        return _reverse(apps_registry.BY_KEY[pref.landing_page].landing_url_name)
    if pref.landing == LAST_APP:
        remembered = request.COOKIES.get(LAST_APP_COOKIE)
        if remembered in OPPORTUNITIES_COOKIE_VALUES:
            remembered = "opportunities"   # the old "crypto" value
        app = apps_registry.BY_COOKIE.get(remembered)
        if app is None or app.key not in nav:
            return None
        return _reverse(app.landing_url_name)
    return None


def single_app_url(request):
    """For someone who can open exactly one app: where to send them instead of the picker (their saved page if
    it's in that app, else the app's main page). None for everyone else."""
    pref = app_access.preference(request)
    allowed = app_access.allowed_keys(request.user, pref)
    if len(allowed) != 1:
        return None
    target = landing_url(request)
    return target or _reverse(apps_registry.BY_KEY[allowed[0]].landing_url_name)
