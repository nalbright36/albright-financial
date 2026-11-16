from django.contrib import admin

from .attachments import size_label
from .models import Attachment


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
