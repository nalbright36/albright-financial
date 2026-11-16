"""Making the API easy for AIs: GET me, the public guide (docs) and OpenAPI spec built from one catalog (every route is
in it - a new endpoint left out fails here), the remote MCP server (tools = the spec's operations; tools/call runs the
real view with the caller's key), and the OAuth flow MCP connectors use (register -> approve -> token = a normal key)."""
import base64
import hashlib
import json
import re
from urllib.parse import parse_qs, urlsplit

from django.contrib.auth.models import User
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from albright_labs.testing import allow_api_keys, with_apps
from albright_notes.models import Note

from . import catalog
from .models import SCOPE_KEYS, BotRequestLog, BotToken

PASSWORD = "pw-not-real-12345"
API = "/api/bot/v1/"


def _norm(route):
    return re.sub(r"<[^>]+>|{[^}]+}", "{}", route)


@override_settings(BOT_API_REQUIRE_HTTPS=False)
class Base(TestCase):
    # DEBUG is off in tests, so the site's own addresses are https (connect.site_origin) - as in production.
    def setUp(self):
        self.boss = User.objects.create_superuser(username="nate", password=PASSWORD, email="")
        self.kim = allow_api_keys(with_apps(User.objects.create_user(username="kim", password=PASSWORD), ["notes", "planner"]))
        Note.objects.create(owner=self.kim, title="Secret surprise party plan")
        self.token, self.raw = BotToken.issue(self.kim, "Kim's Claude", ["notes:read", "notes:write", "schedule:read"])
        self.api = Client()

    def auth(self, raw=None):
        return {"HTTP_AUTHORIZATION": f"Bearer {raw or self.raw}"}

    def rpc(self, method, params=None, raw=None, msg_id=1, **headers):
        body = {"jsonrpc": "2.0", "method": method, **({"params": params} if params is not None else {})}
        if msg_id is not None:
            body["id"] = msg_id
        extra = self.auth(raw) if raw is not False else {}
        return self.api.post(API + "mcp", data=json.dumps(body), content_type="application/json",
                             HTTP_ACCEPT="application/json, text/event-stream", **extra, **headers)


class MeTests(Base):
    def test_me_lists_owner_key_scopes_apps_and_endpoints(self):
        data = self.api.get(API + "me", **self.auth()).json()
        self.assertEqual((data["username"], data["key_name"]), ("kim", "Kim's Claude"))
        self.assertEqual(data["scopes"], ["notes:read", "notes:write", "schedule:read"])
        self.assertIn({"app": "Notes", "access": "read and change"}, data["apps"])
        self.assertIn({"app": "Planner", "access": "read"}, data["apps"])
        ids = {e["operation_id"] for e in data["endpoints"]}
        self.assertTrue({"getMe", "listNotes", "addNote", "updateNote", "listSchedule"} <= ids)
        self.assertNotIn("addScheduleItem", ids)
        self.assertNotIn("listGroceries", ids)

    def test_me_needs_a_key(self):
        self.assertEqual(self.api.get(API + "me").status_code, 401)


