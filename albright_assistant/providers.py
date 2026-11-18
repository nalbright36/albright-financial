"""The AI providers the assistant can use with the person's own developer API key - one interface, one adapter each.
Built from each provider's official docs as checked in November 2026:

  OpenAI      Chat Completions   POST https://api.openai.com/v1/chat/completions   (models: GET /v1/models)
  OpenRouter  the same adapter   POST https://openrouter.ai/api/v1/chat/completions (its docs: OpenAI-compatible,
                                 tools / tool_calls / role "tool"); models: GET /api/v1/models?supported_parameters=tools;
                                 the key is checked with GET /api/v1/key (the model list doesn't need one)
  Anthropic   Messages           POST https://api.anthropic.com/v1/messages, "anthropic-version: 2026-10-21", the key as
                                 "Authorization: Bearer"; no temperature (removed in that version); thinking blocks are
                                 sent back exactly as received (required with tool use)
  xAI         Responses          POST https://api.x.ai/v1/responses (xAI calls Chat Completions legacy); store=false and
                                 the history resent each time; models: GET /v1/language-models
  Google      generateContent    POST https://generativelanguage.googleapis.com/v1beta/models/<id>:generateContent with
                                 "x-goog-api-key" (still supported; the newer Interactions API isn't needed); a model
                                 part's thoughtSignature is sent back in the same part (required for function calling)

Interface: Provider.list_models(key) -> [{"id", "label", "created", "price_in", "price_out"}] (also how a key is
validated - the cheapest call there is), default_model(models), chat(key, model, system, messages, tools, timeout)
-> Reply. Messages are the assistant's own neutral form:
  {"role": "user", "text"} | {"role": "assistant", "text", "tool_calls": [{"id", "name", "arguments"}], "native"}
  | {"role": "tool", "call_id", "name", "text", "is_error"}
Tools: [{"name", "description", "parameters": JSON Schema}]. Any failure is a ProviderError with a kind (see
MESSAGES) - never the key, never the provider's raw body.
"""
import json
import re
from dataclasses import dataclass, field
from decimal import Decimal

import requests

DEFAULT_TIMEOUT = 60          # seconds per provider call
LIST_TIMEOUT = 20
USER_AGENT = "AlbrightLabs-Assistant/1.0"


# --- errors -----------------------------------------------------------------------------------------------------------

MESSAGES = {
    "invalid_key": "{provider} didn't accept your API key. Check that it's copied in full (or make a new one) and connect "
                   "again on Your AI assistant.",
    "billing": "{provider} says there are no credits or no billing set up on your account. Add a payment method or "
               "credits on their billing page, then try again.",
    "rate_limited": "{provider} is limiting how fast you can send requests. Wait a minute and try again.",
    "model_not_found": "{provider} doesn't offer the model \"{model}\" to your key. Pick another model on Your AI "
                       "assistant.",
    "timeout": "{provider} took too long to answer. Try again, or ask for something smaller.",
    "unavailable": "{provider} is busy or down right now. Try again in a few minutes.",
    "permission": "{provider} says this key isn't allowed to do that. Check the key's permissions (or workspace) on "
                  "their site.",
    "network": "Couldn't reach {provider}. Try again in a minute.",
    "bad_request": "{provider} couldn't handle that request{detail}.",
    "blocked": "{provider} declined to answer that (its safety filter).",
}


class ProviderError(Exception):
    def __init__(self, kind, provider_label, model="", detail="", status=None):
        self.kind, self.status = kind, status
        text = MESSAGES.get(kind, MESSAGES["bad_request"])
        self.detail = detail
        super().__init__(text.format(provider=provider_label, model=model,
                                     detail=f": {detail}" if detail and kind == "bad_request" else ""))

    @property
    def message(self):
        return str(self)


