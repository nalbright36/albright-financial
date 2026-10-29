"""Import a recipe from a link - the website's "Import from link" box, the
recipe page's Re-import, and the Bot API's meals/recipes/import.

  fetch()              GETs ONE page - the link given, never anything it links to.
                       http(s) only, ports 80/443, no user:password@. Every
                       address the host resolves to must be public (no
                       private, loopback, link-local, reserved...) - checked
                       before each request and again when the socket connects
                       (so DNS can't switch to a private address in between).
                       10 seconds in all, at most 3 redirects (each re-checked),
                       at most 3 MB, no proxies.
  parse_recipe_html()  The page's schema.org Recipe: JSON-LD (@graph, lists of
                       types, nested objects, @id references), else microdata.
  ai_extract()         No recipe data: the AI reads the page's VISIBLE text, with
                       the shared OpenAI setup, daily limit and monthly budget
                       (albright_crypto_app.ai_tasks). Only what's on the page:
                       every ingredient line, step, time and serving count it
                       returns must appear in that text, or it's dropped. These
                       recipes are marked needs_review.
  parse_ingredient()   "1 1/2 cups flour, sifted" -> quantity/unit/name/note,
                       keeping the line exactly as written; a line that doesn't
                       split still keeps its text as the name.
  normalize_url()      The key for "already saved": lowercase host without www.,
                       no tracking parameters, no fragment, sorted query.

Everything above is a pure function apart from fetch() and ai_extract(); the
database helpers (find_saved, save_new, reimport, import_url) are at the end.
"""
import html as html_lib
import ipaddress
import json
import re
import socket
import time
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import requests
from requests.adapters import HTTPAdapter
from urllib3.connection import HTTPConnection, HTTPSConnection
from urllib3.connectionpool import HTTPConnectionPool, HTTPSConnectionPool
from urllib3.exceptions import NewConnectionError

from . import groceries

TIMEOUT = 10                     # seconds, for the whole fetch (redirects included)
MAX_REDIRECTS = 3
MAX_BYTES = 3 * 1024 * 1024
MAX_URL = 2000
ALLOWED_PORTS = (80, 443)
MAX_MINUTES = 10080              # a week - anything longer isn't a real time
MAX_INGREDIENTS = MAX_STEPS = 150
MAX_AI_TEXT = 20000              # characters of visible text sent to the AI
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/130.0 Safari/537.36 AlbrightMeals/1.0 (recipe import; +https://www.sportslabalgo.com)")
HEADERS = {"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5",
           "Accept-Language": "en-US,en;q=0.8"}

NOT_FOUND = "No recipe found on that page."
BLOCKED = "That link can't be imported - only public http(s) web pages."
TOO_LARGE = "That page is too big to import (over 3 MB)."
TIMED_OUT = "That page took too long to load (over 10 seconds)."


class RecipeImportError(Exception):
    """A clear, user-facing reason. code: blocked, fetch, timeout, too_large, not_found, ai_limit, ai_unavailable."""

    def __init__(self, message, code="fetch"):
        super().__init__(message)
        self.message, self.code = message, code


# ---------------------------------------------------------------------------
# Links: the "already saved" key
# ---------------------------------------------------------------------------

_TRACKING = re.compile(r"^(utm_\w*|fbclid|gclid|gbraid|wbraid|dclid|msclkid|yclid|mc_cid|mc_eid|igshid|igsh|_ga|_gl|"
                       r"_hsenc|_hsmi|mkt_tok|ref|ref_src|si|fb_action_ids|fb_action_types|fb_source|s_cid|cmpid|"
                       r"ncid|epik)$", re.I)


def clean_url(url):
    """The link without tracking parameters or a #fragment (scheme and host lowercased) - what's stored."""
    try:
        parts = urlsplit((url or "").strip())
    except ValueError:
        return (url or "").strip()
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not _TRACKING.match(k)]
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, urlencode(query), ""))


def normalize_url(url):
    """"https://WWW.Example.com/Pasta/?utm_source=x&b=2&a=1#top" -> "example.com/Pasta?a=1&b=2".
    http and https give the same key; so do a trailing slash and none."""
    if not (url or "").strip():
        return ""
    try:
        parts = urlsplit(clean_url(url))
        host = (parts.hostname or "").lower()
        port = parts.port
    except ValueError:
        return url.strip().lower()
    if host.startswith("www."):
        host = host[4:]
    if port and port not in ALLOWED_PORTS:
        host = f"{host}:{port}"
    path = re.sub(r"/{2,}", "/", parts.path or "/")
    if len(path) > 1:
        path = path.rstrip("/")
    query = urlencode(sorted(parse_qsl(parts.query, keep_blank_values=True)))
    return f"{host}{path}" + (f"?{query}" if query else "")


