/*
 * Albright Notes - the two-pane workspace (albright_notes/workspace.html). Vanilla JS; the rich editor itself is
 * the vendored TipTap bundle (window.NoteEditor, albright_notes/static/albright_notes/vendor/note-editor.js, built
 * from albright_notes/editor_src) - this file wires it into the page.
 *
 *   "/"            focuses the search box (when you're not typing somewhere)
 *   the list       a tap opens the note; swipe a row left on a touch screen for Pin and Delete (a full swipe
 *                  deletes) - a tap anywhere else or a scroll closes it again; on a desktop they show on hover.
 *                  Delete moves the note to Recently deleted with an Undo toast for 5 seconds
 *   the editor     the note is Markdown underneath (Note.body) - the editor reads it and writes it back on save.
 *                  Markdown shortcuts while typing (# , ## , ### , - , 1. , [ ] , > , ---, ```), "/" for the block
 *                  menu, Aa for text styles, a selection toolbar on a desktop and a bar above the keyboard on a
 *                  phone. Lists continue on Enter; Enter on an empty item ends the list; Tab / Shift+Tab nest.
 *                  On a phone the note opens in a reading layout; tap the text to edit right there
 *   "@"            a due date ("@fri 5pm", "@dec 3" - a chip; tap it to change it or "Add to Planner"), the grocery
 *                  list ("@grocery milk, eggs" - sent when you leave the line, with Undo), or a person. Linked dates
 *                  and grocery tags stay in step with their Planner task / grocery line both ways (smart.py)
 *   autosave       title, body, tags and "Move checked items to the bottom" save as you type (debounced): "Saving…" /
 *                  "Saved ✓". Each save sends the version it started from; a 409 means someone else changed the
 *                  note meanwhile and the warning offers "Load their version" or "Keep mine (overwrite)"
 *   saving         one save at a time; a failed save keeps its changes and retries ("Offline - will save"); leaving
 *                  the page sends what's unsaved with fetch keepalive
 *   footer         word count and "Edited 2 min ago", kept current
 *   images/files   the image button, or paste / drop files on the note: each is attached to the note
 *                  (labs/attachments.js uploads it) and goes in the text where you were - an image inline, any
 *                  other file as a 📎 link
 * On a phone the list and the note are separate screens (notes.css, data-screen); the formatting bar stays at the
 * bottom of the note screen. More (⋯) is a menu placed under its button.
 */