class SpecTests(Base):
    def test_every_bot_route_is_in_the_spec(self):
        from .urls import api_urlpatterns
        spec = self.api.get(API + "openapi.json").json()
        documented = {_norm(path.lstrip("/")) for path in spec["paths"]}
        for pattern in api_urlpatterns:
            route = str(pattern.pattern)
            if route in catalog.NOT_OPERATIONS:
                continue
            self.assertIn(_norm(route), documented, f"{route} isn't in catalog.py (the docs, spec and MCP tools)")
        # ...and every operation is a real route.
        routes = {_norm(str(p.pattern)) for p in api_urlpatterns}
        for op in catalog.OPS:
            self.assertIn(_norm(op.path), routes, op.op_id)

    def test_spec_is_valid_openapi_31(self):
        spec = self.api.get(API + "openapi.json").json()
        self.assertEqual(spec["openapi"], "3.1.0")
        self.assertEqual(spec["components"]["securitySchemes"]["bearerAuth"], {"type": "http", "scheme": "bearer"})
        self.assertTrue(spec["servers"][0]["url"].endswith("/api/bot/v1"))
        ids = []
        for path, item in spec["paths"].items():
            self.assertTrue(path.startswith("/"))
            for method, op in item.items():
                self.assertIn(method, ("get", "post", "patch", "delete"))
                self.assertRegex(op["operationId"], r"^[A-Za-z][A-Za-z0-9]{2,40}$")
                self.assertTrue(op["summary"] and op["description"] and op["responses"])
                declared = {p["name"] for p in op.get("parameters", []) if p["in"] == "path"}
                self.assertEqual(declared, set(re.findall(r"{(\w+)}", path)), path)
                ids.append(op["operationId"])
        self.assertEqual(len(ids), len(set(ids)))
        for ref in re.findall(r'"\$ref": "#/components/schemas/(\w+)"', json.dumps(spec)):
            self.assertIn(ref, spec["components"]["schemas"])
        try:
            from openapi_spec_validator import validate
        except ImportError:
            return
        validate(spec)

    def test_spec_covers_every_scope(self):
        named = {s for op in catalog.OPS for s in op.scopes}
        self.assertEqual(named, set(SCOPE_KEYS))

    def test_docs_and_spec_need_no_key_and_hold_no_user_data(self):
        for path in ("docs", "openapi.json", "fitness/openapi.json"):
            response = self.api.get(API + path)
            self.assertEqual(response.status_code, 200, path)
            body = response.content.decode()
            for private in ("kim", "Secret surprise", self.raw, "abt_", "nate"):
                self.assertNotIn(private, body, f"{path}: {private}")
        self.assertTrue(self.api.get(API + "docs")["Content-Type"].startswith("text/markdown"))

    def test_docs_are_a_guide_for_an_ai(self):
        text = self.api.get(API + "docs").content.decode()
        for phrase in ("Authorization: Bearer <key>", "GET https://testserver/api/bot/v1/me", "## Errors",
                       "## Rate limits", "## Rules", "idempotency_key", "Example request:", "Example response (201):",
                       "operationId: `logFood`", "Before deleting anything or changing more than 5 items"):
            self.assertIn(phrase, text)
        for op in catalog.OPS:
            self.assertIn(f"`{op.heading}`", text)

    def test_docs_with_a_key_list_only_its_endpoints(self):
        text = self.api.get(API + "docs", **self.auth()).content.decode()
        self.assertIn("POST notes", text)
        self.assertNotIn("`POST schedule`", text)                   # schedule:write isn't on this key
        self.assertIn("`GET  schedule?start=", text)
        self.assertIn("YOUR TOKEN'S SCOPES: notes:read, notes:write, schedule:read", text)
        self.assertEqual(self.api.get(API + "docs", **self.auth("abt_wrong")).status_code, 401)


