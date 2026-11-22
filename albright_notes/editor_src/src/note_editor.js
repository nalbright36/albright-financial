/*
 * Albright Notes' rich-text editor: TipTap (ProseMirror) set up for notes, built into one vendored file
 * (albright_notes/static/albright_notes/vendor/note-editor.js) by build.mjs - nothing loads from a CDN.
 * Exposes window.NoteEditor; the page wiring (autosave, toolbars, menus) is albright_notes/static/albright_notes/notes.js.
 *
 * Notes stay Markdown (Note.body, the Bot API, the server's render.py). This file turns that Markdown into the
 * editor's document (mdToHtml -> TipTap parses the HTML) and back (toMarkdown). The Markdown the editor writes:
 *
 *   # / ## / ###           headings            -# text           small text
 *   - item / 1. item       lists (4-space nesting)                - [ ] / - [x]   checklist
 *   > text                 quote               ---               divider
 *   ```lang ... ```        code block          ![name](/attachments/<id>/)       image
 *   :::callout 💡 ... :::  callout             :::toggle Title ... :::           toggle (collapsible)
 *   **b** *i* ++u++ ~~s~~ ==mark== `code` [text](url)
 *   blank lines            one blank line separates blocks; each extra blank line is an empty paragraph,
 *                          so the gaps someone leaves stay (render.py reads them the same way)
 *
 * Keep this file and albright_notes/render.py in step: both read the same Markdown.
 */
import { Editor, Node, Extension, mergeAttributes, textblockTypeInputRule } from "@tiptap/core";
import { Plugin, PluginKey } from "@tiptap/pm/state";
import StarterKit from "@tiptap/starter-kit";
import { TaskList, TaskItem } from "@tiptap/extension-list";
import Highlight from "@tiptap/extension-highlight";
import Image from "@tiptap/extension-image";
import { Details, DetailsSummary, DetailsContent } from "@tiptap/extension-details";
import { Placeholder } from "@tiptap/extensions";
import { TableKit } from "@tiptap/extension-table";
import BubbleMenu from "@tiptap/extension-bubble-menu";
import Suggestion from "@tiptap/suggestion";
import MarkdownIt from "markdown-it";
import markPlugin from "markdown-it-mark";
import insPlugin from "markdown-it-ins";

const EMPTY = "⁣";        // an empty paragraph, while the Markdown is turned into HTML
const SMALL = "⁤";        // a "-# small text" paragraph, likewise
const CALLOUT_ICONS = ["💡", "📌", "⚠️", "✅", "ℹ️", "🔥", "❗"];

// ---------------------------------------------------------------------------------------------------------------
// Markdown -> HTML (for the editor to parse)
// ---------------------------------------------------------------------------------------------------------------
const md = new MarkdownIt({ html: false, breaks: true, linkify: false }).use(markPlugin).use(insPlugin);
md.renderer.rules.ins_open = () => "<u>";
md.renderer.rules.ins_close = () => "</u>";

// "- [ ] item" lists -> TipTap's task list markup. A list with any checklist item becomes a checklist.
md.core.ruler.push("note_tasks", (state) => {
    const tokens = state.tokens;
    const TASK = /^\[([ xX])\](?:[ \t]+|$)/;
    for (let i = 0; i < tokens.length; i++) {
        if (tokens[i].type !== "bullet_list_open") continue;
        const level = tokens[i].level, items = [];
        for (let j = i + 1; j < tokens.length && !(tokens[j].type === "bullet_list_close" && tokens[j].level === level); j++) {
            if (tokens[j].type === "list_item_open" && tokens[j].level === level + 1) {
                const inline = tokens[j + 2] && tokens[j + 2].type === "inline" ? tokens[j + 2] : null;
                items.push({ open: tokens[j], inline, match: inline ? TASK.exec(inline.content) : null });
            }
        }
        if (!items.some((it) => it.match)) continue;
        tokens[i].attrSet("data-type", "taskList");
        items.forEach((it) => {
            it.open.attrSet("data-type", "taskItem");
            it.open.attrSet("data-checked", it.match && it.match[1] !== " " ? "true" : "false");
            if (it.match && it.inline) {
                const first = it.inline.children && it.inline.children[0];
                if (first && first.type === "text") first.content = first.content.replace(TASK, "");
            }
        });
    }
});

// Smart-link tokens (albright_notes/smart.py): "@[date:2026-12-03T17:00|task:12]", "@[grocery:45,46]",
// "@[grocery:45|already]", "@[grocery]", "@[person:7|Sam]" -> a chip the editor parses into a smartChip node.
const CHIP_RE = /^@\[(date|grocery|person)(?::([^\]|\n]*))?((?:\|[^\]|\n]*)*)\]/;
export function parseToken(token) {
    const m = CHIP_RE.exec(token || "");
    if (!m) return null;
    const kind = m[1], value = (m[2] || "").trim(), parts = (m[3] || "").split("|").slice(1);
    const attrs = { kind, date: null, time: null, task: null, ids: "", already: false, pid: null, name: "" };
    if (kind === "date") {
        const [day, at] = value.split("T");
        attrs.date = day || null;
        attrs.time = at ? at.slice(0, 5) : null;
        const task = parts.find((p) => p.startsWith("task:"));
        attrs.task = task && /^\d+$/.test(task.slice(5)) ? Number(task.slice(5)) : null;
    } else if (kind === "grocery") {
        attrs.ids = value.split(",").filter((i) => /^\d+$/.test(i.trim())).map((i) => i.trim()).join(",");
        attrs.already = parts.includes("already");
    } else {
        attrs.pid = /^\d+$/.test(value) ? Number(value) : null;
        attrs.name = (parts[0] || "").trim();
    }
    return attrs;
}
export function tokenOf(a) {
    if (a.kind === "date") return "@[date:" + (a.date || "") + (a.time ? "T" + a.time : "") + (a.task ? "|task:" + a.task : "") + "]";
    if (a.kind === "grocery") return "@[grocery" + (a.ids ? ":" + a.ids : "") + (a.already ? "|already" : "") + "]";
    return "@[person:" + (a.pid || "") + "|" + String(a.name || "").replace(/[\]|\n]/g, "") + "]";
}
md.inline.ruler.before("link", "smart_chip", (state, silent) => {
    if (state.src.charCodeAt(state.pos) !== 0x40 || state.src.charCodeAt(state.pos + 1) !== 0x5B) return false;
    const m = CHIP_RE.exec(state.src.slice(state.pos));
    if (!m) return false;
    if (!silent) state.push("smart_chip", "", 0).content = m[0];
    state.pos += m[0].length;
    return true;
});
md.renderer.rules.smart_chip = (tokens, i) => '<span data-chip="' + escapeHtml(tokens[i].content) + '"></span>';

