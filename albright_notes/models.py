"""Albright Notes: each user's private Markdown notes, optionally shared with
other people who can open Notes (view or edit), and writable by the owner's
bots through the Bot API (notes:read / notes:write).

A note is only ever visible to its owner and the people it's shared with
(Note.objects.visible_to) - everyone else gets a 404. Feature ideas for the
site itself don't belong here: they stay in Admin > Ideas."""
from django.conf import settings
from django.db import models

ME = "me"                 # Note.created_by for a note its owner wrote on the site
MAX_TAGS = 20
MAX_TAG_LENGTH = 50
MAX_BODY = 100_000
TRASH_DAYS = 30           # "Recently deleted" keeps a note this long (prune_activity removes it after)


class NoteQuerySet(models.QuerySet):
    def visible_to(self, user):
        """Notes this user owns or that are shared with them (view or edit)."""
        if not (user and user.is_authenticated):
            return self.none()
        from albright_labs import sharing
        return sharing.visible_queryset(user, Note, self)

    def editable_by(self, user):
        if not (user and user.is_authenticated):
            return self.none()
        from albright_labs import sharing
        return sharing.editable_queryset(user, Note, self)


class NoteManager(models.Manager.from_queryset(NoteQuerySet)):
    """Note.objects: every note except the ones in Recently deleted - the pages, the Bot API, sharing and the
    Planner's links all go through it, so a deleted note is gone everywhere until it's restored."""

    def get_queryset(self):
        return super().get_queryset().filter(deleted_at__isnull=True)


class Note(models.Model):
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="notes")
    title = models.CharField(max_length=200)
    body = models.TextField(blank=True)                          # Markdown
    tags = models.JSONField(default=list, blank=True)           # ["groceries", "taxes"], lowercase
    pinned = models.BooleanField(default=False)
    archived = models.BooleanField(default=False)
    # In "Recently deleted" since then (notes.trash / restore); prune_activity removes it TRASH_DAYS later.
    deleted_at = models.DateTimeField(null=True, blank=True, db_index=True)
    # Ticked checklist items move to the bottom of their list (a per-note switch in the editor's More panel).
    sort_checked = models.BooleanField(default=False)
    # The rich editor (editor_src/note_editor.js) rewrites a note's Markdown in its own tidy form the first time
    # it saves it; the Markdown from before that is kept here, with when, until we're sure nothing was lost.
    body_backup = models.TextField(blank=True)
    editor_saved_at = models.DateTimeField(null=True, blank=True)
    # "me" (written on the site), or the name of the bot token that created it.
    created_by = models.CharField(max_length=100, default=ME)
    bot_token = models.ForeignKey("albright_bot_api.BotToken", on_delete=models.SET_NULL, null=True, blank=True,
                                  related_name="+")
    # Bumped on every save - autosave sends the version it started from, so an edit made
    # meanwhile (another person, or a bot) is a conflict, not silently overwritten.
    version = models.PositiveIntegerField(default=1)
    # The version of the last save a person (or bot) made. A task / grocery line changing rewrites its line of the
    # note (smart.sync_write) and bumps version only - so the editor's next save is merged, not a conflict.
    human_version = models.PositiveIntegerField(default=0)
    updated_by = models.CharField(max_length=150, blank=True)    # who saved it last, for the conflict warning
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = NoteManager()
    all_objects = NoteQuerySet.as_manager()          # deleted ones too (Recently deleted, restore, purge)

    class Meta:
        ordering = ["-pinned", "-updated_at", "-pk"]
        indexes = [models.Index(fields=["owner", "archived", "-updated_at"], name="notes_owner_recent")]

    def __str__(self):
        return self.title

    @property
    def from_bot(self):
        return self.created_by != ME

    def can_edit(self, user):
        from albright_labs import sharing
        return sharing.can_edit(user, self)




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


class NotesSettings(models.Model):
    """A person's Notes settings."""
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="notes_settings")
    # Every date picked with "@" in a note becomes a Planner task too (smart.add_task).
    auto_planner = models.BooleanField(default=False)

    def __str__(self):
        return f"Notes settings for {self.user}"
