"""Admin > Ideas and Admin > Bot suggestions share one list: the same
statuses and status tabs (New first, always the default), the same filters,
rows, quick status buttons and daily review mode
(templates/albright_control/review_list.html + static/js/review_list.js).

Each page is a Kind: its model, filters, and how one record becomes a row
(a plain dict - the template renders the list from it and review mode gets
the same dicts as JSON)."""
from django.db.models import Case, Count, IntegerField, Q, Value, When
from django.urls import reverse
from django.utils import timezone

from albright_bot_api.models import SiteSuggestion

from . import ideas, prompts
from .models import Idea

STATUSES = Idea.STATUSES                     # SiteSuggestion.STATUSES is the same list (tests check)
TABS = STATUSES + [("all", "All")]
DEFAULT_TAB = "new"
LIST_LIMIT = 500
# Review mode's keys - the legend, help overlay and phone buttons are all built from this list.
SHORTCUTS = [
    (["C"], "Considering", "considering"),
    (["P"], "Planned", "planned"),
    (["I"], "In progress", "in_progress"),
    (["X"], "Declined", "declined"),
    (["N"], "Add a note", "note"),
    (["Y"], "Copy as Claude Code prompt", "copy"),
    (["→", "Space"], "Skip", "skip"),
    (["←"], "Back", "back"),
    (["U"], "Undo", "undo"),
    (["?"], "Help", "help"),
]
QUICK_STATUSES = ("considering", "planned", "declined")      # buttons on every row; the rest are in its menu
SOURCES = {"grok": "Grok", "manual": "Manual", "moved_from_suggestion": "Grok"}
PRIORITY_RANK = {"low": 0, "medium": 1, "high": 2, "urgent": 3}
RANK = {"impact": {"low": 0, "medium": 1, "high": 2}, "effort": {"small": 0, "medium": 1, "large": 2}}


def ago(when, now=None):
    """ "just now", "5m ago", "2h ago", "3d ago", then the date."""
    seconds = ((now or timezone.now()) - when).total_seconds()
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)}m ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h ago"
    if seconds < 30 * 86400:
        return f"{int(seconds // 86400)}d ago"
    local = timezone.localtime(when)
    return f"{local:%b} {local.day}, {local.year}"


def _full_date(when):
    local = timezone.localtime(when)
    return f"{local:%b} {local.day}, {local.year} {local:%I:%M %p}".replace(" 0", " ")


def _rank(field, ranks):
    return Case(*[When(**{field: k}, then=Value(v)) for k, v in ranks.items()], default=Value(-1),
                output_field=IntegerField())


def _links(urls):
    """Every link goes through the external-link check: verified domains open directly, anything else via
    the "not on your verified list" page (albright_crypto_app.links)."""
    from albright_crypto_app.links import link_status, outbound_href
    return [{"text": url, "href": outbound_href(url), "verified": link_status(url)[1] == "allowlisted"}
            for url in urls or []]


class Kind:
    key = ""
    model = None
    page_url = ""
    noun, plural = "", ""
    title = intro = ""
    filters = ()          # (GET name, label for "All ...", choices)
    search_fields = ()
    sorts = [("newest", "Newest first"), ("oldest", "Oldest first"), ("priority", "Priority")]
    audit_status = ""

    def counts(self):
        rows = dict(self.model.objects.values_list("status").annotate(n=Count("pk")).order_by())
        out = {key: rows.get(key, 0) for key, _label in STATUSES}
        out["all"] = sum(out.values())
        return out

    def filter_values(self, params):
        values = {name: params.get(name, "") for name, _label, _choices in self.filters}
        values["q"] = params.get("q", "").strip()
        values["sort"] = params.get("sort", "") if params.get("sort", "") in dict(self.sorts) else ""
        return values

    def queryset(self, values, status):
        qs = self.base_queryset()
        if status != "all":
            qs = qs.filter(status=status)
        for name, _label, choices in self.filters:
            value = values.get(name)
            if not value:
                continue
            if name == "tag":
                qs = qs.filter(tags__contains=[value])
            elif value in dict(choices):
                qs = qs.filter(**{name: value})
        if values.get("q"):
            query = Q()
            for field in self.search_fields:
                query |= Q(**{f"{field}__icontains": values["q"]})
            qs = qs.filter(query)
        return self.order(qs, values.get("sort") or "newest")

    def order(self, qs, sort):
        if sort == "oldest":
            return qs.order_by("created_at", "pk")
        if sort == "priority":
            return qs.annotate(priority_rank=_rank("priority", PRIORITY_RANK)).order_by("-priority_rank", "-created_at")
        return qs.order_by("-created_at", "-pk")

    def base_queryset(self):
        return self.model.objects.all()

    def urls(self, obj):
        raise NotImplementedError

    def row(self, obj, now=None):
        return {
            "id": obj.pk, "title": obj.title, "status": obj.status, "status_label": obj.get_status_display(),
            "app": obj.get_app_display(), "priority": obj.priority, "priority_label": obj.get_priority_display(),
            "created": ago(obj.created_at, now), "created_full": _full_date(obj.created_at),
            "description": obj.description, "notes": obj.my_notes, "urls": self.urls(obj),
        }


