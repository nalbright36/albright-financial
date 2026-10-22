"""Token authentication for the Bot API: "Authorization: Bearer <token>".

  - HTTPS only when BOT_API_REQUIRE_HTTPS is on (default: whenever DEBUG
    is off, i.e. production) -> 403
  - missing / wrong / revoked token -> 401
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

from .models import BotRequestLog, BotToken, hash_token

RATE_LIMIT = 60          # requests per minute per token
RATE_WINDOW = timedelta(minutes=1)


class ApiError(Exception):
    def __init__(self, status, message, summary=""):
        super().__init__(message)
        self.status, self.message, self.summary = status, message, summary or message


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


def bot_endpoint(scope=None, methods=("GET",)):
    """Wraps an API view: fn(request, token, **kwargs) -> (data, status, summary)."""
    def decorate(view):
        @csrf_exempt
        @functools.wraps(view)
        def wrapper(request, **kwargs):
            token = None
            try:
                if require_https() and not _is_https(request):
                    raise ApiError(403, "HTTPS is required.", "rejected: not HTTPS")
                raw = _bearer(request)
                found = BotToken.objects.select_related("owner").filter(token_hash=hash_token(raw)).first() \
                    if raw else None
                if found is None:
                    raise ApiError(401, "Missing, wrong or revoked token.", "rejected: missing or wrong token")
                if not found.is_active:
                    error = ApiError(401, "Missing, wrong or revoked token.", "rejected: revoked token")
                    error.token = found   # logged under the revoked token's name
                    raise error
                token = found
                if request.method not in methods:
                    raise ApiError(405, f"Method not allowed - use {' or '.join(methods)}.", f"rejected: {request.method}")
                if scope and scope not in token.scopes:
                    raise ApiError(403, f"This token doesn't have the {scope} scope.", f"rejected: needs {scope}")
                recent = BotRequestLog.objects.filter(token=token, created_at__gte=timezone.now() - RATE_WINDOW) \
                    .exclude(status=429).count()
                if recent >= RATE_LIMIT:
                    raise ApiError(429, f"Rate limit: {RATE_LIMIT} requests per minute per token.", "rate limited")
                data, status, summary = view(request, token, **kwargs)
            except ApiError as exc:
                log_token = getattr(exc, "token", token)
                _log(request, log_token, exc.status, exc.summary)
                response = JsonResponse({"error": exc.message}, status=exc.status)
                if exc.status == 401:
                    response["WWW-Authenticate"] = 'Bearer realm="albright-bot-api"'
                return response
            BotToken.objects.filter(pk=token.pk).update(last_used_at=timezone.now())
            _log(request, token, status, summary)
            if isinstance(data, str):
                return HttpResponse(data, status=status, content_type="text/plain; charset=utf-8")
            return JsonResponse(data, status=status, safe=False)
        return wrapper
    return decorate