def _short(text, limit=200):
    """A provider's error message, trimmed, with anything key-like masked - for the person, never logged raw."""
    text = re.sub(r"(sk-[A-Za-z0-9_\-]{6})[A-Za-z0-9_\-]+|(xai-[A-Za-z0-9]{4})[A-Za-z0-9]+|(AIza[0-9A-Za-z_\-]{4})"
                  r"[0-9A-Za-z_\-]+", lambda m: (m.group(1) or m.group(2) or m.group(3)) + "…", str(text or ""))
    text = " ".join(text.split())
    return text[:limit] + ("…" if len(text) > limit else "")


def _body(response):
    try:
        return response.json()
    except ValueError:
        return {}


def _error_text(body):
    err = body.get("error") if isinstance(body, dict) else None
    if isinstance(err, dict):
        return str(err.get("message") or ""), str(err.get("code") or err.get("type") or err.get("status") or "")
    if isinstance(err, str):
        return err, ""
    return str(body.get("message") or "") if isinstance(body, dict) else "", ""


BILLING_WORDS = ("credit balance", "insufficient_quota", "credit_balance_exhausted", "spend_limit", "spend limit",
                 "billing", "insufficient credits", "payment", "quota exceeded", "exceeded your current quota",
                 "usage_limit_exceeded", "prepay")


def classify(status, body, label, model=""):
    """An HTTP error -> ProviderError, the same way for every provider (their docs' codes)."""
    text, code = _error_text(body)
    low = f"{text} {code}".lower()
    if status == 401 or "api_key_invalid" in low or "invalid api key" in low or "incorrect api key" in low \
            or "api key not valid" in low:
        return ProviderError("invalid_key", label, model, status=status)
    if status == 402 or any(word in low for word in BILLING_WORDS) or "failed_precondition" in low:
        return ProviderError("billing", label, model, status=status)
    if status == 404 or ("model" in low and ("not found" in low or "does not exist" in low or "not_found" in low)):
        return ProviderError("model_not_found", label, model, status=status)
    if status == 429:
        return ProviderError("rate_limited", label, model, status=status)
    if status in (408, 504) or "deadline" in low or "timeout" in low:
        return ProviderError("timeout", label, model, status=status)
    if status == 403:
        return ProviderError("permission", label, model, detail=_short(text), status=status)
    if status in (500, 502, 503, 529) or (status or 0) >= 500:
        return ProviderError("unavailable", label, model, status=status)
    return ProviderError("bad_request", label, model, detail=_short(text) or f"HTTP {status}", status=status)


# --- the shared shapes -------------------------------------------------------------------------------------------------

@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass
class Reply:
    text: str = ""
    tool_calls: list = field(default_factory=list)
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: Decimal = None          # when the provider reports it (OpenRouter, xAI)
    native: object = None             # provider blocks to send back as-is next time


def _args(raw):
    if isinstance(raw, dict):
        return raw
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {"_unparsed": str(raw)[:2000]}
    return value if isinstance(value, dict) else {"value": value}


def _version_key(model_id):
    """Sort key: the numbers in a model id (gemini-3.5-flash > gemini-2.5-flash)."""
    return [float(n) for n in re.findall(r"\d+(?:\.\d+)?", model_id)]


