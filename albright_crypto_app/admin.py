from django.contrib import admin

from .models import AIExtraction, ConfigOverride, Earning, Opportunity, ScanRun, Source, TaskStep, TimeLog


@admin.register(Opportunity)
class OpportunityAdmin(admin.ModelAdmin):
    list_display = ("title", "opportunity_type", "reward_type", "safety_verdict", "confidence", "ev_per_hour", "status", "discovered_at")
    list_filter = ("opportunity_type", "safety_verdict", "status", "reward_type", "confidence", "excluded")
    search_fields = ("title", "url")


@admin.register(Source)
class SourceAdmin(admin.ModelAdmin):
    list_display = ("name", "opportunity_type", "kind", "active", "terms_checked", "last_checked")


admin.site.register([TaskStep, TimeLog, Earning, AIExtraction, ScanRun, ConfigOverride])
