"""Remembers which app (the reselling app vs. the trading app) a logged-in
user last visited, in a long-lived cookie - read by albright_trading_app's
home view (the "/" root) to send a returning user straight back to
whichever app they were using, instead of always defaulting to the trading
app's own landing page.
"""
from django.conf import settings

LAST_APP_COOKIE = "last_app"
LAST_APP_COOKIE_MAX_AGE = 60 * 60 * 24 * 365  # 1 year

RESELLER_APP_NAME = "albright_reselling_app"

# Views that don't represent the user actually using one app or the other -
# never recorded, even though they're real 200 GET HTML responses.
_EXCLUDED_URL_NAMES = {"user_login", "user_logout"}


class LastAppMiddleware:
    """For every successful (200) GET HTML page request from a logged-in
    user, records "reseller" (albright_reselling_app URLs) or "trading"
    (everything else) in the last_app cookie. Skips static/media files,
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

        app = "reseller" if resolver_match.app_name == RESELLER_APP_NAME else "trading"
        response.set_cookie(
            LAST_APP_COOKIE, app, max_age=LAST_APP_COOKIE_MAX_AGE, httponly=True, samesite="Lax",
        )
