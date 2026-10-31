/*
 * Albright Notes - the three-pane workspace (albright_notes/workspace.html, Option A of
 * docs/design/"Albright Notes - design options.html"). Vanilla JS, loaded by albright_notes/base.html.
 *
 *   "/"            focuses the search box (when you're not typing somewhere)
 *   toolbar        B / I / H / ☑ List format the selection in the Markdown (switching to it if needed);
 *                  🔗 Link record, 👥 Share and ⋯ open their panels
 *   the document   shows the rendered note; click it (not a link or checkbox) to edit the Markdown,
 *                  Esc or clicking away shows it rendered again
 *   autosave       title, body and tags save as you type (debounced): "Saving…" / "Saved ✓". Each save
 *                  sends the version it started from; a 409 means someone else changed the note meanwhile
 *                  and the warning offers "Load their version" or "Keep mine (overwrite)"
 *   checklist      ticking a box in the rendered note rewrites that "- [ ]" line on the server
 *   word count     the footer's count follows the text
 * On a phone the panes stack; opening a note scrolls to it.
 */
(function () {
    "use strict";

    var SAVE_DELAY = 800;
    var workspace = document.getElementById("notes");

    function byId(id) { return document.getElementById(id); }
    function csrfToken() {
        var match = document.cookie.match(/csrftoken=([^;]+)/);
        if (match) return match[1];
        var input = document.querySelector("input[name=csrfmiddlewaretoken]");
        return input ? input.value : "";
    }
    function postJson(url, body) {
        return fetch(url, {
            method: "POST", credentials: "same-origin",
            headers: {"Content-Type": "application/json", "X-CSRFToken": csrfToken(), "X-Requested-With": "XMLHttpRequest"},
            body: JSON.stringify(body || {}),
        }).then(function (r) {
            return r.json().catch(function () { return {}; }).then(function (data) { return {status: r.status, ok: r.ok, data: data}; });
        });
    }
    function el(tag, className, text) {
        var node = document.createElement(tag);
        if (className) node.className = className;
        if (text != null) node.textContent = text;
        return node;
    }
    function isTyping(target) {
        return target && (target.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(target.tagName));
    }

    // Any <select data-autosubmit> (a share's permission) saves on change.
    document.addEventListener("change", function (event) {
        if (event.target.matches && event.target.matches("select[data-autosubmit]")) event.target.form.submit();
    });

    // "/" to search
    var search = byId("nt-q");
    document.addEventListener("keydown", function (event) {
        if (event.key === "/" && search && !isTyping(event.target) && !event.ctrlKey && !event.metaKey && !event.altKey) {
            event.preventDefault();
            search.focus();
            search.select();
        }
    });

    var root = byId("note");
    if (!workspace || !root) return;
    var canEdit = root.dataset.canEdit === "1";
    var version = parseInt(root.dataset.version, 10) || 1;
    var doc = byId("nt-doc");
    var preview = byId("nt-preview");
    var saved = byId("nt-saved");
    var words = byId("nt-words");
    var fields = {title: byId("nt-title"), body: byId("nt-body"), tags: byId("nt-tags")};

    // A phone shows the panes stacked: bring the note you picked into view.
    if (window.matchMedia("(max-width: 860px)").matches && /\/notes\/\d+\//.test(location.pathname)) {
        root.scrollIntoView({block: "start"});
    }

    // ---------- Panels: Link record, Share, More ----------
    root.addEventListener("click", function (event) {
        var button = event.target.closest("[data-pop]");
        if (!button) return;
        var pop = byId(button.dataset.pop), open = pop.hidden;
        root.querySelectorAll(".nt-pop").forEach(function (p) { p.hidden = true; });
        root.querySelectorAll("[data-pop]").forEach(function (b) { b.setAttribute("aria-expanded", "false"); });
        pop.hidden = !open;
        button.setAttribute("aria-expanded", open ? "true" : "false");
        if (open) {
            var first = pop.querySelector("input:not([type=hidden]), select, button");
            if (first) first.focus();
        }
    });
    if (location.hash === "#links" && byId("nt-pop-link")) {
        byId("nt-pop-link").hidden = false;
    }

    // ---------- Word count ----------
    // A word: anything between spaces with a letter or digit in it (views.word_count counts the same way).
    function countWords(text) {
        return (text || "").split(/\s+/).filter(function (w) { return /[0-9A-Za-zÀ-￿]/.test(w); }).length;
    }
    function showWords() {
        if (!words || !fields.body) return;
        var n = countWords(fields.body.value);
        words.textContent = n + " word" + (n === 1 ? "" : "s");
    }

    // ---------- Rendered note <-> Markdown ----------
    function setMode(mode) {
        if (!canEdit) return;
        doc.dataset.mode = mode;
        if (mode === "write") fields.body.focus();
    }
    if (canEdit) {
        preview.addEventListener("click", function (event) {
            if (event.target.closest("a, input, label")) return;
            if (window.getSelection && String(window.getSelection())) return;     // selecting text to copy
            var item = event.target.closest("li.nt-task");
            var box = item && item.querySelector("input.nt-check[data-task]");
            if (box && box.closest("li") === item) { openTask(parseInt(box.dataset.task, 10), null); return; }
            setMode("write");
        });
        fields.body.addEventListener("keydown", function (event) {
            if (event.key === "Escape") { event.preventDefault(); flush(); setMode("preview"); }
            else if (event.key === "Enter") continueList(event);
        });
        fields.body.addEventListener("blur", function (event) {
            // Leaving for the toolbar (B / I / H / List) keeps the Markdown open.
            if (event.relatedTarget && event.relatedTarget.closest && event.relatedTarget.closest(".nt-toolbar")) return;
            flush();
            if (fields.body.value.trim()) setMode("preview");
        });
        if (/[?&]new=1/.test(location.search) && fields.title) {
            fields.title.focus();
            fields.title.select();
        }
    }

    // ---------- Formatting toolbar ----------
    function lineRange(text, start, end) {
        var from = text.lastIndexOf("\n", start - 1) + 1;
        var to = text.indexOf("\n", end);
        return [from, to === -1 ? text.length : to];
    }
    function format(kind) {
        var box = fields.body;
        if (doc.dataset.mode !== "write") setMode("write");
        var text = box.value, start = box.selectionStart, end = box.selectionEnd, out, selStart, selEnd;
        if (kind === "bold" || kind === "italic") {
            var mark = kind === "bold" ? "**" : "*";
            var picked = text.slice(start, end) || (kind === "bold" ? "bold text" : "italic text");
            out = text.slice(0, start) + mark + picked + mark + text.slice(end);
            selStart = start + mark.length;
            selEnd = selStart + picked.length;
        } else {
            var range = lineRange(text, start, end);
            var lines = text.slice(range[0], range[1]).split("\n").map(function (line) {
                if (kind === "heading") return /^#{1,6}\s/.test(line) ? line.replace(/^#{1,6}\s+/, "") : "## " + line;
                return /^\s*[-*+]\s+\[[ xX]\]\s?/.test(line) ? line : "- [ ] " + line.replace(/^\s*[-*+]\s+/, "");
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
    root.querySelectorAll("[data-fmt]").forEach(function (button) {
        button.addEventListener("mousedown", function (event) { event.preventDefault(); });   // keep the selection
        button.addEventListener("click", function () { format(button.dataset.fmt); });
    });

    // ---------- Lists continue on Enter (the Markdown) ----------
    // "- [ ] item" / "- item" / "* item" / "1. item": Enter starts the next item (same indentation; numbers count
    // up), splitting the line if the cursor is mid-text. Enter on an empty item removes its marker and ends the
    // list. Shift+Enter is a plain line break.
    var LIST_ITEM = /^([ \t]*)(?:([-*+])[ \t]+\[[ xX]\](?:[ \t]+|$)|([-*+])[ \t]+|(\d{1,9})([.)])[ \t]+)/;

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

    function continueList(event) {
        if (event.shiftKey || event.ctrlKey || event.metaKey || event.altKey || event.isComposing) return;
        var box = fields.body, text = box.value, start = box.selectionStart, end = box.selectionEnd;
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
        var insert = "\n" + indent + marker + rest;
        replaceText(box, start, lineEnd, insert, start + 1 + indent.length + marker.length);
    }

    // ---------- Checklist items edited in place (the rendered note) ----------
    // Click an item's text to edit it right there. Enter adds a new unchecked item below (splitting at the cursor)
    // and moves to it; Enter on an empty item ends the list (its marker goes, and the Markdown opens on that
    // line); Backspace on an empty item removes it and moves to the item above; Esc or clicking away finishes.
    // Every change rewrites that line of the Markdown and autosaves; the server's rendering replaces the local
    // one once you've stopped editing.
    var TASK_LINE = /^((?:[ \t]*>)*[ \t]*[-*+][ \t]+)\[([ xX])\](?=[ \t]|$)/, FENCE = /^[ \t]*(```|~~~)/;
    var taskEdit = null, pendingHtml = null;

    function taskLines(body) {
        var out = [], fenced = false;
        body.split("\n").forEach(function (line, i) {
            if (FENCE.test(line)) { fenced = !fenced; return; }
            if (!fenced && TASK_LINE.test(line)) out.push(i);
        });
        return out;
    }
    function taskBox(index) { return preview.querySelector('input.nt-check[data-task="' + index + '"]'); }
    function renumber(from, delta) {
        Array.prototype.slice.call(preview.querySelectorAll("input.nt-check[data-task]")).forEach(function (box) {
            var n = parseInt(box.dataset.task, 10);
            if (n >= from) box.dataset.task = String(n + delta);
        });
    }
    function bodyChanged(lines) {
        fields.body.value = lines.join("\n");
        dirty.body = true;
        showSaved("Unsaved changes");
        showWords();
        schedule();
    }
    // The nodes after an item's checkbox (its text), up to any nested list.
    function itemText(box) {
        var nodes = [], node = box.nextSibling;
        while (node && !(node.nodeType === 1 && /^(UL|OL)$/.test(node.tagName))) { nodes.push(node); node = node.nextSibling; }
        return nodes;
    }

    function openTask(index, caret) {
        var box = taskBox(index), lines = fields.body.value.split("\n"), number = taskLines(fields.body.value)[index];
        if (!box || number == null) return;
        closeTask(true);
        pendingHtml = null;
        var m = TASK_LINE.exec(lines[number]);
        var input = el("input", "nt-task-input");
        input.type = "text";
        input.value = lines[number].slice(m[0].length).replace(/^[ \t]+/, "");
        input.setAttribute("aria-label", "Checklist item");
        var nodes = itemText(box);
        nodes.forEach(function (node) { node.remove(); });
        box.after(input);
        taskEdit = {index: index, input: input};
        input.focus();
        var at = caret == null ? input.value.length : caret;
        input.setSelectionRange(at, at);
        input.addEventListener("input", function () { setTask(taskEdit.index, input.value); });
        input.addEventListener("keydown", taskKey);
        input.addEventListener("blur", function () { if (taskEdit && taskEdit.input === input) closeTask(false); });
    }

    function closeTask(switching) {
        if (!taskEdit) return;
        var input = taskEdit.input;
        taskEdit = null;
        input.replaceWith(document.createTextNode(" " + input.value));
        if (!switching) {
            flush();
            if (pendingHtml != null && !Object.keys(dirty).length && !inFlight) {
                preview.innerHTML = pendingHtml;
            }
            pendingHtml = null;
        }
    }

    function setTask(index, value) {
        var lines = fields.body.value.split("\n"), number = taskLines(fields.body.value)[index];
        if (number == null) return;
        var m = TASK_LINE.exec(lines[number]);
        lines[number] = m[1] + "[" + m[2] + "] " + value;
        bodyChanged(lines);
    }

    function taskKey(event) {
        var input = event.target, index = taskEdit.index;
        var lines = fields.body.value.split("\n"), numbers = taskLines(fields.body.value), number = numbers[index];
        var box = taskBox(index), item = box.closest("li");
        if (event.key === "Escape") { event.preventDefault(); input.blur(); return; }
        if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
            event.preventDefault();
            if (!input.value.trim()) {                                    // an empty item: end the list here
                lines[number] = "";
                taskEdit = null;
                item.remove();
                renumber(index + 1, -1);
                bodyChanged(lines);
                var offset = lines.slice(0, number).join("\n").length + (number ? 1 : 0);
                setMode("write");
                fields.body.setSelectionRange(offset, offset);
                return;
            }
            var caret = input.selectionStart, before = input.value.slice(0, caret), after = input.value.slice(input.selectionEnd);
            var prefix = TASK_LINE.exec(lines[number])[1];
            lines[number] = prefix + "[" + TASK_LINE.exec(lines[number])[2] + "] " + before;
            lines.splice(number + 1, 0, prefix + "[ ] " + after);
            input.value = before;
            renumber(index + 1, 1);
            var next = el("li", "nt-task"), check = el("input", "nt-check");
            check.type = "checkbox";
            check.dataset.task = String(index + 1);
            check.setAttribute("aria-label", "Done");
            next.appendChild(check);
            next.appendChild(document.createTextNode(" " + after));
            item.after(next);
            bodyChanged(lines);
            openTask(index + 1, 0);
            return;
        }
        if (event.key === "Backspace" && !input.value && input.selectionStart === 0) {   // remove the empty item
            event.preventDefault();
            lines.splice(number, 1);
            taskEdit = null;
            item.remove();
            renumber(index + 1, -1);
            bodyChanged(lines);
            if (index > 0) openTask(index - 1, null);
            else flush();
        }
    }

    // ---------- Autosave ----------
    var dirty = {}, timer = null, inFlight = null, conflict = false;

    function showSaved(text, isError) {
        if (!saved) return;
        saved.textContent = text;
        saved.classList.toggle("is-error", !!isError);
    }

    function values(keys) {
        var out = {};
        keys.forEach(function (key) {
            if (fields[key]) out[key] = key === "tags" ? fields[key].value.split(",") : fields[key].value;
        });
        return out;
    }

    function applyState(data, replaceInputs) {
        version = data.version;
        root.dataset.version = String(version);
        if (data.html != null) {
            var html = data.html || '<p class="nt-muted">Nothing written yet.</p>';
            if (taskEdit) pendingHtml = html;                 // don't pull the item being edited out from under you
            else { preview.innerHTML = html; pendingHtml = null; }
        }
        if (data.updated && byId("nt-updated")) byId("nt-updated").textContent = "at " + data.updated;
        if (replaceInputs) {
            if (fields.title) fields.title.value = data.title;
            if (fields.body) fields.body.value = data.body;
            if (fields.tags) fields.tags.value = (data.tags || []).join(", ");
        }
        if (fields.title) {
            var row = byId("note-" + root.dataset.noteId);
            var title = row && row.querySelector("b");
            if (title) title.lastChild.textContent = (title.childNodes.length > 1 ? " " : "") + (data.title || fields.title.value);
        }
        showWords();
    }

    function showConflict(data) {
        conflict = true;
        byId("nt-conflict-who").textContent = data.updated_by || "Someone";
        byId("nt-conflict-when").textContent = data.updated || "";
        byId("nt-conflict").hidden = false;
        byId("nt-conflict")._theirs = data;
        showSaved("Not saved", true);
    }

    function save(force) {
        var keys = Object.keys(dirty);
        if (!keys.length || inFlight || (conflict && !force)) return inFlight || Promise.resolve();
        var body = values(keys);
        body.version = version;
        if (force) body.force = true;
        dirty = {};
        showSaved("Saving…");
        inFlight = postJson(root.dataset.saveUrl, body).then(function (res) {
            inFlight = null;
            if (res.status === 409) {
                keys.forEach(function (k) { dirty[k] = true; });
                showConflict(res.data);
                return;
            }
            if (!res.ok) {
                keys.forEach(function (k) { dirty[k] = true; });
                showSaved("Couldn't save - retrying when you type", true);
                return;
            }
            applyState(res.data, false);
            showSaved("Saved ✓");
            if (Object.keys(dirty).length) schedule();
        }).catch(function () {
            inFlight = null;
            keys.forEach(function (k) { dirty[k] = true; });
            showSaved("Offline - not saved", true);
        });
        return inFlight;
    }

    function schedule() {
        clearTimeout(timer);
        timer = setTimeout(function () { save(false); }, SAVE_DELAY);
    }
    function flush() {
        clearTimeout(timer);
        return save(false);
    }

    if (canEdit) {
        Object.keys(fields).forEach(function (key) {
            var input = fields[key];
            if (!input) return;
            input.addEventListener("input", function () {
                dirty[key] = true;
                showSaved("Unsaved changes");
                if (key === "body") showWords();
                schedule();
            });
            if (key !== "body") input.addEventListener("blur", flush);
        });
        // The title box: Enter moves on to the note itself.
        if (fields.title) fields.title.addEventListener("keydown", function (event) {
            if (event.key === "Enter") { event.preventDefault(); setMode("write"); }
        });
        // Leaving with unsaved changes: save now, and ask the browser to wait.
        window.addEventListener("beforeunload", function (event) {
            if (Object.keys(dirty).length || inFlight) {
                flush();
                event.preventDefault();
                event.returnValue = "";
            }
        });

        byId("nt-conflict-load").addEventListener("click", function () {
            var theirs = byId("nt-conflict")._theirs;
            conflict = false;
            dirty = {};
            byId("nt-conflict").hidden = true;
            if (theirs) applyState(theirs, true);
            showSaved("Loaded the latest version");
        });
        byId("nt-conflict-keep").addEventListener("click", function () {
            conflict = false;
            byId("nt-conflict").hidden = true;
            ["title", "body", "tags"].forEach(function (k) { if (fields[k]) dirty[k] = true; });
            save(true);
        });
    }

    // ---------- Checklist: tick a box in the rendered note ----------
    preview.addEventListener("change", function (event) {
        var box = event.target;
        if (!box.matches || !box.matches("input.nt-check[data-task]")) return;
        var index = parseInt(box.dataset.task, 10), checked = box.checked;
        var item = box.closest("li");
        if (item) item.classList.toggle("done", checked);
        var undo = function () { box.checked = !checked; if (item) item.classList.toggle("done", !checked); };
        flush();
        (inFlight || Promise.resolve()).then(function () {
            if (conflict) { undo(); return; }
            return postJson(root.dataset.checkUrl, {index: index, checked: checked, version: version}).then(function (res) {
                if (res.status === 409) { undo(); showConflict(res.data); return; }
                if (!res.ok) { undo(); showSaved("Couldn't save", true); return; }
                applyState(res.data, false);
                if (fields.body && !dirty.body) fields.body.value = res.data.body;
                showWords();
                showSaved("Saved ✓");
            });
        }).catch(function () { undo(); showSaved("Offline - not saved", true); });
    });

    // ---------- Link record: search the apps you can open ----------
    var picker = byId("nt-picker");
    if (picker) {
        var input = byId("nt-picker-q"), list = byId("nt-picker-results"), searchTimer = null, lastQuery = "";
        var renderResults = function (results) {
            list.textContent = "";
            if (!results.length) list.appendChild(el("li", "nt-empty", "Nothing found in the apps you can open."));
            results.forEach(function (r) {
                var li = el("li");
                var form = el("form");
                form.method = "post";
                form.action = picker.dataset.addUrl;
                [["csrfmiddlewaretoken", csrfToken()], ["app", r.app], ["type", r.type], ["id", r.id]].forEach(function (pair) {
                    var hidden = el("input");
                    hidden.type = "hidden"; hidden.name = pair[0]; hidden.value = pair[1];
                    form.appendChild(hidden);
                });
                var button = el("button");
                button.type = "submit";
                button.appendChild(el("span", "", r.label));
                button.appendChild(el("small", "", r.app_label + " · " + r.kind));
                form.appendChild(button);
                li.appendChild(form);
                list.appendChild(li);
            });
            list.hidden = false;
        };
        input.addEventListener("input", function () {
            clearTimeout(searchTimer);
            var q = input.value.trim();
            if (q.length < 2) { list.hidden = true; return; }
            searchTimer = setTimeout(function () {
                lastQuery = q;
                fetch(workspace.dataset.searchUrl + "?q=" + encodeURIComponent(q), {credentials: "same-origin",
                    headers: {"X-Requested-With": "XMLHttpRequest"}})
                    .then(function (r) { return r.json(); })
                    .then(function (data) { if (q === lastQuery) renderResults(data.results || []); })
                    .catch(function () { list.hidden = true; });
            }, 250);
        });
        input.addEventListener("keydown", function (event) {
            if (event.key === "Escape") list.hidden = true;
            if (event.key === "Enter") event.preventDefault();
        });
    }
})();
