"""Where a signed-in user lands: after signing in (unless a "next" URL was
given) and on "/". The choice is per user (models.UserPreference):

  - Home (default): the Labs hub
  - Last used app: from the last_app cookie (albright_trading_proj.middleware)
  - A specific page: one of each app's main nav pages (apps_registry, App.pages)

Only apps the user can open and has kept in their navigation are offered or
followed - if the saved choice's app is hidden or no longer allowed, Home.
"""
from django.urls import NoReverseMatch, reverse

from albright_trading_proj.middleware import LAST_APP_COOKIE, OPPORTUNITIES_COOKIE_VALUES

from . import app_access, apps_registry


def _nav_keys(user, pref=None):
    from .models import UserPreference
    return app_access.nav_keys(user, pref or UserPreference.for_user(user))


def page_choices(user, pref=None):
    """[(app short label, [(key, label), ...]), ...] for the dropdown - the user's nav apps, in their order."""
    return [(apps_registry.BY_KEY[key].short_label, [(page_key, label) for page_key, label, _url
                                                     in apps_registry.BY_KEY[key].pages])
            for key in _nav_keys(user, pref) if apps_registry.BY_KEY[key].pages]


def allowed_page(user, key, pref=None):
    page = apps_registry.PAGES.get(key)
    return page is not None and page[0].key in _nav_keys(user, pref)


def _reverse(url_name):
    try:
        return reverse(url_name)
    except NoReverseMatch:
        return None


def landing_url(request):
    """The URL to send this signed-in user to, or None for Home (the hub)."""
    pref = app_access.preference(request)
    if pref.landing == "page" and allowed_page(request.user, pref.landing_page, pref):
        return _reverse(apps_registry.PAGES[pref.landing_page][2])
    if pref.landing == "last_app":
        remembered = request.COOKIES.get(LAST_APP_COOKIE)
        if remembered in OPPORTUNITIES_COOKIE_VALUES:
            remembered = "opportunities"   # the old "crypto" value
        app = apps_registry.BY_COOKIE.get(remembered)
        if app is None or app.key not in _nav_keys(request.user, pref):
            return None
        return _reverse(app.landing_url_name)
    return None
