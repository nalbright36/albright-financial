"""The only module that talks to an AI provider. Swap providers by rewriting
complete() — callers only use complete(), Result and ProviderError.

Anthropic Messages API over plain HTTPS with `requests` (already a project
dependency). The key is read from the ANTHROPIC_API_KEY environment variable
on the server and is never logged, returned, or sent to the browser."""
import os
from dataclasses import dataclass, field
from decimal import Decimal

import requests
from django.conf import settings

API_URL = "https://api.anthropic.com/v1/messages"

DEFAULTS = {
    "MODEL": "claude-haiku-5-5",
    "ANTHROPIC_VERSION": "2023-06-01",
    "EFFORT": "low",               # Haiku 5.5: low effort = fast, cheap chat answers
    "DISABLE_THINKING": True,      # no hidden thinking tokens billed as output
    # USD per million tokens (Haiku 5.5, prompts <= 100k tokens). Update if pricing changes.
    "PRICE_INPUT_PER_MTOK": "0.10",
    "PRICE_OUTPUT_PER_MTOK": "0.50",
    "PRICE_CACHE_WRITE_PER_MTOK": "0.125",
    "PRICE_CACHE_READ_PER_MTOK": "0.01",
    "TIMEOUT_SECONDS": 30,
}


class ProviderError(Exception):
    """Safe-to-log message; never contains the key or the prompt."""


@dataclass
class Result:
    text: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0
    cost_usd: Decimal = field(default_factory=lambda: Decimal("0"))


def config():
    cfg = dict(DEFAULTS)
    cfg.update(getattr(settings, "RESUME_AI", {}) or {})
    return cfg


def api_key_configured():
    return bool(os.environ.get("ANTHROPIC_API_KEY", "").strip())


def cost_for(input_tokens, output_tokens, cache_write_tokens=0, cache_read_tokens=0, cfg=None):
    cfg = cfg or config()
    total = (Decimal(input_tokens) * Decimal(cfg["PRICE_INPUT_PER_MTOK"])
             + Decimal(output_tokens) * Decimal(cfg["PRICE_OUTPUT_PER_MTOK"])
             + Decimal(cache_write_tokens) * Decimal(cfg["PRICE_CACHE_WRITE_PER_MTOK"])
             + Decimal(cache_read_tokens) * Decimal(cfg["PRICE_CACHE_READ_PER_MTOK"]))
    return (total / Decimal(1_000_000)).quantize(Decimal("0.000001"))


def _post(body, cfg, key):
    return requests.post(
        API_URL,
        headers={"x-api-key": key, "anthropic-version": cfg["ANTHROPIC_VERSION"], "content-type": "application/json"},
        json=body,
        timeout=cfg["TIMEOUT_SECONDS"],
    )


def complete(system, messages, max_tokens=400, effort=None):
    """system: str. messages: [{"role": "user"|"assistant", "content": str}].
    Returns Result or raises ProviderError."""
    cfg = config()
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key:
        raise ProviderError("ANTHROPIC_API_KEY is not set")
    body = {
        "model": cfg["MODEL"],
        "max_tokens": int(max_tokens),
        # cache_control: the long, unchanging system prompt (rules + knowledge) is
        # billed at the cheap cache-read rate on repeat questions.
        "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        "messages": messages,
    }
    if effort or cfg.get("EFFORT"):
        body["output_config"] = {"effort": effort or cfg["EFFORT"]}
    if cfg.get("DISABLE_THINKING"):
        body["thinking"] = {"type": "disabled"}
    try:
        resp = _post(body, cfg, key)
        if resp.status_code == 400 and ("output_config" in body or "thinking" in body):
            # Older/other models may reject these knobs: retry once without them.
            body.pop("output_config", None)
            body.pop("thinking", None)
            resp = _post(body, cfg, key)
    except requests.RequestException as exc:
        raise ProviderError(f"network error: {type(exc).__name__}") from None
    if resp.status_code != 200:
        err_type = ""
        try:
            err_type = (resp.json().get("error") or {}).get("type", "")
        except ValueError:
            pass
        raise ProviderError(f"HTTP {resp.status_code} {err_type}".strip())
    try:
        data = resp.json()
    except ValueError:
        raise ProviderError("invalid JSON from provider") from None
    text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text").strip()
    usage = data.get("usage") or {}
    result = Result(
        text=text,
        model=data.get("model") or cfg["MODEL"],
        input_tokens=int(usage.get("input_tokens") or 0),
        output_tokens=int(usage.get("output_tokens") or 0),
        cache_write_tokens=int(usage.get("cache_creation_input_tokens") or 0),
        cache_read_tokens=int(usage.get("cache_read_input_tokens") or 0),
    )
    result.cost_usd = cost_for(result.input_tokens, result.output_tokens, result.cache_write_tokens,
                               result.cache_read_tokens, cfg)
    return result
