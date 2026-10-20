"""Every external link the crypto app renders goes through outbound_href():
an allowlisted domain (or a subdomain of one) links directly; anything else
links to the "This site isn't on your verified list" page (views.leave),
which shows the full domain before offering to continue. Only http(s)
links are ever rendered - anything else (javascript:, data:, a bare string)
gets no link at all."""
from urllib.parse import quote, urlparse

from django.urls import reverse

from . import config
from .core.safety import check_domain


def is_web_url(url):
    try:
        parsed = urlparse((url or "").strip())
    except ValueError:
        return False
    return parsed.scheme in ("http", "https") and bool(parsed.hostname)


def link_status(url):
    """(domain, status) - status is check_domain's, or "invalid" for a non-web URL."""
    if not is_web_url(url):
        return "", "invalid"
    return check_domain(url, config.allowlist())


def outbound_href(url):
    """Where an <a> for this external URL should point, or "" for no link."""
    if not is_web_url(url):
        return ""
    if link_status(url)[1] == "allowlisted":
        return url
    return reverse("albright_crypto_app:leave") + "?to=" + quote(url, safe="")
