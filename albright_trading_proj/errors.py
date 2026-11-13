"""The site's branded error pages (400, 403, 404, 405, 429, 500 and the CSRF
failure page) and their JSON form for /api/ and requests that ask for JSON.

  - handler400/403/404/500 in the root urls.py point here; CSRF_FAILURE_VIEW
    too. ErrorPageMiddleware brands the empty-bodied 4xx responses Django and
    the views return themselves (require_POST's 405, a bare HttpResponseForbidden).
  - Pages are rendered WITHOUT the request, so no context processor runs, and
    every lookup (the user, app access, URL reversing) is wrapped: an error
    page must never raise its own error.
  - 500 depends on nothing that might be broken: no database, no session or
    user, no base template, inline CSS. It shows a reference code and logs the
    full error (and sends a Telegram alert, when configured) with that code.
  - A 404 never hints that the page exists: pages hidden from users without
    access (AppAccessMiddleware) 404 too, so the text is the same for both,
    and "Go to <App>" only shows for an app the user can open.
  - Nothing technical reaches the visitor: no exception text, stack trace,
    CSRF reason or query string.
"""
import logging
import os
import secrets
import sys
import time
from urllib.parse import urlencode

from django.conf import settings
from django.http import HttpResponse, JsonResponse
from django.template import loader
from django.urls import NoReverseMatch, Resolver404, resolve, reverse

log = logging.getLogger("albright.errors")

# status: (title, one sentence). The 404 text is deliberately the same whether
# the page doesn't exist or the visitor just can't see it.
PAGES = {
    400: ("That request didn't work", "Something in the link or form wasn't what we expected. Go back and try again."),
    403: ("You don't have access to this", "Your account doesn't have permission to open this page or do that."),
    404: ("We couldn't find that page", "The link may be mistyped or out of date."),
    405: ("That isn't allowed here", "This page can't be used that way. Go back and use the buttons on the page."),
    429: ("Too many requests", "Please wait a minute and try again."),
    500: ("Something went wrong on our end", "It's not you. We've logged the problem and will look into it."),
    "csrf": ("Your session expired", "Refresh the page and try again."),
}
JSON_ERRORS = {400: "bad request", 403: "forbidden", 404: "not found", 405: "method not allowed",
               429: "too many requests", 500: "server error", "csrf": "csrf failed"}
SIGN_IN_ON = {403, 404, "csrf"}
BRANDED_EMPTY = {400, 403, 404, 405, 429}       # ErrorPageMiddleware: empty bodies with these statuses

# One Telegram alert per process per ALERT_COOLDOWN seconds - in memory, so it
# works with the database down and an outage can't turn into a message storm.
ALERT_COOLDOWN = 300
ALERT_TIMEOUT = 4
_last_alert = {"at": 0.0, "suppressed": 0}

FALLBACK_500 = ("<!DOCTYPE html><html><head><meta charset=\"utf-8\"><meta name=\"viewport\" "
                "content=\"width=device-width, initial-scale=1\"><title>Server error</title></head>"
                "<body style=\"font-family: system-ui, sans-serif; text-align: center; padding: 48px 16px;\">"
                "<h1>500</h1><p>Something went wrong on our end. Please try again in a minute.</p>"
                "<p>Reference: <code>{reference}</code></p><p><a href=\"/\">Home</a></p></body></html>")


def new_reference():
    """A short code the visitor can quote and the log/alert carries: 8 hex characters."""
    return secrets.token_hex(4).upper()


def wants_json(request):
    """/api/ paths, and requests whose Accept asks for JSON rather than a page."""
    try:
        if (request.path or "").startswith("/api/"):
            return True
        accept = request.META.get("HTTP_ACCEPT", "")
        return "application/json" in accept and "text/html" not in accept
    except Exception:  # noqa: BLE001 - deciding the format must never fail
        return False


def _json(status, key=None, reference=None):
    body = {"error": JSON_ERRORS[key if key is not None else status], "status": status}
    if reference:
        body["reference"] = reference
    return JsonResponse(body, status=status)


