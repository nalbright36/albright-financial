"""Remembers which app (the reselling app, the Opportunities app, the Admin
area, or the trading app) a logged-in user last visited, in a long-lived
cookie - used only by the "Last used app" landing setting
(albright_labs.landing), which sends a returning user straight back there.
"""
from django.conf import settings

from albright_labs import apps_registry, archive

LAST_APP_COOKIE = "last_app"
LAST_APP_COOKIE_MAX_AGE = 60 * 60 * 24 * 365  # 1 year

# Not an app at all (the Labs hub) - never recorded.
_EXCLUDED_APP_NAMES = {"labs"}
# Cookie values that mean the Opportunities app - "crypto" is what it was
# called before the rename, still in returning users' browsers.
OPPORTUNITIES_COOKIE_VALUES = ("opportunities", "crypto")

# Views that don't represent the user actually using one app or the other -
# never recorded, even though they're real 200 GET HTML responses.
_EXCLUDED_URL_NAMES = {"user_login", "user_logout"}


class LastAppMiddleware:
    """For every successful (200) GET HTML page request from a logged-in
    user, records the app it belongs to (by URL namespace, or the view's
    package for the trading app) in the last_app cookie - never for Home,
    account pages or anything that isn't an app. Skips static/media files,
    non-HTML responses (API/AJAX - these never represent navigating to a
    page), POSTs, /admin/, and the login/logout views themselves."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        self._maybe_record(request, response)
        return response

    def _maybe_record(self, request, response):
        if request.method != "GET" or response.status_code != 200:
            return
        user = getattr(request, "user", None)
        if user is None or not user.is_authenticated:
            return
        if request.path.startswith(settings.STATIC_URL) or request.path.startswith(settings.MEDIA_URL):
            return
        if request.path.startswith("/admin/"):
            return
        if not response.get("Content-Type", "").startswith("text/html"):
            return  # API/AJAX responses (JSON, etc.) - not a page visit

        resolver_match = request.resolver_match
        if resolver_match is None or resolver_match.url_name in _EXCLUDED_URL_NAMES:
            return
        if resolver_match.app_name in _EXCLUDED_APP_NAMES:
            return

        if apps_registry.is_account_page(resolver_match):
            return   # your account isn't an app
        found = archive.app_for_view(resolver_match)      # by namespace, or the view's package (trading has no namespace)
        if found is None or archive.is_app_archived(found.key):
            return   # no app (Home, the Bot API pages...), or an archived one (a superuser looking at old data)
        app = found.last_app_cookie
        response.set_cookie(
            LAST_APP_COOKIE, app, max_age=LAST_APP_COOKIE_MAX_AGE, httponly=True, samesite="Lax",
        )
