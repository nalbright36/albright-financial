"""The Admin area's top nav (templates/index.html): Overview, then three
dropdown groups - Bot, System, Site. The group holding the open page is
marked active, and a group shows a count badge when something inside needs
attention (new ideas and bot suggestions, late or failing jobs, unreviewed
resume conversations).

Each item: (label, URL name, the pages that count as it). A page is
"namespace:url_name"; "namespace:*" is every page in that namespace.
"""
from django.core.cache import cache
from django.urls import reverse

GROUPS = (
    ("overview", "Overview", (
        ("Overview", "albright_control:overview", ("albright_control:overview",)),
    )),
    ("bot", "Bot", (
        ("Bot API", "albright_control:bot_api", ("albright_control:bot_api", "albright_control:bot_activity",
                                                 "albright_control:revoke_token", "albright_control:rotate_token",
                                                 "albright_control:edit_token_scopes")),
        ("Bot suggestions", "albright_control:suggestions", ("albright_control:suggestions",
                                                             "albright_control:suggestion_status",
                                                             "albright_control:suggestion_notes",
                                                             "albright_control:suggestion_delete")),
        ("Ideas", "albright_control:ideas", ("albright_control:ideas", "albright_control:idea_add",
                                             "albright_control:idea", "albright_control:idea_status",
                                             "albright_control:idea_notes", "albright_control:suggestion_to_idea")),
    )),
    ("system", "System", (
        ("System health", "albright_control:health", ("albright_control:health",)),
        ("Integrations", "albright_control:integrations", ("albright_control:integrations",
                                                           "albright_control:test_integration")),
        ("AI usage", "albright_control:ai_usage", ("albright_control:ai_usage",)),
        ("Data", "albright_control:data", ("albright_control:data", "albright_control:backup")),
        ("Audit log", "albright_control:audit", ("albright_control:audit",)),
    )),
    ("site", "Site", (
        ("Resume", "albright_resume_admin:overview", ("albright_resume_admin:*",)),
        ("Home page", "labs_admin:settings", ("labs_admin:*",)),
        ("Users", "albright_control:users", ("albright_control:users",)),
    )),
)

# Late/failing jobs come from every job's history - recounted at most once a minute.
HEALTH_CACHE_KEY, HEALTH_CACHE_SECONDS = "admin_nav_job_problems", 60


def _job_problems():
    counts = cache.get(HEALTH_CACHE_KEY)
    if counts is None:
        from . import jobs
        rows = jobs.health_rows()
        counts = (sum(1 for r in rows if r["status"] == "failing"), sum(1 for r in rows if r["status"] == "late"))
        cache.set(HEALTH_CACHE_KEY, counts, HEALTH_CACHE_SECONDS)
    return counts


def attention():
    """URL name -> (count, tone) for the items that need a look."""
    from albright_bot_api.models import SiteSuggestion
    from albright_resume.models import ChatConversation

    from .models import Idea
    failing, late = _job_problems()
    found = {
        "albright_control:suggestions": (SiteSuggestion.objects.filter(status="new").count(), "warn"),
        "albright_control:ideas": (Idea.objects.filter(status="new").count(), "warn"),
        "albright_control:health": (failing + late, "loss" if failing else "warn"),
        "albright_resume_admin:overview": (ChatConversation.objects.filter(reviewed=False).count(), "warn"),
    }
    return {name: value for name, value in found.items() if value[0]}


def _matches(page, patterns):
    namespace = page.split(":", 1)[0]
    return any(p == page or p == f"{namespace}:*" for p in patterns)


def groups(resolver_match):
    page = f"{resolver_match.namespace}:{resolver_match.url_name}" if resolver_match else ""
    badges = attention()
    out = []
    for key, label, items in GROUPS:
        rows = []
        for item_label, url_name, patterns in items:
            count, tone = badges.get(url_name, (0, ""))
            rows.append({"label": item_label, "url": reverse(url_name), "active": _matches(page, patterns),
                         "badge": count, "tone": tone})
        total = sum(r["badge"] for r in rows)
        out.append({"key": key, "label": label, "items": rows, "dropdown": len(rows) > 1,
                    "active": any(r["active"] for r in rows), "badge": total,
                    "tone": "loss" if any(r["tone"] == "loss" for r in rows if r["badge"]) else "warn"})
    return out


def admin_nav(request):
    """Context processor: the groups, only on Admin pages and only for superusers."""
    user = getattr(request, "user", None)
    match = getattr(request, "resolver_match", None)
    from albright_trading_app.context_processors import app_key_for
    if not (user and user.is_authenticated and user.is_superuser) or match is None \
            or app_key_for(match.app_name) != "admin":
        return {}
    return {"admin_nav": groups(match)}