def _static(path):
    """The static URL, or the plain STATIC_URL path if static storage itself is broken."""
    try:
        from django.templatetags.static import static
        return static(path)
    except Exception:  # noqa: BLE001
        prefix = getattr(settings, "STATIC_URL", "static/") or "static/"
        return ("/" + prefix.lstrip("/")).rstrip("/") + "/" + path


def _reverse(name, default=""):
    try:
        return reverse(name)
    except NoReverseMatch:
        return default


def _user(request):
    try:
        user = getattr(request, "user", None)
        if user is not None and user.is_authenticated and user.is_active:
            return user
    except Exception:  # noqa: BLE001 - a broken session/DB just means "signed out" here
        pass
    return None


def _app_for_path(request):
    """The registry app this URL belongs to: the resolved view's namespace, or, for a
    URL that doesn't resolve, the app whose landing page shares its first segment."""
    from albright_labs import apps_registry
    match = getattr(request, "resolver_match", None)
    if match is None:
        try:
            match = resolve(request.path_info)
        except Resolver404:
            match = None
    if match is not None:
        app = apps_registry.app_for_namespace(match.app_name)
        if app is not None and match.url_name not in app.ungated_url_names:
            return app
        if app is None:
            return None
    first = request.path_info.strip("/").split("/")[0]
    if not first:
        return None
    for app in apps_registry.APPS:
        landing = _reverse(app.landing_url_name)
        if landing and landing.strip("/").split("/")[0] == first:
            return app
    return None


def _app_button(request, user):
    """("Go to Meals", url) when the URL belongs to an app this user can open, else None."""
    if user is None:
        return None
    try:
        from albright_labs import app_access
        app = _app_for_path(request)
        if app is None or not app_access.can_use(user, app_access.preference(request), app.key):
            return None
        url = _reverse(app.landing_url_name)
        if not url or url == request.path:
            return None
        return {"label": f"Go to {app.label}", "url": url}
    except Exception:  # noqa: BLE001
        return None


def page_context(request, status, key=None, **extra):
    """Everything a 4xx page shows. Never raises."""
    key = key if key is not None else status
    title, sentence = PAGES[key]
    user = _user(request)
    home = _reverse("labs:hub", "/home/") if user else _reverse("labs:root", "/")
    sign_in = None
    if user is None and key in SIGN_IN_ON:
        login = _reverse("user_login")
        if login:
            try:
                sign_in = login + "?" + urlencode({"next": request.get_full_path()})
            except Exception:  # noqa: BLE001
                sign_in = login
    context = {"status": status, "title": title, "sentence": sentence, "home_url": home or "/",
               "sign_in_url": sign_in, "app_button": _app_button(request, user),
               "logo_on_dark": _static("icons/logo-mark-white.png"),
               "logo_on_light": _static("icons/logo-mark-dark.png")}
    context.update(extra)
    return context


def render_error(request, status, key=None, *, template=None, http_status=None, **extra):
    """The branded page (or JSON) for a 4xx error. http_status overrides the response's
    status (the Admin preview answers 200)."""
    key = key if key is not None else status
    if wants_json(request):
        return _json(status, key)
    template = template or f"errors/{'csrf_failure' if key == 'csrf' else status}.html"
    context = page_context(request, status, key, **extra)
    try:
        html = loader.get_template(template).render(context)   # no request: no context processors
    except Exception:  # noqa: BLE001
        log.exception("Error page %s failed to render", template)
        html = FALLBACK_500.replace("500", str(status)).format(reference="-")
    return HttpResponse(html, status=http_status or status)


def render_500(reference, http_status=500):
    """The standalone 500 page. Static URLs only - no request, user, DB or base template."""
    context = {"status": 500, "title": PAGES[500][0], "sentence": PAGES[500][1], "reference": reference,
               "logo_on_dark": _static("icons/logo-mark-white.png"),
               "logo_on_light": _static("icons/logo-mark-dark.png")}
    try:
        html = loader.get_template("errors/500.html").render(context)
    except Exception:  # noqa: BLE001 - the templates themselves may be the problem
        html = FALLBACK_500.format(reference=reference)
    return HttpResponse(html, status=http_status)


