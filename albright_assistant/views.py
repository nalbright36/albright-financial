"""Your AI assistant (/account/assistant/ - connecting a provider) and the chat's JSON endpoints (/assistant/...) that
static/assistant/assistant.js calls. Everything is 404 for someone without "Can use AI assistant" (access.allowed).
The key is only ever read from the Connect form's POST: no view, page or JSON answer contains it - only its last 4
characters - and it's never logged (errors carry ProviderError's plain message, which masks anything key-like).
"""
import json
import logging

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST

from . import access, photos, providers, runner, tools
from .models import Action, Chat, Connection, Message, Run
from .render import href, markdown_html

log = logging.getLogger(__name__)
MAX_TEXT = 4000
MAX_KEY = 400


def assistant_required(view):
    @login_required
    def wrapper(request, *args, **kwargs):
        if not access.allowed(request.user):
            raise Http404
        return view(request, *args, **kwargs)
    wrapper.__name__, wrapper.__doc__ = view.__name__, view.__doc__
    return wrapper


# --- setup page --------------------------------------------------------------------------------------------------------

def _setup_context(request, **extra):
    conn = access.connection(request.user)
    selected = extra.get("selected") or request.GET.get("provider") or (conn.provider if conn else "")
    if selected not in providers.BY_KEY:
        selected = providers.PROVIDERS[0].key
    extra["selected"] = selected
    provider = providers.get(selected)
    links = {name: href(url) for name, url in provider.links.items()}
    return {"provider": provider, "links": links, "models": conn.models_cache if conn else [], "conn": conn, "providers": providers.PROVIDERS, "limits": {"daily": conn.daily_limit if conn else 200,
                                                                         "steps": conn.max_steps if conn else 8},
            "today_count": runner.messages_today(request.user) if conn else 0, **extra}


@login_required
def leave(request):
    """The warning page an unverified external link (the setup steps, the assistant's replies) goes through."""
    from albright_labs.links import is_web_url, link_status
    url = request.GET.get("to", "")
    domain, status = link_status(url)
    if status == "allowlisted":
        return redirect(url)
    return render(request, "albright_assistant/leave.html",
                  {"url": url if is_web_url(url) else "", "domain": domain, "status": status})


@never_cache
@assistant_required
def setup(request):
    return render(request, "albright_assistant/setup.html", _setup_context(request))


@require_POST
@assistant_required
def connect(request):
    provider_key = request.POST.get("provider", "")
    raw = (request.POST.get("api_key") or "").strip()
    if provider_key not in providers.BY_KEY:
        messages.error(request, "Pick a provider.")
        return redirect("assistant:setup")
    if not raw or len(raw) > MAX_KEY or any(ch.isspace() for ch in raw):
        return render(request, "albright_assistant/setup.html",
                      _setup_context(request, selected=provider_key, key_error="Paste the whole key - it has no spaces."),
                      status=400)
    provider = providers.get(provider_key)
    try:
        models = provider.list_models(raw)              # the cheapest call there is: proves the key works
    except providers.ProviderError as exc:
        log.info("assistant connect failed for user %s: %s (%s)", request.user.pk, provider_key, exc.kind)
        return render(request, "albright_assistant/setup.html",
                      _setup_context(request, selected=provider_key, key_error=exc.message), status=400)
    if not models:
        return render(request, "albright_assistant/setup.html",
                      _setup_context(request, selected=provider_key,
                                     key_error=f"{provider.label} accepted the key but listed no chat models for it."),
                      status=400)
    conn = access.connection(request.user) or Connection(user=request.user)
    if conn.pk and conn.provider != provider_key:
        conn.price_in_per_m = conn.price_out_per_m = None
    conn.provider, conn.key_encrypted, conn.key_last4 = provider_key, access.encrypt(raw), access.last4(raw)
    conn.models_cache, conn.models_fetched_at = models, timezone.now()
    conn.model = provider.default_model(models)
    conn.connected_at, conn.last_error_kind, conn.last_error_at = timezone.now(), "", None
    conn.save()
    access.assistant_token(request.user)
    messages.success(request, f"Connected to {provider.label}. The model is set to {conn.model_label} - change it below "
                              "if you like.")
    return redirect("assistant:setup")


def _int(value, low, high, default):
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def _price(value):
    from decimal import Decimal, InvalidOperation
    value = (value or "").strip().lstrip("$")
    if not value:
        return None
    try:
        price = Decimal(value)
    except InvalidOperation:
        return None
    return price if 0 <= price <= 10000 else None


