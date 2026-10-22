from django.contrib import admin

from .models import BotRequestLog, BotToken, InventorySuggestion, LotNote, OpportunityNote, TradingNote


@admin.register(BotToken)
class BotTokenAdmin(admin.ModelAdmin):
    list_display = ("name", "owner", "display_prefix", "created_at", "last_used_at", "revoked_at")
    exclude = ("token_hash",)   # the hash is never shown or edited
    readonly_fields = ("display_prefix", "created_at", "last_used_at")


@admin.register(BotRequestLog)
class BotRequestLogAdmin(admin.ModelAdmin):
    list_display = ("created_at", "token_name", "method", "endpoint", "status", "summary")
    list_filter = ("status", "token_name")


admin.site.register([TradingNote, LotNote, InventorySuggestion, OpportunityNote])