const FENCE = /^[ \t]*(```|~~~)/;
const LIST_LINE = /^(?:[-*+]|\d{1,9}[.)])[ \t]+(?:[^ \t]|$)/;
const ITEM_LINE = /^(?:[ \t]*>)*[ \t]*(?:[-*+]|\d{1,9}\.)[ \t]/;
const ORDERED = /^(?:[ \t]*>)*[ \t]*\d{1,9}\.[ \t]/;
const EMPTY_BULLET = /^((?:[ \t]*>)*[ \t]*[-*+])[ \t]+$/;
const CONTAINER_OPEN = /^:::(callout|toggle)(?:[ \t]+(.*))?$/;
const CONTAINER_CLOSE = /^:::[ \t]*$/;
const SMALL_LINE = /^-#[ \t]+(.*)$/;

function indentOf(line) { return /^[ \t>]*/.exec(line)[0]; }

// The same fix-ups render.py makes (its _lists_start_anywhere / _blocks): lists may start right after a line of
// text, a plain line after a list ends it, "-" and "1." lists don't merge, "-# " is small text, and each extra
// blank line is an empty paragraph.
function prepare(lines) {
    const out = [];
    let fenced = false, previous = "", blanks = 0, needBreak = false;
    lines.forEach((raw) => {
        let line = raw;
        const isFence = FENCE.test(line);
        if (fenced) {                                         // code: exactly as written
            out.push(line);
            if (isFence) { fenced = false; previous = line; needBreak = false; }
            return;
        }
        if (!line.trim()) { blanks++; return; }
        if (blanks > 1 && out.length) {                       // the gap someone left: empty paragraphs
            out.push("");
            for (let k = 1; k < blanks; k++) out.push(EMPTY, "");
        } else if (blanks || needBreak) out.push("");
        const hadBlank = blanks > 0 || needBreak;
        blanks = 0;
        needBreak = false;
        if (isFence) { fenced = true; out.push(line); previous = line; return; }
        const small = SMALL_LINE.exec(line);
        if (small) {                                          // "-# " small text: a paragraph of its own
            if (out.length && out[out.length - 1] !== "") out.push("");
            out.push(SMALL + small[1]);
            needBreak = true;
            previous = "";
            return;
        }
        line = line.replace(EMPTY_BULLET, "$1 " + EMPTY);
        if (!hadBlank && previous) {
            if (LIST_LINE.test(line) && previous.trim() && !/^\s/.test(previous) && !LIST_LINE.test(previous) &&
                !previous.trimStart().startsWith(">")) out.push("");
            else if (ITEM_LINE.test(line) && ITEM_LINE.test(previous) && indentOf(line) === indentOf(previous) &&
                ORDERED.test(line) !== ORDERED.test(previous)) out.push("");
            else if (ITEM_LINE.test(previous) && !ITEM_LINE.test(line) && !/^\s/.test(line) &&
                !line.startsWith(">") && !FENCE.test(previous)) out.push("");
        }
        out.push(line);
        previous = line;
    });
    return out;
}

function renderChunk(lines) {
    if (!lines.length) return "";
    let html = md.render(prepare(lines).join("\n"));
    html = html.replace(new RegExp("<p>" + EMPTY + "</p>", "g"), "<p></p>")
        .replace(new RegExp("<p>" + SMALL, "g"), '<p data-small="">')
        .replace(new RegExp(EMPTY, "g"), "");
    return html;
}

function escapeHtml(text) {
    return String(text).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

// Top-level ":::callout" / ":::toggle" containers (fence-aware, may nest), everything else in Markdown chunks.
function renderBlocks(lines) {
    let html = "", chunk = [], fenced = false;
    for (let i = 0; i < lines.length; i++) {
        const line = lines[i];
        if (FENCE.test(line)) fenced = !fenced;
        const open = !fenced && CONTAINER_OPEN.exec(line);
        if (!open) { chunk.push(line); continue; }
        let depth = 1, j = i + 1, innerFenced = false;
        for (; j < lines.length; j++) {
            if (FENCE.test(lines[j])) innerFenced = !innerFenced;
            if (innerFenced) continue;
            if (CONTAINER_OPEN.test(lines[j])) depth++;
            else if (CONTAINER_CLOSE.test(lines[j]) && --depth === 0) break;
        }
        html += renderChunk(chunk);
        chunk = [];
        const inner = renderBlocks(lines.slice(i + 1, j)) || "<p></p>";
        const arg = (open[2] || "").trim();
        if (open[1] === "callout") {
            const icon = arg || CALLOUT_ICONS[0];
            html += '<div data-callout="' + escapeHtml(icon) + '">' + inner + "</div>";
        } else {
            html += "<details><summary>" + md.renderInline(arg) + '</summary><div data-type="detailsContent">' + inner + "</div></details>";
        }
        i = j;
    }
    return html + renderChunk(chunk);
}

export function mdToHtml(markdown) {
    return renderBlocks(String(markdown || "").replace(/\r\n?/g, "\n").split("\n"));
}

// ---------------------------------------------------------------------------------------------------------------
// Editor document (JSON) -> Markdown
// ---------------------------------------------------------------------------------------------------------------
const MARK_ORDER = ["link", "bold", "italic", "strike", "underline", "highlight", "code"];

function escapeText(text) {
    let out = text.replace(/[\\`*\[\]<]/g, "\\$&")
        .replace(/(^|[^\w])_|_(?=[^\w]|$)/g, (m) => m.replace("_", "\\_"));
    ["~~", "==", "++"].forEach((pair) => {                // only when there's a pair that would turn into a mark
        if (out.split(pair).length > 2) out = out.split(pair).join("\\" + pair[0] + "\\" + pair[1]);
    });
    return out;
}

