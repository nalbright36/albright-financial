"""The in-site AI assistant, with mocked providers (no network): the key (encrypted, validated, never returned or
logged), each adapter's mapping of tools both ways and its errors, tools running as the person (their data only,
admin tools for admins only), approvals (wait for Approve; Cancel runs nothing), undo, the limits (tool steps, run
and call timeouts, messages per day), the page context, the button's visibility, Admin > AI assistant, and the new
read operations in the Bot API catalog."""
import json
import logging
from datetime import timedelta
from pathlib import Path
from unittest import mock

from cryptography.fernet import Fernet
from django.contrib.auth.models import User
from django.contrib.staticfiles import finders
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from albright_bot_api import catalog
from albright_bot_api.models import BotToken
from albright_labs.models import UserPreference
from albright_labs.testing import with_apps

from . import access, providers, runner, tools
from .models import Action, Chat, Connection, Message, Run
from .providers import ProviderError, Reply, ToolCall

KEY = "sk-test-SECRETSECRETSECRET1234"
FERNET = Fernet.generate_key().decode()
HTTP = "albright_assistant.providers.requests.request"
CHAT = "albright_assistant.providers.OpenAIChat.chat"


class FakeResponse:
    def __init__(self, status=200, body=None):
        self.status_code, self._body = status, body if body is not None else {}
        self.text = json.dumps(self._body)

    def json(self):
        return self._body


def allow(user, on=True):
    UserPreference.objects.update_or_create(user=user, defaults={"assistant_allowed": on})
    return user


@override_settings(ALPACA_CREDENTIALS_ENCRYPTION_KEY=FERNET, ALPACA_CREDENTIALS_ENCRYPTION_OLD_KEYS=[])
class Base(TestCase):
    def setUp(self):
        self.boss = User.objects.create_superuser("boss", "boss@example.com", "pw-Long-enough-1")
        self.pat = with_apps(User.objects.create_user("pat", "pat@example.com", "pw-Long-enough-1", first_name="Pat"),
                             ["planner", "meals", "notes", "fitness"])
        allow(self.pat)
        self.client.force_login(self.pat)

    def connect(self, user=None, provider="openai", model="gpt-test-mini", **extra):
        user = user or self.pat
        conn = Connection.objects.create(user=user, provider=provider, key_encrypted=access.encrypt(KEY),
                                         key_last4=KEY[-4:], model=model,
                                         models_cache=[{"id": model, "label": model}], **extra)
        access.assistant_token(user)
        conn.refresh_from_db()
        return conn

    def send(self, text, chat=None, page=None):
        return self.client.post(reverse("assistant:send"), json.dumps({"text": text, "chat": chat, "page": page or {}}),
                                content_type="application/json")

    def poll(self, run_id, after=0):
        return self.client.get(reverse("assistant:run", args=[run_id]) + f"?after={after}")

    def run_until_done(self, run_id, limit=12):
        data = None
        for _ in range(limit):
            data = self.poll(run_id).json()
            if data["run"]["status"] not in ("queued", "running"):
                return data
        return data


# --- the key -------------------------------------------------------------------------------------------------------------

MODELS = {"data": [{"id": "gpt-test-mini", "created": 200}, {"id": "gpt-test", "created": 100},
                   {"id": "text-embedding-3", "created": 300}, {"id": "gpt-test-audio", "created": 400}]}


