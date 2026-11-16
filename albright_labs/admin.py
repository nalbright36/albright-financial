from django.contrib import admin

from .attachments import size_label
from .models import Attachment, UserPreference


@admin.register(Attachment)
class AttachmentAdmin(admin.ModelAdmin):
    """Metadata only - the bytes (AttachmentBlob) are never loaded here."""
    list_display = ("original_name", "owner", "parent_label", "size", "mime_type", "created_at")
    list_filter = ("content_type", "mime_type")
    search_fields = ("original_name", "owner__username", "key")
    list_select_related = ("owner", "content_type")
    readonly_fields = ("owner", "content_type", "object_id", "key", "original_name", "mime_type", "size_bytes", "width",
                       "height", "has_thumb", "sha256", "created_at")
    date_hierarchy = "created_at"

    def has_add_permission(self, request):
        return False                                   # added through a task or note, where the checks are

    @admin.display(description="Parent")
    def parent_label(self, obj):
        return f"{obj.content_type.model} #{obj.object_id}"

    @admin.display(description="Size", ordering="size_bytes")
    def size(self, obj):
        return size_label(obj.size_bytes)


@admin.register(UserPreference)
class UserPreferenceAdmin(admin.ModelAdmin):
    """Per-user settings - here for "Can create API keys" (also on Admin > Users). Off disables the person's keys
    (403 "API access disabled") without deleting them; on again re-enables them. Superusers always can."""
    list_display = ("user", "api_keys_allowed", "active_keys")
    list_editable = ("api_keys_allowed",)
    list_filter = ("api_keys_allowed",)
    search_fields = ("user__username", "user__email")
    list_select_related = ("user",)
    raw_id_fields = ("user",)
    fields = ("user", "api_keys_allowed")

    @admin.display(description="Active keys")
    def active_keys(self, obj):
        return obj.user.bot_tokens.filter(revoked_at__isnull=True).count()
