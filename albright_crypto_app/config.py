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
    # Relevance (relevance.py): Settings > Filters, saved as one value.
    "filters": None,   # None -> FILTER_DEFAULTS below
}
EDITABLE_KEYS = ("allowlist", "rss_feeds", "skills", "filters")

# Settings > Filters, as saved (ConfigOverride "filters"). Pay floors apply
# only when a posting states its pay.
FILTER_DEFAULTS = {
    "work_types": ["full_time", "contract", "freelance", "part_time", "bounty", "paid_task"],
    "remote_us": True,                  # remote roles open to the US
    "local": True,                      # hybrid / on-site near home
    "home": "Tampa, FL",
    "radius_miles": 30,
    "min_salary_year": 70000,           # full-time
    "min_rate_hour": 40,                # contract / freelance
    "min_fixed_project": 500,           # contract / freelance fixed price
    "exclude_entry": True,              # intern / entry-level only
    "exclude_executive": True,          # VP / C-level
    "min_fit_score": 60,                # a job/freelance fit score below this -> Hidden ("low fit")
    "negative_keywords": ["nurse", "driver", "warehouse", "cashier", "sales representative", "commission only",
                          "call center", "teacher", "physician", "Java developer", ".NET developer", "iOS",
                          "Android", "embedded", "clearance required"],
}
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


# The job-board adapters' sites (models.Source.ATTRIBUTED_KINDS), HN and We Work Remotely - always verified.
BUILT_IN_ALLOWLIST = ("remotive.com", "remoteok.com", "himalayas.app", "jobicy.com", "arbeitnow.com",
                      "news.ycombinator.com", "weworkremotely.com")


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
    """My skills: My profile's list once it has one (Settings > Profile),
    otherwise the older Settings list / settings.py."""
    from .models import MyProfile
    profile = MyProfile.objects.filter(pk=MyProfile.SINGLETON_PK).only("skills").first()
    mine = profile.skills if profile and profile.skills else get("skills")
    return [str(s).strip() for s in mine or [] if str(s).strip()]


def filters():
    saved = get("filters") or {}
    return {**FILTER_DEFAULTS, **{k: v for k, v in saved.items() if k in FILTER_DEFAULTS}}


def fit_min_score():
    return int(filters().get("min_fit_score") or 0)