def is_web_url(url):
    try:
        parts = urlsplit((url or "").strip())
    except ValueError:
        return False
    return parts.scheme in ("http", "https") and bool(parts.hostname)


# ---------------------------------------------------------------------------
# Fetching one page (SSRF-safe)
# ---------------------------------------------------------------------------

def address_allowed(address):
    """Only public internet addresses - never private, loopback, link-local, multicast, reserved..."""
    try:
        ip = ipaddress.ip_address(str(address).split("%", 1)[0])
    except ValueError:
        return False
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


def resolve_public(host, port):
    """Every address the host resolves to (RecipeImportError unless all of them are public)."""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError, OSError) as exc:
        raise RecipeImportError("Couldn't find that website - check the link.", "fetch") from exc
    addresses = list(dict.fromkeys(info[4][0] for info in infos))
    if not addresses or not all(address_allowed(a) for a in addresses):
        raise RecipeImportError(BLOCKED, "blocked")
    return addresses


def check_url(url):
    """RecipeImportError unless this is a public http(s) page we may fetch."""
    if not url or len(url) > MAX_URL:
        raise RecipeImportError(BLOCKED, "blocked")
    try:
        parts = urlsplit(url)
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError as exc:
        raise RecipeImportError(BLOCKED, "blocked") from exc
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password \
            or port not in ALLOWED_PORTS:
        raise RecipeImportError(BLOCKED, "blocked")
    resolve_public(parts.hostname, port)


class _PublicOnlyConnection:
    """Connects only to a public address - resolved and checked right here, at connect time."""

    def _new_conn(self):
        timeout = self.timeout if isinstance(self.timeout, (int, float)) else TIMEOUT
        last = None
        for address in resolve_public(self._dns_host, self.port):
            try:
                sock = socket.create_connection((address, self.port), timeout, source_address=self.source_address)
            except OSError as exc:
                last = exc
                continue
            for option in self.socket_options or []:
                sock.setsockopt(*option)
            return sock
        raise NewConnectionError(self, f"Failed to establish a new connection: {last}")


class _HTTPConnection(_PublicOnlyConnection, HTTPConnection):
    pass


class _HTTPSConnection(_PublicOnlyConnection, HTTPSConnection):
    pass


class _HTTPPool(HTTPConnectionPool):
    ConnectionCls = _HTTPConnection


class _HTTPSPool(HTTPSConnectionPool):
    ConnectionCls = _HTTPSConnection


class _PublicOnlyAdapter(HTTPAdapter):
    def init_poolmanager(self, *args, **kwargs):
        super().init_poolmanager(*args, **kwargs)
        self.poolmanager.pool_classes_by_scheme = {"http": _HTTPPool, "https": _HTTPSPool}


def make_session():
    session = requests.Session()
    session.trust_env = False          # never through a proxy from the environment - it would do the connecting
    adapter = _PublicOnlyAdapter(max_retries=0)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


def _decode(body, content_type):
    match = re.search(r"charset=[\"']?([\w-]+)", content_type or "", re.I) \
        or re.search(rb"<meta[^>]+charset=[\"']?([\w-]+)", body[:4096], re.I)
    encoding = match.group(1) if match else "utf-8"
    encoding = encoding.decode("ascii", "ignore") if isinstance(encoding, bytes) else encoding
    try:
        return body.decode(encoding, errors="replace")
    except LookupError:
        return body.decode("utf-8", errors="replace")


