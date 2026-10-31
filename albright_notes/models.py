"""Albright Notes: each user's private Markdown notes, optionally shared with
other people who can open Notes (view or edit), and writable by the owner's
bots through the Bot API (notes:read / notes:write).

A note is only ever visible to its owner and the people it's shared with
(Note.objects.visible_to) - everyone else gets a 404. Feature ideas for the
site itself don't belong here: they stay in Admin > Ideas."""
from django.conf import settings
from django.db import models
from django.db.models import Q

ME = "me"                 # Note.created_by for a note its owner wrote on the site
MAX_TAGS = 20
MAX_TAG_LENGTH = 50
MAX_BODY = 100_000


class NoteQuerySet(models.QuerySet):
    def visible_to(self, user):
        """Notes this user owns or that are shared with them (view or edit)."""
        if not (user and user.is_authenticated):
            return self.none()
        shared = NoteShare.objects.filter(user=user).values("note_id")
        return self.filter(Q(owner=user) | Q(pk__in=shared))

    def editable_by(self, user):
        if not (user and user.is_authenticated):
            return self.none()
        shared = NoteShare.objects.filter(user=user, permission=NoteShare.EDIT).values("note_id")
        return self.filter(Q(owner=user) | Q(pk__in=shared))


class Note(models.Model):
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="notes")
    title = models.CharField(max_length=200)
    body = models.TextField(blank=True)                          # Markdown
    tags = models.JSONField(default=list, blank=True)           # ["groceries", "taxes"], lowercase
    pinned = models.BooleanField(default=False)
    archived = models.BooleanField(default=False)
    # "me" (written on the site), or the name of the bot token that created it.
    created_by = models.CharField(max_length=100, default=ME)
    bot_token = models.ForeignKey("albright_bot_api.BotToken", on_delete=models.SET_NULL, null=True, blank=True,
                                  related_name="+")
    # Bumped on every save - autosave sends the version it started from, so an edit made
    # meanwhile (another person, or a bot) is a conflict, not silently overwritten.
    version = models.PositiveIntegerField(default=1)
    updated_by = models.CharField(max_length=150, blank=True)    # who saved it last, for the conflict warning
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = NoteQuerySet.as_manager()

    class Meta:
        ordering = ["-pinned", "-updated_at", "-pk"]
        indexes = [models.Index(fields=["owner", "archived", "-updated_at"], name="notes_owner_recent")]

    def __str__(self):
        return self.title

    @property
    def from_bot(self):
        return self.created_by != ME

    def can_edit(self, user):
        return self.owner_id == user.pk or self.shares.filter(user=user, permission=NoteShare.EDIT).exists()


class NoteShare(models.Model):
    VIEW, EDIT = "view", "edit"
    PERMISSIONS = [(VIEW, "Can view"), (EDIT, "Can edit")]
    note = models.ForeignKey(Note, on_delete=models.CASCADE, related_name="shares")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="shared_notes")
    permission = models.CharField(max_length=4, choices=PERMISSIONS, default=VIEW)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["user__username"]
        constraints = [models.UniqueConstraint(fields=["note", "user"], name="notes_one_share_per_user")]


class NoteLink(models.Model):
    """A note pointing at a record elsewhere on the site (albright_notes.links has the kinds)."""
    note = models.ForeignKey(Note, on_delete=models.CASCADE, related_name="links")
    app = models.CharField(max_length=10)                # an app key from albright_labs.apps_registry
    object_type = models.CharField(max_length=30)        # "recipe", "ledger", "lot", "opportunity", "strategy"...
    object_id = models.PositiveBigIntegerField()
    label = models.CharField(max_length=200)             # the record's name when it was linked
    url = models.CharField(max_length=500)               # its page when it was linked
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "pk"]
        constraints = [models.UniqueConstraint(fields=["note", "app", "object_type", "object_id"],
                                               name="notes_one_link_per_record")]
