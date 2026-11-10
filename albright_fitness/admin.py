from django.contrib import admin

from .challenges import Challenge, ChallengeEntry, ChallengeRule, EntryComment


class ChallengeRuleInline(admin.TabularInline):
    model = ChallengeRule
    extra = 0


@admin.register(Challenge)
class ChallengeAdmin(admin.ModelAdmin):
    list_display = ("name", "circle", "start_date", "end_date", "created_by")
    list_filter = ("start_date",)
    search_fields = ("name",)
    inlines = [ChallengeRuleInline]


@admin.register(ChallengeEntry)
class ChallengeEntryAdmin(admin.ModelAdmin):
    list_display = ("challenge", "user", "date", "activity", "amount", "points")
    list_filter = ("activity", "date")


@admin.register(EntryComment)
class EntryCommentAdmin(admin.ModelAdmin):
    list_display = ("entry", "user", "created_at")