@require_POST
@assistant_required
def save_settings(request):
    conn = access.connection(request.user)
    if conn is None:
        return redirect("assistant:setup")
    model = request.POST.get("model", "")
    if model and any(m.get("id") == model for m in conn.models_cache or []):
        conn.model = model
    conn.daily_limit = _int(request.POST.get("daily_limit"), 1, 2000, conn.daily_limit)
    conn.max_steps = _int(request.POST.get("max_steps"), 1, 20, conn.max_steps)
    conn.price_in_per_m = _price(request.POST.get("price_in"))
    conn.price_out_per_m = _price(request.POST.get("price_out"))
    conn.save()
    messages.success(request, "Saved.")
    return redirect("assistant:setup")


@require_POST
@assistant_required
def refresh_models(request):
    conn = access.connection(request.user)
    if conn is None:
        return redirect("assistant:setup")
    provider = providers.get(conn.provider)
    try:
        models = provider.list_models(access.decrypt(conn.key_encrypted))
    except providers.ProviderError as exc:
        messages.error(request, exc.message)
        return redirect("assistant:setup")
    conn.models_cache, conn.models_fetched_at = models, timezone.now()
    if not any(m["id"] == conn.model for m in models):
        conn.model = provider.default_model(models)
    conn.save()
    messages.success(request, f"Updated the model list ({len(models)} models).")
    return redirect("assistant:setup")


@require_POST
@assistant_required
def disconnect(request):
    conn = access.connection(request.user)
    if conn is not None:
        label = conn.provider_label
        if conn.token_id:
            conn.token.revoke()
        conn.delete()                       # the encrypted key goes with it
        messages.success(request, f"Disconnected {label}. Your key is deleted from this site; to stop it working "
                                  "everywhere, delete it on the provider's site too.")
    return redirect("assistant:setup")


# --- the chat (JSON) ---------------------------------------------------------------------------------------------------

