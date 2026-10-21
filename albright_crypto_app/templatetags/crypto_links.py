"""{% load crypto_links %}

{% external_link url %}            - an <a> (or plain text for a non-web URL)
{% external_link url "label" %}    - with custom link text
{{ url|outbound }}                 - just the href
{% via_link opp %}                 - "via Remotive" / "via Remote OK", linking to the job on that site

Allowlisted domains open directly in a new tab; everything else goes
through the "not on your verified list" warning page first (links.py).
"""
from django import template
from django.utils.html import format_html

from ..collectors.job_boards import _on_site
from ..links import link_status, outbound_href

register = template.Library()


@register.filter
def outbound(url):
    return outbound_href(url)


@register.simple_tag
def external_link(url, label=None):
    label = label if label not in (None, "") else url
    href = outbound_href(url)
    if not href:
        return format_html('<span class="crypto-nolink" title="Not a web link">{}</span>', label)
    domain, status = link_status(url)
    if status == "allowlisted":
        return format_html('<a href="{}" target="_blank" rel="noopener noreferrer" class="crypto-link">{}</a>',
                           href, label)
    return format_html('<a href="{}" class="crypto-link crypto-link--unverified" '
                       'title="Not on your verified list - you\'ll see a warning first">{} '
                       '<span class="badge warn">unverified</span></a>', href, label)


@register.simple_tag
def via_link(opp):
    """The credit Remotive and Remote OK require: "via <site>", linking to
    the job's page on their site as a normal, followed link (no nofollow;
    the referrer is kept so they see the visit came from their listing).
    Empty for other sources; plain text if the URL somehow isn't theirs."""
    attribution = opp.source.attribution
    if not attribution:
        return ""
    name, domain = attribution
    if not _on_site(opp.url, domain):
        return format_html('<span class="crypto-via">via {}</span>', name)
    return format_html('<a href="{}" target="_blank" rel="noopener" class="crypto-link crypto-via">via {}</a>',
                       opp.url, name)