# ---------------------------------------------------------------------------
# Handlers (root urls.py) and the CSRF failure view (settings.CSRF_FAILURE_VIEW)
# ---------------------------------------------------------------------------

def bad_request(request, exception=None):
    return render_error(request, 400)


def permission_denied(request, exception=None):
    return render_error(request, 403)


def page_not_found(request, exception=None):
    return render_error(request, 404)


def csrf_failure(request, reason=""):
    # The reason ("CSRF token missing" etc.) is for the log, never the page.
    log.warning("CSRF failure on %s %s: %s", request.method, request.path, reason)
    return render_error(request, 403, "csrf")


def too_many_requests(request, minutes=None):
    """The 429 page for the rate limiters, with Retry-After."""
    extra = {}
    if minutes and minutes != 1:
        extra["sentence"] = f"Please wait {minutes} minutes and try again."
    response = render_error(request, 429, **extra)
    response["Retry-After"] = str((minutes or 1) * 60)
    return response


def server_error(request, *args, **kwargs):
    """handler500: log the exception with a reference code, alert, and show the code."""
    reference = new_reference()
    exc_info = sys.exc_info()
    try:
        log.error("Server error %s on %s %s", reference, request.method, request.path,
                  exc_info=exc_info if exc_info[0] is not None else None)
    except Exception:  # noqa: BLE001
        pass
    alert(reference, request, exc_info)
    if wants_json(request):
        return _json(500, reference=reference)
    return render_500(reference)


# ---------------------------------------------------------------------------
# The Telegram alert for a 500
# ---------------------------------------------------------------------------

def _in_tests():
    """Django's test runner swaps in the locmem mail backend and adds mail.outbox."""
    from django.core import mail
    return hasattr(mail, "outbox")


def alert(reference, request, exc_info):
    """A short Telegram message with the reference code (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID,
    the same bot as the scanner's critical alerts). Never raises; at most one per process
    every ALERT_COOLDOWN seconds; skipped in tests and with ERROR_TELEGRAM_ALERTS off."""
    try:
        if not getattr(settings, "ERROR_TELEGRAM_ALERTS", True) or _in_tests():
            return False
        token, chat_id = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
        if not token or not chat_id:
            return False
        now = time.monotonic()
        if _last_alert["at"] and now - _last_alert["at"] < ALERT_COOLDOWN:
            _last_alert["suppressed"] += 1
            return False
        return send_alert(alert_text(reference, request, exc_info), token, chat_id)
    except Exception:  # noqa: BLE001
        return False


def alert_text(reference, request, exc_info):
    """Reference, method, path (no query string) and exception class - details stay in the log."""
    import html
    kind = exc_info[0].__name__ if exc_info and exc_info[0] is not None else "error"
    path = (getattr(request, "path", "") or "")[:120]
    more = _last_alert["suppressed"]
    lines = [f"<b>CRITICAL · Site</b>", f"Server error <code>{reference}</code> ({html.escape(kind)})",
             f"{html.escape(getattr(request, 'method', '') or '')} {html.escape(path)}"]
    if more:
        lines.append(f"+{more} more since the last alert")
    return "\n".join(lines)


def send_alert(text, token, chat_id):
    import requests
    from albright_reselling_app.scanner.alerts import TELEGRAM_API
    _last_alert["at"], _last_alert["suppressed"] = time.monotonic(), 0
    try:
        requests.post(TELEGRAM_API.format(token=token),
                      json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
                      timeout=ALERT_TIMEOUT).raise_for_status()
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("Telegram error alert failed: %s", exc)
        return False


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------

class ErrorPageMiddleware:
    """Brands the 4xx responses that arrive with an empty body - require_POST/require_GET's
    405, a bare HttpResponseForbidden() / HttpResponseBadRequest(). Responses with
    content (a view's own JSON or page) are left alone. Keeps Allow and Retry-After."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if (response.status_code in BRANDED_EMPTY and not getattr(response, "streaming", False)
                and not response.content):
            branded = render_error(request, response.status_code)
            for header in ("Allow", "Retry-After", "Vary"):
                if response.has_header(header):
                    branded[header] = response[header]
            for name, cookie in response.cookies.items():
                branded.cookies[name] = cookie
            return branded
        return response
