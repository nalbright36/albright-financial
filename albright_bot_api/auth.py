"""Token authentication for the Bot API: "Authorization: Bearer <token>".

  - HTTPS only when BOT_API_REQUIRE_HTTPS is on (default: whenever DEBUG
    is off, i.e. production) -> 403
  - missing / wrong / revoked / expired token -> 401
  - the owner's "Can create API keys" turned off (Admin > Users) -> 403 "API access disabled" (the keys are kept and
    work again when it's turned back on)
  - token without the endpoint's scope -> 403
  - more than RATE_LIMIT requests in the last minute for one token -> 429
Every request - rejected ones too - is written to BotRequestLog. Only the
token authenticates: a logged-in browser session gets no access, which is
why the API views can safely be CSRF-exempt.
"""
import functools
import json
from datetime import timedelta

from django.conf import settings
from django.http import HttpResponse, JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

from albright_labs import activity

from . import scopes
from .models import ASSISTANT, BotRequestLog, BotToken, hash_token

RATE_LIMIT = 60          # requests per minute per token
RATE_WINDOW = timedelta(minutes=1)


class ApiError(Exception):
    def __init__(self, status, message, summary="", extra=None):
        super().__init__(message)
        self.status, self.message, self.summary = status, message, summary or message
        self.extra = extra or {}     # more keys for the JSON error body


def require_https():
    return getattr(settings, "BOT_API_REQUIRE_HTTPS", not settings.DEBUG)


def _is_https(request):
    # Behind the host's proxy the original scheme arrives in X-Forwarded-Proto.
    return request.is_secure() or request.META.get("HTTP_X_FORWARDED_PROTO", "").lower() == "https"


def _bearer(request):
    header = request.META.get("HTTP_AUTHORIZATION", "")
    kind, _, value = header.partition(" ")
    return value.strip() if kind.lower() == "bearer" and value.strip() else None


def json_body(request):
    try:
        data = json.loads(request.body or b"{}")
    except (ValueError, UnicodeDecodeError) as exc:
        raise ApiError(400, "Body must be a JSON object.", "bad JSON") from exc
    if not isinstance(data, dict):
        raise ApiError(400, "Body must be a JSON object.", "bad JSON")
    return data


def _log(request, token, status, summary):
    BotRequestLog.objects.create(token=token, token_name=token.name if token else "(no valid token)",
                                 method=request.method, endpoint=request.path[:200], status=status,
                                 summary=str(summary)[:300])


def authenticate(request):
    """The request's live token, or ApiError: not HTTPS (403), missing / wrong / revoked / expired (401 - the error
    carries .token when there was one, so it's logged under its name), API access turned off for its owner (403).

    The in-site AI assistant (albright_assistant) calls the API views in-process with its own token on the request
    object (request.assistant_token - a Python attribute, nothing a client can send); it's checked against the
    assistant's permission instead of "Can create API keys". A Bearer value never authenticates as that token."""
    inproc = getattr(request, "assistant_token", None)
    if inproc is not None:
        from albright_assistant import access
        if not inproc.is_active or inproc.kind != ASSISTANT:
            raise ApiError(401, "The assistant's access was revoked.", "rejected: assistant token revoked")
        if not access.allowed(inproc.owner):
            error = ApiError(403, "The AI assistant is turned off for this account.", "rejected: assistant not allowed")
            error.token = inproc
            raise error
        return inproc
    if require_https() and not _is_https(request):
        raise ApiError(403, "HTTPS is required.", "rejected: not HTTPS")
    raw = _bearer(request)
    found = BotToken.objects.select_related("owner").filter(token_hash=hash_token(raw)).first() if raw else None
    if found is None:
        raise ApiError(401, "Missing, wrong or revoked token.", "rejected: missing or wrong token")
    if not found.is_active or found.is_expired:
        expired = found.is_active
        error = ApiError(401, "This key has expired." if expired else "Missing, wrong or revoked token.",
                         "rejected: expired token" if expired else "rejected: revoked token")
        error.token = found
        raise error
    if not scopes.keys_allowed(found.owner):
        error = ApiError(403, "API access disabled", "rejected: API access disabled for the owner")
        error.token = found
        raise error
    return found


def error_response(request, exc, token=None):
    """Logs an ApiError and answers it as JSON (401s say how to authenticate)."""
    _log(request, getattr(exc, "token", token), exc.status, exc.summary)
    response = JsonResponse({"error": exc.message, **exc.extra}, status=exc.status)
    if exc.status == 401:
        response["WWW-Authenticate"] = 'Bearer realm="albright-bot-api"'
    return response


def bot_endpoint(scope=None, methods=("GET",)):
    """Wraps an API view: fn(request, token, **kwargs) -> (data, status, summary). scope: one scope, or a tuple of
    scopes any one of which will do (the 403 names the first)."""
    wanted = tuple(scope) if isinstance(scope, (tuple, list)) else ((scope,) if scope else ())
    def decorate(view):
        @csrf_exempt
        @functools.wraps(view)
        def wrapper(request, **kwargs):
            token = None
            try:
                if wanted and all(scopes.is_archived(s) for s in wanted):
                    # An archived app's endpoint (albright_labs.archive): gone, as if it never existed.
                    raise ApiError(404, "Not found.", "rejected: app archived")
                token = authenticate(request)
                if request.method not in methods:
                    raise ApiError(405, f"Method not allowed - use {' or '.join(methods)}.", f"rejected: {request.method}")
                held = [s for s in wanted if s in token.scopes]
                if wanted and not held:
                    raise ApiError(403, f"This token doesn't have the {wanted[0]} scope.", f"rejected: needs {wanted[0]}",
                                   extra={"required": wanted[0]})
                if wanted and not any(scopes.allowed(token.owner, s) for s in held):
                    # The owner lost the app (or isn't an admin): the key stops working for it at once.
                    raise ApiError(403, scopes.denial(token.owner, held[0]), f"rejected: owner can't use {held[0]}",
                                   extra={"required": held[0]})
                recent = BotRequestLog.objects.filter(token=token, created_at__gte=timezone.now() - RATE_WINDOW) \
                    .exclude(status=429).count()
                if recent >= RATE_LIMIT:
                    raise ApiError(429, f"Rate limit: {RATE_LIMIT} requests per minute per token.", "rate limited")
                with activity.acting(token.owner, token.name):    # what it changes shows "via <bot name>"
                    data, status, summary = view(request, token, **kwargs)
            except ApiError as exc:
                return error_response(request, exc, token)
            BotToken.objects.filter(pk=token.pk).update(last_used_at=timezone.now())
            _log(request, token, status, summary)
            if isinstance(data, str):
                return HttpResponse(data, status=status, content_type="text/plain; charset=utf-8")
            return JsonResponse(data, status=status, safe=False)
        return wrapper
    return decorate

