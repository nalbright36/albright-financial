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
            setMode("write");
        });
        fields.body.addEventListener("keydown", function (event) {
            if (event.key === "Escape") { event.preventDefault(); flush(); setMode("preview"); }
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
        if (data.html != null) preview.innerHTML = data.html || '<p class="nt-muted">Nothing written yet.</p>';
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