def fetch(url, session=None, clock=time.monotonic):
    """GETs the one page -> (its final URL, its HTML). Raises RecipeImportError."""
    session = session or make_session()
    deadline = clock() + TIMEOUT
    current = url.strip()
    for _hop in range(MAX_REDIRECTS + 1):
        check_url(current)
        remaining = deadline - clock()
        if remaining <= 0:
            raise RecipeImportError(TIMED_OUT, "timeout")
        try:
            response = session.get(current, headers=HEADERS, timeout=(remaining, remaining), allow_redirects=False,
                                   stream=True)
        except requests.Timeout as exc:
            raise RecipeImportError(TIMED_OUT, "timeout") from exc
        except requests.RequestException as exc:
            raise RecipeImportError("Couldn't load that page.", "fetch") from exc
        try:
            if response.is_redirect:
                current = urljoin(current, response.headers["Location"])
                continue
            if response.status_code >= 400:
                raise RecipeImportError(f"That page answered with an error ({response.status_code}).", "fetch")
            content_type = response.headers.get("Content-Type", "")
            if content_type and not re.search(r"html|xml", content_type, re.I):
                raise RecipeImportError("That link isn't a web page.", "fetch")
            length = response.headers.get("Content-Length", "")
            if length.isdigit() and int(length) > MAX_BYTES:
                raise RecipeImportError(TOO_LARGE, "too_large")
            body = bytearray()
            try:
                for chunk in response.iter_content(64 * 1024):
                    body += chunk
                    if len(body) > MAX_BYTES:
                        raise RecipeImportError(TOO_LARGE, "too_large")
                    if clock() > deadline:
                        raise RecipeImportError(TIMED_OUT, "timeout")
            except requests.RequestException as exc:
                raise RecipeImportError("Couldn't load that page.", "fetch") from exc
            return current, _decode(bytes(body), content_type)
        finally:
            response.close()
    raise RecipeImportError("That link redirects too many times.", "fetch")


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------

_BLOCK_TAGS = {"p", "div", "li", "br", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "ol", "ul", "section", "article",
               "header", "footer", "table", "dd", "dt", "blockquote", "pre", "hr", "figure", "figcaption", "main",
               "aside", "nav", "form", "fieldset", "details", "summary", "dl"}
_INVISIBLE_TAGS = {"script", "style", "noscript", "template", "head", "svg", "iframe", "object", "canvas", "math",
                   "select", "option", "button", "input", "textarea"}
_HIDDEN_STYLE = re.compile(r"display\s*:\s*none|visibility\s*:\s*hidden", re.I)


def _element_text(element, skip_hidden=False):
    """An element's text with a line break around every block element (so list items stay separate lines)."""
    parts = []

    def walk(node):
        tag = node.tag.lower() if isinstance(node.tag, str) else None
        if tag is None:                                     # a comment or processing instruction
            if node.tail:
                parts.append(node.tail)
            return
        hidden = tag in _INVISIBLE_TAGS or (skip_hidden and (
            node.get("hidden") is not None or node.get("aria-hidden") == "true"
            or _HIDDEN_STYLE.search(node.get("style") or "")))
        if not hidden:
            block = tag in _BLOCK_TAGS
            if block:
                parts.append("\n")
            if node.text:
                parts.append(node.text)
            for child in node:
                walk(child)
            if block:
                parts.append("\n")
        if node.tail:
            parts.append(node.tail)

    walk(element)
    lines = (" ".join(line.split()) for line in "".join(parts).splitlines())
    return "\n".join(line for line in lines if line)


def _clean(value, keep_lines=False):
    """Text from JSON-LD/microdata: HTML tags removed, entities decoded (twice - some sites double-encode)."""
    if value is None or isinstance(value, (dict, list)):
        return ""
    text = str(value)
    for _ in range(2):
        unescaped = html_lib.unescape(text)
        if unescaped == text:
            break
        text = unescaped
    if "<" in text and ">" in text:
        from lxml import html as lxml_html
        try:
            text = _element_text(lxml_html.fragment_fromstring(text, create_parent="div"))
        except Exception:  # noqa: BLE001 - not really HTML; strip anything tag-shaped instead
            text = re.sub(r"<[^>]+>", " ", text)
    text = text.replace("\xa0", " ")
    if keep_lines:
        lines = (" ".join(line.split()) for line in text.splitlines())
        return "\n".join(line for line in lines if line)
    return " ".join(text.split())


def _as_list(value):
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _types(node):
    """{"recipe", "howtostep", ...} - the last part of each @type, lowercase."""
    if not isinstance(node, dict):
        return set()
    return {re.split(r"[/#:]", str(t).rstrip("/"))[-1].lower() for t in _as_list(node.get("@type")) if t}


def _first_text(value, index=None):
    for item in _as_list(value):
        item = _resolve(item, index)
        if isinstance(item, dict):
            text = _first_text(item.get("name") or item.get("text") or item.get("@value"), index)
        else:
            text = _clean(item)
        if text:
            return text
    return ""