class Provider:
    key = label = ""
    host = ""                       # the API host (Admin > AI assistant "Check hosts", the manage.py check)
    key_prefix = ""                 # what keys start with - a hint only, never enforced
    links = {}                      # the setup tab's links (verified against the provider's docs)

    def __init__(self, session=None):
        self.http = session or requests

    # -- HTTP -----------------------------------------------------------------------------------------------------
    def _request(self, method, url, api_key, model="", timeout=DEFAULT_TIMEOUT, **kwargs):
        headers = {"User-Agent": USER_AGENT, **self.headers(api_key), **kwargs.pop("headers", {})}
        try:
            response = self.http.request(method, url, headers=headers, timeout=timeout, **kwargs)
        except requests.Timeout:
            raise ProviderError("timeout", self.label, model) from None
        except requests.RequestException:
            raise ProviderError("network", self.label, model) from None
        body = _body(response)
        if response.status_code >= 400:
            raise classify(response.status_code, body, self.label, model)
        if isinstance(body, dict) and body.get("error") and not body.get("choices") and not body.get("candidates"):
            err = body["error"]                       # OpenRouter can report a failure inside a 200
            status = err.get("code") if isinstance(err, dict) and isinstance(err.get("code"), int) else 400
            raise classify(status, body, self.label, model)
        return body

    def headers(self, api_key):
        return {"Authorization": f"Bearer {api_key}"}

    # -- the interface --------------------------------------------------------------------------------------------
    def list_models(self, api_key):
        raise NotImplementedError

    def default_model(self, models):
        """The newest model that looks like a fast, inexpensive all-rounder ("mini" / "flash" / "sonnet" ...), else the
        newest. No model names are hard-coded: it's picked from the provider's own list."""
        if not models:
            return ""
        preferred = [m for m in models if any(w in m["id"].lower() for w in self.cheap_words)
                     and not any(w in m["id"].lower() for w in self.avoid_words)]
        pool = preferred or models
        return max(pool, key=lambda m: (m.get("created") or 0, _version_key(m["id"])))["id"]

    cheap_words = ("mini",)
    avoid_words = ("nano", "preview", "audio", "realtime", "search", "transcribe", "tts", "image")

    def chat(self, api_key, model, system, messages, tools, timeout=DEFAULT_TIMEOUT):
        raise NotImplementedError


# --- OpenAI (and OpenRouter): Chat Completions ------------------------------------------------------------------------

NOT_CHAT = ("embedding", "whisper", "tts", "dall-e", "image", "audio", "realtime", "transcribe", "moderation",
            "search", "davinci", "babbage", "codex", "computer-use", "sora", "instruct")


class OpenAIChat(Provider):
    key, label, host = "openai", "OpenAI", "api.openai.com"
    base = "https://api.openai.com/v1"
    key_prefix = "sk-"
    links = {"signup": "https://platform.openai.com/signup", "billing": "https://platform.openai.com/settings/organization/billing",
             "limits": "https://platform.openai.com/settings/organization/limits", "keys": "https://platform.openai.com/api-keys"}

    def list_models(self, api_key):
        body = self._request("GET", f"{self.base}/models", api_key, timeout=LIST_TIMEOUT)
        out = []
        for row in body.get("data") or []:
            mid = str(row.get("id") or "")
            if mid and (mid.startswith("gpt-") or re.match(r"^o\d", mid) or mid.startswith("chatgpt-")) \
                    and not any(w in mid for w in NOT_CHAT):
                out.append({"id": mid, "label": mid, "created": row.get("created") or 0})
        return sorted(out, key=lambda m: -m["created"])

    def to_native(self, system, messages):
        out = [{"role": "system", "content": system}]
        for m in messages:
            if m["role"] == "user":
                out.append({"role": "user", "content": m["text"]})
            elif m["role"] == "assistant":
                row = {"role": "assistant", "content": m.get("text") or None}
                if m.get("tool_calls"):
                    row["tool_calls"] = [{"id": c["id"], "type": "function",
                                          "function": {"name": c["name"], "arguments": json.dumps(c["arguments"])}}
                                         for c in m["tool_calls"]]
                out.append(row)
            elif m["role"] == "tool":
                out.append({"role": "tool", "tool_call_id": m["call_id"], "content": m["text"]})
        return out

    def chat(self, api_key, model, system, messages, tools, timeout=DEFAULT_TIMEOUT):
        payload = {"model": model, "messages": self.to_native(system, messages)}
        if tools:
            payload["tools"] = [{"type": "function", "function": {"name": t["name"], "description": t["description"],
                                                                  "parameters": t["parameters"]}} for t in tools]
            payload["tool_choice"] = "auto"
        payload.update(self.extra_payload())
        body = self._request("POST", f"{self.base}/chat/completions", api_key, model=model, timeout=timeout,
                             json=payload)
        choices = body.get("choices") or []
        if not choices:
            raise ProviderError("bad_request", self.label, model, detail="no answer came back")
        message = choices[0].get("message") or {}
        calls = [ToolCall(c.get("id") or f"call_{n}", (c.get("function") or {}).get("name", ""),
                          _args((c.get("function") or {}).get("arguments")))
                 for n, c in enumerate(message.get("tool_calls") or []) if c.get("type", "function") == "function"]
        usage = body.get("usage") or {}
        cost = usage.get("cost")
        return Reply(text=message.get("content") or "", tool_calls=calls, tokens_in=int(usage.get("prompt_tokens") or 0),
                     tokens_out=int(usage.get("completion_tokens") or 0),
                     cost_usd=Decimal(str(cost)) if isinstance(cost, (int, float)) else None)

    def extra_payload(self):
        return {}


