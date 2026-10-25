"""Small text/URL helpers shared by the collectors and the rest of the app
(templatetags.crypto_links, inbox_views). Standard library only - this
module must never import a collector (or anything that does), so importing
it can't start an import cycle."""
import html
import re
from urllib.parse import urlparse


def on_site(url, domain):
    """An https URL on the domain or one of its subdomains (job boards' own links)."""
    host = (urlparse(url or "").hostname or "").lower()
    return urlparse(url or "").scheme == "https" and (host == domain or host.endswith("." + domain))


def plain(text):
    """HTML (or plain text) -> readable plain text."""
    text = re.sub(r"<(br|/p|/li|/h\d)\s*/?>", "\n", str(text or ""), flags=re.I)
    text = html.unescape(re.sub(r"<[^>]+>", " ", text))
    text = re.sub(r"[ \t]+", " ", re.sub(r"\n\s*\n+", "\n\n", text))
    return re.sub(r" +([.,;:!?)])", r"\1", text).strip()  # "<i>pipelines</i>." -> "pipelines."


def facts_text(description, facts):
    """The description, then "Label: value" lines for the stated facts."""
    lines = [f"{label}: {value}" for label, value in facts if value not in (None, "", [])]
    return "\n".join(filter(None, [plain(description), "\n".join(lines)]))