class McpTests(Base):
    def test_without_a_key_401_points_at_the_oauth_metadata(self):
        response = self.rpc("initialize", {"protocolVersion": "2025-06-18"}, raw=False)
        self.assertEqual(response.status_code, 401)
        self.assertIn('resource_metadata="https://testserver/.well-known/oauth-protected-resource/api/bot/v1/mcp"',
                      response["WWW-Authenticate"])
        self.assertEqual(self.rpc("tools/list", raw="abt_nope").status_code, 401)

    def test_initialize_and_notifications(self):
        result = self.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                         "clientInfo": {"name": "test", "version": "1"}}).json()["result"]
        self.assertEqual(result["protocolVersion"], "2025-06-18")
        self.assertIn("tools", result["capabilities"])
        self.assertIn("/api/bot/v1/docs", result["instructions"])
        self.assertEqual(self.rpc("notifications/initialized", msg_id=None).status_code, 202)
        self.assertEqual(self.api.get(API + "mcp", **self.auth()).status_code, 405)
        self.assertEqual(self.rpc("nope/nothing").json()["error"]["code"], -32601)

    def test_tools_match_the_spec(self):
        _t, raw = BotToken.issue(self.boss, "All", SCOPE_KEYS)
        tools = self.rpc("tools/list", raw=raw).json()["result"]["tools"]
        spec = self.api.get(API + "openapi.json").json()
        ops = {op["operationId"]: op for item in spec["paths"].values() for op in item.values()}
        self.assertEqual({t["name"] for t in tools}, {i for i, op in ops.items() if catalog.BY_ID[i].tool})
        for tool in tools:
            self.assertEqual(tool["title"], ops[tool["name"]]["summary"])
            self.assertIn(ops[tool["name"]]["description"], tool["description"])
            self.assertEqual(tool["inputSchema"]["type"], "object")
        # A narrower key sees only its own tools.
        mine = {t["name"] for t in self.rpc("tools/list").json()["result"]["tools"]}
        self.assertIn("addNote", mine)
        self.assertNotIn("logFood", mine)

    def test_tools_call_runs_the_real_endpoint_as_the_key(self):
        result = self.rpc("tools/call", {"name": "addNote", "arguments": {"title": "From Claude", "tags": ["ai"]}}).json()["result"]
        self.assertFalse(result["isError"])
        note = Note.objects.get(title="From Claude")
        self.assertEqual((note.owner, result["structuredContent"]["id"]), (self.kim, note.pk))
        listed = self.rpc("tools/call", {"name": "listNotes", "arguments": {"q": "claude"}}).json()["result"]
        self.assertEqual([n["title"] for n in listed["structuredContent"]["notes"]], ["From Claude"])
        patched = self.rpc("tools/call", {"name": "updateNote", "arguments": {"id": note.pk, "append": "more"}}).json()["result"]
        self.assertFalse(patched["isError"])
        self.assertTrue(BotRequestLog.objects.filter(token=self.token, method="POST", endpoint=API + "notes", status=201).exists())

    def test_tools_call_errors_come_back_as_tool_errors(self):
        result = self.rpc("tools/call", {"name": "addScheduleItem", "arguments": {"title": "x", "date": "2026-11-16"}}).json()["result"]
        self.assertTrue(result["isError"])
        self.assertIn("HTTP 403", result["content"][0]["text"])
        self.assertEqual(self.rpc("tools/call", {"name": "getDocs", "arguments": {}}).json()["error"]["code"], -32602)


