"""Albright Labs - the site hub at "/": a public landing page for visitors
and, once signed in, Home (a tile per app) plus where to land after signing in."""
import hashlib
import secrets
from datetime import timedelta

from django.conf import settings
from django.db import models
from django.utils import timezone

from . import apps_registry

LANDING_CHOICES = [("home", "Home"), ("last_app", "Last used app"), ("page", "A specific page")]


def default_allowed_apps():
    """New accounts start with no apps until a superuser grants some (an invite's apps, or
    Admin > Users). Accounts from before this rule got a row with what they had (migration 0003)."""
    return []


def default_app_keys():
    return apps_registry.all_keys()


class UserPreference(models.Model):
    """Per-user settings for the hub. landing_page is a key from
    apps_registry.PAGES (e.g. "resell:watchlist"). A user with no row has
    the defaults: no apps allowed (superusers always have every app), and
    any app they're later given shown, in the registry's order."""
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="labs_preference")
    landing = models.CharField(max_length=10, choices=LANDING_CHOICES, default="home")
    landing_page = models.CharField(max_length=40, blank=True)
    # Which apps this user may open (set by superusers in Admin > Users; superusers
    # always have every app). App keys from apps_registry.
    allowed_apps = models.JSONField(default=default_allowed_apps, blank=True)
    # Which apps show in their header and on Home, and in what order (their own
    # choice, My account). Hiding one never changes access.
    visible_apps = models.JSONField(default=default_app_keys, blank=True)
    app_order = models.JSONField(default=default_app_keys, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.user}: {self.landing}{' ' + self.landing_page if self.landing_page else ''}"

    @classmethod
    def for_user(cls, user):
        """The saved preference, or an unsaved default (Home) - never creates a row just by reading."""
        return cls.objects.filter(user=user).first() or cls(user=user)


class LandingSettings(models.Model):
    """The public landing page's editable text (Admin > Home page). One row."""
    SINGLETON_PK = 1
    name = models.CharField(max_length=120, blank=True,
                            help_text="Shown as the hero title. Blank: the name on the resume page.")
    headline = models.CharField(max_length=200, blank=True, help_text="One line under the name.")
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return "Landing page settings"

    @classmethod
    def load(cls):
        return cls.objects.get_or_create(pk=cls.SINGLETON_PK)[0]


INVITE_DAYS_CHOICES = [(1, "1 day"), (3, "3 days"), (7, "7 days"), (14, "14 days"), (30, "30 days")]


def hash_token(raw):
    return hashlib.sha256(raw.encode()).hexdigest()


class Invitation(models.Model):
    """An invite to sign up (Admin > Users > Invite user) - the only way to make an
    account yourself. Only the token's SHA-256 is stored; the raw token is in the
    link, shown once in Admin and emailed. Single use: it stops working once used,
    expired or revoked. Resending makes a new link (the old one stops working)."""
    email = models.EmailField()
    name = models.CharField(max_length=120, blank=True)
    apps = models.JSONField(default=list, blank=True)       # app keys from apps_registry, granted on sign-up
    token_hash = models.CharField(max_length=64, unique=True, editable=False)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    days = models.PositiveSmallIntegerField(default=7)          # how long each link works
    expires_at = models.DateTimeField()
    last_sent_at = models.DateTimeField(null=True, blank=True)    # last emailed
    used_at = models.DateTimeField(null=True, blank=True)
    used_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                related_name="+")
    revoked = models.BooleanField(default=False)

    class Meta:
        ordering = ["-created_at", "-pk"]

    def __str__(self):
        return f"Invite for {self.email} ({self.status})"

    @classmethod
    def create(cls, *, email, name="", apps=(), created_by=None, days=7):
        """A new invite and its raw token (the only time the token exists in full)."""
        raw = secrets.token_urlsafe(32)
        invite = cls.objects.create(email=email, name=name, apps=list(apps), created_by=created_by, days=days,
                                    token_hash=hash_token(raw), expires_at=timezone.now() + timedelta(days=days))
        return invite, raw

    @classmethod
    def find_usable(cls, raw):
        """The pending invite for this raw token, or None (unknown, used, expired or revoked)."""
        if not raw or len(raw) > 100:
            return None
        invite = cls.objects.filter(token_hash=hash_token(raw)).first()
        return invite if invite and invite.status == "pending" else None

    def new_token(self):
        """Resend: a fresh link, working for the invite's number of days from now. Returns the raw token."""
        raw = secrets.token_urlsafe(32)
        self.token_hash = hash_token(raw)
        self.expires_at = timezone.now() + timedelta(days=self.days)
        return raw

    def matches(self, raw):
        return secrets.compare_digest(self.token_hash, hash_token(raw or ""))

    @property
    def status(self):
        if self.revoked:
            return "revoked"
        if self.used_at:
            return "used"
        if self.expires_at <= timezone.now():
            return "expired"
        return "pending"

    @property
    def app_labels(self):
        return [apps_registry.BY_KEY[key].label for key in self.apps if key in apps_registry.BY_KEY]


class RateLimitHit(models.Model):
    """One counted attempt on a rate-limited page (sign in, sign up, forgot password),
    by client IP (albright_labs.ratelimit). Kept in the database so every web worker
    shares the count. Rows older than a day are pruned as new ones come in."""
    scope = models.CharField(max_length=20)
    ip = models.CharField(max_length=45)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        indexes = [models.Index(fields=["scope", "ip", "created_at"])]

