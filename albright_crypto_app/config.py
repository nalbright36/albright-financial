"""The CRYPTO settings, with the Settings page's edits (ConfigOverride)
taking priority for the keys it can edit, and settings.py otherwise."""
from django.conf import settings

DEFAULTS = {
    "budget_usd": 0, "allowlist": ["github.com"], "rss_feeds": [],
    "github_queries": ["label:bounty state:open", 'label:"💎 Bounty" state:open'],
    "scan_interval_hours": 4, "alert_min_ev_per_hour": 15, "alert_min_confidence": "medium", "ai_daily_limit": 30,
}
EDITABLE_KEYS = ("allowlist", "rss_feeds")
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


def allowlist():
    return [d.strip().lower() for d in get("allowlist") or [] if d and d.strip()]


def budget_usd():
    return float(get("budget_usd") or 0)


def ai_review_cfg():
    """The reseller app's AI settings (model, prices, monthly budget) - one
    shared budget across both apps' AI calls."""
    return settings.RESELLING_SCANNER["AI_REVIEW"]