@override_settings(BOT_API_REQUIRE_HTTPS=False)
class OAuthTests(TestCase):
    REDIRECT = "https://claude.ai/api/mcp/auth_callback"

    def setUp(self):
        self.kim = allow_api_keys(with_apps(User.objects.create_user(username="kim", password=PASSWORD), ["notes", "planner"]))
        self.client.force_login(self.kim)
        self.api = Client()
        self.verifier = "v" * 64
        self.challenge = base64.urlsafe_b64encode(hashlib.sha256(self.verifier.encode()).digest()).rstrip(b"=").decode()

    def register(self, uris=None):
        response = self.api.post("/oauth/register", data=json.dumps({"client_name": "Claude", "redirect_uris": uris or [self.REDIRECT]}),
                                 content_type="application/json")
        return response

    def authorize_url(self, client_id, redirect=None, **extra):
        from urllib.parse import urlencode
        params = {"response_type": "code", "client_id": client_id, "redirect_uri": redirect or self.REDIRECT,
                  "code_challenge": self.challenge, "code_challenge_method": "S256", "state": "xyz", **extra}
        return reverse("labs:api_key_authorize") + "?" + urlencode(params)

    def approve(self, client_id, access="full", redirect=None):
        response = self.client.post(self.authorize_url(client_id, redirect), {"decision": "approve", "access": access})
        self.assertEqual(response.status_code, 302)
        return parse_qs(urlsplit(response["Location"]).query)

    def exchange(self, client_id, code, verifier=None, redirect=None):
        return self.api.post("/oauth/token", {"grant_type": "authorization_code", "code": code, "client_id": client_id,
                                              "redirect_uri": redirect or self.REDIRECT, "code_verifier": verifier or self.verifier})

    def test_metadata(self):
        resource = self.api.get("/.well-known/oauth-protected-resource/api/bot/v1/mcp").json()
        self.assertEqual(resource["resource"], "https://testserver/api/bot/v1/mcp")
        self.assertEqual(resource["authorization_servers"], ["https://testserver"])
        server = self.api.get("/.well-known/oauth-authorization-server").json()
        self.assertEqual(server["issuer"], "https://testserver")
        self.assertEqual(server["code_challenge_methods_supported"], ["S256"])
        self.assertEqual(server["authorization_endpoint"], "https://testserver/account/api-keys/authorize/")
        self.assertTrue(server["registration_endpoint"].endswith("/oauth/register"))

    def test_full_flow_makes_a_normal_key_that_works_over_mcp(self):
        client_id = self.register().json()["client_id"]
        page = self.client.get(self.authorize_url(client_id))
        self.assertContains(page, "Connect Claude?")
        self.assertContains(page, "claude.ai")                               # where it sends them back
        params = self.approve(client_id, access="read")
        self.assertEqual(params["state"], ["xyz"])
        response = self.exchange(client_id, params["code"][0])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Cache-Control"], "no-store")
        raw = response.json()["access_token"]
        token = BotToken.objects.get(owner=self.kim)
        self.assertEqual(token.name, "Claude (connected)")
        self.assertTrue(all(s.endswith(":read") for s in token.scopes))
        tools = self.api.post(API + "mcp", data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}),
                              content_type="application/json", HTTP_AUTHORIZATION=f"Bearer {raw}").json()
        self.assertIn("listNotes", {t["name"] for t in tools["result"]["tools"]})
        self.assertContains(self.client.get(reverse("labs:api_keys")), "Claude (connected)")

    def test_wrong_verifier_and_replayed_code(self):
        client_id = self.register().json()["client_id"]
        code = self.approve(client_id)["code"][0]
        self.assertEqual(self.exchange(client_id, code, verifier="w" * 64).json()["error"], "invalid_grant")
        code = self.approve(client_id)["code"][0]
        raw = self.exchange(client_id, code).json()["access_token"]
        self.assertEqual(self.exchange(client_id, code).json()["error"], "invalid_grant")
        self.assertEqual(self.api.get(API + "me", HTTP_AUTHORIZATION=f"Bearer {raw}").status_code, 401)   # replay revokes it

    def test_claude_code_loopback_redirect_on_any_port(self):
        client_id = self.register(["http://localhost/callback"]).json()["client_id"]
        params = self.approve(client_id, redirect="http://localhost:3118/callback")
        self.assertEqual(self.exchange(client_id, params["code"][0], redirect="http://localhost:3118/callback").status_code, 200)

    def test_bad_requests(self):
        self.assertEqual(self.register(["http://evil.example/cb"]).status_code, 400)
        client_id = self.register().json()["client_id"]
        self.assertEqual(self.client.get(self.authorize_url(client_id, "https://evil.example/cb")).status_code, 400)
        response = self.client.get(self.authorize_url(client_id, code_challenge_method="plain"))
        self.assertIn("error=invalid_request", response["Location"])
        denied = self.client.post(self.authorize_url(client_id), {"decision": "deny"})
        self.assertIn("error=access_denied", denied["Location"])
        self.assertFalse(BotToken.objects.exists())

    def test_without_the_flag_it_cant_be_approved(self):
        allow_api_keys(self.kim, False)
        client_id = self.register().json()["client_id"]
        self.assertContains(self.client.get(self.authorize_url(client_id)), "Ask Nathan to enable it.")
        response = self.client.post(self.authorize_url(client_id), {"decision": "approve", "access": "full"})
        self.assertIn("error=access_denied", response["Location"])
        self.assertFalse(BotToken.objects.exists())