class OpenRouter(OpenAIChat):
    key, label, host = "openrouter", "OpenRouter", "openrouter.ai"
    base = "https://openrouter.ai/api/v1"
    key_prefix = "sk-or-"
    links = {"signup": "https://openrouter.ai/", "billing": "https://openrouter.ai/settings/credits",
             "limits": "https://openrouter.ai/settings/keys", "keys": "https://openrouter.ai/settings/keys"}
    cheap_words = ("mini", "flash", "haiku")
    avoid_words = ("nano", "preview", "audio", "image", ":free", "lite")

    def list_models(self, api_key):
        self._request("GET", f"{self.base}/key", api_key, timeout=LIST_TIMEOUT)          # the key itself
        body = self._request("GET", f"{self.base}/models", api_key, timeout=LIST_TIMEOUT,
                             params={"supported_parameters": "tools"})
        out = []
        for row in body.get("data") or []:
            mid = str(row.get("id") or "")
            if not mid:
                continue
            price = row.get("pricing") or {}
            out.append({"id": mid, "label": row.get("name") or mid, "created": row.get("created") or 0,
                        "price_in": _per_million(price.get("prompt")), "price_out": _per_million(price.get("completion"))})
        return sorted(out, key=lambda m: -m["created"])

    def default_model(self, models):
        # The newest of the big providers' inexpensive tool-using models; OpenRouter lists hundreds.
        known = [m for m in models if m["id"].split("/")[0] in ("openai", "anthropic", "google", "x-ai")]
        return super().default_model(known or models)

    def extra_payload(self):
        return {"usage": {"include": True}}        # OpenRouter reports the call's cost in usage.cost


def _per_million(per_token):
    try:
        value = Decimal(str(per_token))
    except Exception:  # noqa: BLE001 - "-1" (varies) or missing
        return None
    return str((value * 1_000_000).quantize(Decimal("0.0001"))) if value >= 0 else None


# --- Anthropic: Messages ------------------------------------------------------------------------------------------------

ANTHROPIC_VERSION = "2026-10-21"
ANTHROPIC_MAX_TOKENS = 8192


