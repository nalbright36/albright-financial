"""Outbound http(s) links: only a verified (allowlisted) domain links
directly; anything else goes through a "not on your verified list" page.

is_web_url / link_status / outbound_href used to live in the Opportunities
app. Notes, Planner and Admin use them too, so they live here now.
"""
from urllib.parse import quote, urlparse

from django.urls import reverse


def is_web_url(url):
    try:
        parsed = urlparse((url or "").strip())
    except ValueError:
        return False
    return parsed.scheme in ("http", "https") and bool(parsed.hostname)


def allowlist():
    """Verified domains for outbound links. Uses the Opportunities allowlist
    (built-in boards + Settings overrides) while that app still stores it."""
    try:
        from albright_crypto_app import config
        return config.allowlist()
    except Exception:
        return []


def link_status(url):
    """(domain, status) - status is check_domain's, or "invalid" for a non-web URL."""
    from albright_crypto_app.core.safety import check_domain
    if not is_web_url(url):
        return "", "invalid"
    return check_domain(url, allowlist())


def outbound_href(url, leave="albright_crypto_app:leave"):
    """Where an <a> for this external URL should point, or "" for no link.

    leave is the URL name of the warning page (Opportunities by default;
    Notes uses its own leave view instead of this helper).
    """
    if not is_web_url(url):
        return ""
    if link_status(url)[1] == "allowlisted":
        return url
    return reverse(leave) + "?to=" + quote(url, safe="")
