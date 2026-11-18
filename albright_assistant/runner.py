"""Running the assistant on PythonAnywhere: no background workers and only a few web workers, so a run never holds a
request open for its whole length. start() saves the message and a Run and returns at once; the browser then polls
/assistant/runs/<id>/ about once a second, and each poll does at most ONE step (advance): one provider call (at most
CALL_SECONDS), then the tool calls it asked for. The run's state lives in the database (models.Run); a poll that finds
another poll mid-step (Run.lock_until) just reports what's there. No streaming.

Limits, all enforced here: CALL_SECONDS per provider call, RUN_SECONDS per run (the clock restarts after an
approval - waiting for the person doesn't count), the person's max tool steps per message and messages per day.
"""
from datetime import datetime, time, timedelta
from decimal import Decimal

from django.db.models import Q
from django.utils import timezone

from albright_bot_api import catalog

from . import access, prompt, providers, tools
from .models import Chat, Message, Run

CALL_SECONDS = 60
RUN_SECONDS = 120
LOCK_SECONDS = CALL_SECONDS + 15
HISTORY_LIMIT = 40          # messages of the chat sent back to the provider (cut at a message of the person's)


class Refused(Exception):
    """Why a message can't be sent (no permission, not connected, disabled, the daily limit)."""


# --- starting ----------------------------------------------------------------------------------------------------------

def messages_today(user):
    zone = prompt._zone(user)
    midnight = datetime.combine(timezone.now().astimezone(zone).date(), time.min, tzinfo=zone)
    return Message.objects.filter(chat__user=user, role=Message.USER, created_at__gte=midnight).count()


def check(user):
    """The person's connection, or Refused."""
    if not access.allowed(user):
        raise Refused("The AI assistant isn't turned on for your account.")
    conn = access.connection(user)
    if conn is None:
        raise Refused("Connect your AI provider first (My account > Your AI assistant).")
    if conn.disabled:
        raise Refused("An admin has turned the assistant off for your account.")
    if not conn.model:
        raise Refused("Pick a model on Your AI assistant first.")
    return conn


def start(user, chat, text, page):
    conn = check(user)
    if messages_today(user) >= conn.daily_limit:
        raise Refused(f"You've reached your limit of {conn.daily_limit} messages today. You can change it on Your AI "
                      "assistant.")
    for waiting in Run.objects.filter(chat=chat, status=Run.WAITING):      # a new message cancels a pending approval
        cancel(waiting, note=False)
    if not chat.title:
        chat.title = " ".join(text.split())[:60]
    chat.provider, chat.model = conn.provider, conn.model
    chat.save()
    run = Run.objects.create(chat=chat, user=user, page=prompt.clean_page(page))
    Message.objects.create(chat=chat, run=run, role=Message.USER, text=text)
    return run


# --- one step ----------------------------------------------------------------------------------------------------------

def advance(request, run):
    """Does the next step if the run is ready for one and no other poll is doing it."""
    if not run.active:
        return run
    now = timezone.now()
    claimed = Run.objects.filter(pk=run.pk, status__in=(Run.QUEUED, Run.RUNNING)) \
        .filter(Q(lock_until__isnull=True) | Q(lock_until__lt=now)) \
        .update(lock_until=now + timedelta(seconds=LOCK_SECONDS), status=Run.RUNNING)
    if not claimed:
        return run
    run.refresh_from_db()
    try:
        _step(request, run)
    finally:
        Run.objects.filter(pk=run.pk).update(lock_until=None)
        run.refresh_from_db()
    return run


def _notice(run, text, kind):
    return Message.objects.create(chat=run.chat, run=run, role=Message.NOTICE, text=text, data={"kind": kind})


def finish(run, status=Run.DONE, kind=""):
    run.status, run.error_kind, run.finished_at = status, kind, timezone.now()
    run.pending = []
    run.save(update_fields=["status", "error_kind", "finished_at", "pending", "updated_at"])


def _fail(run, conn, kind, text):
    _notice(run, text, kind)
    if conn is not None:
        conn.last_error_kind, conn.last_error_at = kind, timezone.now()
        conn.save(update_fields=["last_error_kind", "last_error_at"])
    finish(run, Run.ERROR, kind)


def history(chat):
    rows = list(chat.messages.exclude(role=Message.NOTICE).order_by("-pk")[:HISTORY_LIMIT])[::-1]
    while rows and rows[0].role != Message.USER:            # never start mid tool call
        rows.pop(0)
    out = []
    for m in rows:
        if m.role == Message.USER:
            out.append({"role": "user", "text": m.text})
        elif m.role == Message.ASSISTANT:
            out.append({"role": "assistant", "text": m.text, "tool_calls": m.data.get("tool_calls") or [],
                        "native": m.data.get("native"), "provider": m.data.get("provider")})
        elif m.role == Message.TOOL:
            out.append({"role": "tool", "call_id": m.data.get("call_id"), "name": m.data.get("name"), "text": m.text,
                        "is_error": bool(m.data.get("is_error"))})
    return out


