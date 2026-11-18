"""The remote MCP server: POST /api/bot/v1/mcp - how Claude (and ChatGPT, Grok, Gemini) use this site's API as tools.

Streamable HTTP transport, stateless: each POST carries one JSON-RPC message (or a batch) and gets a JSON answer (or
202 for a notification). There's no event stream, so GET is a 405. It speaks the protocol directly instead of mounting
the official MCP Python SDK: the SDK's HTTP transport is an ASGI (Starlette) app, and this site runs under WSGI on
PythonAnywhere. Only the methods a tools-only server needs: initialize, ping, tools/list, tools/call.

Auth is the same Bearer API key as the rest of the API - a key from My account > API keys, or the one an OAuth
connection made (oauth_views). Without one: 401 with WWW-Authenticate pointing at the OAuth metadata, which is how
Claude starts its sign-in.

The tools are catalog.OPS (same names and descriptions as the OpenAPI operations). tools/call runs the real API view
with the caller's key, so scopes, the owner checks, rate limits and the activity log all apply exactly as they do to a
direct request.
"""
import json
from io import BytesIO
from urllib.parse import urlencode

from django.http import HttpResponse, JsonResponse
from django.urls import Resolver404, resolve
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

from . import catalog, connect
from .auth import ApiError, _log, authenticate, error_response
from .models import BotToken

PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
SERVER_INFO = {"name": "albright-labs", "title": "Albright Labs", "version": "1.0.0"}


def resource_metadata_url(request):
    return connect.site_origin(request) + "/.well-known/oauth-protected-resource" + catalog.API_PREFIX + "mcp"


def _unauthorized(request, exc):
    response = error_response(request, exc)
    challenge = f'Bearer resource_metadata="{resource_metadata_url(request)}"'
    if request.META.get("HTTP_AUTHORIZATION"):
        challenge += ', error="invalid_token"'
    response["WWW-Authenticate"] = challenge
    return response


def _rpc_error(msg_id, code, message):
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def _rpc_result(msg_id, result):
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


@csrf_exempt
def mcp(request):
    try:
        token = authenticate(request)
    except ApiError as exc:
        return _unauthorized(request, exc) if exc.status == 401 else error_response(request, exc)
    if request.method != "POST":
        response = HttpResponse(status=405)
        response["Allow"] = "POST"
        return response
    try:
        message = json.loads(request.body or b"")
    except (ValueError, UnicodeDecodeError):
        return JsonResponse(_rpc_error(None, -32700, "Parse error: the body must be JSON-RPC."), status=400)
    if isinstance(message, list):
        answers = [a for a in (_handle(request, token, m) for m in message) if a is not None]
        return JsonResponse(answers, safe=False) if answers else HttpResponse(status=202)
    answer = _handle(request, token, message)
    return HttpResponse(status=202) if answer is None else JsonResponse(answer)


def _handle(request, token, message):
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or not isinstance(message.get("method"), str):
        return _rpc_error(message.get("id") if isinstance(message, dict) else None, -32600, "Invalid request.")
    method, msg_id, params = message["method"], message.get("id"), message.get("params") or {}
    if "id" not in message:                     # a notification (notifications/initialized, cancelled, ...)
        return None
    if method == "initialize":
        asked = params.get("protocolVersion")
        _log_envelope(request, token, "mcp initialize")
        return _rpc_result(msg_id, {
            "protocolVersion": asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": SERVER_INFO,
            "instructions": connect.instructions(connect.api_base(request)),
        })
    if method == "ping":
        return _rpc_result(msg_id, {})
    if method == "tools/list":
        tools = catalog.tools(token)
        _log_envelope(request, token, f"mcp tools/list ({len(tools)})")
        return _rpc_result(msg_id, {"tools": tools})
    if method == "tools/call":
        op = catalog.BY_ID.get(params.get("name"))
        if op is None or not op.tool:
            return _rpc_error(msg_id, -32602, f"Unknown tool: {params.get('name')!r}.")
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            return _rpc_error(msg_id, -32602, "arguments must be an object.")
        return _rpc_result(msg_id, call(request, op, arguments))
    return _rpc_error(msg_id, -32601, f"Method not found: {method}.")


def _log_envelope(request, token, summary):
    BotToken.objects.filter(pk=token.pk).update(last_used_at=timezone.now())
    _log(request, token, 200, summary)


def _query_value(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        return [_query_value(v) for v in value]
    return value


def call(request, op, arguments, as_token=None):
    """Runs one operation as an API request with the caller's key; returns the MCP tool result. as_token: the in-site
    AI assistant's token (albright_assistant.tools) - set on the inner request, never sent as a header."""
    path = op.path
    for name in op.path_params:
        value = arguments.get(name)
        if not isinstance(value, int) or isinstance(value, bool):
            try:
                value = int(str(value))
            except ValueError:
                return _tool_text(f"{name} is required and must be a whole number.", error=True)
        path = path.replace("{" + name + "}", str(value))
    query = {k: _query_value(v) for k, v in arguments.items() if k in op.query and v is not None}
    body = None
    if op.method != "GET":
        body = {k: v for k, v in arguments.items() if k not in op.path_params and k not in op.query}
    full_path = catalog.API_PREFIX + path
    try:
        match = resolve(full_path)
    except Resolver404:
        return _tool_text(f"No such endpoint: {op.method} {path}.", error=True)
    inner = _inner_request(request, op.method, full_path, query, body)
    inner.resolver_match = match
    if as_token is not None:
        inner.assistant_token = as_token
    response = match.func(inner, *match.args, **match.kwargs)
    text = response.content.decode("utf-8", "replace")
    data = None
    if response.get("Content-Type", "").startswith("application/json"):
        try:
            data = json.loads(text)
        except ValueError:
            data = None
    error = response.status_code >= 400
    result = _tool_text(f"HTTP {response.status_code}: {text}" if error else text, error=error)
    if isinstance(data, dict):
        result["structuredContent"] = data
    return result


def _tool_text(text, error=False):
    return {"content": [{"type": "text", "text": text}], "isError": error}


def _inner_request(outer, method, path, query, body):
    from django.core.handlers.wsgi import WSGIRequest
    data = json.dumps(body).encode() if body is not None else b""
    environ = {k: v for k, v in outer.META.items() if isinstance(v, str) and k != "HTTP_COOKIE"}
    environ.update({
        "REQUEST_METHOD": method, "PATH_INFO": path, "SCRIPT_NAME": "", "QUERY_STRING": urlencode(query, doseq=True),
        "CONTENT_TYPE": "application/json", "CONTENT_LENGTH": str(len(data)), "wsgi.input": BytesIO(data),
        "wsgi.url_scheme": outer.scheme, "wsgi.errors": outer.META.get("wsgi.errors"),
        "wsgi.version": (1, 0), "wsgi.multithread": False, "wsgi.multiprocess": True, "wsgi.run_once": False,
    })
    return WSGIRequest(environ)
