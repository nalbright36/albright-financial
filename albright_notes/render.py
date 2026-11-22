"""A note's Markdown as safe HTML, and its checklist lines.

render() turns Markdown into HTML on the server (Python-Markdown), then
sanitizes it with an allowlist (nh3): only plain formatting, lists, tables,
code, links and the note's own attachment images survive - no scripts, styles,
iframes, forms, inline event handlers or javascript:/data: URLs. An <img> is
kept only when its src is one of this site's attachments (/attachments/<id>/ or
/attachments/<id>/thumb/ - served only to people who may see them), with alt,
width and height; any other src is dropped and so is the image. Links open in a new tab with
rel="noopener noreferrer", and every external one goes through the site's
link check (outbound_href: a verified domain links directly, anything else
via the "not on your verified list" page).

Checklist lines ("- [ ] item" / "- [x] item") become checkboxes AFTER
sanitizing (the sanitizer never lets an <input> through); the Nth checkbox
is the Nth checklist line of the Markdown, so ticking one rewrites exactly
that line (toggle()).

For someone who may edit, every rendered <li> also gets data-li="N": the Nth list line of the Markdown ("- ",
"* ", "1. ", "- [ ] "), so notes.js can edit any list item in place - only when the counts line up, like the
checkboxes."""
import hashlib
import re
from urllib.parse import quote

import markdown
import nh3
from django.urls import reverse
from django.utils.safestring import mark_safe

TAGS = {"p", "br", "hr", "h1", "h2", "h3", "h4", "h5", "h6", "strong", "em", "b", "i", "del", "s", "code", "pre",
        "blockquote", "ul", "ol", "li", "a", "table", "thead", "tbody", "tr", "th", "td", "img"}
ATTRIBUTES = {"a": {"href", "title"}, "th": {"align"}, "td": {"align"}, "ol": {"start"},
              "img": {"src", "alt", "width", "height"}}
ATTACHMENT_SRC = re.compile(r"^/attachments/\d+/(?:thumb/)?$")
SIZE = re.compile(r"^\d{1,4}$")
IMG_WITHOUT_SRC = re.compile(r"<img(?![^>]*\ssrc=)[^>]*>")
EXTENSIONS = ["fenced_code", "tables", "sane_lists", "nl2br"]

# "- [ ] item", "* [x] item", also inside a blockquote ("> - [ ] item").
CHECK_LINE = re.compile(r"^((?:[ \t]*>)*[ \t]*[-*+][ \t]+)\[([ xX])\](?=[ \t]|$)")
FENCE = re.compile(r"^[ \t]*(```|~~~)")
# The same item once rendered and sanitized: "<li>[ ] ..." or, in a loose list, "<li>\n<p>[ ] ...".
CHECK_HTML = re.compile(r"<li>(\s*<p>)?\[([ xX])\][ \t]?")
LIST_LINE = re.compile(r"^(?:[-*+]|\d{1,9}[.)])[ \t]+(?:[^ \t]|$)")
# Any list item line, also nested or quoted (the same lines notes.js's ITEM_LINE finds).
ITEM_LINE = re.compile(r"^(?:[ \t]*>)*[ \t]*(?:[-*+]|\d{1,9}\.)[ \t]")
# "- " with nothing after it: an item just started. Alone it would be a "---" heading underline, so it renders
# with a no-break space.
EMPTY_BULLET = re.compile(r"^((?:[ \t]*>)*[ \t]*[-*+])[ \t]+$")
LI_OPEN = re.compile(r"<li(?=[ >])")
ORDERED = re.compile(r"^(?:[ \t]*>)*[ \t]*\d{1,9}\.[ \t]")
MD_IMAGE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
PREVIEW_STRIP = re.compile(r"^\s*(?:#{1,6}\s+|>\s*|[-*+]\s+(?:\[[ xX]\]\s+)?|\d+[.)]\s+)*")


def outbound_href(url):
    """Where a link to this external URL should point, or "" for no link (not http/https)."""
    from albright_labs.links import is_web_url, link_status
    if not is_web_url(url):
        return ""
    if link_status(url)[1] == "allowlisted":
        return url
    return reverse("albright_notes:leave") + "?to=" + quote(url, safe="")


def _href(tag, attribute, value):
    if tag == "img":
        value = (value or "").strip()
        if attribute == "src":
            return value if ATTACHMENT_SRC.match(value) else None        # only this site's attachments
        if attribute in ("width", "height"):
            return value if SIZE.match(value) else None
        return value
    if tag != "a" or attribute != "href":
        return value
    value = (value or "").strip()
    if value.startswith("#") or (value.startswith("/") and not value.startswith("//")):
        return value                                      # this site
    if value.lower().startswith("mailto:"):
        return value
    return outbound_href(value) or None                   # http(s) via the link check; anything else dropped


def sanitize(html):
    clean = nh3.clean(html, tags=TAGS, attributes=ATTRIBUTES, url_schemes={"http", "https", "mailto"},
                      attribute_filter=_href, link_rel="noopener noreferrer",
                      set_tag_attribute_values={"a": {"target": "_blank"}})
    return IMG_WITHOUT_SRC.sub("", clean)              # an image whose src was dropped goes too


