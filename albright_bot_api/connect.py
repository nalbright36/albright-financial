""""Connect your AI": this site's real addresses for the API, its guide, the OpenAPI spec and the MCP server, and the
instructions block a person pastes into their AI (My account > API keys, and Fitness > Food > Log food with your AI).

None of it ever holds a key: the person puts their key in the AI's secret / connection settings - or, for AIs that
connect over MCP with OAuth (Claude, ChatGPT, Grok, Gemini), approves the connection on this site and never sees one.
"""
from django.conf import settings
from django.urls import reverse


def site_origin(request):
    """https://<site> - https in production (the API needs it; behind the host's proxy the request itself can look
    like http)."""
    origin = request.build_absolute_uri("/").rstrip("/")
    if not settings.DEBUG and origin.startswith("http://"):
        origin = "https://" + origin[len("http://"):]
    return origin


def api_base(request):
    """https://<site>/api/bot/v1 - every endpoint hangs off it."""
    return site_origin(request) + reverse("bot_api_v1:docs").rsplit("/", 1)[0]


def urls(request):
    base = api_base(request)
    return {"base": base, "docs": base + "/docs", "openapi": base + "/openapi.json", "mcp": base + "/mcp",
            "me": base + "/me"}


def instructions(base):
    """The same text on every tab - generated with the real site address, never a key."""
    return f"""You can read and update my Albright Labs account through its API at {base}. Read {base}/docs once at the start to learn the endpoints, and call /me to see what you're allowed to do.
Rules:
- Act only on what I ask. Before deleting anything or changing more than 5 items at once, tell me what you'll do and wait for my OK.
- When adding items, send an idempotency_key so retries don't create duplicates.
- After every change, tell me in one or two lines what you did.
- If a request fails, read the error message, fix the request once, and if it still fails, tell me what went wrong.
- Never share anything from my account with anyone else.
- For food logging: estimate macros per item, check foods/search for known foods first, log everything in one request, then tell me my new day totals.
"""
