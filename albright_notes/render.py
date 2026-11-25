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

The editor's own blocks (albright_notes/editor_src/src/note_editor.js writes them; keep the two in step):
"-# small text", ":::callout 💡 ... :::", ":::toggle Title ... :::", ++underline++, ==highlight==, ~~strike~~, and
each blank line beyond the one that separates two blocks is an empty paragraph - so the gaps someone leaves in a
note are still there after saving (Markdown alone collapses them, which is why spacing used to vanish)."""
import hashlib
import re
from urllib.parse import quote

import markdown
import nh3
from django.utils.html import escape
from markdown.extensions import Extension
from markdown.inlinepatterns import SimpleTagInlineProcessor
from django.urls import reverse
from django.utils.safestring import mark_safe

TAGS = {"p", "br", "hr", "h1", "h2", "h3", "h4", "h5", "h6", "strong", "em", "b", "i", "del", "s", "code", "pre",
        "blockquote", "ul", "ol", "li", "a", "table", "thead", "tbody", "tr", "th", "td", "img", "mark", "u"}
ATTRIBUTES = {"a": {"href", "title"}, "th": {"align"}, "td": {"align"}, "ol": {"start"},
              "img": {"src", "alt", "width", "height"}}
ATTACHMENT_SRC = re.compile(r"^/attachments/\d+/(?:thumb/)?$")
SIZE = re.compile(r"^\d{1,4}$")
IMG_WITHOUT_SRC = re.compile(r"<img(?![^>]*\ssrc=)[^>]*>")
EMPTY = "⁣"            # an empty paragraph while rendering (the editor's own marker, note_editor.js)
SMALL = "⁤"            # a "-# small text" paragraph, likewise


class NoteInline(Extension):
    """The editor's inline marks (~~strike~~, ==highlight==, ++underline++) and its backslash escapes."""

    def extendMarkdown(self, md):
        md.ESCAPED_CHARS = list(md.ESCAPED_CHARS) + ["=", "~", ":", "<", "|"]
        for name, delim, tag in (("nt_del", "~", "del"), ("nt_mark", "=", "mark"), ("nt_u", r"\+", "u")):
            md.inlinePatterns.register(
                SimpleTagInlineProcessor(rf"({delim}{{2}})(?=\S)(.+?)(?<=\S){delim}{{2}}", tag), name, 40)


EXTENSIONS = ["fenced_code", "tables", "sane_lists", "nl2br", NoteInline()]
CONTAINER_OPEN = re.compile(r"^:::(callout|toggle)(?:[ \t]+(.*))?$")
CONTAINER_CLOSE = re.compile(r"^:::[ \t]*$")
SMALL_LINE = re.compile(r"^-#[ \t]+(.*)$")

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
ORDERED = re.compile(r"^(?:[ \t]*>)*[ \t]*\d{1,9}\.[ \t]")
SMART_HTML = re.compile(r"@\[(date|grocery|person|budget)(?::([^\]|<\n]*))?((?:\|[^\]|<\n]*)*)\]")
MD_IMAGE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
PREVIEW_STRIP = re.compile(r"^\s*(?:#{1,6}\s+|>\s*|-#\s+|[-*+]\s+(?:\[[ xX]\]\s+)?|\d+[.)]\s+)*")


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
    way) is a list of its own. Each extra blank line becomes an empty paragraph and "-# " a small-text one.
    Only for rendering; the saved Markdown is untouched. note_editor.js's prepare() does the same."""
    out, fenced, previous, blanks, need_break = [], False, "", 0, False
    for line in (body or "").split("\n"):
        if fenced:                                    # code: exactly as written
            out.append(line)
            if FENCE.match(line):
                fenced, previous, need_break = False, line, False
            continue
        if not line.strip():
            blanks += 1
            continue
        if blanks > 1 and out:                        # the gap someone left: empty paragraphs
            out.append("")
            for _ in range(blanks - 1):
                out += [EMPTY, ""]
        elif blanks or need_break:
            out.append("")
        had_blank = blanks > 0 or need_break
        blanks, need_break = 0, False
        if FENCE.match(line):
            fenced = True
            out.append(line)
            previous = line
            continue
        small = SMALL_LINE.match(line)
        if small:
            if out and out[-1] != "":
                out.append("")
            out.append(SMALL + small.group(1))
            need_break, previous = True, ""
            continue
        line = EMPTY_BULLET.sub("\\1  ", line)
        if not had_blank and previous:
            if LIST_LINE.match(line) and previous.strip() and not previous[:1].isspace() \
                    and not LIST_LINE.match(previous) and not previous.lstrip().startswith(">"):
                out.append("")
            elif ITEM_LINE.match(line) and ITEM_LINE.match(previous) and _indent(line) == _indent(previous) \
                    and bool(ORDERED.match(line)) != bool(ORDERED.match(previous)):
                out.append("")                    # "1. " right after "- " (or the other way): a list of its own
            elif ITEM_LINE.match(previous) and not ITEM_LINE.match(line) \
                    and not line[:1].isspace() and not line.startswith(">") and not FENCE.match(previous):
                out.append("")                    # ... and a plain line right after a list item ends the list
            elif ITEM_LINE.match(line) and not ITEM_LINE.match(previous) \
                    and len(_indent(previous)) > len(_indent(line)):
                out.append("")                    # a new item after indented content (an image in the item)
        out.append(line)
        previous = line
    return "\n".join(out)


