"""The assistant's replies as safe HTML: Markdown (the Notes app's extensions), then an allowlist sanitizer (nh3) -
the Notes tags minus images and big headings (#-headings become h4). Links: this site's paths as they are, mailto,
and http(s) through links.outbound_href with this app's own warning page (assistant:leave - the Notes and
Opportunities ones need those apps); everything else is dropped. Links open in a new tab with noopener."""
import re

import markdown
import nh3

from albright_labs.links import outbound_href
from albright_notes import render as notes_render

TAGS = notes_render.TAGS - {"img", "h1", "h2", "h3"}
ATTRIBUTES = {k: v for k, v in notes_render.ATTRIBUTES.items() if k != "img"}
HEADING = re.compile(r"^#{1,3}\s", re.M)


def href(url):
    """An external link's href (direct when verified, else via the warning page), or "" for none."""
    return outbound_href(url, leave="assistant:leave")


def _filter(tag, attribute, value):
    if tag != "a" or attribute != "href":
        return value
    value = (value or "").strip()
    if (value.startswith("/") and not value.startswith("//")) or value.startswith("#") \
            or value.lower().startswith("mailto:"):
        return value
    return href(value) or None


def markdown_html(text):
    source = HEADING.sub("#### ", text or "")
    html = markdown.markdown(source, extensions=notes_render.EXTENSIONS, output_format="html")
    return nh3.clean(html, tags=TAGS, attributes=ATTRIBUTES, url_schemes={"http", "https", "mailto"},
                     attribute_filter=_filter, link_rel="noopener noreferrer",
                     set_tag_attribute_values={"a": {"target": "_blank"}})
