"""Albright Labs - the site hub at "/": a public landing page for visitors
and, once signed in, Home (a tile per app) plus where to land after signing in."""
import hashlib
import secrets
from datetime import timedelta

from django.conf import settings
from django.db import models
from django.utils import timezone

from . import apps_registry

# "Open after sign-in": the app picker (Home), the last app used, an app's main page, or one of its pages.
LANDING_CHOICES = [("home", "App picker (Home)"), ("last_app", "Last used app"), ("app", "An app"),
                   ("page", "A specific page")]


# ---------------------------------------------------------------------------
# Circles - groups of people who choose to share data, app by app (albright_labs.circles enforces every rule)
# ---------------------------------------------------------------------------

CIRCLE_APPS = ("planner", "meals", "money", "notes", "fitness")
CIRCLE_INVITE_DAYS = 14


class Circle(models.Model):
    """A group of people who share some apps' data. enabled_apps: which apps the circle shares (a subset of
    CIRCLE_APPS); each member also opts in app by app (CircleMembership.apps). Replaced the Household (migration 0008
    made one circle per household)."""
    name = models.CharField(max_length=100)
    color = models.CharField(max_length=7, blank=True)              # "#RRGGBB" (optional)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    enabled_apps = models.JSONField(default=list, blank=True)

    class Meta:
        ordering = ["name", "pk"]

    def __str__(self):
        return self.name


class CircleMembership(models.Model):
    """One person in a circle. Nothing of the circle's shows until status is ACTIVE and they've opted in to the app
    (apps) - and the circle has it enabled and they can open the app (albright_labs.circles)."""
    OWNER, MEMBER = "owner", "member"
    ROLES = [(OWNER, "Owner"), (MEMBER, "Member")]
    INVITED, ACTIVE, LEFT, REMOVED = "invited", "active", "left", "removed"
    STATUSES = [(INVITED, "Invited"), (ACTIVE, "Active"), (LEFT, "Left"), (REMOVED, "Removed")]
    circle = models.ForeignKey(Circle, on_delete=models.CASCADE, related_name="memberships")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="circle_memberships")
    role = models.CharField(max_length=6, choices=ROLES, default=MEMBER)
    status = models.CharField(max_length=7, choices=STATUSES, default=INVITED)
    invited_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name="+")
    joined_at = models.DateTimeField(null=True, blank=True)
    apps = models.JSONField(default=list, blank=True)               # the member's own opt-in, app by app (off by default)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["joined_at", "pk"]
        constraints = [models.UniqueConstraint(fields=["circle", "user"], name="labs_one_membership_per_circle")]

    def __str__(self):
        return f"{self.user} in {self.circle} ({self.status})"