class IdeaKind(Kind):
    key = "ideas"
    model = Idea
    page_url = "albright_control:ideas"
    noun, plural = "idea", "ideas"
    title = "Ideas"
    intro = ("Ideas you sent to the bot (it saves them with the ideas:write scope), ones you add yourself, "
             "and Bot suggestions you moved here.")
    filters = (("app", "apps", Idea.APPS), ("category", "categories", Idea.CATEGORIES),
               ("priority", "priorities", Idea.PRIORITIES), ("tag", "tags", ()))
    search_fields = ("title", "description", "original_message", "my_notes")
    sorts = Kind.sorts + [("quick", "Quick wins")]
    audit_status = "idea_status"

    def base_queryset(self):
        return Idea.objects.select_related("token")

    def order(self, qs, sort):
        if sort == "quick":
            # High impact + small effort first, then by impact, effort and priority.
            return (qs.annotate(quick=Case(When(impact="high", effort="small", then=Value(1)), default=Value(0),
                                           output_field=IntegerField()),
                                impact_rank=_rank("impact", RANK["impact"]), effort_rank=_rank("effort", RANK["effort"]),
                                priority_rank=_rank("priority", PRIORITY_RANK))
                    .order_by("-quick", "-impact_rank", "effort_rank", "-priority_rank", "-created_at"))
        return super().order(qs, sort)

    def tags(self):
        return sorted({t for tags in Idea.objects.values_list("tags", flat=True) for t in (tags or [])})

    def urls(self, obj):
        return {"status": reverse("albright_control:idea_status", args=[obj.pk]),
                "notes": reverse("albright_control:idea_notes", args=[obj.pk]),
                "detail": reverse("albright_control:idea", args=[obj.pk])}

    def row(self, obj, now=None):
        return {**super().row(obj, now),
                "impact_label": obj.get_impact_display(), "effort_label": obj.get_effort_display(),
                "category": obj.get_category_display(), "source": SOURCES.get(obj.source, "Manual"),
                "original_message": obj.original_message, "links": _links(obj.links), "tags": list(obj.tags or []),
                "duplicate_of": obj.possible_duplicate_of_id, "prompt": ideas.claude_code_prompt(obj)}


class SuggestionKind(Kind):
    key = "suggestions"
    model = SiteSuggestion
    page_url = "albright_control:suggestions"
    noun, plural = "suggestion", "suggestions"
    title = "Bot suggestions"
    intro = "Website ideas the bot posts with the site:suggest scope. Nothing changes until you act on one."
    filters = (("app", "apps", SiteSuggestion.APPS), ("priority", "priorities", SiteSuggestion.PRIORITIES))
    search_fields = ("title", "description", "area", "my_notes")
    audit_status = "suggestion_status"

    def base_queryset(self):
        return SiteSuggestion.objects.select_related("token")

    def tags(self):
        return []

    def urls(self, obj):
        return {"status": reverse("albright_control:suggestion_status", args=[obj.pk]),
                "notes": reverse("albright_control:suggestion_notes", args=[obj.pk]),
                "delete": reverse("albright_control:suggestion_delete", args=[obj.pk]),
                "move": reverse("albright_control:suggestion_to_idea", args=[obj.pk])}

    def row(self, obj, now=None):
        return {**super().row(obj, now), "source": "Grok", "area": obj.area, "links": [],
                "prompt": prompts.claude_code_prompt(obj)}


KINDS = {"ideas": IdeaKind(), "suggestions": SuggestionKind()}


def new_counts():
    """New items per page - the Admin nav's badges."""
    return {key: kind.model.objects.filter(status="new").count() for key, kind in KINDS.items()}