(function () {
    "use strict";

    var SAVE_DELAY = 800;
    var UNDO_MS = 5000;
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
    if (!workspace) return;

    // Any <select data-autosubmit> (a share's permission) saves on change; forms with data-confirm ask first.
    document.addEventListener("change", function (event) {
        if (event.target.matches && event.target.matches("select[data-autosubmit]")) event.target.form.submit();
    });
    workspace.addEventListener("submit", function (event) {
        var form = event.target;
        if (form.matches("form[data-confirm]") && !window.confirm(form.dataset.confirm)) event.preventDefault();
    });

    // Menus made of <details> (templates, sort) close when you click elsewhere.
    document.addEventListener("click", function (event) {
        workspace.querySelectorAll("details.nt-tpl[open], details.nt-sort[open]").forEach(function (d) {
            if (!d.contains(event.target)) d.open = false;
        });
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

    // ---------- Toast (Undo) ----------
    var toastEl = byId("nt-toast"), toastTimer = null;
    function toast(text, actionLabel, action) {
        if (!toastEl) return;
        clearTimeout(toastTimer);
        toastEl.querySelector(".nt-toast__text").textContent = text;
        var button = toastEl.querySelector(".nt-toast__action");
        button.hidden = !actionLabel;
        button.textContent = actionLabel || "";
        button.onclick = function () { hideToast(); if (action) action(); };
        toastEl.hidden = false;
        toastTimer = setTimeout(hideToast, UNDO_MS);
    }
    function hideToast() { if (toastEl) toastEl.hidden = true; clearTimeout(toastTimer); }
    function undoDelete(url, title) {
        postJson(url, {}).then(function (res) {
            if (res.ok && res.data.url) window.location.href = res.data.url;
            else toast("Couldn't restore “" + title + "” - it's in Recently deleted.");
        }).catch(function () { toast("Couldn't restore - it's in Recently deleted."); });
    }
    if (workspace.dataset.undoUrl) {               // just deleted the open note: the list page offers Undo
        var undoUrl = workspace.dataset.undoUrl, undoTitle = workspace.dataset.undoTitle;
        var clean = new URL(window.location.href);
        clean.searchParams.delete("undo");
        history.replaceState(null, "", clean.pathname + clean.search + clean.hash);
        toast("Deleted “" + undoTitle + "”", "Undo", function () { undoDelete(undoUrl, undoTitle); });
    }

    // ---------- The list: Pin / Delete (swipe on a phone, hover on a desktop) ----------
    var root = byId("note");

    function deleteRow(item, form) {
        var title = (item.querySelector(".nt-note__title") || {}).textContent || "Note";
        var isOpen = root && root.dataset.noteId === item.dataset.noteRow;
        item.style.height = item.offsetHeight + "px";
        postJson(form.action, {}).then(function (res) {
            if (!res.ok) { toast("Couldn't delete - try again."); resetSwipe(item); return; }
            if (isOpen) { window.location.href = (root.dataset.listUrl || "/notes/") + (root.dataset.listUrl.indexOf("?") < 0 ? "?" : "&") + "undo=" + item.dataset.noteRow; return; }
            item.classList.add("is-gone");
            item.remove();
            toast("Deleted “" + title.trim() + "”", "Undo", function () { undoDelete(res.data.restore_url, title.trim()); });
        }).catch(function () { toast("Offline - couldn't delete."); resetSwipe(item); });
    }
    function pinRow(item, form) {
        postJson(form.action, {}).then(function (res) {
            if (res.ok) window.location.reload();
            else toast("Couldn't pin - try again.");
        }).catch(function () { toast("Offline - couldn't pin."); });
    }
    var list = byId("nt-notes");
    if (list) {
        list.addEventListener("submit", function (event) {
            var button = event.submitter || event.target.querySelector("[data-act]");
            var act = button && button.dataset.act, item = event.target.closest(".nt-item");
            if (!act || !item) return;
            event.preventDefault();
            if (act === "delete") deleteRow(item, event.target);
            else if (act === "pin") pinRow(item, event.target);
        });
    }

    // Swipe: drag a row left to show its actions (stops at their width); let go past most of the row to delete.
    function resetSwipe(item) {
        var note = item.querySelector(".nt-note");
        if (note) note.style.transform = "";
        item.classList.remove("is-open", "is-dragging");
    }
    if (list && window.PointerEvent) {
        var drag = null;
        list.addEventListener("pointerdown", function (event) {
            if (event.pointerType === "mouse") return;
            var item = event.target.closest(".nt-item");
            var actions = item && item.querySelector(".nt-item__actions");
            if (!actions || event.target.closest(".nt-item__actions")) return;
            list.querySelectorAll(".nt-item.is-open").forEach(function (other) { if (other !== item) resetSwipe(other); });
            drag = {item: item, note: item.querySelector(".nt-note"), x: event.clientX, y: event.clientY, dx: 0,
                    base: item.classList.contains("is-open") ? -actions.offsetWidth : 0, width: actions.offsetWidth, moving: false};
        });
        list.addEventListener("pointermove", function (event) {
            if (!drag) return;
            var dx = event.clientX - drag.x, dy = event.clientY - drag.y;
            if (!drag.moving) {
                if (Math.abs(dy) > 10 && Math.abs(dy) > Math.abs(dx)) { drag = null; return; }   // scrolling
                if (Math.abs(dx) < 8) return;
                drag.moving = true;
                drag.item.classList.add("is-dragging");
            }
            drag.dx = Math.min(0, drag.base + dx);
            drag.note.style.transform = "translateX(" + drag.dx + "px)";
        });
        var endDrag = function () {
            if (!drag) return;
            var d = drag;
            drag = null;
            d.item.classList.remove("is-dragging");
            if (!d.moving) return;
            var full = d.item.offsetWidth * 0.6;
            if (-d.dx > full) {                                           // a full swipe deletes
                d.note.style.transform = "translateX(-100%)";
                var form = d.item.querySelector(".nt-act--delete").form;
                deleteRow(d.item, form);
            } else if (-d.dx > d.width / 2) {
                d.note.style.transform = "translateX(" + (-d.width) + "px)";
                d.item.classList.add("is-open");
            } else resetSwipe(d.item);
        };
        list.addEventListener("pointerup", endDrag);
        list.addEventListener("pointercancel", endDrag);
        // A swiped row: a tap on the note closes it instead of opening the note.
        list.addEventListener("click", function (event) {
            var item = event.target.closest(".nt-item.is-open");
            if (item && !event.target.closest(".nt-item__actions")) { event.preventDefault(); resetSwipe(item); }
        }, true);
        // ...and a tap anywhere outside the open row, or any scroll, closes it too.
        var closeOpen = function (except) {
            list.querySelectorAll(".nt-item.is-open").forEach(function (item) { if (item !== except) resetSwipe(item); });
        };
        document.addEventListener("pointerdown", function (event) {
            var target = event.target.closest ? event.target : null;
            closeOpen(target && target.closest(".nt-item.is-open"));
        }, true);
        var onScroll = function () { if (!drag || !drag.moving) closeOpen(null); };
        window.addEventListener("scroll", onScroll, {passive: true});
        list.addEventListener("scroll", onScroll, {passive: true});
    }

    if (!root) return;
    var canEdit = root.dataset.canEdit === "1";
    var version = parseInt(root.dataset.version, 10) || 1;
    var doc = byId("nt-doc");
    var preview = byId("nt-preview");
    var saved = byId("nt-saved");
    var words = byId("nt-words");
    var updatedEl = byId("nt-updated");
    var fields = {title: byId("nt-title"), tags: byId("nt-tags"), sort_checked: byId("nt-sort-checked")};
    var coarse = window.matchMedia("(pointer: coarse)").matches;
    var ne = null, editor = null;           // the NoteEditor wrapper and its TipTap editor


    // ---------- Panels: Link record, Share, More ----------
    root.addEventListener("click", function (event) {
        var button = event.target.closest("[data-pop]");
        if (!button) return;
        var pop = byId(button.dataset.pop), open = pop.hidden;
        root.querySelectorAll(".nt-pop").forEach(function (p) { p.hidden = true; });
        root.querySelectorAll("[data-pop]").forEach(function (b) { b.setAttribute("aria-expanded", "false"); });
        pop.hidden = !open;
        button.setAttribute("aria-expanded", open ? "true" : "false");
        if (open && pop === more) placeMore(button);
        if (open) {
            var first = pop.querySelector("input:not([type=hidden]):not([type=checkbox]), select, button");
            if (first) first.focus();
        }
    });
    if (location.hash === "#links" && byId("nt-pop-link")) byId("nt-pop-link").hidden = false;

    // More (⋯) is a menu: placed under its button, closed by a click outside, Esc, or picking Ask AI.
    var more = byId("nt-pop-more"), moreBtn = root.querySelector('[data-pop="nt-pop-more"]');
    function placeMore(button) {
        if (!more || !button) return;
        var r = button.getBoundingClientRect();
        more.style.top = Math.round(r.bottom + 6) + "px";
        more.style.right = Math.max(12, Math.round(window.innerWidth - r.right)) + "px";
    }
    function closeMore() {
        if (!more || more.hidden) return;
        more.hidden = true;
        if (moreBtn) moreBtn.setAttribute("aria-expanded", "false");
    }
    document.addEventListener("click", function (event) {
        if (!more || more.hidden) return;
        if (event.target.closest("[data-assistant-open]")) { closeMore(); return; }
        if (!more.contains(event.target) && !event.target.closest('[data-pop="nt-pop-more"]')) closeMore();
    });
    document.addEventListener("keydown", function (event) {
        if (event.key === "Escape" && more && !more.hidden) { closeMore(); if (moreBtn) moreBtn.focus(); }
    });
    ["scroll", "resize"].forEach(function (type) {
        window.addEventListener(type, function () { if (more && !more.hidden) placeMore(moreBtn); }, {passive: true});
    });

    // The note's own Delete (More panel): to Recently deleted, then the list with Undo.
    var deleteForm = byId("nt-delete-form");
    if (deleteForm) deleteForm.addEventListener("submit", function (event) {
        event.preventDefault();
        flush();
        postJson(deleteForm.action, {}).then(function (res) {
            if (!res.ok) { showSaved("Couldn't delete", true); return; }
            var listUrl = root.dataset.listUrl || "/notes/";
            window.location.href = listUrl + (listUrl.indexOf("?") < 0 ? "?" : "&") + "undo=" + root.dataset.noteId;
        });
    });

    // ---------- Word count, "Edited 2 min ago" ----------
    // A word: anything between spaces with a letter or digit in it (views.word_count counts the same way).
    function countWords(text) {
        return (text || "").split(/\s+/).filter(function (w) { return /[0-9A-Za-zÀ-￿]/.test(w); }).length;
    }
    function showWords() {
        if (!words || !ne) return;
        var n = countWords(ne.text());
        words.textContent = n + " word" + (n === 1 ? "" : "s");
    }
    var wordsTimer = null;
    function wordsSoon() { clearTimeout(wordsTimer); wordsTimer = setTimeout(showWords, 400); }

    function ago(iso) {
        var then = new Date(iso);
        if (isNaN(then)) return null;
        var s = Math.max(0, (Date.now() - then.getTime()) / 1000);
        if (s < 45) return "just now";
        if (s < 3600) return Math.round(s / 60) + " min ago";
        if (s < 86400) { var h = Math.round(s / 3600); return h + " hour" + (h === 1 ? "" : "s") + " ago"; }
        if (s < 7 * 86400) return then.toLocaleDateString(undefined, {weekday: "long"});
        return then.toLocaleDateString(undefined, {month: "short", day: "numeric", year: then.getFullYear() === new Date().getFullYear() ? undefined : "numeric"});
    }
    function showUpdated(iso) {
        if (!updatedEl) return;
        if (iso) updatedEl.setAttribute("datetime", iso);
        var text = ago(updatedEl.getAttribute("datetime"));
        if (text) updatedEl.textContent = text;
    }
    showUpdated();
    setInterval(showUpdated, 30000);

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
            if (key === "body") { if (ne) { out.body = ne.getMarkdown(); out.link_base = linkBase; } return; }
            var input = fields[key];
            if (!input) return;
            if (key === "tags") out.tags = input.value.split(",");
            else if (key === "sort_checked") out.sort_checked = !!input.checked;
            else out[key] = input.value;
        });
        return out;
    }
    function applyState(data, replaceInputs) {
        version = data.version;
        root.dataset.version = String(version);
        if (!ne && data.html != null) preview.innerHTML = data.html || '<p class="nt-muted">Nothing written yet.</p>';
        showUpdated(data.updated_iso);
        if (replaceInputs) {
            if (fields.title) fields.title.value = data.title;
            if (ne) ne.setMarkdown(data.body || "");
            if (fields.tags) fields.tags.value = (data.tags || []).join(", ");
            if (fields.sort_checked) fields.sort_checked.checked = !!data.sort_checked;
        }
        var row = byId("note-" + root.dataset.noteId);
        var title = row && row.querySelector(".nt-note__title");
        if (title && (data.title || (fields.title && fields.title.value))) {
            var text = title.lastChild;
            if (text && text.nodeType === 3) text.textContent = data.title || fields.title.value;
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
    var retryTimer = null;
    function retryLater() {                    // offline or the server failed: try again in a bit (and when back online)
        clearTimeout(retryTimer);
        retryTimer = setTimeout(function () { save(false); }, 5000);
    }
    window.addEventListener("online", function () { if (Object.keys(dirty).length) save(false); });

    function save(force) {
        var keys = Object.keys(dirty);
        if (!keys.length || inFlight || (conflict && !force)) return inFlight || Promise.resolve();
        var body = values(keys);
        body.version = version;
        if (force) body.force = true;
        dirty = {};
        showSaved("Saving…");
        var send = window.QuickSave ? QuickSave.request(root.dataset.saveUrl, {body: body, json: true})
            : postJson(root.dataset.saveUrl, body);
        inFlight = send.then(function (res) {
            inFlight = null;
            if (res.status === 409) {
                keys.forEach(function (k) { dirty[k] = true; });
                showConflict(res.data);
                return;
            }
            if (!res.ok) {
                keys.forEach(function (k) { dirty[k] = true; });
                showSaved("Couldn't save - will retry", true);
                retryLater();
                return;
            }
            applyState(res.data, false);
            if (res.data.link_state) linkBase = res.data.link_state;
            if (editor && res.data.sync && res.data.sync.length) NoteEditor.applySync(editor, res.data.sync);
            showSaved(Object.keys(dirty).length ? "Unsaved changes" : "Saved ✓");
            if (Object.keys(dirty).length) schedule();
        }).catch(function () {
            inFlight = null;
            keys.forEach(function (k) { dirty[k] = true; });
            showSaved("Offline - will save", true);
            retryLater();
        });
        return inFlight;
    }
    // Leaving (closing the tab, switching apps on a phone): send what's unsaved now, with keepalive so the request
    // outlives the page. sendBeacon can't carry the CSRF header; fetch keepalive can.
    function saveOnLeave() {
        var keys = Object.keys(dirty);
        if (!keys.length || inFlight || conflict || !window.fetch) return;
        var body = values(keys);
        body.version = version;
        var text = JSON.stringify(body);
        if (text.length > 60000) { flush(); return; }          // keepalive bodies are capped at 64 KB
        clearTimeout(timer);
        dirty = {};
        inFlight = fetch(root.dataset.saveUrl, {method: "POST", credentials: "same-origin", keepalive: true, body: text,
            headers: {"Content-Type": "application/json", "X-CSRFToken": csrfToken(), "X-Requested-With": "XMLHttpRequest"}})
            .then(function (r) {
                return r.json().catch(function () { return {}; }).then(function (data) {
                    inFlight = null;
                    if (r.ok) { applyState(data, false); showSaved("Saved ✓"); return; }
                    keys.forEach(function (k) { dirty[k] = true; });
                    if (r.status === 409) showConflict(data); else retryLater();
                });
            }).catch(function () { inFlight = null; keys.forEach(function (k) { dirty[k] = true; }); retryLater(); });
    }
    function schedule() {
        clearTimeout(timer);
        timer = setTimeout(function () { save(false); }, SAVE_DELAY);
    }
    function flush() {
        clearTimeout(timer);
        return save(false);
    }
    function changed(key) {
        dirty[key] = true;
        showSaved("Unsaved changes");
        schedule();
    }

    if (canEdit) {
        ["title", "tags"].forEach(function (key) {
            var input = fields[key];
            if (!input) return;
            input.addEventListener("input", function () { changed(key); });
            input.addEventListener("blur", flush);
        });
        if (fields.sort_checked) fields.sort_checked.addEventListener("change", function () {
            changed("sort_checked");
            if (fields.sort_checked.checked && editor) NoteEditor.sortChecked(editor);
        });
        if (fields.title) fields.title.addEventListener("keydown", function (event) {
            if (event.key === "Enter") { event.preventDefault(); if (editor) { setReading(false); editor.commands.focus("start"); } }
        });
        if (/[?&]new=1/.test(location.search) && fields.title) {
            fields.title.focus();
            fields.title.select();
        }
        window.addEventListener("pagehide", saveOnLeave);
        document.addEventListener("visibilitychange", function () { if (document.hidden) saveOnLeave(); });
        window.addEventListener("beforeunload", function (event) {
            saveOnLeave();
            if (Object.keys(dirty).length) { event.preventDefault(); event.returnValue = ""; }
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
            ["title", "body", "tags", "sort_checked"].forEach(function (k) { dirty[k] = true; });
            save(true);
        });
    }

    // ---------- Links in a note go through the site's link check (render.outbound_href does the same) ----------
    function openLink(href) {
        if (!href) return;
        var url;
        try { url = new URL(href, window.location.href); } catch (e) { return; }
        if (!/^https?:$|^mailto:$/.test(url.protocol)) return;
        var target = url.origin === window.location.origin || url.protocol === "mailto:" ? url.href
            : workspace.dataset.leaveUrl + "?to=" + encodeURIComponent(url.href);
        window.open(target, "_blank", "noopener");
    }

    // ---------- The editor ----------
    function startEditor() {
        if (!canEdit || !window.NoteEditor || !byId("nt-source")) return;   // a reader keeps the rendered note
        var mount = byId("nt-editor-mount");
        var source = JSON.parse(byId("nt-source").textContent || '""');
        var bubble = byId("nt-bubble");
        bubble.hidden = false;
        ne = NoteEditor.create({
            element: mount,
            markdown: source,
            editable: !coarse,
            bubble: bubble,
            isCoarse: function () { return coarse; },
            placeholder: "Start writing… type / for blocks",
            onReadOnlyChecked: function () { return true; },     // ticking works in the phone's reading view too
            onFiles: function (files, pos) { attachFiles(files, pos); },
            onUpdate: function (_ed, tr) {
                markActive();
                if (tr && tr.getMeta("noteSync")) return;           // from the Planner / grocery list: already saved
                changed("body"); wordsSoon(); watchLinks();
            },
            onSelection: function () { markActive(); resolveSoon(); },
            onFocus: function () { showBar(true); },
            onBlur: function (_ed, event) { onEditorBlur(event); },
            slash: {onOpen: slashOpen, onUpdate: slashUpdate, onKey: slashKey, onClose: closeBlockMenu, onImage: pickFile},
            at: smart ? {people: smart.people || [], groceries: !!smart.groceries, onOpen: atUpdate, onUpdate: atUpdate,
                         onKey: atKey, onClose: closeBlockMenu, onPick: function (range) { openChipPop(null, range); }} : null,
        });
        editor = ne.editor;
        preview.hidden = true;
        mount.hidden = false;
        if (coarse) setReading(true);
        showBar(false);
        showWords();
        markActive();

        // Ticked a checklist item: with "Move checked items to the bottom" on, it moves down.
        mount.addEventListener("change", function (event) {
            if (event.target.type === "checkbox" && fields.sort_checked && fields.sort_checked.checked) {
                setTimeout(function () { NoteEditor.sortChecked(editor); }, 0);
            }
        });
        knownLinks = linksNow();
        mount.addEventListener("mousedown", function (event) {
            var el = event.target.closest(".nt-smart");
            if (!el) return;
            event.preventDefault();
            var pos = editor.view.posAtDOM(el, 0);
            var chip = NoteEditor.findChips(editor).filter(function (c) { return c.pos === pos || c.pos === pos - 1; })[0];
            if (chip && chip.attrs.kind !== "person") openChipPop(chip);
        });
        // Links: open on Ctrl/Cmd+click while editing, or a plain tap in the reading view.
        mount.addEventListener("click", function (event) {
            var a = event.target.closest("a[href]");
            if (a && (!editor.isEditable || event.metaKey || event.ctrlKey)) {
                event.preventDefault();
                openLink(a.getAttribute("href"));
                return;
            }
            if (!editor.isEditable && !event.target.closest("input, label, button, summary, .nt-callout__icon, .nt-smart")) {
                editAt(event.clientX, event.clientY);
            }
        });
        // Keyboard: Ctrl/Cmd+K for a link.
        mount.addEventListener("keydown", function (event) {
            if ((event.ctrlKey || event.metaKey) && !event.shiftKey && (event.key || "").toLowerCase() === "k") {
                event.preventDefault();
                openLinkBox();
            }
            if (event.key === "Escape" && blockMenu && !blockMenu.hidden && !slashState) closeBlockMenu();
        });
        // Dropping files on the note: a dashed outline while over it.
        doc.addEventListener("dragover", function (event) {
            var types = event.dataTransfer && event.dataTransfer.types;
            if (types && Array.prototype.indexOf.call(types, "Files") !== -1) doc.classList.add("is-dropping");
        });
        doc.addEventListener("dragleave", function (event) { if (!doc.contains(event.relatedTarget)) doc.classList.remove("is-dropping"); });
        doc.addEventListener("drop", function () { doc.classList.remove("is-dropping"); });
    }

    // The phone's reading view: the editor isn't editable (no keyboard while reading or scrolling) until you tap.
    function setReading(on) {
        if (!editor) return;
        editor.setEditable(!on, false);
        doc.classList.toggle("is-reading", on);
    }
    function editAt(x, y) {
        setReading(false);
        var at = editor.view.posAtCoords({left: x, top: y});
        editor.chain().focus().setTextSelection(at ? at.pos : editor.state.doc.content.size).run();
    }
    function onEditorBlur(event) {
        var to = event && event.relatedTarget;
        if (to && to.closest && to.closest("#nt-mbar, #nt-bubble, #nt-blockmenu, #nt-linkbox, .nt-toolbar")) return;
        setTimeout(function () {
            if (editor.isFocused || (document.activeElement && document.activeElement.closest &&
                document.activeElement.closest("#nt-linkbox"))) return;
            showBar(false);
            resolveChips(true);
            flush();
            if (coarse) setReading(true);
        }, 150);
    }

    // ---------- Formatting: toolbar, selection toolbar, phone bar ----------
    var LIST_BLOCKS = {check: "taskList", bullet: "bulletList", number: "orderedList"};
    function format(kind, anchor) {
        if (!editor) return;
        if (!editor.isEditable) setReading(false);
        var chain = editor.chain().focus();
        switch (kind) {
            case "bold": chain.toggleBold().run(); break;
            case "italic": chain.toggleItalic().run(); break;
            case "underline": chain.toggleUnderline().run(); break;
            case "strike": chain.toggleStrike().run(); break;
            case "highlight": chain.toggleHighlight().run(); break;
            case "code": chain.toggleCode().run(); break;
            case "undo": chain.undo().run(); break;
            case "redo": chain.redo().run(); break;
            case "link": openLinkBox(); break;
            case "image": pickFile(); break;
            case "styles": openStyles(anchor); break;
            default: NoteEditor.runBlock(editor, kind);
        }
        markActive();
    }
    function markActive() {
        if (!editor) return;
        root.querySelectorAll("[data-fmt]").forEach(function (button) {
            var kind = button.dataset.fmt, on = null;
            if (LIST_BLOCKS[kind]) on = editor.isActive(LIST_BLOCKS[kind]);
            else if (/^(bold|italic|underline|strike|highlight|code|link)$/.test(kind)) on = editor.isActive(kind);
            if (on !== null) button.setAttribute("aria-pressed", on ? "true" : "false");
        });
    }
    root.addEventListener("mousedown", function (event) {          // keep the editor's selection
        if (event.target.closest("[data-fmt]")) event.preventDefault();
    });
    root.addEventListener("click", function (event) {
        var button = event.target.closest("[data-fmt]");
        if (button) format(button.dataset.fmt, button);
    });

    // The phone bar above the keyboard: visualViewport says how much of the screen the keyboard covers.
    var bar = byId("nt-mbar");
    function placeBar() {
        if (!bar || bar.hidden || !window.visualViewport) return;
        var vv = window.visualViewport;
        var covered = Math.max(0, window.innerHeight - vv.height - vv.offsetTop);
        bar.style.transform = "translateY(" + (-covered) + "px)";
    }
    function showBar(on) {                         // on a phone it stays: at the bottom, or above the keyboard
        if (!bar) return;
        bar.hidden = !coarse;
        bar.classList.toggle("is-typing", !!on);
        placeBar();
    }
    if (window.visualViewport) {
        window.visualViewport.addEventListener("resize", placeBar);
        window.visualViewport.addEventListener("scroll", placeBar);
    }

    // ---------- Block menu: "/" while typing, and the Aa button ----------
    var blockMenu = byId("nt-blockmenu"), slashState = null, styleAnchor = null;
    function placeMenu(menu, rect) {
        if (!rect) return;
        menu.hidden = false;
        var width = menu.offsetWidth, height = menu.offsetHeight;
        var left = Math.min(Math.max(8, rect.left), window.innerWidth - width - 8);
        var top = rect.bottom + 6;
        if (top + height > window.innerHeight - 8) top = Math.max(8, rect.top - height - 6);
        menu.style.left = left + "px";
        menu.style.top = top + "px";
    }
    function drawMenu(items, active, pick) {
        blockMenu.textContent = "";
        if (!items.length) { blockMenu.appendChild(el("p", "nt-menu__empty", "No matching blocks")); return; }
        items.forEach(function (item, i) {
            var row = el("button", "nt-menu__item" + (i === active ? " is-active" : ""));
            row.type = "button";
            row.setAttribute("role", "option");
            row.setAttribute("aria-selected", i === active ? "true" : "false");
            row.appendChild(el("span", "nt-menu__icon", item.icon));
            var text = el("span", "nt-menu__text");
            text.appendChild(el("span", "", item.label));
            text.appendChild(el("small", "", item.hint));
            row.appendChild(text);
            row.addEventListener("mousedown", function (event) { event.preventDefault(); });
            row.addEventListener("click", function () { pick(item); });
            blockMenu.appendChild(row);
        });
        var current = blockMenu.querySelector(".is-active");
        if (current) current.scrollIntoView({block: "nearest"});
    }
    function slashOpen(props) {
        slashState = {props: props, active: 0};
        slashUpdate(props);
    }
    function slashUpdate(props) {
        if (!slashState) slashState = {active: 0};
        slashState.props = props;
        slashState.active = Math.min(slashState.active, Math.max(0, props.items.length - 1));
        drawMenu(props.items, slashState.active, function (item) { props.command(item); });
        placeMenu(blockMenu, props.clientRect && props.clientRect());
    }
    function slashKey(props) {
        if (!slashState) return false;
        var items = slashState.props.items, key = props.event.key;
        if (key === "ArrowDown" || key === "ArrowUp") {
            if (!items.length) return true;
            slashState.active = (slashState.active + (key === "ArrowDown" ? 1 : items.length - 1)) % items.length;
            slashUpdate(slashState.props);
            return true;
        }
        if (key === "Enter" || key === "Tab") {
            if (items[slashState.active]) slashState.props.command(items[slashState.active]);
            return true;
        }
        if (key === "Escape") { closeBlockMenu(); return true; }
        return false;
    }
    function closeBlockMenu() {
        slashState = null;
        atState = null;
        styleAnchor = null;
        if (blockMenu) blockMenu.hidden = true;
    }
    function openStyles(anchor) {
        if (!editor) return;
        if (styleAnchor === anchor && !blockMenu.hidden) { closeBlockMenu(); return; }
        styleAnchor = anchor;
        var items = NoteEditor.BLOCKS;
        drawMenu(items, -1, function (item) {
            closeBlockMenu();
            if (item.id === "image") pickFile();
            else NoteEditor.runBlock(editor, item.id);
            markActive();
        });
        var rect = anchor ? anchor.getBoundingClientRect() : null;
        if (anchor && anchor.closest("#nt-mbar")) {         // above the phone bar
            blockMenu.hidden = false;
            rect = {left: rect.left, top: rect.top, bottom: rect.top - blockMenu.offsetHeight - 12};
        }
        placeMenu(blockMenu, rect);
    }
    document.addEventListener("mousedown", function (event) {
        if (blockMenu && !blockMenu.hidden && !slashState && !blockMenu.contains(event.target) && !event.target.closest("[data-fmt=styles]")) closeBlockMenu();
        if (linkBox && !linkBox.hidden && !linkBox.contains(event.target) && !event.target.closest("[data-fmt=link]")) linkBox.hidden = true;
    });

    // ---------- Link box ----------
    var linkBox = byId("nt-linkbox"), linkInput = byId("nt-linkbox-url");
    function openLinkBox() {
        if (!editor || !linkBox) return;
        if (!editor.isEditable) setReading(false);
        var sel = editor.state.selection;
        var coords = editor.view.coordsAtPos(sel.from);
        linkInput.value = editor.getAttributes("link").href || "";
        linkBox.hidden = false;
        var left = Math.min(Math.max(8, coords.left), window.innerWidth - linkBox.offsetWidth - 8);
        var top = coords.bottom + 8;
        if (top + linkBox.offsetHeight > window.innerHeight - 8) top = Math.max(8, coords.top - linkBox.offsetHeight - 8);
        linkBox.style.left = left + "px";
        linkBox.style.top = top + "px";
        linkInput.focus();
        linkInput.select();
    }
    function saveLink() {
        var href = linkInput.value.trim();
        linkBox.hidden = true;
        var chain = editor.chain().focus().extendMarkRange("link");
        if (!href) { chain.unsetLink().run(); return; }
        if (!/^(https?:|mailto:|\/)/i.test(href)) href = "https://" + href;
        if (editor.state.selection.empty && !editor.isActive("link")) {
            chain.insertContent({type: "text", text: href, marks: [{type: "link", attrs: {href: href}}]}).run();
        } else chain.setLink({href: href}).run();
    }
    if (linkBox) {
        linkBox.querySelector("[data-link-save]").addEventListener("click", saveLink);
        linkBox.querySelector("[data-link-remove]").addEventListener("click", function () {
            linkBox.hidden = true;
            editor.chain().focus().extendMarkRange("link").unsetLink().run();
        });
        linkInput.addEventListener("keydown", function (event) {
            if (event.key === "Enter") { event.preventDefault(); saveLink(); }
            if (event.key === "Escape") { event.preventDefault(); linkBox.hidden = true; editor.commands.focus(); }
        });
    }

    // ---------- Smart links: "@" dates, @grocery, mentions (albright_notes/smart.py) ----------
    var smart = byId("nt-smart") ? JSON.parse(byId("nt-smart").textContent || "null") : null;
    var linkBase = (smart && smart.link_state) || {};       // the link states the server last gave us (sent back on save)
    var atState = null;
    function atUpdate(props) {
        if (!props.items.length) { atState = null; if (blockMenu) blockMenu.hidden = true; return; }
        if (!atState) atState = {active: 0};
        atState.props = props;
        atState.active = Math.min(atState.active, props.items.length - 1);
        drawMenu(props.items, atState.active, function (item) { props.command(item); });
        placeMenu(blockMenu, props.clientRect && props.clientRect());
    }
    function atKey(props) {
        if (!atState || blockMenu.hidden) return false;
        var items = atState.props.items, key = props.event.key;
        if (key === "ArrowDown" || key === "ArrowUp") {
            atState.active = (atState.active + (key === "ArrowDown" ? 1 : items.length - 1)) % items.length;
            atUpdate(atState.props);
            return true;
        }
        if (key === "Enter" || key === "Tab") { atState.props.command(items[atState.active]); return true; }
        if (key === "Escape") { atState = null; blockMenu.hidden = true; return true; }
        return false;
    }

    function api(url, body) {
        return postJson(url, body).then(function (res) {
            if (!res.ok) throw new Error((res.data && res.data.error) || "failed");
            return res.data;
        });
    }
    function chipBy(uid) {
        return NoteEditor.findChips(editor).filter(function (c) { return c.uid === uid; })[0] || null;
    }

    // A chip just made is sent when you leave its line (so "@grocery milk, eggs" is finished first): the grocery
    // tag puts the line's items on your grocery list; a date becomes a Planner task when that setting is on.
    var resolveTimer = null, skipPrompt = {};
    function resolveSoon() { clearTimeout(resolveTimer); resolveTimer = setTimeout(function () { resolveChips(false); }, 120); }
    function resolveChips(force) {
        if (!editor || !smart) return;
        var sel = editor.state.selection.from;
        NoteEditor.findChips(editor).forEach(function (c) {
            if (!c.attrs.fresh || (!force && sel >= c.block && sel <= c.blockEnd)) return;
            if (c.attrs.kind === "grocery" && !c.attrs.ids) {
                var items = c.text.split(",").map(function (t) { return t.trim(); }).filter(Boolean);
                if (!items.length) return;                                // nothing on the line yet: wait
                NoteEditor.updateChip(editor, c.uid, {fresh: false}, true);
                api(root.dataset.groceriesUrl, {items: items}).then(function (data) {
                    NoteEditor.updateChip(editor, c.uid, {ids: data.ids.join(","), already: data.already});
                    if (data.added.length) {
                        var n = data.added.length;
                        toast("Added " + n + " item" + (n === 1 ? "" : "s") + " to groceries", "Undo", function () {
                            api(root.dataset.groceriesRemoveUrl, {ids: data.added}).then(function () {
                                var now = chipBy(c.uid);
                                if (now) { skipPrompt["grocery:" + now.attrs.ids] = true; NoteEditor.removeChip(editor, c.uid); }
                            });
                        });
                    } else toast("Already on your grocery list");
                }).catch(function (err) {
                    NoteEditor.updateChip(editor, c.uid, {fresh: true}, true);
                    toast(err.message === "failed" ? "Couldn't add to groceries" : err.message);
                });
            } else if (c.attrs.kind === "date") {
                NoteEditor.updateChip(editor, c.uid, {fresh: false}, true);
                if (smart.auto_planner && smart.planner && !c.attrs.task) addToPlanner(c);
            } else NoteEditor.updateChip(editor, c.uid, {fresh: false}, true);
        });
    }
    function addToPlanner(c) {
        return api(root.dataset.plannerUrl, {date: c.attrs.date, time: c.attrs.time, text: c.text})
            .then(function (data) {
                NoteEditor.updateChip(editor, c.uid, {task: data.task_id});
                toast("Added to the Planner");
            })
            .catch(function (err) { toast(err.message === "failed" ? "Couldn't add to the Planner" : err.message); });
    }

    // Taking out a dated line (or its date) that's in the Planner, or a grocery tag: ask about the other side.
    var knownLinks = {}, linkTimer = null;
    function linksNow() {
        var out = {};
        NoteEditor.findChips(editor).forEach(function (c) { if (c.key) out[c.key] = {text: c.text, attrs: c.attrs}; });
        return out;
    }
    function watchLinks() {
        clearTimeout(linkTimer);
        linkTimer = setTimeout(function () {
            var now = linksNow();
            Object.keys(knownLinks).forEach(function (key) {
                if (now[key] || skipPrompt[key]) { delete skipPrompt[key]; return; }
                var gone = knownLinks[key], what = gone.text ? "“" + gone.text.slice(0, 40) + "”" : "that item";
                if (key.indexOf("task:") === 0) {
                    toast("Removed " + what + " - delete its Planner task too?", "Delete task", function () {
                        api(root.dataset.plannerDeleteUrl.replace("/0/", "/" + gone.attrs.task + "/"), {})
                            .then(function () { toast("Planner task deleted"); })
                            .catch(function () { toast("Couldn't delete the Planner task"); });
                    });
                } else {
                    var ids = gone.attrs.ids.split(",").map(Number);
                    toast("Removed " + what + " - take it off the grocery list?", "Remove", function () {
                        api(root.dataset.groceriesRemoveUrl, {ids: ids}).then(function () { toast("Removed from groceries"); });
                    });
                }
            });
            knownLinks = now;
        }, 700);
    }

    // Tapping a chip: a date's popover (change it, Add to Planner, remove) or a grocery tag's (open the list, remove).
    var chipPop = byId("nt-chippop"), chipUid = null, chipRange = null;
    function placePop(rect) {
        chipPop.hidden = false;
        var left = Math.min(Math.max(8, rect.left), window.innerWidth - chipPop.offsetWidth - 8);
        var top = rect.bottom + 8;
        if (top + chipPop.offsetHeight > window.innerHeight - 8) top = Math.max(8, rect.top - chipPop.offsetHeight - 8);
        chipPop.style.left = left + "px";
        chipPop.style.top = top + "px";
    }
    function openChipPop(chip, range) {
        if (!chipPop) return;
        chipUid = chip ? chip.uid : null;
        chipRange = range || null;
        var kind = chip ? chip.attrs.kind : "date";
        chipPop.querySelector('[data-chippop="date"]').hidden = kind !== "date";
        chipPop.querySelector('[data-chippop="grocery"]').hidden = kind !== "grocery";
        var rect;
        if (chip) {
            var dom = editor.view.nodeDOM(chip.pos);
            rect = dom && dom.getBoundingClientRect ? dom.getBoundingClientRect() : editor.view.coordsAtPos(chip.pos);
        } else rect = editor.view.coordsAtPos(range ? range.from : editor.state.selection.from);
        if (kind === "date") {
            var dateIn = chipPop.querySelector("[data-chip-date]"), timeIn = chipPop.querySelector("[data-chip-time]");
            dateIn.value = chip ? chip.attrs.date || "" : (smart && smart.today) || "";
            timeIn.value = chip ? chip.attrs.time || "" : "";
            var plan = chipPop.querySelector("[data-chip-planner]"), open = chipPop.querySelector("[data-chip-open-task]");
            if (plan) plan.hidden = !chip || !!chip.attrs.task;
            if (open) {
                open.hidden = !chip || !chip.attrs.task;
                if (chip && chip.attrs.task) open.href = root.dataset.plannerItemUrl.replace("/0/", "/" + chip.attrs.task + "/") + "?date=" + chip.attrs.date;
            }
            chipPop.querySelector("[data-chip-remove]").hidden = !chip;
            placePop(rect);
            dateIn.focus();
        } else {
            chipPop.querySelector("[data-chip-grocery-text]").textContent = chip.attrs.ids
                ? (chip.attrs.already ? "Already on your grocery list: " : "On your grocery list: ") + (chip.text || "")
                : "Not sent yet - it goes on the grocery list when you leave this line.";
            chipPop.querySelector("[data-chip-grocery-remove]").hidden = !chip.attrs.ids;
            placePop(rect);
        }
    }
    function closeChipPop() { if (chipPop) chipPop.hidden = true; chipUid = null; chipRange = null; }
    if (chipPop) {
        chipPop.querySelector("[data-chip-save]").addEventListener("click", function () {
            var date = chipPop.querySelector("[data-chip-date]").value, time = chipPop.querySelector("[data-chip-time]").value || null;
            if (!date) return;
            if (chipUid) NoteEditor.updateChip(editor, chipUid, {date: date, time: time});
            else NoteEditor.insertChip(editor, {kind: "date", date: date, time: time}, chipRange);
            closeChipPop();
            editor.commands.focus();
        });
        var planBtn = chipPop.querySelector("[data-chip-planner]");
        if (planBtn) planBtn.addEventListener("click", function () {
            var c = chipBy(chipUid);
            closeChipPop();
            if (c) addToPlanner(c);
        });
        chipPop.querySelector("[data-chip-remove]").addEventListener("click", function () {
            var uid = chipUid;
            closeChipPop();
            NoteEditor.removeChip(editor, uid);                  // a linked one asks about its task (watchLinks)
        });
        chipPop.querySelector("[data-chip-grocery-remove]").addEventListener("click", function () {
            var c = chipBy(chipUid);
            closeChipPop();
            if (!c || !c.attrs.ids) return;
            api(root.dataset.groceriesRemoveUrl, {ids: c.attrs.ids.split(",").map(Number)}).then(function () {
                skipPrompt["grocery:" + c.attrs.ids] = true;
                NoteEditor.removeChip(editor, c.uid);
                toast("Removed from groceries");
            });
        });
        document.addEventListener("mousedown", function (event) {
            if (!chipPop.hidden && !chipPop.contains(event.target) && !event.target.closest(".nt-smart")) closeChipPop();
        });
        chipPop.addEventListener("keydown", function (event) {
            if (event.key === "Escape") { closeChipPop(); editor.commands.focus(); }
            if (event.key === "Enter" && event.target.matches("input")) { event.preventDefault(); chipPop.querySelector("[data-chip-save]").click(); }
        });
    }

    // Back to the tab: anything checked off or moved in the Planner / grocery list meanwhile shows here (only where
    // you haven't changed it yourself since).
    function refreshLinks() {
        if (!editor || !smart || !root.dataset.stateUrl) return;
        fetch(root.dataset.stateUrl, {credentials: "same-origin", headers: {"X-Requested-With": "XMLHttpRequest"}})
            .then(function (r) { return r.json(); })
            .then(function (data) {
                var theirs = data.link_state || {}, sync = [];
                NoteEditor.findChips(editor).forEach(function (c) {
                    var base = linkBase[c.key], server = c.key && theirs[c.key];
                    if (!base || !server) return;
                    var mine = {checked: c.checked, date: c.attrs.date, time: c.attrs.time};
                    if (mine.checked !== base.checked || mine.date !== base.date || mine.time !== base.time) return;
                    if (server.checked !== mine.checked || server.date !== mine.date || server.time !== mine.time) {
                        var item = {key: c.key};
                        if (server.checked !== mine.checked && server.checked !== null) item.checked = server.checked;
                        if (server.date !== mine.date || server.time !== mine.time) { item.date = server.date; item.time = server.time; }
                        sync.push(item);
                    }
                    linkBase[c.key] = server;
                });
                NoteEditor.applySync(editor, sync);
            }).catch(function () {});
    }
    document.addEventListener("visibilitychange", function () { if (!document.hidden) refreshLinks(); });

    var autoPlanner = byId("nt-auto-planner");
    if (autoPlanner) autoPlanner.addEventListener("change", function () {
        var on = autoPlanner.checked;
        api(root.dataset.settingsUrl, {auto_planner: on}).then(function () {
            smart.auto_planner = on;
            showSaved(on ? "Dated items will go to the Planner" : "Saved ✓");
        }).catch(function () { autoPlanner.checked = !on; showSaved("Couldn't save that setting", true); });
    });

    // ---------- Images and files (albright_labs/static/labs/attachments.js does the upload) ----------
    var attSection = root.querySelector("[data-att]");
    var fileInput = byId("nt-image-input"), pickPos = null;
    function pickFile() {
        if (!fileInput) return;
        pickPos = editor ? editor.state.selection.from : null;
        fileInput.click();
    }
    if (fileInput) fileInput.addEventListener("change", function () {
        attachFiles(fileInput.files, pickPos);
        fileInput.value = "";
    });
    function insertAttachment(row, pos) {
        if (!editor || !row) return;
        var size = editor.state.doc.content.size;
        var at = pos == null || pos > size ? size : pos;
        var node = row.is_image ? {type: "image", attrs: {src: row.url, alt: row.name}}
            : {type: "paragraph", content: [{type: "text", text: "📎 " + row.name, marks: [{type: "link", attrs: {href: row.url}}]}]};
        editor.chain().focus().insertContentAt(at, node).run();
    }
    function attachFiles(files, pos) {
        if (!attSection || !window.Attachments) { showSaved("Can't attach files here", true); return; }
        Array.prototype.slice.call(files || []).forEach(function (file) {
            var isImage = /^image\//.test(file.type) || /\.(heic|heif)$/i.test(file.name);
            showSaved("Uploading " + (isImage ? "image" : "file") + "…");
            window.Attachments.upload(attSection, file, function (row) {
                showSaved(row ? "Saved ✓" : "Couldn't upload " + file.name, !row);
                if (row) insertAttachment(row, pos);
            });
        });
    }

    // ---------- Link record: search the apps you can open ----------
    var picker = byId("nt-picker");
    if (picker) {
        var input = byId("nt-picker-q"), results = byId("nt-picker-results"), searchTimer = null, lastQuery = "";
        var renderResults = function (found) {
            results.textContent = "";
            if (!found.length) results.appendChild(el("li", "nt-empty", "Nothing found in the apps you can open."));
            found.forEach(function (r) {
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
                results.appendChild(li);
            });
            results.hidden = false;
        };
        input.addEventListener("input", function () {
            clearTimeout(searchTimer);
            var q = input.value.trim();
            if (q.length < 2) { results.hidden = true; return; }
            searchTimer = setTimeout(function () {
                lastQuery = q;
                fetch(workspace.dataset.searchUrl + "?q=" + encodeURIComponent(q), {credentials: "same-origin",
                    headers: {"X-Requested-With": "XMLHttpRequest"}})
                    .then(function (r) { return r.json(); })
                    .then(function (data) { if (q === lastQuery) renderResults(data.results || []); })
                    .catch(function () { results.hidden = true; });
            }, 250);
        });
        input.addEventListener("keydown", function (event) {
            if (event.key === "Escape") results.hidden = true;
            if (event.key === "Enter") event.preventDefault();
        });
    }

    // The editor bundle is a deferred script: it has run by DOMContentLoaded.
    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", startEditor);
    else startEditor();
})();

/* Home's "New note" tile links here with ?start=new (albright_labs.home_tiles): press "+ New note" for them, once
   (the parameter is dropped first, so Back or a reload doesn't make another). */
(function () {
    "use strict";
    var url = new URL(window.location.href);
    if (url.searchParams.get("start") !== "new") return;
    url.searchParams.delete("start");
    history.replaceState(null, "", url.pathname + url.search + url.hash);
    var button = document.getElementById("nt-new");
    if (button) button.click();
})();
