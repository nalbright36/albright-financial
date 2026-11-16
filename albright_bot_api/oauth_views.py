"""The smallest OAuth 2.1 flow MCP connectors need (Claude's custom connectors require OAuth for a person adding one;
ChatGPT, Grok and Gemini use the same flow). An approved connection is just a normal API key (BotToken) made behind
the scenes - it shows in My account > API keys, can be revoked there, and the "Can create API keys" switch covers it.

  GET  /.well-known/oauth-protected-resource[/api/bot/v1/mcp]  which authorization server protects the MCP endpoint
  GET  /.well-known/oauth-authorization-server                  where to register, authorize and get a token
  POST /oauth/register     Dynamic Client Registration (RFC 7591): public clients, PKCE, redirect URIs https (or
                           http on localhost / 127.0.0.1, any port, for desktop apps like Claude Code)
  GET  /account/api-keys/authorize/   the approval page (signed in): pick Full access or Read only, approve or cancel
  POST /oauth/token        authorization_code + PKCE (S256 only) -> {"access_token": <a new API key>}

No refresh tokens: the key lasts until it's revoked. A code works once and for 10 minutes; a code used twice
revokes the key it made.
"""
import base64
import hashlib
import json
import secrets
from datetime import timedelta
from urllib.parse import urlencode, urlsplit

from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

from . import connect, scopes
from .models import BotToken, OAuthClient, OAuthCode, hash_token

CODE_MINUTES = 10


def _metadata(data):
    response = JsonResponse(data)
    response["Access-Control-Allow-Origin"] = "*"
    response["Cache-Control"] = "public, max-age=3600"
    return response


def protected_resource(request):
    origin = connect.site_origin(request)
    links = connect.urls(request)
    return _metadata({"resource": links["mcp"], "authorization_servers": [origin], "bearer_methods_supported": ["header"],
                      "resource_name": "Albright Labs", "resource_documentation": links["docs"]})


def authorization_server(request):
    from django.urls import reverse
    origin = connect.site_origin(request)
    return _metadata({
        "issuer": origin,
        "authorization_endpoint": origin + reverse("labs:api_key_authorize"),
        "token_endpoint": origin + reverse("bot_oauth:token"),
        "registration_endpoint": origin + reverse("bot_oauth:register"),
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
        "service_documentation": connect.urls(request)["docs"],
    })


def _oauth_error(error, description, status=400):
    response = JsonResponse({"error": error, "error_description": description}, status=status)
    response["Cache-Control"] = "no-store"
    return response


def _redirect_ok(uri):
    try:
        parts = urlsplit(uri)
    except ValueError:
        return False
    if parts.fragment or not parts.netloc:
        return False
    return parts.scheme == "https" or (parts.scheme == "http" and parts.hostname in ("localhost", "127.0.0.1", "::1"))


def _matches(registered, given):
    """Exact match - or, for a loopback redirect, the same everything but the port (RFC 8252: desktop apps pick one)."""
    if given in registered:
        return True
    try:
        g = urlsplit(given)
    except ValueError:
        return False
    if g.scheme != "http" or g.hostname not in ("localhost", "127.0.0.1", "::1"):
        return False
    for uri in registered:
        r = urlsplit(uri)
        if (r.scheme, r.hostname, r.path, r.query) == (g.scheme, g.hostname, g.path, g.query):
            return True
    return False


@csrf_exempt
def register(request):
    from albright_labs import ratelimit
    if request.method != "POST":
        return _oauth_error("invalid_request", "Use POST.", 405)
    if ratelimit.blocked(request, "oauth"):
        return _oauth_error("invalid_request", "Too many registrations - try again later.", 429)
    ratelimit.hit(request, "oauth")
    try:
        data = json.loads(request.body or b"{}")
    except (ValueError, UnicodeDecodeError):
        return _oauth_error("invalid_client_metadata", "The body must be JSON.")
    uris = data.get("redirect_uris")
    if not isinstance(uris, list) or not 1 <= len(uris) <= 10 or not all(isinstance(u, str) and len(u) <= 500 and
                                                                          _redirect_ok(u) for u in uris):
        return _oauth_error("invalid_redirect_uri", "redirect_uris: 1-10 https URLs (or http on localhost).")
    method = data.get("token_endpoint_auth_method", "none")
    if method != "none":
        return _oauth_error("invalid_client_metadata", 'Only public clients: token_endpoint_auth_method "none".')
    name = str(data.get("client_name") or "An AI app").strip()[:100] or "An AI app"
    OAuthClient.objects.filter(created_at__lt=timezone.now() - timedelta(days=90), codes__isnull=True).delete()
    client = OAuthClient.objects.create(client_id=secrets.token_urlsafe(24), name=name, redirect_uris=uris)
    response = JsonResponse({"client_id": client.client_id, "client_id_issued_at": int(client.created_at.timestamp()),
                             "client_name": client.name, "redirect_uris": uris, "grant_types": ["authorization_code"],
                             "response_types": ["code"], "token_endpoint_auth_method": "none"}, status=201)
    response["Cache-Control"] = "no-store"
    return response