def _json_body(request):
    try:
        data = json.loads(request.body or b"{}")
    except (ValueError, UnicodeDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _header(user, chat=None):
    conn = access.connection(user)
    out = {"connected": bool(conn and not conn.disabled), "provider": conn.provider_label if conn else "",
           "model": conn.model_label if conn else "", "setup_url": reverse("assistant:setup"),
           "vision": bool(conn and providers.supports_images(conn.provider, conn.model, conn.models_cache))}
    if chat is not None:
        out.update({"chat": chat.pk, "title": chat.title or "New chat", "tokens_in": chat.tokens_in,
                    "tokens_out": chat.tokens_out,
                    "cost": f"{chat.cost_usd:.4f}" if chat.cost_usd is not None else None})
    return out


def _action_json(action):
    return {"id": action.pk, "summary": action.summary, "app": action.app, "can_undo": action.can_undo,
            "undone": action.undone_at is not None,
            "undo_until": action.undo_until.isoformat() if action.undo_until else None}


def _message_json(m, actions, pictures=None):
    out = {"id": m.pk, "role": m.role}
    if m.role == Message.USER:
        out["text"] = m.text
        out["images"] = [photos.describe(a) for a in (pictures or {}).get(m.pk, [])]
    elif m.role == Message.ASSISTANT:
        out["html"] = markdown_html(m.text) if m.text else ""
        out["thinking"] = bool(m.data.get("tool_calls")) and not m.text
    elif m.role == Message.TOOL:
        out["actions"] = [_action_json(a) for a in actions.get(m.pk, [])]
        out["error"] = bool(m.data.get("is_error"))
    elif m.role == Message.NOTICE:
        out["text"], out["kind"] = m.text, m.data.get("kind", "")
    return out


def _messages(chat, after=0):
    rows = list(chat.messages.filter(pk__gt=after))
    actions = {}
    for a in Action.objects.filter(chat=chat, message_id__in=[m.pk for m in rows]):
        actions.setdefault(a.message_id, []).append(a)
    pictures = photos.of_messages([m for m in rows if m.role == Message.USER and m.data.get("photos")])
    return [_message_json(m, actions, pictures) for m in rows
            if m.role != Message.TOOL or actions.get(m.pk) or m.data.get("is_error")]


def _run_json(run):
    if run is None:
        return None
    return {"id": run.pk, "status": run.status, "steps": run.steps, "changed": run.changed,
            "pending": [{"id": p["id"], "description": p.get("description", ""), "reason": p.get("reason", ""),
                         "approve_label": p.get("approve_label", ""), "preview": p.get("preview")}
                        for p in run.pending] if run.status == Run.WAITING else []}


@never_cache
@require_GET
@assistant_required
def state(request):
    chats = Chat.objects.filter(user=request.user)[:50]
    return JsonResponse({**_header(request.user), "chats": [{"id": c.pk, "title": c.title or "New chat",
                                                               "updated": c.updated_at.isoformat()} for c in chats]})


@never_cache
@require_GET
@assistant_required
def chat_detail(request, pk):
    chat = get_object_or_404(Chat, pk=pk, user=request.user)
    run = chat.runs.filter(status__in=(Run.QUEUED, Run.RUNNING, Run.WAITING)).order_by("-pk").first()
    return JsonResponse({**_header(request.user, chat), "messages": _messages(chat), "run": _run_json(run)})


@require_POST
@assistant_required
def chat_delete(request, pk):
    get_object_or_404(Chat, pk=pk, user=request.user).delete()
    return JsonResponse({"ok": True})


@require_POST
@assistant_required
def chats_delete_all(request):
    Chat.objects.filter(user=request.user).delete()
    return JsonResponse({"ok": True})


@require_POST
@assistant_required
def send(request):
    """A message: JSON {"chat", "text", "page"}, or - with photos - multipart form data with the same fields ("page"
    as JSON text) and up to photos.MAX_PHOTOS files named "photos"."""
    files = []
    if request.content_type == "multipart/form-data":
        data = {"chat": request.POST.get("chat") or None, "text": request.POST.get("text", "")}
        try:
            data["page"] = json.loads(request.POST.get("page") or "{}")
        except ValueError:
            data["page"] = {}
        try:
            files = photos.check(request.FILES.getlist("photos"))
        except photos.PhotoError as exc:
            return JsonResponse({"error": str(exc)}, status=400)
    else:
        data = _json_body(request)
    text = str(data.get("text") or "").strip()
    if not text and not files:
        return JsonResponse({"error": "Type a message first."}, status=400)
    if len(text) > MAX_TEXT:
        return JsonResponse({"error": f"Keep it under {MAX_TEXT} characters."}, status=400)
    chat_id = data.get("chat")
    if chat_id:
        chat = Chat.objects.filter(pk=chat_id, user=request.user).first()
        if chat is None:
            return JsonResponse({"error": "That chat is gone."}, status=404)
    else:
        chat = None
    try:
        runner.check(request.user)
        if chat is not None and chat.runs.filter(status__in=(Run.QUEUED, Run.RUNNING)).exists():
            return JsonResponse({"error": "Wait for the answer to finish first."}, status=409)
        after = chat.messages.order_by("-pk").values_list("pk", flat=True).first() or 0 if chat else 0
        with transaction.atomic():                     # a photo that fails leaves no message (or new chat) behind
            chat = chat or runner.new_chat(request.user)
            run = runner.start(request.user, chat, text, data.get("page"), files)
    except runner.Refused as exc:
        return JsonResponse({"error": str(exc)}, status=403)
    except photos.PhotoError as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    return JsonResponse({**_header(request.user, chat), "run": _run_json(run), "messages": _messages(chat, after)})


@never_cache
@require_GET
@assistant_required
def run_poll(request, pk):
    run = get_object_or_404(Run, pk=pk, user=request.user)
    run = runner.advance(request, run)
    after = _int(request.GET.get("after"), 0, 2**31, 0)
    return JsonResponse({**_header(request.user, run.chat), "run": _run_json(run), "messages": _messages(run.chat, after)})


@require_POST
@assistant_required
def run_approve(request, pk):
    run = get_object_or_404(Run, pk=pk, user=request.user)
    runner.approve(request, run)
    return JsonResponse({"run": _run_json(run)})


@require_POST
@assistant_required
def run_cancel(request, pk):
    run = get_object_or_404(Run, pk=pk, user=request.user)
    runner.cancel(run)
    return JsonResponse({"run": _run_json(run)})


@require_POST
@assistant_required
def action_undo(request, pk):
    action = get_object_or_404(Action, pk=pk, user=request.user)
    try:
        tools.undo(request, action, access.assistant_token(request.user))
    except tools.UndoError as exc:
        return JsonResponse({"error": str(exc), "action": _action_json(action)}, status=409)
    return JsonResponse({"action": _action_json(action), "app": action.app})