def _chunk(lines):
    """Markdown lines (no containers) -> sanitized HTML."""
    if not lines:
        return ""
    html = sanitize(markdown.markdown(_lists_start_anywhere("\n".join(lines)), extensions=EXTENSIONS,
                                      output_format="html"))
    return html.replace(f"<p>{EMPTY}</p>", '<p class="nt-empty-line"><br></p>') \
        .replace(f"<p>{SMALL}", '<p class="nt-small-text">').replace(EMPTY, "").replace(SMALL, "")


def _inline(text):
    html = _chunk([text]).strip()
    return html[3:-4] if html.startswith("<p>") and html.endswith("</p>") else html


def _blocks(lines):
    """Top-level ":::callout" / ":::toggle" containers (outside code, may nest) around Markdown chunks."""
    html, chunk, fenced, i = [], [], False, 0
    while i < len(lines):
        line = lines[i]
        if FENCE.match(line):
            fenced = not fenced
        opened = None if fenced else CONTAINER_OPEN.match(line)
        if not opened:
            chunk.append(line)
            i += 1
            continue
        depth, j, inner_fenced = 1, i + 1, False
        while j < len(lines):
            if FENCE.match(lines[j]):
                inner_fenced = not inner_fenced
            elif not inner_fenced:
                if CONTAINER_OPEN.match(lines[j]):
                    depth += 1
                elif CONTAINER_CLOSE.match(lines[j]):
                    depth -= 1
                    if not depth:
                        break
            j += 1
        html.append(_chunk(chunk))
        chunk = []
        inner = _blocks(lines[i + 1:j]) or "<p></p>"
        arg = (opened.group(2) or "").strip()
        if opened.group(1) == "callout":
            html.append(f'<div class="nt-callout"><span class="nt-callout__icon" aria-hidden="true">{escape(arg[:8] or "💡")}'
                        f'</span><div class="nt-callout__body">{inner}</div></div>')
        else:
            html.append(f'<details class="nt-toggle"><summary>{_inline(arg)}</summary><div>{inner}</div></details>')
        i = j + 1
    html.append(_chunk(chunk))
    return "".join(html)


def render(body, editable=False):
    """Safe HTML for a note's body. Checkboxes are tickable (data-task) only for someone who may edit it, and
    only when every rendered checkbox lines up with a checklist line of the Markdown."""
    html = _blocks((body or "").replace("\r\n", "\n").split("\n"))
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
    return mark_safe(_chips(CHECK_HTML.sub(box, html)))


def _chips(html):
    """Smart-link tokens (smart.py) -> chips: a due date ("Fri 5 PM", red when overdue), "In groceries" /
    "Already on list", "Budget draft", "@Sam". The tokens are plain text by now (escaped), so their parts are safe."""
    if "@[" not in html:
        return html
    from datetime import date as date_cls

    from django.utils import timezone

    from . import smart
    today = timezone.localdate()

    def chip(match):
        c = smart.parse(match)
        if c["kind"] == "date":
            try:
                day = date_cls.fromisoformat(c["date"] or "")
            except ValueError:
                return match.group(0)
            late = " is-overdue" if day < today else ""
            linked = " is-linked" if c["task"] else ""
            title = "Due " + smart.label(day, c["time"], today) + (" · in the Planner" if c["task"] else "")
            return (f'<span class="nt-smart nt-smart--date{late}{linked}" title="{title}">'
                    f'{smart.label(day, c["time"], today)}</span>')
        if c["kind"] == "grocery":
            text = "Grocery list" if not c["ids"] else ("Already on list" if c["already"] else "In groceries")
            return f'<span class="nt-smart nt-smart--grocery">{text}</span>'
        if c["kind"] == "budget":
            pending = "" if c["draft"] else " is-pending"
            return f'<span class="nt-smart nt-smart--budget{pending}">{"Budget draft" if c["draft"] else "Budget"}</span>'
        return f'<span class="nt-smart nt-smart--person">@{c["name"] or "someone"}</span>'
    return SMART_HTML.sub(chip, html)


def preview(body, limit=140):
    """A one-line plain-text preview: the body's first non-empty line without its Markdown markers."""
    for line in (body or "").split("\n"):
        container = CONTAINER_OPEN.match(line)
        if container:
            line = (container.group(2) or "") if container.group(1) == "toggle" else ""
        elif CONTAINER_CLOSE.match(line):
            continue
        line = SMART_HTML.sub("", line)
        text = PREVIEW_STRIP.sub("", MD_IMAGE.sub("", line)).replace("**", "").replace("`", "").strip()
        if text and not FENCE.match(line):
            return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"
    return ""