class Anthropic(Provider):
    key, label, host = "anthropic", "Anthropic (Claude)", "api.anthropic.com"
    base = "https://api.anthropic.com/v1"
    key_prefix = "sk-ant-"
    links = {"signup": "https://platform.claude.com/", "billing": "https://platform.claude.com/settings/billing",
             "limits": "https://platform.claude.com/settings/spend-limits", "keys": "https://platform.claude.com/settings/keys"}
    cheap_words = ("sonnet",)
    avoid_words = ()

    def headers(self, api_key):
        return {"Authorization": f"Bearer {api_key}", "anthropic-version": ANTHROPIC_VERSION}

    def list_models(self, api_key):
        body = self._request("GET", f"{self.base}/models", api_key, timeout=LIST_TIMEOUT, params={"limit": 1000})
        out = []
        for row in body.get("data") or []:
            if row.get("lifecycle", "active") != "active" or not row.get("id"):
                continue
            out.append({"id": row["id"], "label": row.get("display_name") or row["id"], "line": row.get("line") or "",
                        "created": row.get("created_at") or ""})
        return sorted(out, key=lambda m: m["created"], reverse=True)

    def default_model(self, models):
        sonnets = [m for m in models if m.get("line") == "sonnet"] or \
                  [m for m in models if "sonnet" in m["id"]]
        return (sonnets or models)[0]["id"] if models else ""          # the list is newest first

    def to_native(self, messages):
        out = []

        def add(role, blocks):
            if out and out[-1]["role"] == role:
                out[-1]["content"].extend(blocks)
            else:
                out.append({"role": role, "content": list(blocks)})

        for m in messages:
            if m["role"] == "user":
                add("user", [{"type": "text", "text": m["text"]}])
            elif m["role"] == "assistant":
                native = m.get("native")
                if m.get("provider") == self.key and isinstance(native, list) and native:
                    add("assistant", native)
                else:
                    blocks = [{"type": "text", "text": m["text"]}] if m.get("text") else []
                    blocks += [{"type": "tool_use", "id": c["id"], "name": c["name"], "input": c["arguments"]}
                               for c in m.get("tool_calls") or []]
                    if blocks:
                        add("assistant", blocks)
            elif m["role"] == "tool":
                add("user", [{"type": "tool_result", "tool_use_id": m["call_id"], "content": m["text"],
                              "is_error": bool(m.get("is_error"))}])
        return out

    def chat(self, api_key, model, system, messages, tools, timeout=DEFAULT_TIMEOUT):
        payload = {"model": model, "max_tokens": ANTHROPIC_MAX_TOKENS, "system": system,
                   "messages": self.to_native(messages)}
        if tools:
            payload["tools"] = [{"name": t["name"], "description": t["description"], "input_schema": t["parameters"]}
                                for t in tools]
        body = self._request("POST", f"{self.base}/messages", api_key, model=model, timeout=timeout, json=payload)
        content = body.get("content") or []
        text = "\n\n".join(b.get("text", "") for b in content if b.get("type") == "text").strip()
        calls = [ToolCall(b.get("id"), b.get("name"), b.get("input") or {}) for b in content if b.get("type") == "tool_use"]
        usage = body.get("usage") or {}
        return Reply(text=text, tool_calls=calls, tokens_in=int(usage.get("input_tokens") or 0)
                     + int(usage.get("cache_read_input_tokens") or 0) + int(usage.get("cache_creation_input_tokens") or 0),
                     tokens_out=int(usage.get("output_tokens") or 0), native=content)


# --- xAI: Responses -----------------------------------------------------------------------------------------------------

class XAI(Provider):
    key, label, host = "xai", "xAI (Grok)", "api.x.ai"
    base = "https://api.x.ai/v1"
    key_prefix = "xai-"
    links = {"signup": "https://console.x.ai/", "billing": "https://console.x.ai/team/default/billing",
             "limits": "https://console.x.ai/team/default/billing", "keys": "https://console.x.ai/"}
    cheap_words = ("fast", "mini")
    avoid_words = ("image", "vision", "video", "imagine")

    def list_models(self, api_key):
        try:
            body = self._request("GET", f"{self.base}/language-models", api_key, timeout=LIST_TIMEOUT)
            rows = body.get("models") or []
        except ProviderError as exc:
            if exc.kind not in ("model_not_found", "bad_request"):
                raise
            rows = self._request("GET", f"{self.base}/models", api_key, timeout=LIST_TIMEOUT).get("data") or []
        out = []
        for row in rows:
            mid = str(row.get("id") or "")
            outputs = row.get("output_modalities") or ["text"]
            if mid and "text" in outputs and not any(w in mid for w in ("image", "video", "imagine")):
                out.append({"id": mid, "label": mid, "created": row.get("created") or 0})
        return sorted(out, key=lambda m: -m["created"])

    def to_native(self, messages):
        items = []
        for m in messages:
            if m["role"] == "user":
                items.append({"role": "user", "content": m["text"]})
            elif m["role"] == "assistant":
                if m.get("text"):
                    items.append({"role": "assistant", "content": m["text"]})
                items += [{"type": "function_call", "call_id": c["id"], "name": c["name"],
                           "arguments": json.dumps(c["arguments"])} for c in m.get("tool_calls") or []]
            elif m["role"] == "tool":
                items.append({"type": "function_call_output", "call_id": m["call_id"], "output": m["text"]})
        return items

    def chat(self, api_key, model, system, messages, tools, timeout=DEFAULT_TIMEOUT):
        payload = {"model": model, "instructions": system, "input": self.to_native(messages), "store": False}
        if tools:
            payload["tools"] = [{"type": "function", "name": t["name"], "description": t["description"],
                                 "parameters": t["parameters"]} for t in tools]
        body = self._request("POST", f"{self.base}/responses", api_key, model=model, timeout=timeout, json=payload)
        texts, calls = [], []
        for item in body.get("output") or []:
            if item.get("type") == "message":
                texts += [c.get("text", "") for c in item.get("content") or [] if c.get("type") == "output_text"]
            elif item.get("type") == "function_call":
                calls.append(ToolCall(item.get("call_id") or item.get("id"), item.get("name", ""), _args(item.get("arguments"))))
        usage = body.get("usage") or {}
        nano = usage.get("cost_in_nano_usd")
        return Reply(text="\n\n".join(t for t in texts if t).strip(), tool_calls=calls,
                     tokens_in=int(usage.get("input_tokens") or 0), tokens_out=int(usage.get("output_tokens") or 0),
                     cost_usd=(Decimal(str(nano)) / Decimal(1_000_000_000)) if isinstance(nano, (int, float)) else None)