const LINE_START = /^([ \t]*)(#{1,6}(?=[ \t]|$)|>|[-+*](?=[ \t]|$)|\d{1,9}(?=[.)]([ \t]|$))|:::|-#(?=[ \t])|=+[ \t]*$|-+[ \t]*$)/;
function escapeLineStarts(text) {
    return text.split("\n").map((line) => line.replace(LINE_START, (m, lead, what) => {
        if (/^\d/.test(what)) return lead + what + "\\";           // "1\. not a list"
        return lead + "\\" + what;
    })).join("\n");
}

function sameMark(a, b) { return a.type === b.type && JSON.stringify(a.attrs || {}) === JSON.stringify(b.attrs || {}); }
function openMark(mark, text) {
    switch (mark.type) {
        case "bold": return "**";
        case "italic": return "*";
        case "strike": return "~~";
        case "underline": return "++";
        case "highlight": return "==";
        case "code": return /`/.test(text) ? "`` " : "`";
        case "link": return "[";
        default: return "";
    }
}
function closeMark(mark, codeText) {
    switch (mark.type) {
        case "bold": return "**";
        case "italic": return "*";
        case "strike": return "~~";
        case "underline": return "++";
        case "highlight": return "==";
        case "code": return /`/.test(codeText || "") ? " ``" : "`";
        case "link": {
            const href = String((mark.attrs && mark.attrs.href) || "").replace(/ /g, "%20").replace(/\(/g, "%28").replace(/\)/g, "%29");
            return "](" + href + ")";
        }
        default: return "";
    }
}

function inline(nodes) {
    let out = "", active = [], codeText = "";
    const closeTo = (keep) => {
        if (keep >= active.length) return;
        const trail = /\s*$/.exec(out)[0];
        out = out.slice(0, out.length - trail.length);
        for (let i = active.length - 1; i >= keep; i--) out += closeMark(active[i], codeText);
        out += trail;
        active = active.slice(0, keep);
    };
    (nodes || []).forEach((node) => {
        if (node.type === "hardBreak") { closeTo(0); out += "\n"; return; }
        if (node.type === "smartChip") { out += tokenOf(node.attrs || {}); return; }
        if (node.type !== "text") return;
        const marks = (node.marks || []).filter((m) => MARK_ORDER.includes(m.type))
            .sort((a, b) => MARK_ORDER.indexOf(a.type) - MARK_ORDER.indexOf(b.type));
        let keep = 0;
        while (keep < active.length && keep < marks.length && sameMark(active[keep], marks[keep])) keep++;
        let text = node.text || "";
        if (!text.trim() && keep < marks.length) {           // whitespace alone doesn't open marks
            closeTo(keep);
            out += text;
            return;
        }
        closeTo(keep);
        const lead = /^\s*/.exec(text)[0];
        if (keep < marks.length) {
            out += lead;
            text = text.slice(lead.length);
            const rest = text.replace(/\s+$/, "");
            const tail = text.slice(rest.length);
            marks.slice(keep).forEach((m) => { if (m.type === "code") codeText = rest; out += openMark(m, rest); active.push(m); });
            const inCode = active.some((m) => m.type === "code");
            out += inCode ? rest : escapeText(rest);
            out += tail;                                     // closeTo() moves trailing spaces outside the marks
            return;
        }
        out += active.some((m) => m.type === "code") ? text : escapeText(text);
    });
    closeTo(0);
    return escapeLineStarts(out);
}

function prefixLines(text, first, rest) {
    return text.split("\n").map((line, i) => (i === 0 ? first : (line ? rest : rest.replace(/[ \t]+$/, ""))) + line).join("\n");
}

function isEmptyParagraph(node) {
    return node.type === "paragraph" && !(node.content || []).some((c) => c.type !== "hardBreak" && (c.text || "").length);
}

function blocks(nodes, opts) {
    // Blocks separated by one blank line; each empty paragraph between them is one more blank line.
    const parts = [];
    let gap = 0;
    (nodes || []).forEach((node) => {
        if (isEmptyParagraph(node)) { gap++; return; }
        const text = block(node);
        if (text == null) return;
        parts.push({ text, gap: parts.length ? gap : 0 });
        gap = 0;
    });
    return parts.map((p, i) => (i ? "\n\n" + "\n".repeat(opts && opts.tight ? 0 : p.gap) : "") + p.text).join("");
}

function listItems(node, marker) {
    return (node.content || []).map((item, i) => {
        const m = marker(item, i);
        const children = item.content || [];
        const firstBlock = children[0] && children[0].type === "paragraph" ? children[0] : null;
        let head = firstBlock ? inline(firstBlock.content) : "";
        let text = prefixLines(head, m, "    ");
        const rest = firstBlock ? children.slice(1) : children;
        rest.forEach((child) => {
            if (isEmptyParagraph(child)) return;
            const sep = /List$/.test(child.type) ? "\n" : "\n\n";
            text += sep + prefixLines(block(child), "    ", "    ");
        });
        return text;
    }).join("\n");
}

function tableMarkdown(node) {
    const rows = (node.content || []).map((row) => (row.content || []).map((cell) =>
        (cell.content || []).map((p) => inline(p.content).replace(/\n/g, " ")).join(" ").replace(/\|/g, "\\|").trim()));
    if (!rows.length) return null;
    const width = Math.max(...rows.map((r) => r.length));
    const line = (cells) => "| " + Array.from({ length: width }, (_, i) => cells[i] || "").join(" | ") + " |";
    return [line(rows[0]), "| " + Array.from({ length: width }, () => "---").join(" | ") + " |"].concat(rows.slice(1).map(line)).join("\n");
}

function block(node) {
    switch (node.type) {
        case "paragraph": return inline(node.content);
        case "smallText": return "-# " + inline(node.content);
        case "heading": return "#".repeat(Math.min(Math.max(node.attrs.level || 1, 1), 6)) + " " + inline(node.content);
        case "horizontalRule": return "---";
        case "blockquote": return prefixLines(blocks(node.content), "> ", "> ").replace(/^> $/gm, ">");
        case "codeBlock": {
            const text = (node.content || []).map((c) => c.text || "").join("").replace(/\n$/, "");
            const fence = /```/.test(text) ? "~~~~" : "```";
            return fence + ((node.attrs && node.attrs.language) || "") + "\n" + text + "\n" + fence;
        }
        case "bulletList": return listItems(node, () => "- ");
        case "orderedList": {
            const start = (node.attrs && node.attrs.start) || 1;
            return listItems(node, (_item, i) => (start + i) + ". ");
        }
        case "taskList": return listItems(node, (item) => (item.attrs && item.attrs.checked ? "- [x] " : "- [ ] "));
        case "image": {
            const alt = String((node.attrs && node.attrs.alt) || "").replace(/[\[\]\n]/g, "");
            return "![" + alt + "](" + ((node.attrs && node.attrs.src) || "") + ")";
        }
        case "callout": {
            const icon = (node.attrs && node.attrs.icon) || CALLOUT_ICONS[0];
            return ":::callout " + icon + "\n" + (blocks(node.content) || "") + "\n:::";
        }
        case "details": {
            const summary = (node.content || []).find((c) => c.type === "detailsSummary");
            const content = (node.content || []).find((c) => c.type === "detailsContent");
            return ":::toggle " + (summary ? inline(summary.content).replace(/\n/g, " ") : "") + "\n" +
                (content ? blocks(content.content) : "") + "\n:::";
        }
        case "table": return tableMarkdown(node);
        default: return node.content ? blocks(node.content) : null;
    }
}

export function toMarkdown(json) {
    return blocks(json && json.content).replace(/\n+$/, "");
}

// ---------------------------------------------------------------------------------------------------------------
// Custom blocks
// ---------------------------------------------------------------------------------------------------------------
const SmallText = Node.create({
    name: "smallText",
    group: "block",
    content: "inline*",
    // Not a node priority: that would make it the schema's default block (new lines would be small text).
    parseHTML() { return [{ tag: "p[data-small]", priority: 100 }]; },
    renderHTML({ HTMLAttributes }) { return ["p", mergeAttributes(HTMLAttributes, { "data-small": "", class: "nt-small-text" }), 0]; },
    addCommands() {
        return { setSmallText: () => ({ commands }) => commands.setNode(this.name) };
    },
    addInputRules() { return [textblockTypeInputRule({ find: /^-#\s$/, type: this.type })]; },
});

const Callout = Node.create({
    name: "callout",
    group: "block",
    content: "block+",
    defining: true,
    addAttributes() {
        return { icon: { default: CALLOUT_ICONS[0], parseHTML: (el) => el.getAttribute("data-callout") || CALLOUT_ICONS[0] } };
    },
    parseHTML() { return [{ tag: "div[data-callout]" }]; },
    renderHTML({ node }) {
        return ["div", { "data-callout": node.attrs.icon, class: "nt-callout" },
            ["span", { class: "nt-callout__icon", contenteditable: "false", role: "button", "aria-label": "Change icon", title: "Change icon" }, node.attrs.icon],
            ["div", { class: "nt-callout__body" }, 0]];
    },
    addCommands() {
        return {
            setCallout: () => ({ commands }) => commands.wrapIn(this.name),
            toggleCallout: () => ({ commands, editor }) => (editor.isActive(this.name) ? commands.lift(this.name) : commands.wrapIn(this.name)),
        };
    },
    addProseMirrorPlugins() {
        return [new Plugin({
            props: {
                handleDOMEvents: {
                    mousedown: (view, event) => {
                        const icon = event.target.closest && event.target.closest(".nt-callout__icon");
                        if (!icon || !view.editable) return false;
                        event.preventDefault();
                        const pos = view.posAtDOM(icon.parentNode, 0) - 1;
                        const node = view.state.doc.nodeAt(pos);
                        if (!node || node.type.name !== "callout") return true;
                        const next = CALLOUT_ICONS[(CALLOUT_ICONS.indexOf(node.attrs.icon) + 1) % CALLOUT_ICONS.length];
                        view.dispatch(view.state.tr.setNodeMarkup(pos, undefined, { ...node.attrs, icon: next }));
                        return true;
                    },
                },
            },
        })];
    },
});

// Paste or drop files: handed to the page (notes.js uploads them as attachments, then inserts them).
const FileDrop = Extension.create({
    name: "fileDrop",
    addOptions() { return { onFiles: null }; },
    addProseMirrorPlugins() {
        const options = this.options;
        const files = (list) => Array.prototype.slice.call(list || []);
        return [new Plugin({
            props: {
                handlePaste: (view, event) => {
                    const found = files(event.clipboardData && event.clipboardData.files);
                    if (!found.length || !options.onFiles) return false;
                    event.preventDefault();
                    options.onFiles(found, view.state.selection.from);
                    return true;
                },
                handleDrop: (view, event) => {
                    const found = files(event.dataTransfer && event.dataTransfer.files);
                    if (!found.length || !options.onFiles) return false;
                    event.preventDefault();
                    const at = view.posAtCoords({ left: event.clientX, top: event.clientY });
                    options.onFiles(found, at ? at.pos : view.state.selection.from);
                    return true;
                },
            },
        })];
    },
});

// ---------------------------------------------------------------------------------------------------------------
// Smart links: due dates, grocery tags, mentions (albright_notes/smart.py stores and syncs them)
// ---------------------------------------------------------------------------------------------------------------
function isoDay(d) {
    return d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0") + "-" + String(d.getDate()).padStart(2, "0");
}
function fromIso(text) {
    const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(text || "");
    return m ? new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3])) : null;
}
function addDays(d, n) { const out = new Date(d.getFullYear(), d.getMonth(), d.getDate()); out.setDate(out.getDate() + n); return out; }
function timeLabel(t) {
    if (!t) return "";
    const [h, m] = t.split(":").map(Number);
    return (h % 12 || 12) + (m ? ":" + String(m).padStart(2, "0") : "") + (h < 12 ? " AM" : " PM");
}
// "Today", "Tomorrow", "Fri", "Dec 3", "Dec 3, 2027" (+ " 5 PM") - smart.label does the same on the server.
export function dateLabel(date, time, now) {
    const day = fromIso(date);
    if (!day) return "Date";
    const today = now ? new Date(now.getFullYear(), now.getMonth(), now.getDate()) : (() => { const n = new Date(); return new Date(n.getFullYear(), n.getMonth(), n.getDate()); })();
    const delta = Math.round((day - today) / 86400000);
    let text;
    if (delta === 0) text = "Today";
    else if (delta === 1) text = "Tomorrow";
    else if (delta === -1) text = "Yesterday";
    else if (delta > 1 && delta < 7) text = day.toLocaleDateString("en-US", { weekday: "short" });
    else if (day.getFullYear() === today.getFullYear()) text = day.toLocaleDateString("en-US", { month: "short", day: "numeric" });
    else text = day.toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" });
    return text + (time ? " " + timeLabel(time) : "");
}
export function chipLabel(a, now) {
    if (a.kind === "date") return dateLabel(a.date, a.time, now);
    if (a.kind === "grocery") return !a.ids ? "Grocery list" : a.already ? "Already on list" : "In groceries";
    return "@" + (a.name || "someone");
}

const WEEKDAYS = { sun: 0, sunday: 0, mon: 1, monday: 1, tue: 2, tues: 2, tuesday: 2, wed: 3, weds: 3, wednesday: 3,
    thu: 4, thur: 4, thurs: 4, thursday: 4, fri: 5, friday: 5, sat: 6, saturday: 6 };
const MONTHS = { jan: 0, january: 0, feb: 1, february: 1, mar: 2, march: 2, apr: 3, april: 3, may: 4, jun: 5, june: 5,
    jul: 6, july: 6, aug: 7, august: 7, sep: 8, sept: 8, september: 8, oct: 9, october: 9, nov: 10, november: 10, dec: 11, december: 11 };

// Natural dates for "@": "tomorrow", "fri", "next fri", "dec 3", "3 dec", "12/3", "2026-12-03", "next week", "in 3 days",
// each with an optional time ("5pm", "5:30 pm", "17:00", "at noon"). A time alone is today (tomorrow once it's past).
// Returns {date: "YYYY-MM-DD", time: "HH:MM"|null} or null.
export function parseWhen(input, now) {
    now = now || new Date();
    let s = String(input || "").toLowerCase().replace(/[,.]/g, " ").replace(/\s+/g, " ").trim();
    if (!s) return null;
    let time = null;
    const tm = /(?:^|\s)(?:at\s+)?(?:(\d{1,2})(?::(\d{2}))?\s*(am|pm|a|p)|(\d{1,2}):(\d{2})|(noon|midnight))(?=\s|$)/.exec(s);
    if (tm) {
        let h, m = 0;
        if (tm[1]) { h = Number(tm[1]); m = Number(tm[2] || 0); if (h < 1 || h > 12) return null; h = (h % 12) + (tm[3][0] === "p" ? 12 : 0); }
        else if (tm[4]) { h = Number(tm[4]); m = Number(tm[5]); if (h > 23) return null; }
        else h = tm[6] === "noon" ? 12 : 0;
        if (m > 59) return null;
        time = String(h).padStart(2, "0") + ":" + String(m).padStart(2, "0");
        s = (s.slice(0, tm.index) + " " + s.slice(tm.index + tm[0].length)).replace(/\s+/g, " ").replace(/\bat$/, "").trim();
    }
    const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
    let d = null, m;
    if (!s) {
        if (!time) return null;
        const [h, mi] = time.split(":").map(Number);
        d = h * 60 + mi > now.getHours() * 60 + now.getMinutes() ? today : addDays(today, 1);
    } else if (/^(today|tod|tonight)$/.test(s)) d = today;
    else if (/^(tomorrow|tmrw?|tmr|tom)$/.test(s)) d = addDays(today, 1);
    else if ((m = /^(next |this )?([a-z]+)$/.exec(s)) && m[2] in WEEKDAYS) {
        if (m[1] === "next ") {                                   // that day in next week (weeks start Monday)
            const monday = addDays(today, ((8 - today.getDay()) % 7) || 7);
            d = addDays(monday, (WEEKDAYS[m[2]] + 6) % 7);
        } else d = addDays(today, (WEEKDAYS[m[2]] - today.getDay() + 7) % 7);
    } else if (s === "next week") d = addDays(today, ((8 - today.getDay()) % 7) || 7);
    else if (s === "weekend" || s === "this weekend") d = addDays(today, (6 - today.getDay() + 7) % 7);
    else if (s === "next month") d = new Date(today.getFullYear(), today.getMonth() + 1, 1);
    else if ((m = /^in (\d{1,3}) (day|days|week|weeks|month|months)$/.exec(s))) {
        const n = Number(m[1]);
        d = m[2][0] === "d" ? addDays(today, n) : m[2][0] === "w" ? addDays(today, n * 7)
            : new Date(today.getFullYear(), today.getMonth() + n, today.getDate());
    } else if ((m = /^([a-z]+) (\d{1,2})(?:st|nd|rd|th)?(?: (\d{4}))?$/.exec(s)) && m[1] in MONTHS) {
        d = monthDay(today, MONTHS[m[1]], Number(m[2]), m[3]);
    } else if ((m = /^(\d{1,2})(?:st|nd|rd|th)? ([a-z]+)(?: (\d{4}))?$/.exec(s)) && m[2] in MONTHS) {
        d = monthDay(today, MONTHS[m[2]], Number(m[1]), m[3]);
    } else if ((m = /^(\d{1,2})\/(\d{1,2})(?:\/(\d{2}|\d{4}))?$/.exec(s))) {
        d = monthDay(today, Number(m[1]) - 1, Number(m[2]), m[3] && (m[3].length === 2 ? "20" + m[3] : m[3]));
    } else if ((m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(s))) {
        d = monthDay(today, Number(m[2]) - 1, Number(m[3]), m[1]);
    }
    return d ? { date: isoDay(d), time } : null;
}
function monthDay(today, month, day, year) {
    if (month < 0 || month > 11 || day < 1 || day > 31) return null;
    let d = new Date(year ? Number(year) : today.getFullYear(), month, day);
    if (d.getMonth() !== month) return null;                    // "feb 30"
    if (!year && d < today) d = new Date(today.getFullYear() + 1, month, day);
    return d;
}

// What "@" offers for what's typed after it: dates, the grocery list, people.
export function atItems(query, opts) {
    opts = opts || {};
    const q = String(query || "").toLowerCase().trim();
    if (q.length > 30 || q.split(" ").length > 4) return [];
    const now = opts.now || new Date(), items = [], seen = new Set();
    const addDate = (when, hint) => {
        if (!when || seen.has(when.date + when.time)) return;
        seen.add(when.date + when.time);
        const day = fromIso(when.date);
        const full = day.toLocaleDateString("en-US", { weekday: "short", month: "short", day: "numeric" });
        items.push({ kind: "date", date: when.date, time: when.time, icon: "📅",
            label: dateLabel(when.date, when.time, now), hint: hint || full + (when.time ? " · " + timeLabel(when.time) : "") });
    };
    addDate(parseWhen(q, now));
    ["today", "tomorrow", "next week", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
        .filter((word) => (q ? word.startsWith(q) : ["today", "tomorrow", "next week"].includes(word)))
        .forEach((word) => addDate(parseWhen(word, now)));
    if (!q || "date".startsWith(q) || "pick a date".startsWith(q)) items.push({ kind: "pick", icon: "🗓", label: "Pick a date…", hint: "Due date and time" });
    if (opts.groceries && (!q || "grocery list".startsWith(q) || q.startsWith("grocer") || "groceries".startsWith(q))) {
        items.push({ kind: "grocery", icon: "🛒", label: "Grocery list", hint: "Put this line on your grocery list" });
    }
    (opts.people || []).filter((p) => !q || p.name.toLowerCase().split(/\s+/).some((w) => w.startsWith(q)) || p.name.toLowerCase().startsWith(q))
        .slice(0, 5).forEach((p) => items.push({ kind: "person", pid: p.id, name: p.name, icon: "@", label: p.name, hint: "Mention" }));
    return items.slice(0, 9);
}

let chipSeq = 0;
const SmartChip = Node.create({
    name: "smartChip",
    group: "inline",
    inline: true,
    atom: true,
    selectable: true,
    addAttributes() {
        const keep = (name, fallback) => ({ default: fallback, rendered: false, parseHTML: (el) => {
            const a = parseToken(el.getAttribute("data-chip"));
            return a ? a[name] : fallback;
        } });
        return {
            kind: keep("kind", "date"), date: keep("date", null), time: keep("time", null), task: keep("task", null),
            ids: keep("ids", ""), already: keep("already", false), pid: keep("pid", null), name: keep("name", ""),
            uid: { default: null, rendered: false, parseHTML: () => "c" + (++chipSeq) },    // finds it again (not saved)
            fresh: { default: false, rendered: false, parseHTML: () => false },          // just made: not sent yet
        };
    },
    parseHTML() { return [{ tag: "span[data-chip]" }]; },
    renderHTML({ node }) {
        const a = node.attrs;
        let cls = "nt-smart nt-smart--" + a.kind;
        if (a.kind === "date") {
            const day = fromIso(a.date), n = new Date();
            if (day && day < new Date(n.getFullYear(), n.getMonth(), n.getDate())) cls += " is-overdue";
            if (a.task) cls += " is-linked";
        }
        if (a.kind === "grocery" && !a.ids) cls += " is-pending";
        return ["span", { "data-chip": tokenOf(a), class: cls, contenteditable: "false" }, chipLabel(a)];
    },
    renderText({ node }) { return chipLabel(node.attrs); },
});

// "@" anywhere a word could start opens the smart-link menu; the page draws it (options.at: onOpen/onUpdate/onKey/
// onClose, people, groceries, onPick(item, range) for "Pick a date…").
function atExtension(at) {
    return Extension.create({
        name: "atMenu",
        addProseMirrorPlugins() {
            return [Suggestion({
                editor: this.editor,
                char: "@",
                pluginKey: new PluginKey("atMenu"),
                allowSpaces: true,
                startOfLine: false,
                allowedPrefixes: [" ", " ", "(", "["],
                items: ({ query }) => atItems(query, { people: at.people, groceries: at.groceries }),
                command: ({ editor, range, props }) => insertChip(editor, props, range, at),
                allow: ({ state }) => !state.selection.$from.parent.type.spec.code,
                render: () => ({
                    onStart: (props) => at.onOpen && at.onOpen(props),
                    onUpdate: (props) => {
                        if (/^grocer(y|ies) $/i.test(props.query) && at.groceries) {   // "@grocery" then space
                            props.command({ kind: "grocery" });
                            return;
                        }
                        if (at.onUpdate) at.onUpdate(props);
                    },
                    onKeyDown: (props) => (at.onKey ? at.onKey(props) : false),
                    onExit: () => at.onClose && at.onClose(),
                }),
            })];
        },
    });
}

export function insertChip(editor, item, range, at) {
    if (item.kind === "pick") {
        if (at && at.onPick) at.onPick(range);
        return;
    }
    const attrs = { kind: item.kind, fresh: true, uid: "c" + (++chipSeq) };
    if (item.kind === "date") Object.assign(attrs, { date: item.date, time: item.time || null });
    if (item.kind === "person") Object.assign(attrs, { pid: item.pid, name: item.name });
    let chain = editor.chain().focus();
    if (range) chain = chain.deleteRange(range);
    chain.insertContent([{ type: "smartChip", attrs }, { type: "text", text: " " }]).run();
}

// Every chip in the document: [{pos, node, key, uid, block (its textblock's position), checked (null or the task item's)}].
export function findChips(editor) {
    const out = [];
    editor.state.doc.descendants((node, pos) => {
        if (node.type.name !== "smartChip") return true;
        const $pos = editor.state.doc.resolve(pos);
        let checked = null, itemPos = null;
        for (let d = $pos.depth; d > 0; d--) {
            if ($pos.node(d).type.name === "taskItem") { checked = !!$pos.node(d).attrs.checked; itemPos = $pos.before(d); break; }
        }
        const a = node.attrs;
        const key = a.kind === "date" && a.task ? "task:" + a.task : a.kind === "grocery" && a.ids ? "grocery:" + a.ids : null;
        out.push({ pos, node, attrs: a, key, uid: a.uid, block: $pos.before($pos.depth), blockEnd: $pos.after($pos.depth),
                   text: $pos.parent.textContent.trim(), checked, itemPos });
        return false;
    });
    return out;
}

export function updateChip(editor, uid, changes, meta) {
    const chip = findChips(editor).find((c) => c.uid === uid);
    if (!chip) return false;
    const tr = editor.state.tr.setNodeMarkup(chip.pos, undefined, { ...chip.node.attrs, ...changes });
    if (meta) tr.setMeta("noteSync", true).setMeta("addToHistory", false);
    editor.view.dispatch(tr);
    return true;
}

export function removeChip(editor, uid) {
    const chip = findChips(editor).find((c) => c.uid === uid);
    if (!chip) return false;
    editor.view.dispatch(editor.state.tr.delete(chip.pos, chip.pos + chip.node.nodeSize));
    return true;
}

// What came from the Planner / grocery list ([{key, checked?, date?, time?, unlink?}]) - patched in without moving the
// cursor or adding to undo.
export function applySync(editor, sync) {
    if (!sync || !sync.length) return false;
    const tr = editor.state.tr;
    findChips(editor).forEach((c) => {
        sync.filter((s) => s.key && s.key === c.key).forEach((s) => {
            const attrs = { ...c.node.attrs };
            if (s.unlink) attrs.task = null;
            if (s.date !== undefined) { attrs.date = s.date; attrs.time = s.time || null; }
            tr.setNodeMarkup(tr.mapping.map(c.pos), undefined, attrs);
            if (s.checked !== undefined && c.itemPos != null && c.checked !== s.checked) {
                const at = tr.mapping.map(c.itemPos), item = tr.doc.nodeAt(at);
                if (item) tr.setNodeMarkup(at, undefined, { ...item.attrs, checked: s.checked });
            }
        });
    });
    if (!tr.docChanged) return false;
    editor.view.dispatch(tr.setMeta("noteSync", true).setMeta("addToHistory", false));
    return true;
}

// ---------------------------------------------------------------------------------------------------------------
// Blocks for the "/" menu and the Aa menu
// ---------------------------------------------------------------------------------------------------------------
export const BLOCKS = [
    { id: "paragraph", label: "Text", hint: "Plain body text", keys: "text body paragraph normal", icon: "T",
      run: (c) => c.setParagraph() },
    { id: "h1", label: "Heading 1", hint: "# then space", keys: "heading h1 title big", icon: "H1",
      run: (c) => c.setHeading({ level: 1 }) },
    { id: "h2", label: "Heading 2", hint: "## then space", keys: "heading h2 subtitle", icon: "H2",
      run: (c) => c.setHeading({ level: 2 }) },
    { id: "h3", label: "Heading 3", hint: "### then space", keys: "heading h3", icon: "H3",
      run: (c) => c.setHeading({ level: 3 }) },
    { id: "small", label: "Small text", hint: "-# then space", keys: "small caption fine print", icon: "t",
      run: (c) => c.setSmallText() },
    { id: "check", label: "Checklist", hint: "[ ] then space", keys: "checklist todo task check box", icon: "☑",
      run: (c) => c.toggleTaskList() },
    { id: "bullet", label: "Bulleted list", hint: "- then space", keys: "bullet list unordered", icon: "•",
      run: (c) => c.toggleBulletList() },
    { id: "number", label: "Numbered list", hint: "1. then space", keys: "numbered ordered list", icon: "1.",
      run: (c) => c.toggleOrderedList() },
    { id: "quote", label: "Quote", hint: "> then space", keys: "quote blockquote", icon: "❝",
      run: (c) => c.toggleBlockquote() },
    { id: "callout", label: "Callout", hint: "A highlighted box with an icon", keys: "callout note tip warning box", icon: "💡",
      run: (c) => c.toggleCallout() },
    { id: "toggle", label: "Toggle", hint: "A collapsible section", keys: "toggle collapse details fold", icon: "▸",
      run: (c) => c.setDetails() },
    { id: "divider", label: "Divider", hint: "--- on its own line", keys: "divider rule line hr separator", icon: "—",
      run: (c) => c.setHorizontalRule() },
    { id: "code", label: "Code block", hint: "``` then space", keys: "code block pre snippet", icon: "</>",
      run: (c) => c.toggleCodeBlock() },
    { id: "image", label: "Image or file", hint: "From your device - or paste / drop one", keys: "image photo picture file attachment upload", icon: "🖼",
      run: null },
];

export function filterBlocks(query) {
    const q = String(query || "").toLowerCase().trim();
    if (!q) return BLOCKS;
    return BLOCKS.filter((b) => (b.label + " " + b.keys).toLowerCase().split(/\s+/).some((w) => w.startsWith(q)) ||
        b.label.toLowerCase().includes(q));
}

export function runBlock(editor, id, range) {
    const item = BLOCKS.find((b) => b.id === id);
    if (!item || !item.run) return false;
    let chain = editor.chain().focus();
    if (range) chain = chain.deleteRange(range);
    return item.run(chain).run();
}

// "/" anywhere a word could start opens the block menu; the page draws it (options.slash: onOpen/onUpdate/onKey/onClose).
function slashExtension(slash) {
    return Extension.create({
        name: "slashMenu",
        addProseMirrorPlugins() {
            return [Suggestion({
                editor: this.editor,
                char: "/",
                allowSpaces: false,
                startOfLine: false,
                allowedPrefixes: [" ", " "],
                items: ({ query }) => filterBlocks(query),
                command: ({ editor, range, props }) => {
                    if (props.id === "image") {
                        editor.chain().focus().deleteRange(range).run();
                        if (slash.onImage) slash.onImage();
                        return;
                    }
                    runBlock(editor, props.id, range);
                },
                allow: ({ state }) => !state.selection.$from.parent.type.spec.code,
                render: () => ({
                    onStart: (props) => slash.onOpen && slash.onOpen(props),
                    onUpdate: (props) => slash.onUpdate && slash.onUpdate(props),
                    onKeyDown: (props) => (slash.onKey ? slash.onKey(props) : false),
                    onExit: () => slash.onClose && slash.onClose(),
                }),
            })];
        },
    });
}

// Ticked items move to the bottom of their checklist (the note's "Move checked items to the bottom").
export function sortChecked(editor) {
    const { state } = editor;
    const tr = state.tr;
    const lists = [];
    state.doc.descendants((node, pos) => { if (node.type.name === "taskList") lists.push({ node, pos }); });
    lists.reverse().forEach(({ node, pos }) => {
        const items = [];
        node.forEach((child) => items.push(child));
        const sorted = items.filter((i) => !i.attrs.checked).concat(items.filter((i) => i.attrs.checked));
        if (sorted.every((item, i) => item === items[i])) return;
        tr.replaceWith(pos + 1, pos + node.nodeSize - 1, sorted);
    });
    if (tr.docChanged) editor.view.dispatch(tr);
}

// ---------------------------------------------------------------------------------------------------------------
// The editor
// ---------------------------------------------------------------------------------------------------------------
export function create(options) {
    const extensions = [
        StarterKit.configure({
            heading: { levels: [1, 2, 3, 4, 5, 6] },
            link: { openOnClick: false, autolink: true, linkOnPaste: true, defaultProtocol: "https",
                    HTMLAttributes: { rel: "noopener noreferrer", target: "_blank" } },
            dropcursor: { color: "var(--nt-accent, #EC4899)", width: 2 },
        }),
        TaskList,
        TaskItem.configure({ nested: true, onReadOnlyChecked: options.onReadOnlyChecked || (() => false) }),
        Highlight,
        Image.configure({ inline: false, allowBase64: false }),
        Details.configure({ persist: false, HTMLAttributes: { class: "nt-toggle" } }),
        DetailsSummary,
        DetailsContent,
        TableKit.configure({ table: { resizable: false } }),
        SmallText,
        Callout,
        SmartChip,
        FileDrop.configure({ onFiles: options.onFiles || null }),
        Placeholder.configure({
            includeChildren: true,
            placeholder: ({ node, editor }) => {
                if (node.type.name === "heading") return "Heading " + node.attrs.level;
                if (node.type.name === "detailsSummary") return "Toggle title";
                if (editor.isEmpty) return options.placeholder || "Start writing… type / for blocks";
                return "";
            },
            showOnlyCurrent: false,
        }),
    ];
    if (options.slash) extensions.push(slashExtension(options.slash));
    if (options.at) extensions.push(atExtension(options.at));
    if (options.bubble) {
        extensions.push(BubbleMenu.configure({
            element: options.bubble,
            shouldShow: ({ editor, state, from, to }) => {
                if (!editor.isEditable || from === to || options.isCoarse && options.isCoarse()) return false;
                if (state.selection.node) return false;
                return !editor.isActive("codeBlock");
            },
            options: { placement: "top", offset: 8 },
        }));
    }
    const editor = new Editor({
        element: options.element,
        extensions,
        content: mdToHtml(options.markdown || ""),
        editable: options.editable !== false,
        injectCSS: false,                       // the site's CSP has no inline styles: notes.css has the base rules
        editorProps: {
            attributes: { class: "note-content nt-pm", spellcheck: "true", "aria-label": "Note", role: "textbox", "aria-multiline": "true" },
        },
        onUpdate: options.onUpdate ? ({ editor: ed, transaction }) => options.onUpdate(ed, transaction) : undefined,
        onSelectionUpdate: options.onSelection ? ({ editor: ed }) => options.onSelection(ed) : undefined,
        onFocus: options.onFocus ? ({ editor: ed, event }) => options.onFocus(ed, event) : undefined,
        onBlur: options.onBlur ? ({ editor: ed, event }) => options.onBlur(ed, event) : undefined,
    });
    return {
        editor,
        getMarkdown: () => toMarkdown(editor.getJSON()),
        setMarkdown: (markdown) => editor.commands.setContent(mdToHtml(markdown), { emitUpdate: false }),
        text: () => editor.getText({ blockSeparator: "\n" }),
    };
}

export { CALLOUT_ICONS };
