/*
 * Home (albright_labs/hub.html): "My tiles" (albright_labs.home_tiles). Vanilla JS; the page works without it
 * (every button submits, "+ Add" opens the sheet as a section).
 *  - Outside Edit mode a tile is just its link: no click is ever prevented, so one tap opens the page.
 *  - "+ Add" / the Add tile open the Add sheet (a <dialog>): search (label + description + app, every word
 *    anywhere, with the app chip), and a + per page that adds it at once (labs:toggle_tile) - it turns into a
 *    check; tap again to remove. New tiles go to the end. Closing the sheet clears the search.
 *  - Edit mode (the Edit button, which then reads Done; or ?edit=1): tiles wiggle; each gets a remove (x) and
 *    move left/right buttons; a press-and-drag moves a tile (pointer events - one path for mouse, pen and touch;
 *    touch waits ~200ms so a swipe still scrolls). The other tiles slide aside to show where it lands, the page
 *    auto-scrolls near the top/bottom edge, and the drop saves the order (labs:save_tile_order) - on failure the
 *    tiles go back and a short error shows. Done saves the settings too (labs:save_picker), without a reload.
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
                grid.insertBefore(holder.firstElementChild, addTile());
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
        if (justDragged) { e.preventDefault(); e.stopPropagation(); return; }    // the click that ends a drag
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
    var HOLD_MS = 200, SLOP = 8, EDGE = 60;
    var drag = null;

    function onPointerDown(e) {
        if (!editing() || drag || (e.pointerType === "mouse" && e.button !== 0)) return;
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
        if (navigator.vibrate && drag.type === "touch") navigator.vibrate(10);
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
            if (drag.type === "mouse") {
                drag.x = e.clientX; drag.y = e.clientY;
                if (Math.abs(dx) + Math.abs(dy) >= SLOP) begin();
                return;
            }
            // Touch before the hold: a swipe. Tiles have touch-action: none in Edit mode, so scroll the page
            // ourselves - normal scrolling still works - and don't start a drag.
            if (Math.abs(dx) + Math.abs(dy) >= SLOP) {
                clearTimeout(drag.timer);
                window.scrollBy(0, drag.lastY - e.clientY);
                drag.lastY = e.clientY;
                drag.scrolling = true;
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
        if (d.scrolling) { justDragged = true; setTimeout(function () { justDragged = false; }, 0); }
        if (!d.active) return;
        root.classList.remove("is-dragging-tile");
        // Settle into the slot, then drop the lifted look.
        d.li.classList.add("is-settling");
        d.li.style.transform = "";
        d.li.classList.remove("is-dragging");
        setTimeout(function () { d.li.classList.remove("is-settling"); }, 200);
        justDragged = true;                                    // the click after pointerup isn't a tap
        setTimeout(function () { justDragged = false; }, 0);
        if (e.type === "pointercancel") { restore(d.before); return; }
        saveOrder(d.before);
    }

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
        if (!root.dataset.editReady) {
            root.dataset.editReady = "1";
            grid.addEventListener("click", onEditClick);
            grid.addEventListener("pointerdown", onPointerDown);
            window.addEventListener("pointermove", onPointerMove, {passive: false});
            window.addEventListener("pointerup", onPointerUp);
            window.addEventListener("pointercancel", onPointerUp);
            grid.addEventListener("dragstart", function (e) { if (editing()) e.preventDefault(); });   // no native drag
            grid.addEventListener("contextmenu", function (e) { if (editing() && e.target.closest(".ht")) e.preventDefault(); });
        }
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

    if (editing()) startEditing();      // opened as ?edit=1
})();
