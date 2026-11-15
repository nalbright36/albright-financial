"""What js/app_nav.js may keep as an app's "last page" (the launcher's and Home's tiles resume each app there).

A saved page counts only when it's on this site, belongs to that app, isn't a shared page, and the user can still
open the app. The script checks every entry with these rules when it saves one, when a tile is about to use one, and
on every page load (anything that fails is deleted), so a stale entry - say /money/household/, which once was a Money
page and now redirects to Circles - can never send a tile to the wrong place again.

rules(user, pref) is what the page carries (index.html, json_script "nav-resume-rules"); belongs() is the same check
in Python - the script's valid() mirrors it line for line.
"""
from functools import lru_cache

from django.urls import URLResolver, get_resolver, reverse

from . import app_access, apps_registry

# Shared pages and areas that are never any app's last page: Home and My account (/home/... - account, Circles,
# People, Activity, Notifications), the notification panel, circle invites, share links, sign-up, sign in/out and
# password reset, the offline page, Django's admin, the Bot API, the public resume, static files, and the old
# Opportunities prefixes (they only redirect).
SHARED_PREFIXES = ("/home/", "/notifications/", "/circles/", "/share/", "/join/", "/user_login/", "/user_logout/",
                   "/reset_password/", "/reset/", "/offline/", "/admin/", "/bot-api/", "/api/", "/resume/",
                   "/static/", "/crypto/", "/albright_crypto_app/")

# Old pages inside an app that now only redirect somewhere else - never kept, and dropped if already saved.
RETIRED_URL_NAMES = ("albright_money:household",)


@lru_cache(maxsize=1)
def app_prefixes():
    """{app key: ["/money/", ...]} - where each app's URL namespaces are mounted (the root URLconf). An app mounted
    at the site root (Trading, "") has none: its pages are whatever no other app or shared area claims."""
    prefixes = {}
    for pattern in get_resolver().url_patterns:
        if not isinstance(pattern, URLResolver):
            continue
        app = apps_registry.app_for_namespace(pattern.app_name or pattern.namespace)
        route = str(pattern.pattern)
        if app and route and not route.startswith("^"):
            prefixes.setdefault(app.key, [])
            path = "/" + route
            if path not in prefixes[app.key]:
                prefixes[app.key].append(path)
    return prefixes


def retired_paths():
    return [reverse(name) for name in RETIRED_URL_NAMES]


def rules(user, pref):
    """For the page: each app this user can open ({main, prefixes}), plus the shared prefixes and retired paths."""
    prefixes = app_prefixes()
    apps = {}
    for key in app_access.allowed_keys(user, pref):
        app = apps_registry.BY_KEY[key]
        apps[key] = {"main": reverse(app.landing_url_name), "prefixes": prefixes.get(key, [])}
    claimed = [p for ps in prefixes.values() for p in ps]          # every app's, opened by this user or not
    return {"apps": apps, "claimed": claimed, "shared": list(SHARED_PREFIXES), "retired": retired_paths()}


def belongs(url, key, page_rules):
    """True when url may be kept/used as app `key`'s last page under these rules (js/app_nav.js: valid())."""
    if not isinstance(url, str) or not url.startswith("/") or url.startswith("//") or url.startswith("/\\"):
        return False
    app = page_rules["apps"].get(key)
    if app is None:                                  # Home, an unknown key, or an app the user can no longer open
        return False
    path = url.split("#", 1)[0].split("?", 1)[0]
    if path == "/" or path in page_rules["retired"]:
        return False
    if any(path.startswith(prefix) for prefix in page_rules["shared"]):
        return False
    if app["prefixes"]:
        return any(path.startswith(prefix) for prefix in app["prefixes"])
    return not any(path.startswith(prefix) for prefix in page_rules["claimed"])   # Trading: no one else's page