def _resolve(value, index):
    """{"@id": "#author"} -> the node with that @id elsewhere on the page (when it's only a reference)."""
    if index and isinstance(value, dict) and set(value) <= {"@id", "@type"} and value.get("@id") in index:
        return index[value["@id"]]
    return value


# ---------------------------------------------------------------------------
# Durations, servings, ingredients
# ---------------------------------------------------------------------------

_NUM = r"(?:\d+(?:\.\d+)?)"
_ISO_DURATION = re.compile(rf"^P(?:({_NUM})Y)?(?:({_NUM})M)?(?:({_NUM})W)?(?:({_NUM})D)?"
                           rf"(?:T(?:({_NUM})H)?(?:({_NUM})M)?(?:({_NUM})S)?)?$", re.I)
_HOURS = re.compile(r"(\d+(?:\.\d+)?)\s*(?:h|hr|hrs|hour|hours)\b", re.I)
_MINUTES = re.compile(r"(\d+)\s*(?:m|min|mins|minute|minutes)\b", re.I)


def iso_minutes(value):
    """ISO 8601 duration -> whole minutes: "PT1H30M" -> 90, "P0DT0H20M" -> 20, "PT45S" -> 1.
    Also reads plain text ("1 hr 15 mins"). None when empty, zero or unreadable."""
    text = _clean(value[0] if isinstance(value, list) and value else value).strip()
    if not text:
        return None
    match = _ISO_DURATION.match(text.upper().replace(",", "."))
    if match and text.upper() not in ("P", "PT"):
        years, months, weeks, days, hours, minutes, seconds = (float(g) if g else 0 for g in match.groups())
        total = (((years * 365 + months * 30 + weeks * 7 + days) * 24 + hours) * 60 + minutes) + seconds / 60
    else:
        hours = sum(float(h) for h in _HOURS.findall(text))
        minutes = sum(int(m) for m in _MINUTES.findall(text))
        total = hours * 60 + minutes
        if not total and text.isdigit():
            total = int(text)                                # a bare number of minutes
    total = int(-(-total // 1))                              # round seconds up to a minute
    return total if 0 < total <= MAX_MINUTES else None


def servings_from(value):
    """recipeYield -> a number of servings (1-100): 4, "4", "4-6 servings", ["4", "4 servings"]."""
    for item in _as_list(value):
        if isinstance(item, bool):
            continue
        if isinstance(item, (int, float)):
            number = int(item)
        else:
            match = re.search(r"\d+", _clean(item))
            number = int(match.group()) if match else 0
        if 1 <= number <= 100:
            return number
    return None


_FRACTION_CHARS = {"½": "1/2", "⅓": "1/3", "⅔": "2/3", "¼": "1/4", "¾": "3/4", "⅕": "1/5", "⅖": "2/5", "⅗": "3/5",
                   "⅘": "4/5", "⅙": "1/6", "⅚": "5/6", "⅛": "1/8", "⅜": "3/8", "⅝": "5/8", "⅞": "7/8"}
_QTY = r"(?:\d+\s+\d+/\d+|\d+/\d+|\d+(?:\.\d+)?|\.\d+)"
_RANGE = re.compile(rf"^({_QTY})\s*(?:-|–|—|to)\s*({_QTY})(?![\w/])\s*(.*)$", re.I)
_LEADING_QTY = re.compile(rf"^({_QTY})(?![\w/])\s*(.*)$")


def _ascii_fractions(text):
    """"1½" -> "1 1/2", "½" -> "1/2", "1⁄2" (fraction slash) -> "1/2"."""
    text = text.replace("⁄", "/")
    for symbol, fraction in _FRACTION_CHARS.items():
        text = re.sub(rf"(\d)\s*{symbol}", rf"\1 {fraction}", text)
        text = text.replace(symbol, f" {fraction} ")
    return " ".join(text.split())


def parse_ingredient(line):
    """"1 1/2 cups flour, sifted" -> {"name": "flour", "quantity": "1.5", "unit": "cup", "note": "sifted",
    "original": "1 1/2 cups flour, sifted"}. A range ("2-3 cloves") takes the larger number. Never loses a
    line: whatever doesn't split stays in the name, and "original" is always the line as written."""
    original = str(line or "").strip()
    text = _ascii_fractions(original)
    quantity, unit, note = None, "", ""
    rest = text
    match = _RANGE.match(text)
    if match:
        low, high = groceries.parse_quantity(match.group(1)), groceries.parse_quantity(match.group(2))
        if low is not None and high is not None:
            quantity, rest = max(low, high), match.group(3)
    if quantity is None:
        match = _LEADING_QTY.match(text)
        if match and groceries.parse_quantity(match.group(1)) is not None:
            quantity, rest = groceries.parse_quantity(match.group(1)), match.group(2)
    rest = rest.strip()
    notes = []
    if quantity is not None:
        def leading_paren(text):                                    # "1 (14 oz) can", "4 cups (1 l) broth"
            paren = re.match(r"^\(([^)]*)\)\s*(.*)$", text)
            if paren:
                notes.append(paren.group(1).strip())
                return paren.group(2)
            return text

        rest = leading_paren(rest)
        first, _, remainder = rest.partition(" ")
        if first.lower().rstrip(".") in groceries.UNITS and remainder.strip():
            unit, rest = groceries.normalize_unit(first), leading_paren(remainder.strip())
        if rest.lower().startswith("of "):
            rest = rest[3:]
    trailing = re.search(r"\(([^)]*)\)\s*$", rest)
    if trailing and trailing.start() > 0:
        notes.append(trailing.group(1).strip())
        rest = rest[:trailing.start()].strip()
    if "," in rest:
        rest, comma_note = (part.strip() for part in rest.split(",", 1))
        notes.append(comma_note)
    name = rest.strip(" ,;-") or original
    note = "; ".join(n for n in notes if n)
    return {"name": name[:groceries.MAX_NAME], "quantity": groceries.format_quantity(quantity), "unit": unit,
            "note": note[:groceries.MAX_NOTE], "original": original[:500]}


def ingredient_lines(recipe_ingredients):
    """Stored ingredients back to one line each (the original line when there is one)."""
    from .forms import ingredient_line
    return [i["original"] if isinstance(i, dict) and i.get("original") else ingredient_line(i)
            for i in recipe_ingredients or []]


def step_groups(steps):
    """["## Sauce", "Whisk.", "Simmer."] -> [{"heading": "Sauce", "start": 1, "steps": ["Whisk.", "Simmer."]}]
    - numbering carries on across sections."""
    groups, number = [], 1
    for step in steps or []:
        text = str(step)
        if text.startswith("## "):
            groups.append({"heading": text[3:].strip(), "start": number, "steps": []})
            continue
        if not groups:
            groups.append({"heading": "", "start": number, "steps": []})
        groups[-1]["steps"].append(text)
        number += 1
    return [g for g in groups if g["steps"] or g["heading"]]


# ---------------------------------------------------------------------------
# Structured data: JSON-LD, then microdata
# ---------------------------------------------------------------------------

def _document(html_text):
    from lxml import html as lxml_html
    text = re.sub(r"^\s*<\?xml[^>]*\?>", "", html_text or "")
    if not text.strip():
        return None
    try:
        return lxml_html.document_fromstring(text)
    except Exception:  # noqa: BLE001 - lxml's ParserError and friends: not a page we can read
        return None


def _json_ld_blocks(doc):
    blocks = []
    for script in doc.xpath("//script[@type]"):
        if script.get("type", "").split(";")[0].strip().lower() != "application/ld+json":
            continue
        text = (script.text or "").strip()
        text = re.sub(r"^\s*(?:<!--|<!\[CDATA\[)|(?:-->|\]\]>)\s*$", "", text).strip().rstrip(";")
        try:
            blocks.append(json.loads(text, strict=False))
        except ValueError:
            continue
    return blocks


def _walk_nodes(data, depth=0):
    """Every JSON-LD object on the page, nested ones included."""
    if depth > 12:
        return
    if isinstance(data, list):
        for item in data:
            yield from _walk_nodes(item, depth + 1)
    elif isinstance(data, dict):
        yield data
        for value in data.values():
            if isinstance(value, (dict, list)):
                yield from _walk_nodes(value, depth + 1)


def _id_index(blocks):
    return {node["@id"]: node for node in _walk_nodes(blocks)
            if isinstance(node.get("@id"), str) and len(node) > 2}


def _microdata_value(element):
    tag = element.tag.lower() if isinstance(element.tag, str) else ""
    if element.get("content") is not None:
        return element.get("content")
    if tag in ("a", "link", "area"):
        return element.get("href", "")
    if tag in ("img", "audio", "embed", "iframe", "source", "track", "video"):
        return element.get("src", "")
    if tag == "object":
        return element.get("data", "")
    if tag == "time" and element.get("datetime"):
        return element.get("datetime")
    if tag in ("data", "meter") and element.get("value") is not None:
        return element.get("value")
    return _element_text(element)


def _microdata_item(scope):
    item = {"@type": (scope.get("itemtype") or "").split()}
    pending = list(scope)
    while pending:
        element = pending.pop(0)
        if not isinstance(element.tag, str):
            continue
        prop = element.get("itemprop")
        nested = element.get("itemscope") is not None
        if prop:
            value = _microdata_item(element) if nested else _microdata_value(element)
            for name in prop.split():
                item.setdefault(name, []).append(value)
        if not nested:
            pending[0:0] = list(element)
    return item


def _microdata_recipe(doc):
    for element in doc.xpath("//*[@itemscope][@itemtype]"):
        if "recipe" in _types({"@type": element.get("itemtype").split()}):
            return _microdata_item(element)
    return None


def _instructions(value, index):
    steps = []

    def add(text):
        for line in _clean(text, keep_lines=True).splitlines():
            line = re.sub(r"^(?:step\s*)?\d{1,2}\s*[.):]\s+", "", line.strip(), flags=re.I).strip()
            if line:
                steps.append(line[:2000])

    def walk(item, depth=0):
        if depth > 8 or len(steps) >= MAX_STEPS:
            return
        item = _resolve(item, index)
        if isinstance(item, list):
            for part in item:
                walk(part, depth + 1)
        elif isinstance(item, dict):
            types = _types(item)
            if "howtosection" in types or ("itemListElement" in item and not item.get("text")):
                heading = _first_text(item.get("name"))
                if heading and "howtostep" not in types:
                    steps.append(f"## {heading[:200]}")
                walk(item.get("itemListElement"), depth + 1)
            else:
                text = item.get("text") or item.get("name") or item.get("description")
                add(text[0] if isinstance(text, list) and text else text)
        elif item not in (None, ""):
            add(item)

    walk(value)
    return steps[:MAX_STEPS]


def _image(value, base, index):
    for item in _as_list(value):
        item = _resolve(item, index)
        if isinstance(item, dict):
            item = item.get("url") or item.get("contentUrl") or ""
        if isinstance(item, list):
            item = item[0] if item else ""
        url = urljoin(base, _clean(item)) if item else ""
        if is_web_url(url) and len(url) <= 500:
            return url
    return ""


def _people(value, index):
    names = []
    for item in _as_list(value):
        name = _first_text(_resolve(item, index), index)
        if name and name not in names:
            names.append(name)
    return ", ".join(names)[:200]


def _page_meta(doc, page_url):
    def meta(*names):
        for name in names:
            for element in doc.xpath(f'//meta[@property="{name}" or @name="{name}"]'):
                if (element.get("content") or "").strip():
                    return element.get("content").strip()
        return ""

    canonical = ""
    for element in doc.xpath("//link[@rel][@href]"):
        if "canonical" in element.get("rel", "").lower().split():
            canonical = urljoin(page_url, element.get("href").strip())
            break
    canonical = canonical or (urljoin(page_url, meta("og:url")) if meta("og:url") else "")
    if not is_web_url(canonical) or len(canonical) > 500:
        canonical = ""
    titles = doc.xpath("//title")
    return {"canonical": canonical, "site_name": _clean(meta("og:site_name", "application-name")),
            "title": _clean(meta("og:title")) or (_clean(titles[0].text_content()) if titles else ""),
            "image": meta("og:image")}


def _site_name(meta, node, blocks, index, page_url):
    if meta["site_name"]:
        return meta["site_name"][:120]
    for owner in [node, *_walk_nodes(blocks)]:            # the recipe's publisher, else the page's
        publisher = _first_text(_resolve(owner.get("publisher"), index), index)
        if publisher:
            return publisher[:120]
    for other in _walk_nodes(blocks):
        if "website" in _types(other) and _first_text(other.get("name")):
            return _first_text(other.get("name"))[:120]
    host = (urlsplit(page_url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def _from_schema(node, page_url, meta, blocks, index):
    ingredients = [_clean(i) for i in _as_list(node.get("recipeIngredient") or node.get("ingredients"))]
    canonical = meta["canonical"]
    return {
        "title": _first_text(node.get("name") or node.get("headline"))[:200],
        "description": _clean(_first_text(node.get("description")))[:5000],
        "ingredients": [parse_ingredient(i) for i in ingredients if i][:MAX_INGREDIENTS],
        "steps": _instructions(node.get("recipeInstructions"), index),
        "servings": servings_from(node.get("recipeYield") or node.get("yield")),
        "prep_minutes": iso_minutes(node.get("prepTime")),
        "cook_minutes": iso_minutes(node.get("cookTime")),
        "total_time_minutes": iso_minutes(node.get("totalTime")),
        "image_url": _image(node.get("image"), page_url, index) or _image(meta["image"], page_url, None),
        "author": _people(node.get("author") or node.get("creator"), index),
        "source_name": _site_name(meta, node, blocks, index, page_url),
        "source_url": clean_url(canonical or page_url)[:500],
        "page_url": page_url,
        "needs_review": False,
    }


def parse_recipe_html(html_text, page_url):
    """The page's schema.org Recipe as import fields (plus "method": "json-ld" / "microdata"), or None
    when it has none - or one without a name, ingredients or steps."""
    doc = _document(html_text)
    if doc is None:
        return None
    meta = _page_meta(doc, page_url)
    blocks = _json_ld_blocks(doc)
    index = _id_index(blocks)
    for method, node in (("json-ld", next((n for n in _walk_nodes(blocks) if "recipe" in _types(n)), None)),
                         ("microdata", _microdata_recipe(doc))):
        if node is None:
            continue
        recipe = _from_schema(node, page_url, meta, blocks, index)
        if recipe["title"] and (recipe["ingredients"] or recipe["steps"]):
            return {**recipe, "method": method}
    return None


# ---------------------------------------------------------------------------
# AI fallback: the page's visible text only
# ---------------------------------------------------------------------------

AI_INSTRUCTIONS = """You read the visible text of ONE web page and copy out the recipe on it, if there is one.

Rules:
- Use ONLY what is written in the text. Never invent, guess, estimate, convert or complete anything: no
  quantity, ingredient, step, time or serving count that isn't written there.
- Copy each ingredient line and each step WORD FOR WORD from the text, as one string each. A heading that
  starts a group of steps (e.g. "For the sauce") is its own string, written "## For the sauce".
- Anything the page doesn't say stays empty ("" or []).
- If the page has no recipe, return {"found": false}.

Return only this JSON object:
{"found": true, "title": "", "description": "", "ingredients": [""], "steps": [""],
 "servings": "the yield exactly as written", "prep_time": "as written, e.g. 15 minutes", "cook_time": "",
 "total_time": "", "author": ""}"""


def visible_text(html_text):
    """What a reader sees: no scripts, styles, head, or hidden elements."""
    doc = _document(html_text)
    if doc is None:
        return ""
    body = doc.find("body")
    return _element_text(body if body is not None else doc, skip_hidden=True)


def _squash(text):
    """For "is this on the page?": lowercase letters and digits only, fractions spelled 1/2."""
    return re.sub(r"[\W_]+", "", _ascii_fractions(html_lib.unescape(str(text or ""))).lower())


def _on_page(value, page):
    squashed = _squash(value)
    return len(squashed) >= 2 and squashed in page


def ai_extract(html_text, page_url, client=None):
    """The recipe the AI finds in the page's visible text, keeping only what's really there; None if none.
    Raises RecipeImportError when the daily AI limit / monthly budget is used up or the AI can't run."""
    from albright_crypto_app.ai_tasks import run_json
    from albright_crypto_app.extraction import ExtractionLimitReached

    text = visible_text(html_text)[:MAX_AI_TEXT]
    if len(text) < 40:
        return None
    try:
        data, _cost = run_json(AI_INSTRUCTIONS, f"PAGE: {page_url}\n\nVISIBLE TEXT:\n{text}", "recipe",
                               client=client, max_tokens=4000)
    except ExtractionLimitReached as exc:
        raise RecipeImportError("That page has no recipe data, and today's AI limit (or this month's AI "
                                "budget) is used up - try again tomorrow, or add it by hand.", "ai_limit") from exc
    except Exception as exc:  # noqa: BLE001 - provider missing, no key, bad response...
        raise RecipeImportError("That page has no recipe data, and the AI reader couldn't read it right now.",
                                "ai_unavailable") from exc
    if not isinstance(data, dict) or data.get("found") is False:
        return None
    page = _squash(text)

    def strings(key, limit):
        values = data.get(key) if isinstance(data.get(key), list) else []
        return [" ".join(str(v).split())[:limit] for v in values if isinstance(v, str) and v.strip()]

    ingredients = [line for line in strings("ingredients", 500) if _on_page(line, page)][:MAX_INGREDIENTS]
    steps = [line for line in strings("steps", 2000)
             if _on_page(line[3:] if line.startswith("## ") else line, page)][:MAX_STEPS]
    if not ingredients and not steps:
        return None

    def checked(key, limit=200):
        value = data.get(key)
        value = " ".join(value.split())[:limit] if isinstance(value, str) else ""
        return value if value and _on_page(value, page) else ""

    meta = _page_meta(_document(html_text), page_url)
    host = (urlsplit(page_url).hostname or "").lower()
    return {
        "title": checked("title") or meta["title"][:200] or "Imported recipe",
        "description": checked("description", 5000),
        "ingredients": [parse_ingredient(line) for line in ingredients],
        "steps": steps,
        "servings": servings_from(checked("servings", 100)),
        "prep_minutes": iso_minutes(checked("prep_time", 100)),
        "cook_minutes": iso_minutes(checked("cook_time", 100)),
        "total_time_minutes": iso_minutes(checked("total_time", 100)),
        "image_url": _image(meta["image"], page_url, None),
        "author": checked("author"),
        "source_name": meta["site_name"][:120] or (host[4:] if host.startswith("www.") else host),
        "source_url": clean_url(meta["canonical"] or page_url)[:500],
        "page_url": page_url,
        "needs_review": True,
        "method": "ai",
    }


def read_page(url, session=None, ai_client=None):
    """Fetches the link and reads its recipe -> import fields. Raises RecipeImportError (NOT_FOUND when
    there's nothing usable)."""
    page_url, html_text = fetch(url, session=session)
    recipe = parse_recipe_html(html_text, page_url) or ai_extract(html_text, page_url, client=ai_client)
    if not recipe:
        raise RecipeImportError(NOT_FOUND, "not_found")
    return recipe


# ---------------------------------------------------------------------------
# Saving
# ---------------------------------------------------------------------------

RECIPE_FIELDS = ("title", "description", "ingredients", "steps", "servings", "prep_minutes", "cook_minutes",
                 "total_time_minutes", "image_url", "author", "source_name", "needs_review")


def find_saved(*urls):
    """The recipe already saved from any of these links (compared by normalize_url), or None."""
    from .models import Recipe
    keys = {normalize_url(u)[:500] for u in urls if u}
    return Recipe.objects.filter(source_key__in=keys).order_by("pk").first() if keys else None


def save_new(data, user=None, token=None):
    from django.utils import timezone

    from .models import Recipe
    recipe = Recipe(**{key: data.get(key) for key in RECIPE_FIELDS}, source_url=data["source_url"],
                    imported_at=timezone.now(), from_bot=token is not None, bot_token=token,
                    created_by=user if user and user.is_authenticated else None)
    recipe.title = recipe.title or "Imported recipe"
    recipe.description, recipe.author, recipe.image_url = (recipe.description or "", recipe.author or "",
                                                           recipe.image_url or "")
    recipe.save()
    return recipe


def import_url(url, user=None, token=None, session=None, ai_client=None):
    """-> (recipe, "created" | "already_saved" | "needs_review"). Raises RecipeImportError."""
    existing = find_saved(url)
    if existing:
        return existing, "already_saved"
    data = read_page(url, session=session, ai_client=ai_client)
    existing = find_saved(data["source_url"], data["page_url"])
    if existing:
        return existing, "already_saved"
    recipe = save_new(data, user=user, token=token)
    return recipe, "needs_review" if recipe.needs_review else "created"


def reimport(recipe, session=None, ai_client=None):
    """Refreshes a recipe from its source link: title, description, ingredients, steps, times, servings,
    picture, author and site name. Its tags, notes and plan entries stay. Raises RecipeImportError."""
    from django.utils import timezone
    if not is_web_url(recipe.source_url):
        raise RecipeImportError("This recipe has no source link to re-import from.", "blocked")
    data = read_page(recipe.source_url, session=session, ai_client=ai_client)
    for key in RECIPE_FIELDS:
        value = data.get(key)
        setattr(recipe, key, value if value is not None or key.endswith("minutes") or key == "servings" else "")
    recipe.title = recipe.title or "Imported recipe"
    recipe.imported_at = timezone.now()
    recipe.save()
    return recipe