# --- Google: Gemini generateContent ------------------------------------------------------------------------------------

GEMINI_SCHEMA_KEYS = {"type", "format", "title", "description", "nullable", "enum", "maxItems", "minItems", "properties",
                      "required", "minProperties", "maxProperties", "minLength", "maxLength", "pattern", "example",
                      "anyOf", "propertyOrdering", "default", "items", "minimum", "maximum"}


def gemini_schema(schema):
    """JSON Schema -> the subset Gemini's function declarations take (their Schema object): unknown keys dropped,
    a list of types becomes anyOf, string formats other than enum/date-time dropped."""
    if isinstance(schema, list):
        return [gemini_schema(s) for s in schema]
    if not isinstance(schema, dict):
        return schema
    out = {}
    for key, value in schema.items():
        if key not in GEMINI_SCHEMA_KEYS:
            continue
        if key == "properties" and isinstance(value, dict):
            out[key] = {name: gemini_schema(sub) for name, sub in value.items()}
        elif key in ("items",):
            out[key] = gemini_schema(value)
        elif key == "anyOf":
            out[key] = [gemini_schema(s) for s in value]
        elif key == "type" and isinstance(value, list):
            out["anyOf"] = [{"type": t} for t in value if t != "null"]
        elif key == "format" and value not in ("enum", "date-time"):
            continue
        else:
            out[key] = value
    if "required" in out and isinstance(out.get("properties"), dict):
        out["required"] = [r for r in out["required"] if r in out["properties"]]
    return out


