/*
 * Albright Notes (albright_notes/base.html loads it on every Notes page). Vanilla JS.
 *
 * The list: filters apply as soon as one changes (the search box: on Enter);
 * the Filter button is only for no-JS use.
 *
 * A note's page:
 *   - Write / Preview / Side by side (the default on wide screens);
 *   - autosave while typing (title, body, tags), debounced, with "Saving…" /
 *     "Saved" - each save sends the version it started from, and a 409 means
 *     someone else changed the note meanwhile: the conflict banner offers
 *     "Load their version" or "Keep mine (overwrite)";
 *   - ticking a checklist box in the preview rewrites that "- [ ]" line (server-side);
 *   - the link picker searches the apps you can open as you type.
 */
(function () {
    "use strict";

    var SAVE_DELAY = 800;

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

    // ---------- The list: filters apply on change ----------
    var filters = byId("nt-filters");
    if (filters) {
        filters.addEventListener("change", function (event) {
            if (event.target.name !== "q") filters.submit();
        });
    }

    // Any <select data-autosubmit> (sharing permissions) saves on change.
    document.addEventListener("change", function (event) {
        if (event.target.matches && event.target.matches("select[data-autosubmit]")) event.target.form.submit();
    });

    // ---------- One note ----------
    var root = byId("note");
    if (!root) return;
    var canEdit = root.dataset.canEdit === "1";
    var version = parseInt(root.dataset.version, 10) || 1;
    var editor = byId("nt-editor");
    var preview = byId("nt-preview");
    var saved = byId("nt-saved");

    // Write / Preview / Side by side
    function setMode(mode) {
        editor.dataset.mode = mode;
        editor.querySelectorAll("[data-mode-button]").forEach(function (b) {
            b.setAttribute("aria-pressed", b.dataset.modeButton === mode ? "true" : "false");
        });
        if (mode !== "write") flush();
    }
    if (canEdit) {
        editor.addEventListener("click", function (event) {
            var button = event.target.closest("[data-mode-button]");
            if (button) setMode(button.dataset.modeButton);
        });
        if (window.matchMedia("(min-width: 1000px)").matches) setMode("split");
    }

    // ---------- Autosave ----------
    var fields = {title: byId("nt-title"), body: byId("nt-body"), tags: byId("nt-tags")};
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
        if (data.html != null) preview.innerHTML = data.html || '<p class="nt-hint">Nothing written yet.</p>';
        if (data.updated && byId("nt-updated")) byId("nt-updated").textContent = "at " + data.updated;
        if (replaceInputs) {
            if (fields.title) fields.title.value = data.title;
            if (fields.body) fields.body.value = data.body;
            if (fields.tags) fields.tags.value = (data.tags || []).join(", ");
            document.title = data.title + document.title.slice(document.title.indexOf(" · "));
        }
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
            showSaved("Saved");
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
                schedule();
            });
            input.addEventListener("blur", flush);
        });
        // The title box: Enter moves to the body instead of doing nothing.
        if (fields.title) fields.title.addEventListener("keydown", function (event) {
            if (event.key === "Enter") { event.preventDefault(); if (fields.body) fields.body.focus(); }
        });
        // Leaving with unsaved changes: save now (sendBeacon can't carry the CSRF header, so ask first).
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

    // ---------- Checklist: tick a box in the preview ----------
    preview.addEventListener("change", function (event) {
        var box = event.target;
        if (!box.matches || !box.matches("input.nt-check[data-task]")) return;
        var index = parseInt(box.dataset.task, 10), checked = box.checked;
        flush();
        (inFlight || Promise.resolve()).then(function () {
            if (conflict) { box.checked = !checked; return; }
            return postJson(root.dataset.checkUrl, {index: index, checked: checked, version: version}).then(function (res) {
                if (res.status === 409) { box.checked = !checked; showConflict(res.data); return; }
                if (!res.ok) { box.checked = !checked; showSaved("Couldn't save", true); return; }
                applyState(res.data, false);
                if (fields.body && !dirty.body) fields.body.value = res.data.body;
                showSaved("Saved");
            });
        }).catch(function () { box.checked = !checked; showSaved("Offline - not saved", true); });
    });

    // ---------- Link picker ----------
    var picker = byId("nt-picker");
    if (picker) {
        var input = byId("nt-picker-q"), list = byId("nt-picker-results"), searchTimer = null, lastQuery = "";
        var renderResults = function (results) {
            list.textContent = "";
            if (!results.length) {
                list.appendChild(el("li", "nt-picker__empty", "Nothing found in the apps you can open."));
            }
            results.forEach(function (r) {
                var li = el("li", "nt-picker__result");
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
                fetch(picker.dataset.searchUrl + "?q=" + encodeURIComponent(q), {credentials: "same-origin",
                    headers: {"X-Requested-With": "XMLHttpRequest"}})
                    .then(function (r) { return r.json(); })
                    .then(function (data) { if (q === lastQuery) renderResults(data.results || []); })
                    .catch(function () { list.hidden = true; });
            }, 250);
        });
        input.addEventListener("keydown", function (event) {
            if (event.key === "Escape") { list.hidden = true; }
            if (event.key === "Enter") event.preventDefault();
        });
    }
})();
