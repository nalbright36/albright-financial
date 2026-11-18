"""The in-site AI assistant's data. Nothing here holds a usable secret in the clear: the person's provider key is
encrypted with the site's MultiFernet (albright_trading_app.crypto_utils) and only its last 4 characters are kept
readable. Message text is the person's own and is never shown in Admin (Admin > AI assistant shows counts only).

  Connection  one per person: their provider, encrypted key, model, limits, and the in-process API token the
              assistant acts with (albright_bot_api.BotToken kind="assistant")
  Chat        a conversation; its token counts and cost estimate
  Message     a turn: the person's words, the assistant's reply (with any tool calls), a tool result, or a notice
  Run         one answer in progress: advanced a step per poll (runner.advance), state kept here
  Action      one change the assistant made (the chat's action chips), with how to undo it
"""
from django.conf import settings
from django.db import models
from django.utils import timezone

from . import providers as provider_list

PROVIDERS = [(p.key, p.label) for p in provider_list.PROVIDERS]
DEFAULT_DAILY_LIMIT = 200
DEFAULT_MAX_STEPS = 8


class Connection(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="assistant_connection")
    provider = models.CharField(max_length=20, choices=PROVIDERS)
    key_encrypted = models.TextField()                       # Fernet token; decrypted only to call the provider
    key_last4 = models.CharField(max_length=4)
    model = models.CharField(max_length=200, blank=True)
    models_cache = models.JSONField(default=list, blank=True)    # [{"id", "label", "price_in", "price_out"}]
    models_fetched_at = models.DateTimeField(null=True, blank=True)
    daily_limit = models.PositiveIntegerField(default=DEFAULT_DAILY_LIMIT)
    max_steps = models.PositiveSmallIntegerField(default=DEFAULT_MAX_STEPS)
    # Optional: what the model costs (USD per million tokens), for the chat's estimate when the provider's list
    # doesn't say. Blank: the estimate shows tokens only.
    price_in_per_m = models.DecimalField(max_digits=10, decimal_places=4, null=True, blank=True)
    price_out_per_m = models.DecimalField(max_digits=10, decimal_places=4, null=True, blank=True)
    disabled = models.BooleanField(default=False)            # Admin > AI assistant "Disable"
    token = models.ForeignKey("albright_bot_api.BotToken", on_delete=models.SET_NULL, null=True, blank=True,
                              related_name="+")
    connected_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    last_error_kind = models.CharField(max_length=30, blank=True)
    last_error_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"{self.user}: {self.provider} …{self.key_last4}"

    @property
    def provider_label(self):
        return dict(PROVIDERS).get(self.provider, self.provider)

    @property
    def model_label(self):
        for row in self.models_cache or []:
            if row.get("id") == self.model:
                return row.get("label") or self.model
        return self.model


class Chat(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="assistant_chats")
    title = models.CharField(max_length=120, blank=True)
    provider = models.CharField(max_length=20, blank=True)
    model = models.CharField(max_length=200, blank=True)
    tokens_in = models.PositiveIntegerField(default=0)
    tokens_out = models.PositiveIntegerField(default=0)
    cost_usd = models.DecimalField(max_digits=12, decimal_places=6, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at", "-pk"]


class Message(models.Model):
    USER, ASSISTANT, TOOL, NOTICE = "user", "assistant", "tool", "notice"
    ROLES = [(USER, "You"), (ASSISTANT, "Assistant"), (TOOL, "Tool result"), (NOTICE, "Notice")]
    chat = models.ForeignKey(Chat, on_delete=models.CASCADE, related_name="messages")
    run = models.ForeignKey("Run", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    role = models.CharField(max_length=10, choices=ROLES)
    text = models.TextField(blank=True)
    # assistant: {"tool_calls": [{"id", "name", "arguments"}], "native": provider-specific blocks to send back
    # (Anthropic thinking, Gemini thought signatures, xAI reasoning), "provider"}; tool: {"call_id", "name",
    # "is_error"}; notice: {"kind": "error" | "limit" | "cancelled" ...}
    data = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["pk"]


class Run(models.Model):
    QUEUED, RUNNING, WAITING, DONE, ERROR = "queued", "running", "waiting", "done", "error"
    STATUSES = [(QUEUED, "Queued"), (RUNNING, "Running"), (WAITING, "Waiting for approval"), (DONE, "Done"),
                (ERROR, "Error")]
    chat = models.ForeignKey(Chat, on_delete=models.CASCADE, related_name="runs")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")
    status = models.CharField(max_length=10, choices=STATUSES, default=QUEUED)
    steps = models.PositiveSmallIntegerField(default=0)
    page = models.JSONField(default=dict, blank=True)          # the page context sent with the message
    pending = models.JSONField(default=list, blank=True)       # tool calls waiting for Approve / Cancel
    lock_until = models.DateTimeField(null=True, blank=True)   # a poll is calling the provider until then
    error_kind = models.CharField(max_length=30, blank=True)
    started_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    changed = models.JSONField(default=list, blank=True)       # apps whose data this run changed (page refresh)

    @property
    def active(self):
        return self.status in (self.QUEUED, self.RUNNING)


class Action(models.Model):
    """A change the assistant made - the chat's action chip. undo: {"kind": "op", "op", "args"} (an API operation
    that reverses it) or {"kind": "delete", "model", "ids"} (rows it created), or {} when it can't be undone."""
    chat = models.ForeignKey(Chat, on_delete=models.CASCADE, related_name="actions")
    run = models.ForeignKey(Run, on_delete=models.SET_NULL, null=True, blank=True, related_name="actions")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")
    message = models.ForeignKey(Message, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    op = models.CharField(max_length=60)
    app = models.CharField(max_length=20, blank=True)
    summary = models.CharField(max_length=200)
    undo = models.JSONField(default=dict, blank=True)
    undo_until = models.DateTimeField(null=True, blank=True)
    undone_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["pk"]

    @property
    def can_undo(self):
        return bool(self.undo) and self.undone_at is None and self.undo_until is not None \
            and self.undo_until > timezone.now()