def checklist_lines(body):
    """[(line number, checked)] for every checklist line outside fenced code blocks."""
    out, fenced = [], False
    for number, line in enumerate((body or "").split("\n")):
        if FENCE.match(line):
            fenced = not fenced
            continue
        match = None if fenced else CHECK_LINE.match(line)
        if match:
            out.append((number, match.group(2) != " "))
    return out


def toggle(body, index, checked):
    """The body with checklist item `index` ticked or unticked, or None if there's no such item."""
    lines = (body or "").split("\n")
    items = checklist_lines(body)
    if not 0 <= index < len(items):
        return None
    number = items[index][0]
    lines[number] = CHECK_LINE.sub(lambda m: f"{m.group(1)}[{'x' if checked else ' '}]", lines[number], count=1)
    return "\n".join(lines)


def item_ids(body):
    """A stable id per checklist item, in order: a hash of the item's own text, plus which copy of that text it is
    ("milk" twice: ...-0, ...-1). Adding, removing or reordering other lines - typing elsewhere in the note, or
    someone else's edit - never changes an item's id, so ticking one by id (toggle_id) can't tick the wrong line.
    Worked out from the Markdown, so every note has them without storing anything."""
    lines, seen, out = (body or "").split("\n"), {}, []
    for number, _checked in checklist_lines(body):
        text = CHECK_LINE.sub("", lines[number], count=1).strip()
        digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:10]
        n = seen.get(digest, 0)
        seen[digest] = n + 1
        out.append(f"{digest}-{n}")
    return out


def toggle_id(body, item_id, checked):
    """The body with the checklist item `item_id` (item_ids) ticked or unticked; None when no item has that id
    (its text was edited since). Only that one line changes."""
    ids = item_ids(body)
    if item_id not in ids:
        return None
    return toggle(body, ids.index(item_id), checked)


def list_lines(body):
    """The line numbers of every list item line outside fenced code blocks, in order."""
    out, fenced = [], False
    for number, line in enumerate((body or "").split("\n")):
        if FENCE.match(line):
            fenced = not fenced
        elif not fenced and ITEM_LINE.match(line):
            out.append(number)
    return out


def _indent(line):
    return re.match(r"^[ \t>]*", line).group(0)


def _lists_start_anywhere(body):
    """People type "Shopping:" then "- milk" on the next line - Markdown needs a blank line before a list
    that follows a paragraph, so add one (outside code blocks); likewise a plain line typed right after a list
    is a new paragraph, not more of the last item, and a numbered list right after a bulleted one (or the other
    way) is a list of its own. Only for rendering; the saved Markdown is untouched."""
    out, fenced, previous = [], False, ""
    for line in (body or "").split("\n"):
        if FENCE.match(line):
            fenced = not fenced
        elif not fenced:
            line = EMPTY_BULLET.sub("\\1 \u00a0", line)
            if LIST_LINE.match(line) and previous.strip() and not previous[:1].isspace() \
                    and not LIST_LINE.match(previous) and not previous.lstrip().startswith(">"):
                out.append("")
            elif ITEM_LINE.match(line) and ITEM_LINE.match(previous) and _indent(line) == _indent(previous) \
                    and bool(ORDERED.match(line)) != bool(ORDERED.match(previous)):
                out.append("")                    # "1. " right after "- " (or the other way): a list of its own
            elif line.strip() and ITEM_LINE.match(previous) and not ITEM_LINE.match(line) \
                    and not line[:1].isspace() and not line.startswith(">") and not FENCE.match(previous):
                out.append("")                    # ... and a plain line right after a list item ends the list
        out.append(line)
        previous = line
    return "\n".join(out)


def render(body, editable=False):
    """Safe HTML for a note's body. Checkboxes are tickable (data-task) only for someone who may edit it, and
    only when every rendered checkbox lines up with a checklist line of the Markdown."""
    html = sanitize(markdown.markdown(_lists_start_anywhere(body), extensions=EXTENSIONS, output_format="html"))
    expected = len(checklist_lines(body))
    live = editable and len(CHECK_HTML.findall(html)) == expected
    count = iter(range(10 ** 6))
    ids = item_ids(body) if live else []

    def box(match):
        n = next(count)
        state = " checked" if match.group(2) != " " else ""
        attrs = f' data-task="{n}" data-item-id="{ids[n]}"' if live else " disabled"
        done = " done" if state else ""
        return f'<li class="nt-task{done}">{match.group(1) or ""}<input type="checkbox" class="nt-check"{attrs}{state} ' \
               f'aria-label="Done"> '
    html = CHECK_HTML.sub(box, html)
    if editable and len(LI_OPEN.findall(html)) == len(list_lines(body)):
        items = iter(range(10 ** 6))
        html = LI_OPEN.sub(lambda m: f'<li data-li="{next(items)}"', html)
    return mark_safe(html)


def preview(body, limit=140):
    """A one-line plain-text preview: the body's first non-empty line without its Markdown markers."""
    for line in (body or "").split("\n"):
        text = PREVIEW_STRIP.sub("", MD_IMAGE.sub("", line)).replace("**", "").replace("`", "").strip()
        if text and not FENCE.match(line):
            return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"
    return ""
