"""{% load crypto_links %}

{% external_link url %}            - an <a> (or plain text for a non-web URL)
{% external_link url "label" %}    - with custom link text
{{ url|outbound }}                 - just the href

Allowlisted domains open directly in a new tab; everything else goes
through the "not on your verified list" warning page first (links.py).
"""
from django import template
from django.utils.html import format_html

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
