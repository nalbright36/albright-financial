"""http(s)-only URL check for links the bot sends."""
from urllib.parse import urlparse


def is_http_url(value):
    try:
        parsed = urlparse(value)
    except ValueError:
        return False
    return parsed.scheme in ("http", "https") and bool(parsed.hostname)