def _cost(conn, reply):
    if reply.cost_usd is not None:
        return reply.cost_usd
    row = next((m for m in conn.models_cache or [] if m.get("id") == conn.model), {})
    p_in = row.get("price_in") or conn.price_in_per_m
    p_out = row.get("price_out") or conn.price_out_per_m
    if p_in is None or p_out is None:
        return None
    return (Decimal(str(p_in)) * reply.tokens_in + Decimal(str(p_out)) * reply.tokens_out) / Decimal(1_000_000)


def _step(request, run):
    conn = None
    try:
        conn = check(run.user)
    except Refused as exc:
        return _fail(run, None, "refused", str(exc))
    elapsed = (timezone.now() - run.started_at).total_seconds()
    if elapsed >= RUN_SECONDS:
        return _fail(run, conn, "timeout", f"Stopped: this took longer than {RUN_SECONDS // 60} minutes. Try again, "
                                           "or ask for something smaller.")
    if run.steps >= conn.max_steps:
        _notice(run, f"Stopped after {conn.max_steps} tool steps (your limit). Say \"continue\" to keep going.", "limit")
        return finish(run)
    token = access.assistant_token(run.user)
    provider = providers.get(conn.provider)
    try:
        reply = provider.chat(access.decrypt(conn.key_encrypted), conn.model, prompt.system(run.user, run.page, token),
                              history(run.chat), tools.definitions(token),
                              timeout=max(5, min(CALL_SECONDS, RUN_SECONDS - elapsed)))
    except providers.ProviderError as exc:
        return _fail(run, conn, exc.kind, exc.message)
    chat = run.chat
    chat.tokens_in += reply.tokens_in
    chat.tokens_out += reply.tokens_out
    cost = _cost(conn, reply)
    if cost is not None:
        chat.cost_usd = (chat.cost_usd or Decimal(0)) + cost
    chat.save(update_fields=["tokens_in", "tokens_out", "cost_usd", "updated_at"])
    conn.last_used_at = timezone.now()
    conn.save(update_fields=["last_used_at"])
    calls = [{"id": c.id, "name": c.name, "arguments": c.arguments} for c in reply.tool_calls]
    Message.objects.create(chat=chat, run=run, role=Message.ASSISTANT, text=reply.text,
                           data={"tool_calls": calls, "native": reply.native, "provider": provider.key})
    if not reply.tool_calls:
        return finish(run)
    run.steps += 1
    reasons = tools.needs_approval(reply.tool_calls)
    for call in reply.tool_calls:
        if call.id not in reasons:
            _execute(request, run, token, call)
    waiting = [{"id": c.id, "name": c.name, "arguments": c.arguments, "reason": reasons[c.id],
                "description": tools.describe(c)} for c in reply.tool_calls if c.id in reasons]
    if waiting:
        run.pending, run.status = waiting, Run.WAITING
        run.save(update_fields=["pending", "status", "steps", "changed", "updated_at"])
        return None
    run.save(update_fields=["steps", "changed", "updated_at"])
    if run.steps >= conn.max_steps:
        _notice(run, f"Stopped after {conn.max_steps} tool steps (your limit). Say \"continue\" to keep going.", "limit")
        finish(run)
    return None


def _execute(request, run, token, call):
    text, data, is_error = tools.run(request, token, call)
    message = Message.objects.create(chat=run.chat, run=run, role=Message.TOOL, text=text,
                                     data={"call_id": call.id, "name": call.name, "is_error": is_error})
    op = catalog.BY_ID.get(call.name)
    if not is_error and op is not None and op.writes:
        tools.record(run, message, call, data)
        app = tools.app_of(call.name)
        if app and app not in run.changed:
            run.changed = [*run.changed, app]


# --- approvals ---------------------------------------------------------------------------------------------------------

def approve(request, run):
    if run.status != Run.WAITING:
        return run
    token = access.assistant_token(run.user)
    for row in run.pending:
        _execute(request, run, token, providers.ToolCall(row["id"], row["name"], row.get("arguments") or {}))
    run.pending, run.status, run.started_at = [], Run.RUNNING, timezone.now()     # a fresh time budget
    run.save(update_fields=["pending", "status", "started_at", "changed", "updated_at"])
    return run


def cancel(run, note=True):
    """Cancel: nothing runs. Each waiting call gets a "cancelled" result (the provider needs one per call)."""
    if run.status != Run.WAITING:
        return run
    for row in run.pending:
        Message.objects.create(chat=run.chat, run=run, role=Message.TOOL,
                               text='{"cancelled": true, "message": "The person cancelled this. Nothing was changed - '
                                    'don\'t try it again unless they ask."}',
                               data={"call_id": row["id"], "name": row["name"], "is_error": False, "cancelled": True})
    if note:
        _notice(run, "Cancelled - nothing was changed.", "cancelled")
    finish(run)
    return run


def new_chat(user):
    return Chat.objects.create(user=user)