def _with_params(uri, params):
    return uri + ("&" if "?" in uri else "?") + urlencode({k: v for k, v in params.items() if v is not None})


@login_required
def authorize(request):
    """The approval page. Problems with the app itself (unknown client, unregistered redirect) are shown here and
    never redirected; everything else goes back to the app as an OAuth error."""
    from albright_control.access import audit
    q = request.GET
    client = OAuthClient.objects.filter(client_id=q.get("client_id", "")).first()
    redirect_uri = q.get("redirect_uri", "")
    if client is None or not redirect_uri or not _matches(client.redirect_uris, redirect_uri):
        return render(request, "albright_labs/api_key_authorize.html",
                      {"problem": "This connection link isn't valid. Start connecting again from your AI app."}, status=400)
    state = q.get("state")
    back = lambda **params: redirect(_with_params(redirect_uri, {**params, "state": state}))
    if q.get("response_type") != "code":
        return back(error="unsupported_response_type")
    if not q.get("code_challenge") or q.get("code_challenge_method") != "S256":
        return back(error="invalid_request", error_description="PKCE with S256 is required.")
    allowed = scopes.keys_allowed(request.user)
    context = {"client": client, "redirect_host": urlsplit(redirect_uri).hostname, "allowed": allowed,
               "choices": [c for c in scopes.ACCESS_CHOICES if c[0] != scopes.CUSTOM],
               "full": scopes.summary(request.user, scopes.preset(request.user, scopes.FULL) or []),
               "nothing": not scopes.preset(request.user, scopes.FULL)}
    if request.method != "POST":
        return render(request, "albright_labs/api_key_authorize.html", context)
    if request.POST.get("decision") != "approve" or not allowed:
        return back(error="access_denied")
    access = request.POST.get("access")
    granted = scopes.preset(request.user, access if access in (scopes.FULL, scopes.READ_ONLY) else scopes.FULL)
    if not granted:
        return back(error="access_denied", error_description="None of this person's apps work with the API.")
    raw = secrets.token_urlsafe(32)
    OAuthCode.objects.filter(expires_at__lt=timezone.now() - timedelta(days=1)).delete()
    OAuthCode.objects.create(code_hash=hash_token(raw), client=client, user=request.user, scopes=granted,
                             key_name=f"{client.name} (connected)"[:100], redirect_uri=redirect_uri,
                             code_challenge=q["code_challenge"][:128],
                             expires_at=timezone.now() + timedelta(minutes=CODE_MINUTES))
    audit(request, "oauth_connected", f'{request.user.username}: approved "{client.name}" '
                                      f'({"read only" if access == scopes.READ_ONLY else "full access"})')
    return back(code=raw)


def _pkce_ok(verifier, challenge):
    if not verifier or not 43 <= len(verifier) <= 128:
        return False
    digest = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return secrets.compare_digest(digest, challenge)


@csrf_exempt
def token(request):
    if request.method != "POST":
        return _oauth_error("invalid_request", "Use POST.", 405)
    data = request.POST
    if data.get("grant_type") != "authorization_code":
        return _oauth_error("unsupported_grant_type", "Only authorization_code.")
    with transaction.atomic():
        code = OAuthCode.objects.select_for_update(of=("self",)).select_related("client", "user") \
            .filter(code_hash=hash_token(data.get("code", ""))).first()
        if code is None:
            return _oauth_error("invalid_grant", "Unknown code.")
        if code.used_at is not None:
            if code.token_id:                                  # replayed: end what it made
                BotToken.objects.get(pk=code.token_id).revoke()
            return _oauth_error("invalid_grant", "This code was already used.")
        if code.expires_at <= timezone.now():
            return _oauth_error("invalid_grant", "This code has expired - connect again.")
        if data.get("client_id") != code.client.client_id:
            return _oauth_error("invalid_grant", "This code was issued to another app.")
        if data.get("redirect_uri") and data.get("redirect_uri") != code.redirect_uri:
            return _oauth_error("invalid_grant", "redirect_uri doesn't match.")
        if not _pkce_ok(data.get("code_verifier", ""), code.code_challenge):
            return _oauth_error("invalid_grant", "code_verifier doesn't match.")
        if not code.user.is_active or not scopes.keys_allowed(code.user):
            return _oauth_error("invalid_grant", "API access disabled")
        bot_token, raw = BotToken.issue(code.user, code.key_name, code.scopes)
        code.used_at, code.token = timezone.now(), bot_token
        code.save(update_fields=["used_at", "token"])
    response = JsonResponse({"access_token": raw, "token_type": "Bearer", "scope": " ".join(bot_token.scopes)})
    response["Cache-Control"] = "no-store"
    response["Pragma"] = "no-cache"
    return response