class KeyTests(Base):
    def test_connect_validates_with_list_models_and_stores_the_key_encrypted(self):
        with mock.patch(HTTP, return_value=FakeResponse(200, MODELS)) as http:
            response = self.client.post(reverse("assistant:connect"), {"provider": "openai", "api_key": KEY})
        self.assertRedirects(response, reverse("assistant:setup"), fetch_redirect_response=False)
        method, url = http.call_args[0]
        self.assertEqual((method, url), ("GET", "https://api.openai.com/v1/models"))
        self.assertEqual(http.call_args[1]["headers"]["Authorization"], f"Bearer {KEY}")
        conn = Connection.objects.get(user=self.pat)
        self.assertNotIn(KEY, conn.key_encrypted)
        self.assertEqual(access.decrypt(conn.key_encrypted), KEY)
        self.assertEqual(conn.key_last4, "1234")
        self.assertEqual([m["id"] for m in conn.models_cache], ["gpt-test-mini", "gpt-test"])   # chat models only
        self.assertEqual(conn.model, "gpt-test-mini")                                         # the default: mini

    def test_a_rejected_key_is_not_stored_and_not_echoed(self):
        with mock.patch(HTTP, return_value=FakeResponse(401, {"error": {"message": f"Incorrect API key {KEY}"}})), \
                self.assertLogs("albright_assistant", level="INFO") as logs:
            response = self.client.post(reverse("assistant:connect"), {"provider": "openai", "api_key": KEY})
        self.assertEqual(response.status_code, 400)
        self.assertContains(response, "didn&#x27;t accept your API key", status_code=400)
        self.assertNotContains(response, KEY, status_code=400)
        self.assertFalse(Connection.objects.exists())
        self.assertFalse(any(KEY in line for line in logs.output))

    def test_the_key_is_never_returned_by_any_view(self):
        conn = self.connect()
        with mock.patch(CHAT, return_value=Reply(text="Hi")):
            sent = self.send("hello").json()
            done = self.run_until_done(sent["run"]["id"])
        pages = [self.client.get(reverse("assistant:setup")), self.client.get(reverse("assistant:state")),
                 self.client.get(reverse("assistant:chat", args=[sent["chat"]])),
                 self.client.get(reverse("labs:account"))]
        for page in pages:
            self.assertNotIn(KEY, page.content.decode())
            self.assertNotIn(KEY[:-4], page.content.decode())
        self.assertNotIn(KEY, json.dumps(done))
        self.assertIn(conn.key_last4, pages[0].content.decode())

    def test_disconnect_deletes_the_key(self):
        self.connect()
        token = Connection.objects.get(user=self.pat).token
        self.client.post(reverse("assistant:disconnect"))
        self.assertFalse(Connection.objects.filter(user=self.pat).exists())
        token.refresh_from_db()
        self.assertIsNotNone(token.revoked_at)

    def test_setup_page_has_tabs_steps_and_privacy_note(self):
        page = self.client.get(reverse("assistant:setup") + "?provider=anthropic").content.decode()
        for p in providers.PROVIDERS:
            self.assertIn(f'href="?provider={p.key}#assistant-connect"', page)
        self.assertIn('id="steps-anthropic"', page)
        self.assertIn("This is separate from a Claude Pro or Max subscription", page)
        self.assertIn("Your messages, and the parts of your account the assistant reads to answer, are sent to "
                      "Anthropic (Claude).", page)
        self.assertIn('type="password"', page)


# --- adapters ------------------------------------------------------------------------------------------------------------

TOOLS = [{"name": "addNote", "description": "Save a note", "parameters": {
    "type": "object", "properties": {"title": {"type": "string"}, "n": {"type": "number", "exclusiveMinimum": 0},
                                     "q": {"type": ["number", "string"]}}, "required": ["title"]}}]
HISTORY = [{"role": "user", "text": "Save a note"},
           {"role": "assistant", "text": "", "tool_calls": [{"id": "c1", "name": "addNote", "arguments": {"title": "A"}}]},
           {"role": "tool", "call_id": "c1", "name": "addNote", "text": '{"id": 8}', "is_error": False}]


