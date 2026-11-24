/*
 * Home (albright_labs/hub.html): "My tiles" (albright_labs.home_tiles). Vanilla JS; the page works without it
 * (every button submits, "+ Add" opens the sheet as a section).
 *  - A tap or click opens the tile. Drag to reorder - any time, no Edit mode needed (pointer events only, one
 *    path for mouse, pen and touch): a mouse drags after moving 6px; a finger holds ~300ms (a small buzz where the
 *    phone can), so a quick tap opens and a swipe scrolls. The tile lifts and follows, the others slide aside, the
 *    page auto-scrolls near the edges, and the drop saves the order (labs:save_tile_order) - on failure the tiles
 *    go back and a short error shows. The click that ends a drag never opens the tile.
 *  - "+ Add" / the Add tile open the Add sheet (a <dialog>): search (label + description + app, every word
 *    anywhere, with the app chip), and a + per page that adds it at once (labs:toggle_tile) - it turns into a
 *    check; tap again to remove. New tiles go to the end. Closing the sheet clears the search.
 *  - A quick action tile (data-quick-url) opens its popup in #qa-dialog instead (see "Quick actions" below).
 *  - Edit mode (the Edit button, which then reads Done; or ?edit=1): tiles wiggle; each gets a remove (x) and
 *    move left/right buttons (keyboard users); dragging works the same. Done saves the settings too
 *    (labs:save_picker), without a reload.
 */
