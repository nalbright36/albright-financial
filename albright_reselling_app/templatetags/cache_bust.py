"""Cache-busts a shared static asset's URL with its own file's last-
modified time, so editing a stable-URL file like static/js/
auction_scanner.js always forces browsers (and any CDN/proxy in front of
them) to fetch the new copy instead of silently keeping an old cached one
under the exact same /static/... URL forever."""
import os

from django import template
from django.contrib.staticfiles import finders
from django.templatetags.static import static as static_url

register = template.Library()


@register.simple_tag
def static_version(path):
    url = static_url(path)
    absolute_path = finders.find(path)
    if not absolute_path:
        return url
    try:
        version = int(os.path.getmtime(absolute_path))
    except OSError:
        return url
    return f"{url}?v={version}"