class AdapterTests(TestCase):
    def call(self, provider, body, messages=HISTORY):
        with mock.patch(HTTP, return_value=FakeResponse(200, body)) as http:
            reply = provider.chat("k", "m-1", "SYSTEM", messages, TOOLS, timeout=30)
        return reply, http.call_args

    def test_openai(self):
        reply, call = self.call(providers.BY_KEY["openai"], {
            "choices": [{"message": {"content": None, "tool_calls": [
                {"id": "c2", "type": "function", "function": {"name": "addNote", "arguments": '{"title": "B"}'}}]}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5}})
        self.assertEqual(call[0], ("POST", "https://api.openai.com/v1/chat/completions"))
        sent = call[1]["json"]
        self.assertEqual(sent["messages"][0], {"role": "system", "content": "SYSTEM"})
        self.assertEqual(sent["tools"][0]["function"]["name"], "addNote")
        self.assertEqual(sent["messages"][2]["tool_calls"][0]["function"]["arguments"], '{"title": "A"}')
        self.assertEqual(sent["messages"][3], {"role": "tool", "tool_call_id": "c1", "content": '{"id": 8}'})
        self.assertEqual(call[1]["timeout"], 30)
        self.assertEqual(reply.tool_calls, [ToolCall("c2", "addNote", {"title": "B"})])
        self.assertEqual((reply.tokens_in, reply.tokens_out), (10, 5))

    def test_openrouter_shares_the_openai_adapter_and_reports_cost(self):
        reply, call = self.call(providers.BY_KEY["openrouter"], {
            "choices": [{"message": {"content": "Done"}}], "usage": {"prompt_tokens": 1, "completion_tokens": 1, "cost": 0.0012}})
        self.assertEqual(call[0][1], "https://openrouter.ai/api/v1/chat/completions")
        self.assertEqual(str(reply.cost_usd), "0.0012")
        with mock.patch(HTTP, side_effect=[FakeResponse(200, {"data": {"limit": None}}), FakeResponse(200, {"data": [
                {"id": "openai/gpt-x-mini", "name": "GPT X mini", "created": 5, "pricing": {"prompt": "0.0000004", "completion": "0.0000016"}},
                {"id": "someone/big", "name": "Big", "created": 9, "pricing": {"prompt": "0.00001", "completion": "0.00003"}}]})]) as http:
            models = providers.BY_KEY["openrouter"].list_models("k")
        self.assertEqual(http.call_args_list[0][0][1], "https://openrouter.ai/api/v1/key")
        self.assertEqual(http.call_args_list[1][1]["params"], {"supported_parameters": "tools"})
        self.assertEqual(models[1]["price_in"], "0.4000")
        self.assertEqual(providers.BY_KEY["openrouter"].default_model(models), "openai/gpt-x-mini")

    def test_anthropic(self):
        native = [{"type": "thinking", "thinking": "hmm", "signature": "sig"},
                  {"type": "tool_use", "id": "t2", "name": "addNote", "input": {"title": "B"}}]
        reply, call = self.call(providers.BY_KEY["anthropic"], {"content": native, "usage": {"input_tokens": 7, "output_tokens": 3}})
        self.assertEqual(call[0], ("POST", "https://api.anthropic.com/v1/messages"))
        headers, sent = call[1]["headers"], call[1]["json"]
        self.assertEqual(headers["anthropic-version"], "2026-10-21")
        self.assertEqual(headers["Authorization"], "Bearer k")
        self.assertNotIn("temperature", sent)
        self.assertEqual(sent["system"], "SYSTEM")
        self.assertEqual(sent["tools"][0]["input_schema"]["required"], ["title"])
        self.assertEqual(sent["messages"][1]["content"][0], {"type": "tool_use", "id": "c1", "name": "addNote", "input": {"title": "A"}})
        self.assertEqual(sent["messages"][2]["content"][0]["type"], "tool_result")
        self.assertEqual(reply.tool_calls, [ToolCall("t2", "addNote", {"title": "B"})])
        # Next turn: the thinking block goes back exactly as received.
        history = HISTORY + [{"role": "assistant", "text": "", "tool_calls": [{"id": "t2", "name": "addNote", "arguments": {}}],
                              "native": native, "provider": "anthropic"},
                             {"role": "tool", "call_id": "t2", "name": "addNote", "text": "{}", "is_error": True}]
        _reply, call = self.call(providers.BY_KEY["anthropic"], {"content": [{"type": "text", "text": "ok"}]}, history)
        self.assertEqual(call[1]["json"]["messages"][3]["content"], native)
        self.assertTrue(call[1]["json"]["messages"][4]["content"][-1]["is_error"])

    def test_xai_uses_the_responses_api(self):
        reply, call = self.call(providers.BY_KEY["xai"], {"output": [
            {"type": "reasoning", "summary": []},
            {"type": "function_call", "call_id": "x2", "name": "addNote", "arguments": '{"title": "B"}'},
            {"type": "message", "content": [{"type": "output_text", "text": "Saving."}]}],
            "usage": {"input_tokens": 4, "output_tokens": 2, "cost_in_nano_usd": 1500000}})
        self.assertEqual(call[0], ("POST", "https://api.x.ai/v1/responses"))
        sent = call[1]["json"]
        self.assertEqual(sent["instructions"], "SYSTEM")
        self.assertFalse(sent["store"])
        self.assertEqual(sent["tools"][0]["name"], "addNote")
        self.assertIn({"type": "function_call", "call_id": "c1", "name": "addNote", "arguments": '{"title": "A"}'}, sent["input"])
        self.assertIn({"type": "function_call_output", "call_id": "c1", "output": '{"id": 8}'}, sent["input"])
        self.assertEqual(reply.tool_calls, [ToolCall("x2", "addNote", {"title": "B"})])
        self.assertEqual(reply.text, "Saving.")
        self.assertEqual(str(reply.cost_usd), "0.0015")

    def test_gemini(self):
        parts = [{"functionCall": {"name": "addNote", "args": {"title": "B"}, "id": "g2"}, "thoughtSignature": "SIG"}]
        reply, call = self.call(providers.BY_KEY["gemini"], {"candidates": [{"content": {"role": "model", "parts": parts}}],
                                                            "usageMetadata": {"promptTokenCount": 9, "candidatesTokenCount": 2}})
        self.assertEqual(call[0], ("POST", "https://generativelanguage.googleapis.com/v1beta/models/m-1:generateContent"))
        self.assertEqual(call[1]["headers"]["x-goog-api-key"], "k")
        sent = call[1]["json"]
        decl = sent["tools"][0]["functionDeclarations"][0]
        self.assertNotIn("exclusiveMinimum", decl["parameters"]["properties"]["n"])
        self.assertEqual(decl["parameters"]["properties"]["q"], {"anyOf": [{"type": "number"}, {"type": "string"}]})
        self.assertEqual(sent["contents"][2], {"role": "user", "parts": [
            {"functionResponse": {"name": "addNote", "id": "c1", "response": {"id": 8}}}]})
        self.assertEqual(reply.tool_calls, [ToolCall("g2", "addNote", {"title": "B"})])
        history = HISTORY + [{"role": "assistant", "text": "", "tool_calls": [], "native": parts, "provider": "gemini"}]
        _r, call = self.call(providers.BY_KEY["gemini"], {"candidates": [{"content": {"parts": [{"text": "ok"}]}}]}, history)
        self.assertEqual(call[1]["json"]["contents"][3]["parts"][0]["thoughtSignature"], "SIG")      # sent back as-is

    def test_errors_map_to_plain_messages(self):
        openai = providers.BY_KEY["openai"]
        cases = [(FakeResponse(401, {"error": {"message": "bad"}}), "invalid_key"),
                 (FakeResponse(402, {}), "billing"),
                 (FakeResponse(429, {"error": {"code": "insufficient_quota", "message": "You exceeded your current quota"}}), "billing"),
                 (FakeResponse(429, {"error": {"message": "Rate limit reached"}}), "rate_limited"),
                 (FakeResponse(404, {"error": {"message": "The model `m-1` does not exist"}}), "model_not_found"),
                 (FakeResponse(503, {}), "unavailable"),
                 (FakeResponse(504, {}), "timeout"),
                 (FakeResponse(400, {"type": "error", "error": {"type": "invalid_request_error",
                                                               "message": "Your credit balance is too low"}}), "billing"),
                 (FakeResponse(400, {"error": {"status": "INVALID_ARGUMENT", "message": "API key not valid. Please pass a valid API key."}}), "invalid_key")]
        for response, kind in cases:
            with self.subTest(kind=kind, status=response.status_code), mock.patch(HTTP, return_value=response):
                with self.assertRaises(ProviderError) as caught:
                    openai.chat("k", "m-1", "S", [], [])
                self.assertEqual(caught.exception.kind, kind)
        import requests
        with mock.patch(HTTP, side_effect=requests.Timeout()), self.assertRaises(ProviderError) as caught:
            openai.chat("k", "m-1", "S", [], [])
        self.assertEqual(caught.exception.kind, "timeout")
        self.assertIn("took too long", caught.exception.message)
        self.assertIn("no credits", ProviderError("billing", "OpenAI").message)
        self.assertIn('doesn\'t offer the model "m-1"', ProviderError("model_not_found", "OpenAI", "m-1").message)
        self.assertIn("Wait a minute", ProviderError("rate_limited", "OpenAI").message)
        self.assertIn("didn't accept your API key", ProviderError("invalid_key", "OpenAI").message)


# --- tools ---------------------------------------------------------------------------------------------------------------

class ToolTests(Base):
    def request(self):
        request = RequestFactory().get("/assistant/runs/1/")
        request.user = self.pat
        return request

    def test_tools_come_from_the_catalog_and_admin_tools_only_for_admins(self):
        mine = {t["name"] for t in tools.definitions(access.assistant_token(self.pat))}
        boss = {t["name"] for t in tools.definitions(access.assistant_token(self.boss))}
        self.assertIn("addScheduleItem", mine)
        self.assertIn("searchMyItems", mine)
        self.assertIn("getTodaySummary", mine)
        for admin_only in ("addIdea", "listLots", "addOpportunity", "suggestSiteChange"):
            self.assertNotIn(admin_only, mine)
            self.assertIn(admin_only, boss)
        self.assertNotIn("getDocs", mine)
        self.assertTrue(mine <= {op.op_id for op in catalog.OPS})

    def test_the_assistant_token_is_not_an_api_key(self):
        token = access.assistant_token(self.pat)
        self.assertEqual(token.kind, "assistant")
        self.assertFalse(BotToken.objects.filter(owner=self.pat).exists())        # not in any key list
        self.assertTrue(BotToken.all_objects.filter(owner=self.pat).exists())
        response = self.client.get(reverse("labs:api_keys"))
        self.assertNotContains(response, "<strong>AI assistant</strong>", status_code=response.status_code)  # not in the key list

    def test_tools_run_as_the_person_and_cannot_touch_others_data(self):
        from albright_notes.models import Note
        theirs = Note.objects.create(owner=self.boss, title="Boss secret", body="private")
        mine = Note.objects.create(owner=self.pat, title="Mine", body="hello there")
        token = access.assistant_token(self.pat)
        text, data, is_error = tools.run(self.request(), token, ToolCall("1", "getNote", {"id": theirs.pk}))
        self.assertTrue(is_error)
        self.assertNotIn("private", text)
        text, data, is_error = tools.run(self.request(), token, ToolCall("2", "getNote", {"id": mine.pk}))
        self.assertFalse(is_error)
        self.assertEqual(data["body"], "hello there")
        text, data, is_error = tools.run(self.request(), token, ToolCall("3", "addNote", {"title": "From AI"}))
        self.assertFalse(is_error)
        note = Note.objects.get(pk=data["id"])
        self.assertEqual(note.owner, self.pat)
        text, _d, is_error = tools.run(self.request(), token, ToolCall("4", "addIdea", {"title": "x"}))
        self.assertTrue(is_error)                                               # admin-only: not for pat

    def test_turning_the_permission_off_stops_tool_calls(self):
        token = access.assistant_token(self.pat)
        allow(self.pat, False)
        text, _d, is_error = tools.run(self.request(), token, ToolCall("1", "listNotes", {}))
        self.assertTrue(is_error)
        self.assertIn("turned off", text)

    def test_approval_rules(self):
        calls = [ToolCall("a", "addNote", {"title": "x"}), ToolCall("b", "deleteFoodEntry", {"id": 1}),
                 ToolCall("c", "addScheduleItem", {"title": "x", "date": "2026-11-20", "share": {"circle": 1}}),
                 ToolCall("d", "pauseProgram", {"id": 2}), ToolCall("e", "completeScheduleItem", {"id": 3})]
        reasons = tools.needs_approval(calls)
        self.assertEqual(set(reasons), {"b", "c", "d"})
        few = [ToolCall(str(n), "updateGrocery", {"id": n, "checked": True}) for n in range(5)]
        self.assertEqual(tools.needs_approval(few), {})
        many = few + [ToolCall("6", "updateGrocery", {"id": 6, "checked": True})]
        self.assertEqual(len(tools.needs_approval(many)), 6)


# --- runs ----------------------------------------------------------------------------------------------------------------

def tool_reply(*calls):
    return Reply(tool_calls=[ToolCall(f"c{n}", name, args) for n, (name, args) in enumerate(calls)], tokens_in=10, tokens_out=2)


class RunTests(Base):
    def setUp(self):
        super().setUp()
        self.conn = self.connect()

    def test_a_plain_answer(self):
        with mock.patch(CHAT, return_value=Reply(text="**Hi** <script>x</script>", tokens_in=12, tokens_out=3)) as chat:
            sent = self.send("hello")
            self.assertEqual(sent.status_code, 200)
            data = self.run_until_done(sent.json()["run"]["id"])
        self.assertEqual(data["run"]["status"], "done")
        html = [m for m in data["messages"] if m["role"] == "assistant"][0]["html"]
        self.assertIn("<strong>Hi</strong>", html)
        self.assertNotIn("<script", html)
        self.assertEqual(chat.call_count, 1)
        self.assertLessEqual(chat.call_args[1]["timeout"], runner.CALL_SECONDS)
        self.assertEqual(Chat.objects.get(pk=sent.json()["chat"]).tokens_in, 12)

    def test_safe_tool_runs_at_once_with_an_action_chip_and_undo(self):
        from albright_planner.models import ScheduleItem
        replies = [tool_reply(("addScheduleItem", {"title": "Call dentist", "date": "2026-11-19"})), Reply(text="Added it.")]
        with mock.patch(CHAT, side_effect=replies):
            data = self.run_until_done(self.send("remind me").json()["run"]["id"])
        item = ScheduleItem.objects.get(title="Call dentist")
        chips = [a for m in data["messages"] if m["role"] == "tool" for a in m["actions"]]
        self.assertEqual(chips[0]["summary"], "Added task: Call dentist - Thu Nov 19")
        self.assertTrue(chips[0]["can_undo"])
        self.assertEqual(data["run"]["changed"], ["planner"])
        undone = self.client.post(reverse("assistant:action_undo", args=[chips[0]["id"]]))
        self.assertEqual(undone.status_code, 200)
        self.assertFalse(ScheduleItem.objects.filter(pk=item.pk).exists())
        again = self.client.post(reverse("assistant:action_undo", args=[chips[0]["id"]]))
        self.assertEqual(again.status_code, 409)

    def test_undo_expires_after_ten_minutes(self):
        replies = [tool_reply(("addNote", {"title": "x"})), Reply(text="ok")]
        with mock.patch(CHAT, side_effect=replies):
            self.run_until_done(self.send("note").json()["run"]["id"])
        action = Action.objects.get()
        Action.objects.filter(pk=action.pk).update(undo_until=timezone.now() - timedelta(seconds=1))
        self.assertEqual(self.client.post(reverse("assistant:action_undo", args=[action.pk])).status_code, 409)

    def test_approval_waits_and_cancel_runs_nothing(self):
        from albright_fitness.models import WeightEntry
        entry = WeightEntry.objects.create(owner=self.pat, date=timezone.localdate(), weight_kg=80)
        with mock.patch(CHAT, side_effect=[tool_reply(("deleteProgram", {"id": 999}))]), \
                mock.patch("albright_assistant.tools.run", wraps=tools.run) as run_tool:
            sent = self.send("delete my program").json()
            data = self.run_until_done(sent["run"]["id"])
            self.assertEqual(data["run"]["status"], "waiting")
            self.assertEqual(data["run"]["pending"][0]["reason"], "Deletes something")
            run_tool.assert_not_called()
            cancelled = self.client.post(reverse("assistant:run_cancel", args=[sent["run"]["id"]]))
            run_tool.assert_not_called()
        self.assertEqual(cancelled.json()["run"]["status"], "done")
        tool_msg = Message.objects.get(role="tool")
        self.assertTrue(tool_msg.data["cancelled"])
        self.assertTrue(WeightEntry.objects.filter(pk=entry.pk).exists())
        self.assertFalse(Action.objects.exists())

    def test_approve_runs_the_waiting_call(self):
        replies = [tool_reply(("deleteProgram", {"id": 999})), Reply(text="It wasn't there.")]
        with mock.patch(CHAT, side_effect=replies), mock.patch("albright_assistant.tools.run", wraps=tools.run) as run_tool:
            sent = self.send("delete my program").json()
            self.run_until_done(sent["run"]["id"])
            self.client.post(reverse("assistant:run_approve", args=[sent["run"]["id"]]))
            self.assertEqual(run_tool.call_count, 1)
            data = self.run_until_done(sent["run"]["id"])
        self.assertEqual(data["run"]["status"], "done")

    def test_tool_step_limit(self):
        self.conn.max_steps = 2
        self.conn.save()
        with mock.patch(CHAT, return_value=tool_reply(("listNotes", {}))) as chat:
            data = self.run_until_done(self.send("loop").json()["run"]["id"])
        self.assertEqual(chat.call_count, 2)
        self.assertEqual(data["run"]["status"], "done")
        self.assertTrue(any(m["role"] == "notice" and "2 tool steps" in m["text"] for m in data["messages"]))

    def test_run_timeout(self):
        with mock.patch(CHAT) as chat:
            sent = self.send("slow").json()
            Run.objects.filter(pk=sent["run"]["id"]).update(started_at=timezone.now() - timedelta(seconds=130))
            data = self.run_until_done(sent["run"]["id"])
        chat.assert_not_called()
        self.assertEqual(data["run"]["status"], "error")
        self.assertTrue(any("longer than 2 minutes" in m.get("text", "") for m in data["messages"]))

    def test_call_timeout_is_the_rest_of_the_run_at_most_60s(self):
        with mock.patch(CHAT, return_value=Reply(text="ok")) as chat:
            sent = self.send("hi").json()
            Run.objects.filter(pk=sent["run"]["id"]).update(started_at=timezone.now() - timedelta(seconds=100))
            self.run_until_done(sent["run"]["id"])
        self.assertLessEqual(chat.call_args[1]["timeout"], 21)

    def test_daily_limit(self):
        self.conn.daily_limit = 1
        self.conn.save()
        with mock.patch(CHAT, return_value=Reply(text="ok")):
            first = self.send("one").json()
            self.run_until_done(first["run"]["id"])
            second = self.send("two", chat=first["chat"])
        self.assertEqual(second.status_code, 403)
        self.assertIn("limit of 1 messages today", second.json()["error"])

    def test_provider_errors_become_clear_messages(self):
        with mock.patch(CHAT, side_effect=ProviderError("billing", "OpenAI")):
            data = self.run_until_done(self.send("hi").json()["run"]["id"])
        self.assertEqual(data["run"]["status"], "error")
        self.assertTrue(any("no credits" in m.get("text", "") for m in data["messages"]))
        self.conn.refresh_from_db()
        self.assertEqual(self.conn.last_error_kind, "billing")

    def test_page_context_is_sent(self):
        with mock.patch(CHAT, return_value=Reply(text="ok")) as chat:
            sent = self.send("summarize this", page={"app": "notes", "page": "Gift ideas · Notes", "path": "/notes/5/",
                                                     "item": {"type": "note", "id": 5}, "evil": "<x>"}).json()
            self.run_until_done(sent["run"]["id"])
        system = chat.call_args[0][2]
        self.assertIn("the Notes app", system)
        self.assertIn("note #5 - read it with getNote (id 5)", system)
        self.assertIn("Pat", system)
        self.assertNotIn("evil", json.dumps(Run.objects.get().page))

    def test_disabled_by_admin(self):
        self.conn.disabled = True
        self.conn.save()
        self.assertEqual(self.send("hi").status_code, 403)

    def test_other_peoples_runs_and_chats_are_404(self):
        with mock.patch(CHAT, return_value=Reply(text="ok")):
            sent = self.send("hi").json()
        self.client.force_login(self.boss)
        self.assertEqual(self.poll(sent["run"]["id"]).status_code, 404)
        self.assertEqual(self.client.get(reverse("assistant:chat", args=[sent["chat"]])).status_code, 404)

    def test_delete_chats(self):
        with mock.patch(CHAT, return_value=Reply(text="ok")):
            first = self.send("a").json()
            self.run_until_done(first["run"]["id"])
        self.client.post(reverse("assistant:chat_delete", args=[first["chat"]]))
        self.assertFalse(Chat.objects.exists())
        Chat.objects.create(user=self.pat)
        self.client.post(reverse("assistant:chats_delete_all"))
        self.assertFalse(Chat.objects.filter(user=self.pat).exists())


# --- the button, the setup page's gate, Admin ------------------------------------------------------------------------------

class WidgetTests(Base):
    def test_hidden_without_the_permission(self):
        allow(self.pat, False)
        page = self.client.get(reverse("labs:hub")).content.decode()
        self.assertNotIn('id="assistant-button"', page)
        self.assertEqual(self.client.get(reverse("assistant:setup")).status_code, 404)
        self.assertEqual(self.client.get(reverse("assistant:state")).status_code, 404)

    def test_the_button_is_in_the_header_not_floating(self):
        page = self.client.get(reverse("labs:hub")).content.decode()
        nav = page.split('<nav class="navbar">', 1)[1].split("</nav>", 1)[0]
        self.assertIn('id="assistant-button"', nav)
        self.assertIn('aria-label="AI Assistant"', nav)
        self.assertLess(nav.index('id="assistant-button"'), nav.index('id="notif-bell"'))    # left of the bell
        self.assertNotIn("assistant-fab", page)
        self.assertNotIn("as-fab", page)
        for name in ("assistant/assistant.css", "assistant/assistant.js", "js/fab_dock.js"):
            self.assertNotIn("as-inline", Path(finders.find(name)).read_text(encoding="utf-8"), name)

    def test_not_connected_shows_the_connect_card(self):
        page = self.client.get(reverse("labs:hub")).content.decode()
        self.assertIn('id="assistant-button"', page)
        self.assertIn('id="assistant-connect-card"', page)
        self.assertIn(reverse("assistant:setup"), page)

    def test_connected_shows_the_panel_with_page_context(self):
        self.connect()
        from albright_notes.models import Note
        note = Note.objects.create(owner=self.pat, title="Gift ideas", body="x")
        page = self.client.get(reverse("albright_notes:note", args=[note.pk])).content.decode()
        self.assertIn('id="assistant-panel"', page)
        self.assertIn(f'"item": {{"type": "note", "id": {note.pk}}}', page)
        self.assertIn('data-chip="Summarize this note"', page)
        self.assertIn("assistant/assistant.js", page)

    def test_superusers_always_have_it(self):
        self.client.force_login(self.boss)
        self.assertEqual(self.client.get(reverse("assistant:setup")).status_code, 200)

    def test_admin_users_page_switch(self):
        self.client.force_login(self.boss)
        self.client.post(reverse("albright_control:users"), {"action": "assistant", "user_id": self.pat.pk, "allowed": "0"})
        self.assertFalse(UserPreference.objects.get(user=self.pat).assistant_allowed)


class AdminTests(Base):
    def test_lists_connections_without_keys_or_messages(self):
        self.connect()
        with mock.patch(CHAT, return_value=Reply(text="The secret answer")):
            sent = self.send("my private question").json()
            self.run_until_done(sent["run"]["id"])
        self.client.force_login(self.boss)
        page = self.client.get(reverse("assistant_admin:overview")).content.decode()
        self.assertIn('id="assistant-connections"', page)
        self.assertIn("pat", page)
        self.assertIn("OpenAI", page)
        for secret in (KEY, KEY[-4:], "private question", "secret answer"):
            self.assertNotIn(secret, page)

    def test_disable_switch_and_superusers_only(self):
        conn = self.connect()
        self.assertNotEqual(self.client.get(reverse("assistant_admin:overview")).status_code, 200)
        self.client.force_login(self.boss)
        self.client.post(reverse("assistant_admin:toggle", args=[conn.pk]))
        conn.refresh_from_db()
        self.assertTrue(conn.disabled)


# --- the new Bot API read operations --------------------------------------------------------------------------------------

@override_settings(BOT_API_REQUIRE_HTTPS=False)
class ReadOperationTests(Base):
    def setUp(self):
        super().setUp()
        from albright_labs.testing import allow_api_keys
        allow_api_keys(self.pat)
        self.token, self.raw = BotToken.issue(self.pat, "test", ["notes:read", "schedule:read"])

    def get(self, path):
        return self.client.get(catalog.API_PREFIX + path, HTTP_AUTHORIZATION=f"Bearer {self.raw}")

    def test_in_the_catalog(self):
        for name in ("getNote", "getRecipe", "searchMyItems", "getTodaySummary"):
            self.assertIn(name, catalog.BY_ID)

    def test_get_note_search_and_today(self):
        from albright_notes.models import Note
        note = Note.objects.create(owner=self.pat, title="Chili tips", body="Brown the meat")
        other = Note.objects.create(owner=self.boss, title="Chili boss", body="no")
        self.assertEqual(self.get(f"notes/{note.pk}").json()["body"], "Brown the meat")
        self.assertEqual(self.get(f"notes/{other.pk}").status_code, 404)
        found = self.get("search?q=chili").json()
        self.assertEqual([n["id"] for n in found["notes"]], [note.pk])
        self.assertNotIn("recipes", found)                         # no meals:read on this key
        today = self.get("today").json()
        self.assertIn("planner", today)
        self.assertNotIn("meals", today)
        self.assertEqual(self.get("search").status_code, 400)
