"""The Bot API's own data. An external bot (Grok) can read some site data
and leave suggestions - everything it writes lands in THESE tables (notes,
pending suggestions, drafts), never in the trading/reseller/opportunities
tables directly. Accepting a suggestion is something I do on the site.

Tokens are stored as a SHA-256 hash only (they're 256-bit random strings,
so a fast hash is the right tool - it's how GitHub stores API tokens); the
full token is shown once, when it's created.
"""
import hashlib
import secrets

from django.conf import settings
from django.db import models
from django.utils import timezone

SCOPES = [   # (scope, one-line description of what it allows)
    ("trading:notes", "Post notes to the Grok Bot page (Trading)."),
    ("reseller:read", "Read today's live auction lots and your unsold inventory."),
    ("reseller:suggest", "Leave notes on lots and pending price/listing suggestions on inventory items."),
    ("opportunities:read", "Read opportunities found in the last 7 days."),
    ("opportunities:suggest", "Leave notes and proposal drafts on opportunities (never sent anywhere)."),
    ("site:suggest", "Drop website improvement ideas in the Admin inbox (Bot suggestions)."),
]
SCOPE_KEYS = [key for key, _label in SCOPES]
TOKEN_PREFIX = "abt_"
MAX_TEXT = 5000


def hash_token(raw):
    return hashlib.sha256((raw or "").encode()).hexdigest()


class BotToken(models.Model):
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="bot_tokens")
    name = models.CharField(max_length=100)
    token_hash = models.CharField(max_length=64, unique=True)
    display_prefix = models.CharField(max_length=16)        # "abt_AbCd" - enough to tell tokens apart
    scopes = models.JSONField(default=list)
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.name} ({self.display_prefix}…)"

    @classmethod
    def issue(cls, owner, name, scopes):
        """Creates a token; returns (token row, the full token - shown once, never stored)."""
        raw = TOKEN_PREFIX + secrets.token_urlsafe(32)
        token = cls.objects.create(owner=owner, name=name, token_hash=hash_token(raw), display_prefix=raw[:8],
                                   scopes=[s for s in SCOPE_KEYS if s in scopes])
        return token, raw

    def set_scopes(self, scopes):
        """Change what the token may do - effective on its next request; the token value is unchanged."""
        self.scopes = [s for s in SCOPE_KEYS if s in scopes]
        self.save(update_fields=["scopes"])

    def rotate(self):
        """A new token value with the same name and scopes. The old value
        stops working immediately (its hash is replaced). Returns the new
        full token - shown once, never stored."""
        raw = TOKEN_PREFIX + secrets.token_urlsafe(32)
        self.token_hash, self.display_prefix = hash_token(raw), raw[:8]
        self.save(update_fields=["token_hash", "display_prefix"])
        return raw

    @property
    def is_active(self):
        return self.revoked_at is None

    def revoke(self):
        if self.revoked_at is None:
            self.revoked_at = timezone.now()
            self.save(update_fields=["revoked_at"])


class BotRequestLog(models.Model):
    """Every API request (also the rejected ones) - the Activity log page,
    and what the per-token rate limit counts."""
    token = models.ForeignKey(BotToken, on_delete=models.SET_NULL, null=True, blank=True, related_name="requests")
    token_name = models.CharField(max_length=100, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    method = models.CharField(max_length=8)
    endpoint = models.CharField(max_length=200)
    status = models.PositiveSmallIntegerField()
    summary = models.CharField(max_length=300, blank=True)

    class Meta:
        ordering = ["-created_at", "-pk"]


class _BotWrite(models.Model):
    token = models.ForeignKey(BotToken, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        abstract = True
        ordering = ["-created_at", "-pk"]


class TradingNote(_BotWrite):
    """POST trading/notes - shown on the Grok Bot page under "Bot notes"."""
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="bot_trading_notes")
    date = models.DateField()
    title = models.CharField(max_length=200)
    text = models.TextField()


class LotNote(_BotWrite):
    """POST reseller/lots/<id>/notes - a second opinion in the lot's expandable row."""
    lot = models.ForeignKey("albright_reselling_app.SourcedLot", on_delete=models.CASCADE, related_name="bot_notes")
    text = models.TextField()


class InventorySuggestion(_BotWrite):
    """POST reseller/inventory/<id>/suggestions - PENDING until I accept or
    dismiss it on the item's page. Accepting a price is what sets the
    item's list price; the bot itself never changes it."""
    STATUSES = [("pending", "Pending"), ("accepted", "Accepted"), ("dismissed", "Dismissed")]
    entry = models.ForeignKey("albright_reselling_app.LedgerEntry", on_delete=models.CASCADE,
                              related_name="bot_suggestions")
    suggested_price = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    listing_draft = models.TextField(blank=True)
    reason = models.TextField(blank=True)
    status = models.CharField(max_length=10, choices=STATUSES, default="pending")
    decided_at = models.DateTimeField(null=True, blank=True)


class OpportunityNote(_BotWrite):
    """POST opportunities/<id>/notes and /proposal-draft - labelled "Grok",
    never sent anywhere."""
    KINDS = [("note", "Note"), ("proposal", "Proposal draft")]
    opportunity = models.ForeignKey("albright_crypto_app.Opportunity", on_delete=models.CASCADE,
                                    related_name="bot_notes")
    kind = models.CharField(max_length=8, choices=KINDS)
    text = models.TextField()


class SiteSuggestion(_BotWrite):
    """POST site-suggestions - a website idea for the Admin inbox
    (/control/suggestions/). I set the status and add my own notes."""
    APPS = [("trade", "Trading"), ("resell", "Reselling"), ("opps", "Opportunities"), ("all", "Whole site")]
    PRIORITIES = [("low", "Low"), ("medium", "Medium"), ("high", "High")]
    STATUSES = [("new", "New"), ("accepted", "Accepted"), ("declined", "Declined"), ("done", "Done")]
    app = models.CharField(max_length=6, choices=APPS)
    area = models.CharField(max_length=100, blank=True)
    title = models.CharField(max_length=200)
    description = models.TextField()
    priority = models.CharField(max_length=6, choices=PRIORITIES, default="medium")
    status = models.CharField(max_length=8, choices=STATUSES, default="new")
    my_notes = models.TextField(blank=True)
    updated_at = models.DateTimeField(auto_now=True)