class Gemini(Provider):
    key, label, host = "gemini", "Google (Gemini)", "generativelanguage.googleapis.com"
    base = "https://generativelanguage.googleapis.com/v1beta"
    key_prefix = "AIza"
    links = {"signup": "https://aistudio.google.com/", "billing": "https://aistudio.google.com/billing",
             "limits": "https://aistudio.google.com/spend", "keys": "https://aistudio.google.com/api-keys"}
    cheap_words = ("flash",)
    avoid_words = ("lite", "image", "tts", "live", "audio", "embedding", "exp", "preview", "thinking")

    def headers(self, api_key):
        return {"x-goog-api-key": api_key}

    def list_models(self, api_key):
        body = self._request("GET", f"{self.base}/models", api_key, timeout=LIST_TIMEOUT, params={"pageSize": 1000})
        out = []
        for row in body.get("models") or []:
            name = str(row.get("name") or "")
            mid = name.split("/", 1)[1] if name.startswith("models/") else name
            if not mid or "generateContent" not in (row.get("supportedGenerationMethods") or []):
                continue
            if any(w in mid for w in ("embedding", "aqa", "image", "tts", "live", "audio", "robotics", "veo", "imagen")):
                continue
            out.append({"id": mid, "label": row.get("displayName") or mid, "created": 0})
        return sorted(out, key=lambda m: _version_key(m["id"]), reverse=True)

    def default_model(self, models):
        if not models:
            return ""
        flash = [m for m in models if "flash" in m["id"] and not any(w in m["id"] for w in self.avoid_words)]
        pool = flash or [m for m in models if "flash" in m["id"]] or models
        return max(pool, key=lambda m: _version_key(m["id"]))["id"]

    def to_native(self, messages):
        contents = []

        def add(role, parts):
            if contents and contents[-1]["role"] == role == "user":
                contents[-1]["parts"].extend(parts)        # every response to one turn's calls together (and a
                #                                            message sent after a stopped run joins them)
            else:
                contents.append({"role": role, "parts": list(parts)})

        for m in messages:
            if m["role"] == "user":
                add("user", [{"text": m["text"]}])
            elif m["role"] == "assistant":
                native = m.get("native")
                if m.get("provider") == self.key and isinstance(native, list) and native:
                    add("model", native)
                else:
                    parts = [{"text": m["text"]}] if m.get("text") else []
                    # Calls another provider made (the person switched): Gemini's documented placeholder signature,
                    # on the first call only, like a real one.
                    for n, c in enumerate(m.get("tool_calls") or []):
                        part = {"functionCall": {"name": c["name"], "args": c["arguments"], "id": c["id"]}}
                        if n == 0:
                            part["thoughtSignature"] = "skip_thought_signature_validator"
                        parts.append(part)
                    if parts:
                        add("model", parts)
            elif m["role"] == "tool":
                try:
                    result = json.loads(m["text"])
                except (TypeError, ValueError):
                    result = m["text"]
                add("user", [{"functionResponse": {"name": m["name"], "id": m["call_id"],
                                                   "response": {"result": result} if not isinstance(result, dict) else result}}])
        return contents

    def chat(self, api_key, model, system, messages, tools, timeout=DEFAULT_TIMEOUT):
        payload = {"systemInstruction": {"parts": [{"text": system}]}, "contents": self.to_native(messages)}
        if tools:
            payload["tools"] = [{"functionDeclarations": [{"name": t["name"], "description": t["description"],
                                                           "parameters": gemini_schema(t["parameters"])} for t in tools]}]
        body = self._request("POST", f"{self.base}/models/{model}:generateContent", api_key, model=model,
                             timeout=timeout, json=payload)
        if (body.get("promptFeedback") or {}).get("blockReason"):
            raise ProviderError("blocked", self.label, model)
        candidates = body.get("candidates") or []
        if not candidates:
            raise ProviderError("bad_request", self.label, model, detail="no answer came back")
        parts = (candidates[0].get("content") or {}).get("parts") or []
        texts, calls = [], []
        for n, part in enumerate(parts):
            if "functionCall" in part:
                fc = part["functionCall"]
                fc_id = fc.get("id") or f"call_{n}"
                fc["id"] = fc_id                              # kept in the native parts too, to match the response
                calls.append(ToolCall(fc_id, fc.get("name", ""), fc.get("args") or {}))
            elif part.get("text") and not part.get("thought"):
                texts.append(part["text"])
        if not texts and not calls and candidates[0].get("finishReason") in ("SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST"):
            raise ProviderError("blocked", self.label, model)
        usage = body.get("usageMetadata") or {}
        return Reply(text="".join(texts).strip(), tool_calls=calls, tokens_in=int(usage.get("promptTokenCount") or 0),
                     tokens_out=int(usage.get("candidatesTokenCount") or 0) + int(usage.get("thoughtsTokenCount") or 0),
                     native=parts)


PROVIDERS = [OpenAIChat(), Anthropic(), XAI(), Gemini(), OpenRouter()]
BY_KEY = {p.key: p for p in PROVIDERS}


def get(key):
    return BY_KEY[key]
