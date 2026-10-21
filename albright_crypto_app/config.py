"""The Opportunities app's settings (settings.py CRYPTO - the key keeps its
old name), with the Settings page's edits (ConfigOverride)
taking priority for the keys it can edit, and settings.py otherwise."""
from django.conf import settings

DEFAULTS = {
    "budget_usd": 0, "allowlist": ["github.com"], "rss_feeds": [],
    "github_queries": ["label:bounty state:open", 'label:"💎 Bounty" state:open'],
    "scan_interval_hours": 4, "alert_min_ev_per_hour": 15, "alert_min_confidence": "medium", "ai_daily_limit": 30,
    # Income: my skills profile (Settings page), the skill_match below which
    # freelance work is hidden by default, and the one a freelance alert needs.
    "skills": ["Python", "Django", "PostgreSQL", "data pipelines", "API integrations", "web scraping", "Excel", "VBA",
               "Power BI", "automation", "financial applications"],
    "freelance_min_skill_match": 50, "alert_min_skill_match": 60,
    # HN Who is hiring: a remote (non-contract) posting is kept from this skill match up.
    "hn_min_skill_match": 60,
    # Generated proposals stay under this many words.
    "proposal_max_words": 200,
}
EDITABLE_KEYS = ("allowlist", "rss_feeds", "skills")
CONFIDENCE_RANK = {"low": 0, "medium": 1, "high": 2}


def get(key):
    if key in EDITABLE_KEYS:
        from .models import ConfigOverride
        override = ConfigOverride.objects.filter(key=key).first()
        if override is not None:
            return override.value
    return {**DEFAULTS, **getattr(settings, "CRYPTO", {})}.get(key, DEFAULTS.get(key))


def set_value(key, value):
    from .models import ConfigOverride
    if key not in EDITABLE_KEYS:
        raise KeyError(key)
    ConfigOverride.objects.update_or_create(key=key, defaults={"value": value})


# The job-board adapters' sites (models.Source.ATTRIBUTED_KINDS) and HN - always verified.
BUILT_IN_ALLOWLIST = ("remotive.com", "remoteok.com", "himalayas.app", "jobicy.com", "arbeitnow.com",
                      "news.ycombinator.com")


def allowlist():
    domains = [d.strip().lower() for d in get("allowlist") or [] if d and d.strip()]
    return domains + [d for d in BUILT_IN_ALLOWLIST if d not in domains]


def budget_usd():
    return float(get("budget_usd") or 0)


def ai_review_cfg():
    """The reseller app's AI settings (model, prices, monthly budget) - one
    shared budget across both apps' AI calls."""
    return settings.RESELLING_SCANNER["AI_REVIEW"]


def skills():
    return [str(s).strip() for s in get("skills") or [] if str(s).strip()]