(function () {
    "use strict";

    var root = document.getElementById("labs-picker");
    if (!root) return;
    var form = document.getElementById("picker-form");
    var grid = document.getElementById("ht-grid");
    var note = document.getElementById("picker-note");
    var errorBox = document.getElementById("ht-error");
    var editingInput = document.getElementById("picker-editing");
    var sheet = document.getElementById("tile-sheet");
    var sheetForm = document.getElementById("ht-sheet-form");
    var orderUrl = (root.querySelector("[data-order-url]") || {dataset: {}}).dataset.orderUrl;
    var csrf = form.querySelector("[name=csrfmiddlewaretoken]").value;
    var reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

    // "Show completed tasks" only while "Show Today panel" is ticked.
    var showToday = document.getElementById("show-today"), todaySub = document.getElementById("show-completed-row");
    if (showToday && todaySub) showToday.addEventListener("change", function () { todaySub.hidden = !showToday.checked; });

    function say(text) { if (note) note.textContent = text; }
    var errorTimer = null;
    function showError(text) {
        if (!errorBox) { say(text); return; }
        errorBox.textContent = text;
        errorBox.hidden = false;
        clearTimeout(errorTimer);
        errorTimer = setTimeout(function () { errorBox.hidden = true; }, 5000);
    }
    function editing() { return root.classList.contains("is-editing"); }
    function tiles() { return Array.prototype.slice.call(grid.querySelectorAll(".ht:not([data-fixed])")); }
    function keys() { return tiles().map(function (li) { return li.dataset.key; }); }
    function addTile() { return grid.querySelector(".ht--add"); }

    function post(url, body) {
        body.append("csrfmiddlewaretoken", csrf);
        return fetch(url, {method: "POST", body: body, credentials: "same-origin",
                           headers: {"X-Requested-With": "XMLHttpRequest"}})
            .then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); });
    }

    // Order: save it; on failure put the tiles back as they were. ---------------------------------------------
    function restore(order) {
        var byKey = {};
        tiles().forEach(function (li) { byKey[li.dataset.key] = li; });
        order.forEach(function (key) { if (byKey[key]) grid.insertBefore(byKey[key], addTile()); });
    }
    function saveOrder(before) {
        var now = keys();
        if (before && before.join("|") === now.join("|")) return;
        var body = new FormData();
        now.forEach(function (key) { body.append("tile", key); });
        post(orderUrl, body).then(function () { say("Order saved."); }).catch(function () {
            if (before) restore(before);
            showError("Couldn't save the new order - the tiles are back where they were.");
        });
    }

    // Add / remove -------------------------------------------------------------------------------------------
    var toggleUrl = sheetForm ? sheetForm.getAttribute("action") : "";

    function syncSheetRow(key, added) {
        if (!sheet) return;
        var button = sheet.querySelector('[data-tile-toggle][value="' + key + '"]');
        if (!button) return;
        var name = button.closest(".ht-row").querySelector(".ht-row__name").textContent.replace(/ \(whole app\)$/, "");
        button.classList.toggle("is-added", added);
        button.setAttribute("aria-pressed", added ? "true" : "false");
        button.setAttribute("aria-label", (added ? "Remove " : "Add ") + name);
        button.querySelector("use").setAttribute("href", added ? "#ht-check" : "#ht-plus");
    }

    function toggle(key, want) {
        var body = new FormData();
        body.append("key", key);
        body.append("want", want);
        return post(toggleUrl, body).then(function (data) {
            var existing = grid.querySelector('.ht[data-key="' + key + '"]');
            if (data.added && !existing && data.html) {
                var holder = document.createElement("ol");
                holder.innerHTML = data.html.trim();
                var added = holder.firstElementChild;
                Array.prototype.forEach.call(added.querySelectorAll("a"), function (a) { a.setAttribute("draggable", "false"); });
                grid.insertBefore(added, addTile());
            } else if (!data.added && existing) {
                existing.parentNode.removeChild(existing);
            }
            syncSheetRow(key, data.added);
            return data;
        });
    }

    // The Add sheet ------------------------------------------------------------------------------------------
    var search = document.getElementById("ht-sheet-search");
    var empty = document.getElementById("ht-rows-empty");
    var chip = "";
    function words(text) { return (text || "").toLowerCase().replace(/\s+/g, " ").trim(); }
    function filterRows() {
        if (!sheet) return;
        var terms = words(search && search.value).split(" ").filter(Boolean), shown = 0;
        sheet.querySelectorAll(".ht-row").forEach(function (row) {
            var hay = words(row.getAttribute("data-search"));
            var ok = (!chip || row.getAttribute("data-app") === chip) &&
                     terms.every(function (term) { return hay.indexOf(term) !== -1; });
            row.hidden = !ok;
            if (ok) shown += 1;
        });
        // A "Meals · Pages" / "Meals · Quick actions" heading shows while any row under it does.
        sheet.querySelectorAll("[data-sub]").forEach(function (sub) {
            var next = sub.nextElementSibling, any = false;
            while (next && !next.hasAttribute("data-sub")) { if (!next.hidden) { any = true; break; } next = next.nextElementSibling; }
            sub.hidden = !any;
        });
        if (empty) empty.hidden = shown > 0;
    }
    function openSheet() {
        if (!sheet) return;
        if (sheet.open) sheet.close();
        if (typeof sheet.showModal === "function") sheet.showModal(); else sheet.setAttribute("open", "");
        filterRows();
        if (search && window.matchMedia("(min-width: 720px)").matches) search.focus();
    }
    function closeSheet() {
        if (!sheet) return;
        if (typeof sheet.close === "function" && sheet.open) sheet.close(); else { sheet.removeAttribute("open"); onSheetClosed(); }
    }
    function onSheetClosed() {
        if (search) search.value = "";
        filterRows();
    }
    if (sheet) {
        if (sheet.hasAttribute("open")) {                 // opened as ?add=1 - make it the real modal
            sheet.removeAttribute("open");
            var clean = new URL(window.location.href);
            clean.searchParams.delete("add");
            history.replaceState(null, "", clean.pathname + clean.search);
            openSheet();
        }
        sheet.addEventListener("close", onSheetClosed);
        if (search) {
            // "input" covers typing, paste, dictation and iOS's clear button; "search"/"keyup" are belt and braces
            // for older Safari, which can skip "input" on the clear (x) button.
            ["input", "search", "keyup", "change"].forEach(function (type) { search.addEventListener(type, filterRows); });
            // Enter (iOS "Search") must not submit the sheet's form - that would add the first page.
            search.addEventListener("keydown", function (e) { if (e.key === "Enter") { e.preventDefault(); search.blur(); } });
        }
        sheet.addEventListener("click", function (e) {
            if (e.target === sheet) { closeSheet(); return; }           // the backdrop
            if (e.target.closest("[data-close-sheet]")) { e.preventDefault(); closeSheet(); return; }
            var c = e.target.closest("[data-chip]");
            if (c) {
                chip = c.getAttribute("data-chip");
                sheet.querySelectorAll("[data-chip]").forEach(function (b) {
                    b.classList.toggle("is-on", b === c);
                    b.setAttribute("aria-pressed", b === c ? "true" : "false");
                });
                filterRows();
                return;
            }
            var t = e.target.closest("[data-tile-toggle]");
            if (t) {
                e.preventDefault();
                if (t.disabled) return;
                t.disabled = true;
                toggle(t.value, t.classList.contains("is-added") ? "remove" : "add")
                    .catch(function () { showError("Couldn't save that - try again."); })
                    .then(function () { t.disabled = false; t.focus(); });
            }
        });
    }
    root.addEventListener("click", function (e) {
        var opener = e.target.closest("[data-open-sheet]");
        if (opener && sheet) { e.preventDefault(); openSheet(); }
    });

    // Edit mode: remove, move buttons ------------------------------------------------------------------------
    var justDragged = false;
    function onEditClick(e) {
        if (!editing()) return;
        var remove = e.target.closest("[data-tile-remove]");
        if (remove) {
            e.preventDefault();
            var li = remove.closest(".ht"), next = li.nextElementSibling;
            var label = li.querySelector(".ht__label").textContent;
            toggle(li.dataset.key, "remove").then(function () {
                say("Removed " + label + ".");
                var focus = next && (next.querySelector("[data-tile-remove]") || next.querySelector("a"));
                if (focus) focus.focus();
            }).catch(function () { showError("Couldn't remove that - try again."); });
            return;
        }
        var move = e.target.closest("[data-tile-move]");
        if (move) {
            e.preventDefault();
            var tile = move.closest(".ht"), before = keys();
            var dir = move.getAttribute("data-tile-move");
            animateMove(function () {
                if (dir === "up" && tile.previousElementSibling) grid.insertBefore(tile, tile.previousElementSibling);
                var after = tile.nextElementSibling;
                if (dir === "down" && after && !after.hasAttribute("data-fixed")) grid.insertBefore(after, tile);
            });
            move.focus();
            saveOrder(before);
            return;
        }
        // In Edit mode a tile is something to arrange, not a link (the Add tile still opens the sheet).
        if (e.target.closest("[data-tile-link]")) e.preventDefault();
    }

    // The other tiles slide to their new places (FLIP: measure, change the DOM, animate from the old spot).
    function animateMove(change, skip) {
        var others = tiles().filter(function (li) { return li !== skip; });
        var first = others.map(function (li) { return li.getBoundingClientRect(); });
        change();
        if (reduceMotion) return;
        others.forEach(function (li, i) {
            var last = li.getBoundingClientRect();
            var dx = first[i].left - last.left, dy = first[i].top - last.top;
            if (!dx && !dy) return;
            li.classList.remove("is-sliding");
            li.style.transform = "translate(" + dx + "px," + dy + "px)";
            li.getBoundingClientRect();                         // apply that before animating back
            li.classList.add("is-sliding");
            li.style.transform = "";
        });
    }

    // Drag to reorder ----------------------------------------------------------------------------------------
    var HOLD_MS = 300, MOUSE_SLOP = 6, TOUCH_SLOP = 10, EDGE = 60;
    var drag = null;

    function onPointerDown(e) {
        if (drag || (e.pointerType === "mouse" && e.button !== 0) || !e.isPrimary) return;
        if (e.target.closest("[data-tile-remove], [data-tile-move]")) return;        // those stay buttons
        var li = e.target.closest(".ht:not([data-fixed])");
        if (!li) return;
        drag = {li: li, id: e.pointerId, type: e.pointerType, startX: e.clientX, startY: e.clientY,
                x: e.clientX, y: e.clientY, lastY: e.clientY, active: false, before: keys(), timer: null, raf: 0};
        if (e.pointerType === "mouse") return;                 // a mouse starts on movement
        drag.timer = setTimeout(function () { if (drag && !drag.active) begin(); }, HOLD_MS);
    }

    function begin() {
        drag.active = true;
        clearTimeout(drag.timer);
        var rect = drag.li.getBoundingClientRect();
        drag.offsetX = drag.x - rect.left;                     // where the finger is on the tile
        drag.offsetY = drag.y - rect.top;
        drag.originLeft = rect.left;
        drag.originTop = rect.top;
        drag.li.classList.remove("is-sliding", "is-settling");
        try { drag.li.setPointerCapture(drag.id); } catch (err) { /* still works without capture */ }
        drag.li.classList.add("is-dragging");
        root.classList.add("is-dragging-tile");
        // A small buzz - only once the page has had a tap (before that the browser refuses and logs a warning).
        var activated = !navigator.userActivation || navigator.userActivation.hasBeenActive;
        if (navigator.vibrate && drag.type === "touch" && activated) navigator.vibrate(10);
        follow();
        drag.raf = requestAnimationFrame(autoScroll);
    }

    // The tile sits under the finger: its translate is from where its slot is now to where the finger is.
    function follow() {
        var li = drag.li;
        li.style.transform = "";
        var slot = li.getBoundingClientRect();
        var dx = drag.x - drag.offsetX - slot.left, dy = drag.y - drag.offsetY - slot.top;
        li.style.transform = "translate(" + dx + "px," + dy + "px) scale(1.08)";
    }

    function place() {
        var li = drag.li;
        li.style.visibility = "hidden";                         // see what's under the finger, not the tile
        var under = document.elementFromPoint(drag.x, drag.y);
        li.style.visibility = "";
        var over = under && under.closest(".ht");
        if (!over || over === li || over.parentNode !== grid) return;
        var target;
        if (over.hasAttribute("data-fixed")) {
            target = over;                                     // over the Add tile: go last
        } else {
            var rect = over.getBoundingClientRect();
            var after = drag.x > rect.left + rect.width / 2;
            target = after ? over.nextElementSibling : over;
        }
        if (target === li || target === li.nextElementSibling) return;
        animateMove(function () { grid.insertBefore(li, target); }, li);
    }

    function onPointerMove(e) {
        if (!drag || e.pointerId !== drag.id) return;
        var dx = e.clientX - drag.startX, dy = e.clientY - drag.startY;
        if (!drag.active) {
            drag.x = e.clientX; drag.y = e.clientY;
            if (drag.type === "mouse") {
                if (Math.hypot(dx, dy) > MOUSE_SLOP) begin();
            } else if (Math.hypot(dx, dy) > TOUCH_SLOP) {
                clearTimeout(drag.timer);                      // moved before the hold: a swipe - the page scrolls
                drag = null;
            }
            return;
        }
        e.preventDefault();
        drag.x = e.clientX;
        drag.y = e.clientY;
        place();
        follow();
    }

    function autoScroll() {
        if (!drag || !drag.active) return;
        var speed = 0, vh = window.innerHeight;
        if (drag.y < EDGE) speed = -Math.ceil((EDGE - drag.y) / 4);
        else if (drag.y > vh - EDGE) speed = Math.ceil((drag.y - (vh - EDGE)) / 4);
        if (speed) {
            window.scrollBy(0, speed);
            place();
            follow();
        }
        drag.raf = requestAnimationFrame(autoScroll);
    }

    function onPointerUp(e) {
        if (!drag || e.pointerId !== drag.id) return;
        var d = drag;
        drag = null;
        clearTimeout(d.timer);
        cancelAnimationFrame(d.raf);
        if (!d.active) return;
        root.classList.remove("is-dragging-tile");
        // Settle into the slot, then drop the lifted look.
        d.li.classList.add("is-settling");
        d.li.style.transform = "";
        d.li.classList.remove("is-dragging");
        setTimeout(function () { d.li.classList.remove("is-settling"); }, 200);
        justDragged = true;                                    // the click after pointerup isn't a tap
        setTimeout(function () { justDragged = false; }, 400);
        if (e.type === "pointercancel") { restore(d.before); return; }
        saveOrder(d.before);
    }

    // Dragging is always on: listeners on the grid (tiles added later are covered too).
    grid.addEventListener("click", function (e) {             // capture: before the link or Edit mode see it
        if (justDragged) { e.preventDefault(); e.stopPropagation(); justDragged = false; }
    }, true);
    grid.addEventListener("click", onEditClick);
    grid.addEventListener("pointerdown", onPointerDown);
    window.addEventListener("pointermove", onPointerMove, {passive: false});
    window.addEventListener("pointerup", onPointerUp);
    window.addEventListener("pointercancel", onPointerUp);
    // While a tile is held, the finger moves the tile, not the page (a non-passive touchmove is the one way to
    // stop the scroll after the gesture has started; before the hold, swipes scroll as usual).
    grid.addEventListener("touchmove", function (e) { if (drag && drag.active) e.preventDefault(); }, {passive: false});
    grid.addEventListener("dragstart", function (e) { e.preventDefault(); });                 // no native link drag
    grid.addEventListener("contextmenu", function (e) {      // no long-press menu while picking a tile up
        if (drag || e.target.closest(".ht__link") && e.pointerType === "touch") e.preventDefault();
    });
    Array.prototype.forEach.call(grid.querySelectorAll("a"), function (a) { a.setAttribute("draggable", "false"); });

    // Edit mode on / off -------------------------------------------------------------------------------------
    var editButton = document.getElementById("picker-edit");
    var done = document.getElementById("picker-done");

    function setEditButton(on) {
        if (!editButton) return;
        editButton.textContent = on ? "Done" : "Edit";
        editButton.classList.toggle("btn-primary", on);
        editButton.classList.toggle("btn-secondary", !on);
        editButton.setAttribute("aria-pressed", on ? "true" : "false");
    }

    function startEditing() {
        root.classList.add("is-editing");
        if (editingInput) editingInput.value = "1";
        setEditButton(true);
        var url = new URL(window.location.href);
        url.searchParams.set("picker", "1");
        url.searchParams.set("edit", "1");
        history.replaceState(null, "", url.pathname + url.search);
        say("");
    }

    function stopEditing() {
        root.classList.remove("is-editing");
        if (editingInput) editingInput.value = "";
        setEditButton(false);
        var url = new URL(window.location.href);
        url.searchParams.delete("edit");
        history.replaceState(null, "", url.pathname + url.search);
    }

    function saveAndStop() {
        if (done) done.disabled = true;
        var body = new FormData(form);
        body.delete("csrfmiddlewaretoken");
        return post(form.getAttribute("action"), body).then(function (data) {
            stopEditing();
            say(data.note || "Saved.");
            if (editButton) editButton.focus();
        }).catch(function () {
            showError("Couldn't save - try again.");
        }).then(function () { if (done) done.disabled = false; });
    }

    if (editButton) {
        editButton.addEventListener("click", function (e) {
            e.preventDefault();
            if (editing()) { saveAndStop(); return; }
            startEditing();
            var first = grid.querySelector("[data-tile-remove]");
            if (first) first.focus();
        });
    }

    form.addEventListener("submit", function (e) {
        if (!editing() || (e.submitter && e.submitter.hasAttribute("formaction"))) return;
        e.preventDefault();
        saveAndStop();
    });


    // Quick actions ------------------------------------------------------------------------------------------
    // An action tile with a popup (data-quick-url) opens it in #qa-dialog: fetched from labs:quick_action, its first
    // field focused (the phone keyboard comes up). Its form posts to the app's own save view with X-Quick-Add: 1 and
    // gets JSON back (albright_labs/quick.py): close, a toast ("Task added · View"), then the Today panel and the badges
    // are fetched again (labs:hub_refresh) - no reload. Errors show above the form. "More details" opens the app's full
    // form with the fields marked data-more; data-quick-plain submits the ordinary way. Esc, the backdrop and x close it.
    var qa = document.getElementById("qa-dialog"), qaOpener = null, qaToasts = document.getElementById("qa-toasts");
    function qaEsc(text) { var d = document.createElement("div"); d.textContent = text == null ? "" : String(text); return d.innerHTML; }
    function qaClose() { if (qa && qa.open) qa.close(); }
    function qaFocus() {
        var first = qa.querySelector("[data-quick-pane]:not([hidden]) [autofocus], [data-quick-pane]:not([hidden]) input:not([type=hidden]), [autofocus]");
        if (!first) first = qa.querySelector("input:not([type=hidden]), textarea, select, button");
        if (first) { try { first.focus({preventScroll: true}); } catch (e) { first.focus(); } }
    }
    function qaErrors(list) {
        var box = qa.querySelector("[data-quick-errors]");
        if (!box) return;
        box.innerHTML = (list || []).map(function (e) { return "<p>" + qaEsc(e) + "</p>"; }).join("");
        box.hidden = !(list && list.length);
    }
    function qaToast(data) {
        if (!qaToasts) return;
        var t = document.createElement("div");
        t.className = "toast toast--success";
        t.innerHTML = "<span>" + qaEsc(data.message || "Saved") + (data.url ? ' &middot; <a href="' + qaEsc(data.url) + '">' +
            qaEsc(data.link || "View") + "</a>" : "") + "</span>";
        qaToasts.appendChild(t);
        var gone = function () { if (t.parentNode) t.parentNode.removeChild(t); };
        t.addEventListener("click", function (e) { if (!e.target.closest("a")) gone(); });
        setTimeout(gone, 6000);
    }
    function qaRefresh() {
        var url = qa && qa.getAttribute("data-refresh-url");
        if (!url) return;
        fetch(url, {credentials: "same-origin", headers: {"X-Requested-With": "XMLHttpRequest"}})
            .then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); })
            .then(function (data) {
                var old = document.getElementById("planner-today");
                if (old && data.today_html) {
                    var box = document.createElement("div");
                    box.innerHTML = data.today_html;
                    var fresh = box.querySelector("#planner-today");
                    if (fresh) old.parentNode.replaceChild(fresh, old);
                }
                tiles().forEach(function (li) {
                    var n = (data.badges || {})[li.dataset.key], link = li.querySelector(".ht__link");
                    if (n === undefined || !link) return;
                    var badge = li.querySelector("[data-badge]");
                    if (!n) { if (badge) badge.parentNode.removeChild(badge); return; }
                    if (!badge) {
                        badge = document.createElement("span");
                        badge.className = "ht__badge";
                        badge.setAttribute("data-badge", "");
                        link.appendChild(badge);
                    }
                    badge.innerHTML = (n > 99 ? "99+" : n) + '<span class="visually-hidden"> waiting</span>';
                });
            }).catch(function () { /* the page keeps what it shows */ });
    }
    function qaOpen(url, opener) {
        if (!qa || typeof qa.showModal !== "function") { window.location.href = opener.href; return; }
        qaOpener = opener;
        qa.innerHTML = '<div class="qa qa--loading" role="status"><span class="qa-spinner" aria-hidden="true"></span> Loading…</div>';
        if (!qa.open) qa.showModal();
        fetch(url, {credentials: "same-origin", headers: {"X-Requested-With": "XMLHttpRequest"}})
            .then(function (r) { if (!r.ok) throw new Error(r.status); return r.text(); })
            .then(function (html) { if (!qa.open) return; qa.innerHTML = html; qaFocus(); })
            .catch(function () { qaClose(); window.location.href = opener.href; });     // the full page instead
    }
    function qaPost(form, submitter) {
        if (form.reportValidity && !form.reportValidity()) return;
        var body = new FormData(form), save = form.querySelector("[data-quick-save], [data-amount-save]");
        if (submitter && submitter.name) body.append(submitter.name, submitter.value);
        if (save) save.disabled = true;
        qaErrors([]);
        fetch(form.action, {method: "POST", body: body, credentials: "same-origin",
                            headers: {"X-Quick-Add": "1", "X-Requested-With": "XMLHttpRequest"}})
            .then(function (r) { return r.json().catch(function () { return {ok: false}; }); })
            .then(function (data) {
                if (data.ok) { qaClose(); qaToast(data); qaRefresh(); return; }
                qaErrors(data.errors && data.errors.length ? data.errors : ["Couldn't save that - try again."]);
            })
            .catch(function () { qaErrors(["Couldn't save - check your connection and try again."]); })
            .then(function () { if (save) save.disabled = false; });
    }
    // Add recipe's "Paste a link": Meals' own import gives the preview (its Save form then posts like any other).
    function qaImport(form) {
        if (form.reportValidity && !form.reportValidity()) return;
        var out = qa.querySelector("[data-quick-import-result]"), save = form.querySelector("[data-quick-save]");
        if (save) save.disabled = true;
        qaErrors([]);
        out.innerHTML = '<p class="qa-hint" role="status"><span class="qa-spinner" aria-hidden="true"></span> Reading the recipe…</p>';
        fetch(form.action, {method: "POST", body: new FormData(form), credentials: "same-origin",
                            headers: {"X-Requested-With": "XMLHttpRequest", "Accept": "application/json"}})
            .then(function (r) { return r.json(); })
            .then(function (data) {
                out.innerHTML = data.html || "";
                var heading = out.querySelector("#recipe-import-heading");
                if (heading) heading.focus();
            })
            .catch(function () { out.innerHTML = ""; qaErrors(["Couldn't read that link - try again."]); })
            .then(function () { if (save) save.disabled = false; });
    }
    // Log food: Fitness' search (JSON), then the food's amount panel; its Add posts like any other quick form.
    function qaFoodSearch(form) {
        var q = form.elements.q.value.trim(), out = qa.querySelector("[data-quick-food-results]");
        qa.querySelector("[data-quick-food-amount]").innerHTML = "";
        if (!q) { form.elements.q.focus(); return; }
        out.innerHTML = '<p class="qa-hint" role="status"><span class="qa-spinner" aria-hidden="true"></span> Searching…</p>';
        fetch(form.action + "?format=json&q=" + encodeURIComponent(q), {credentials: "same-origin"})
            .then(function (r) { return r.json(); })
            .then(function (data) {
                var html = (data.groups || []).map(function (g) {
                    return '<p class="qa-results__h">' + qaEsc(g.title) + "</p>" + g.items.map(function (i) {
                        return '<button type="button" class="qa-result" data-food-url="' + qaEsc(i.url) + '"><span>' + qaEsc(i.label) +
                            '</span><small>' + qaEsc(i.detail) + "</small></button>";
                    }).join("");
                }).join("");
                out.innerHTML = html || '<p class="qa-hint">Nothing found' + (data.note ? " - " + qaEsc(data.note) : ".") + "</p>";
                if (html && data.note) out.insertAdjacentHTML("beforeend", '<p class="qa-hint">' + qaEsc(data.note) + "</p>");
            })
            .catch(function () { out.innerHTML = ""; qaErrors(["Couldn't search - try again."]); });
    }
    function qaFoodPick(button) {
        var form = qa.querySelector("[data-quick-food-search]"), box = qa.querySelector("[data-quick-food-amount]");
        var url = button.getAttribute("data-food-url");
        url += (url.indexOf("?") < 0 ? "?" : "&") + "date=" + encodeURIComponent(form.getAttribute("data-date")) +
               "&slot=" + encodeURIComponent(form.getAttribute("data-slot"));
        fetch(url, {credentials: "same-origin", headers: {"X-Requested-With": "XMLHttpRequest"}})
            .then(function (r) { if (!r.ok) throw new Error(r.status); return r.text(); })
            .then(function (html) {
                qa.querySelector("[data-quick-food-results]").innerHTML = "";
                box.innerHTML = html;
                var qty = box.querySelector("[data-qty]");
                if (qty) { qty.focus(); qty.select(); }
            })
            .catch(function () { qaErrors(["Couldn't open that food - try again."]); });
    }
    if (qa) {
        grid.addEventListener("click", function (e) {
            var link = e.target.closest("[data-quick-url]");
            if (!link || editing() || e.defaultPrevented) return;
            e.preventDefault();
            qaOpen(link.getAttribute("data-quick-url"), link);
        });
        qa.addEventListener("click", function (e) {
            if (e.target === qa || e.target.closest("[data-quick-close]")) { e.preventDefault(); qaClose(); return; }
            var tab = e.target.closest("[data-quick-tab]");
            if (tab) {
                var name = tab.getAttribute("data-quick-tab");
                qa.querySelectorAll("[data-quick-tab]").forEach(function (b) {
                    b.classList.toggle("is-on", b === tab);
                    b.setAttribute("aria-selected", b === tab ? "true" : "false");
                });
                qa.querySelectorAll("[data-quick-pane]").forEach(function (p) { p.hidden = p.getAttribute("data-quick-pane") !== name; });
                qaErrors([]);
                var field = qa.querySelector('[data-quick-pane="' + name + '"] input:not([type=hidden]), [data-quick-pane="' + name + '"] textarea');
                if (field) field.focus();
                return;
            }
            var more = e.target.closest("[data-quick-more]");
            if (more) {
                e.preventDefault();
                var target = new URL(more.getAttribute("href"), window.location.origin);
                qa.querySelectorAll("[data-more]").forEach(function (f) {
                    var pane = f.closest("[data-quick-pane]");
                    if (pane && pane.hidden) return;
                    if (f.value && f.value.trim()) target.searchParams.set(f.getAttribute("data-more"), f.value.trim());
                });
                window.location.href = target.pathname + target.search + target.hash;
                return;
            }
            var food = e.target.closest("[data-food-url]");
            if (food) { qaFoodPick(food); return; }
            var chipQty = e.target.closest("[data-qty-chip]");
            if (chipQty) {
                var qty = qa.querySelector("[data-qty]");
                if (qty) { qty.value = chipQty.getAttribute("data-qty-chip"); qty.focus(); }
                return;
            }
            var ai = e.target.closest("[data-quick-ai]");
            if (ai) {                         // the assistant (assistant.js, on document) takes it from here
                var q = qa.querySelector("[name=q]");
                ai.setAttribute("data-assistant-prompt", "Estimate this and log it to my food diary: " + (q ? q.value.trim() : ""));
                qaClose();
            }
        });
        qa.addEventListener("submit", function (e) {
            var f = e.target;
            if (e.submitter && e.submitter.hasAttribute("data-quick-plain")) return;     // More details: the ordinary post
            e.preventDefault();
            if (f.hasAttribute("data-quick-import")) { qaImport(f); return; }
            if (f.hasAttribute("data-quick-food-search")) { qaFoodSearch(f); return; }
            qaPost(f, e.submitter);
        });
        qa.addEventListener("keydown", function (e) {      // a list box (one per line): Ctrl/Cmd+Enter saves
            if (e.key === "Enter" && (e.ctrlKey || e.metaKey) && e.target.matches("textarea")) {
                e.preventDefault();
                var f = e.target.form;
                if (f && f.requestSubmit) f.requestSubmit(); else if (f) qaPost(f);
            }
        });
        qa.addEventListener("close", function () {
            qa.innerHTML = "";
            if (qaOpener && document.body.contains(qaOpener)) qaOpener.focus();
            qaOpener = null;
        });
    }

    if (editing()) startEditing();      // opened as ?edit=1
})();