class CircleInvite(models.Model):
    """An invite into a circle - for someone with an account (user) or an email (they sign up with the site invite
    first - Invitation.circle_invite - then see this). Only the token's SHA-256 is stored. 14 days."""
    circle = models.ForeignKey(Circle, on_delete=models.CASCADE, related_name="invites")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, null=True, blank=True,
                             related_name="circle_invites")
    email = models.EmailField(blank=True)
    invited_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name="+")
    token_hash = models.CharField(max_length=64, unique=True, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    accepted_at = models.DateTimeField(null=True, blank=True)
    declined_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-pk"]

    def __str__(self):
        return f"Invite to {self.circle} for {self.user or self.email} ({self.status})"

    @classmethod
    def create(cls, *, circle, invited_by, user=None, email=""):
        """A new invite and its raw token (the only time it exists in full)."""
        raw = secrets.token_urlsafe(32)
        invite = cls.objects.create(circle=circle, user=user, email=email, invited_by=invited_by, token_hash=hash_token(raw),
                                    expires_at=timezone.now() + timedelta(days=CIRCLE_INVITE_DAYS))
        return invite, raw

    def renew(self):
        """Resend: a fresh token, 14 days from now. Returns the raw token."""
        raw = secrets.token_urlsafe(32)
        self.token_hash = hash_token(raw)
        self.expires_at = timezone.now() + timedelta(days=CIRCLE_INVITE_DAYS)
        self.save(update_fields=["token_hash", "expires_at"])
        return raw

    @property
    def status(self):
        if self.revoked_at:
            return "revoked"
        if self.accepted_at:
            return "accepted"
        if self.declined_at:
            return "declined"
        if self.expires_at <= timezone.now():
            return "expired"
        return "pending"


class Space(models.Model):
    """Where an app's data lives: one person's Personal space (user) or a circle's space (circle) - exactly one.
    Meals' and Money's rows belong to a space; albright_labs.circles decides who sees which."""
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, null=True, blank=True,
                                related_name="personal_space")
    circle = models.OneToOneField(Circle, on_delete=models.CASCADE, null=True, blank=True, related_name="space")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.CheckConstraint(
            condition=(models.Q(user__isnull=False, circle__isnull=True) | models.Q(user__isnull=True, circle__isnull=False)),
            name="labs_space_personal_or_circle")]

    def __str__(self):
        return self.name

    @property
    def is_personal(self):
        return self.user_id is not None

    @property
    def name(self):
        return "Personal" if self.user_id else self.circle.name

    @property
    def label(self):
        """Whose it is, for credits: the circle's name, or the person's first name ("Nathan's recipe")."""
        if self.circle_id:
            return self.circle.name
        return (self.user.first_name or self.user.get_username()).strip()


def default_allowed_apps():
    """New accounts start with no apps until a superuser grants some (an invite's apps, or
    Admin > Users). Accounts from before this rule got a row with what they had (migration 0003)."""
    return []


def default_app_keys():
    return apps_registry.all_keys()


class UserPreference(models.Model):
    """Per-user settings for the hub. landing is "Open after sign-in" (labs.landing); landing_page is an app key
    for landing="app" (e.g. "meals") or a key from apps_registry.PAGES for landing="page" (e.g.
    "resell:watchlist"). A user with no row has
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
    # Home's Favorites row (app keys, shown first) and whether Home shows the Planner's Today panel.
    pinned_apps = models.JSONField(default=list, blank=True)
    show_today_panel = models.BooleanField(default=False)
    # ...and whether that panel also lists today's completed tasks (struck through, at the bottom). Off: open ones only.
    show_completed_today = models.BooleanField(default=False)
    # Admin > Users "Allow API keys": may this person make their own Bot API keys (My account > My API keys)?
    # Superusers always may. Turning it off stops their keys working (albright_bot_api.scopes).
    api_keys_allowed = models.BooleanField(default=False)
    # The space each app last showed (Meals and Money's switcher): {"meals": space id, ...} - checked on every use.
    current_spaces = models.JSONField(default=dict, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.user}: {self.landing}{' ' + self.landing_page if self.landing_page else ''}"

    @classmethod
    def for_user(cls, user):
        """The saved preference, or an unsaved default (the app picker) - never creates a row just by reading."""
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
    # Signing up with it also shows this circle invite to the new account (Circles > Invite by email).
    circle_invite = models.ForeignKey("CircleInvite", on_delete=models.SET_NULL, null=True, blank=True, related_name="signups")

    class Meta:
        ordering = ["-created_at", "-pk"]

    def __str__(self):
        return f"Invite for {self.email} ({self.status})"

    @classmethod
    def create(cls, *, email, name="", apps=(), created_by=None, days=7, circle_invite=None):
        """A new invite and its raw token (the only time the token exists in full)."""
        raw = secrets.token_urlsafe(32)
        invite = cls.objects.create(email=email, name=name, apps=list(apps), created_by=created_by, days=days,
                                    token_hash=hash_token(raw), expires_at=timezone.now() + timedelta(days=days),
                                    circle_invite=circle_invite)
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

