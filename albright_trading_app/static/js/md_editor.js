/*
 * The Markdown editor shared by Notes (notes.js) and the Planner's notes box (templates/_md_editor.html).
 * Vanilla JS, loaded on every page by templates/index.html before the app's own script.
 *
 *   MdEditor.format(box, kind)      bold / italic / heading / bullet / number / check / link on the selection
 *   MdEditor.continueList(e, box)   Enter continues a list ("- ", "- [ ] ", "1. "); Enter on an empty item ends it
 *   MdEditor.shortcut(e, box)       Ctrl/Cmd+B and Ctrl/Cmd+I; true if it handled the key
 *   MdEditor.autogrow(box)          the box grows with its text (CSS caps it, then it scrolls)
 *
 * A [data-md-editor] block (the Planner's) gets all of that plus its toolbar ([data-fmt] buttons) and the
 * Write / Preview toggle: Preview posts the text to data-preview-url and shows the HTML the server sends back -
 * the same rendering and sanitizing as a note (albright_notes.render). Without JS it's a plain textarea.
 */
(function () {
    "use strict";

    var LIST_ITEM = /^([ \t]*)(?:([-*+])[ \t]+\[[ xX]\](?:[ \t]+|$)|([-*+])[ \t]+|(\d{1,9})([.)])[ \t]+)/;
    var BULLET = /^\s*[-*+]\s+(?!\[[ xX]\])/, NUMBER = /^\s*\d{1,9}[.)]\s+/, CHECK = /^\s*[-*+]\s+\[[ xX]\]\s?/;

    function lineRange(text, start, end) {
        var from = text.lastIndexOf("\n", start - 1) + 1;
        var to = text.indexOf("\n", end);
        return [from, to === -1 ? text.length : to];
    }

    function replaceText(box, from, to, text, caret) {
        box.focus();
        box.setSelectionRange(from, to);
        // insertText keeps the browser's undo history; setRangeText is the fallback.
        var done = document.queryCommandSupported && document.queryCommandSupported("insertText") &&
            document.execCommand("insertText", false, text);
        if (!done) {
            box.setRangeText(text, from, to, "end");
            box.dispatchEvent(new Event("input", {bubbles: true}));
        }
        box.setSelectionRange(caret, caret);
    }

    // A line without its list marker ("- ", "- [ ] ", "1. ").
    function bare(line) { return line.replace(CHECK, "").replace(BULLET, "").replace(NUMBER, ""); }

    function format(box, kind) {
        var text = box.value, start = box.selectionStart, end = box.selectionEnd, out, selStart, selEnd;
        if (kind === "bold" || kind === "italic" || kind === "link") {
            var picked = text.slice(start, end);
            if (kind === "link") {
                var label = picked || "link text", url = "https://";
                out = text.slice(0, start) + "[" + label + "](" + url + ")" + text.slice(end);
                selStart = start + label.length + 3;                     // the URL, ready to type over
                selEnd = selStart + url.length;
            } else {
                var mark = kind === "bold" ? "**" : "*";
                picked = picked || (kind === "bold" ? "bold text" : "italic text");
                out = text.slice(0, start) + mark + picked + mark + text.slice(end);
                selStart = start + mark.length;
                selEnd = selStart + picked.length;
            }
        } else {
            var range = lineRange(text, start, end);
            var lines = text.slice(range[0], range[1]).split("\n");
            var test = {check: CHECK, bullet: BULLET, number: NUMBER}[kind];
            var all = test && lines.every(function (line) { return !line.trim() || test.test(line); });
            var n = 0;
            lines = lines.map(function (line) {
                if (kind === "heading") return /^#{1,6}\s/.test(line) ? line.replace(/^#{1,6}\s+/, "") : "## " + line;
                if (all) return bare(line);                              // already that list: turn it off
                if (!line.trim() && lines.length > 1) return line;
                n += 1;
                return (kind === "check" ? "- [ ] " : kind === "bullet" ? "- " : n + ". ") + bare(line);
            });
            var block = lines.join("\n");
            out = text.slice(0, range[0]) + block + text.slice(range[1]);
            selStart = selEnd = range[0] + block.length;
        }
        box.value = out;
        box.focus();
        box.setSelectionRange(selStart, selEnd);
        box.dispatchEvent(new Event("input", {bubbles: true}));
    }

    // "- [ ] item" / "- item" / "* item" / "1. item": Enter starts the next item (same indentation; numbers count
    // up), splitting the line if the cursor is mid-text. Enter on an empty item removes its marker and ends the
    // list. Shift+Enter is a plain line break.
    function continueList(event, box) {
        if (event.shiftKey || event.ctrlKey || event.metaKey || event.altKey || event.isComposing) return;
        var text = box.value, start = box.selectionStart, end = box.selectionEnd;
        var lineStart = text.lastIndexOf("\n", start - 1) + 1;
        var lineEnd = text.indexOf("\n", start);
        if (lineEnd === -1) lineEnd = text.length;
        if (end > lineEnd) return;                                       // a selection across lines: plain Enter
        var line = text.slice(lineStart, lineEnd), m = LIST_ITEM.exec(line);
        if (!m || start - lineStart < m[0].length) return;               // not a list line, or the cursor is in the marker
        event.preventDefault();
        if (!line.slice(m[0].length).trim()) {                          // an empty item: end the list
            replaceText(box, lineStart, lineEnd, "", lineStart);
            return;
        }
        var indent = m[1];
        var marker = m[2] ? m[2] + " [ ] " : m[3] ? m[3] + " " : (parseInt(m[4], 10) + 1) + m[5] + " ";
        var rest = text.slice(end, lineEnd).replace(/^[ \t]+/, "");
        replaceText(box, start, lineEnd, "\n" + indent + marker + rest, start + 1 + indent.length + marker.length);
    }

    function shortcut(event, box) {
        if (!(event.ctrlKey || event.metaKey) || event.altKey || event.shiftKey) return false;
        var key = (event.key || "").toLowerCase();
        if (key !== "b" && key !== "i") return false;
        event.preventDefault();
        format(box, key === "b" ? "bold" : "italic");
        return true;
    }

    function autogrow(box) {
        function fit() {
            box.style.height = "auto";
            box.style.height = (box.scrollHeight + box.offsetHeight - box.clientHeight) + "px";
        }
        box.addEventListener("input", fit);
        fit();
    }

    function csrfToken() {
        var match = document.cookie.match(/csrftoken=([^;]+)/);
        if (match) return match[1];
        var input = document.querySelector("input[name=csrfmiddlewaretoken]");
        return input ? input.value : "";
    }

    // ---------- A [data-md-editor] block: toolbar, Write / Preview ----------
    function init(root) {
        var box = root.querySelector("[data-md-input]");
        var bar = root.querySelector("[data-md-toolbar]");
        var preview = root.querySelector("[data-md-preview]");
        if (!box) return;
        if (bar) bar.hidden = false;
        autogrow(box);
        box.addEventListener("keydown", function (event) {
            if (shortcut(event, box)) return;
            if (event.key === "Enter") continueList(event, box);
        });
        root.querySelectorAll("[data-fmt]").forEach(function (button) {
            button.addEventListener("mousedown", function (event) { event.preventDefault(); });   // keep the selection
            button.addEventListener("click", function () { setMode("write"); format(box, button.dataset.fmt); });
        });

        var shown = null;                                                // the text the preview shows
        function setMode(mode) {
            if (!preview) return;
            root.dataset.mode = mode;
            root.querySelectorAll("[data-md-mode]").forEach(function (b) {
                b.setAttribute("aria-pressed", b.dataset.mdMode === mode ? "true" : "false");
            });
            box.hidden = mode === "preview";
            preview.hidden = mode !== "preview";
            if (mode === "write") return;
            if (shown === box.value) return;
            var text = box.value;
            preview.textContent = "Loading preview…";
            var body = new FormData();
            body.append("notes", text);
            fetch(root.dataset.previewUrl, {
                method: "POST", credentials: "same-origin", body: body,
                headers: {"X-CSRFToken": csrfToken(), "X-Requested-With": "XMLHttpRequest"},
            }).then(function (r) { return r.ok ? r.json() : Promise.reject(r); }).then(function (data) {
                shown = text;
                if (data.html) preview.innerHTML = data.html;            // sanitized on the server
                else preview.innerHTML = '<p class="md-editor__empty">Nothing to preview yet.</p>';
            }).catch(function () {
                shown = null;
                preview.innerHTML = '<p class="md-editor__empty">Couldn’t load the preview - check your connection.</p>';
            });
        }
        root.querySelectorAll("[data-md-mode]").forEach(function (button) {
            button.addEventListener("click", function () {
                setMode(button.dataset.mdMode);
                if (button.dataset.mdMode === "write") box.focus();
            });
        });
    }

    window.MdEditor = {format: format, continueList: continueList, shortcut: shortcut, autogrow: autogrow,
                       replaceText: replaceText, init: init};          // init: an editor added later (planner.js)
    function start() { document.querySelectorAll("[data-md-editor]").forEach(init); }
    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start);
    else start();
})();
