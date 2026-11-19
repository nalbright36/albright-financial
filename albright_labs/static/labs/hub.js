/*
 * Home (albright_labs/hub.html): "My tiles" (albright_labs.home_tiles). Vanilla JS; the page works without it
 * (every button submits, "+ Add" opens the sheet as a section).
 *  - Outside Edit mode a tile is just its link: no click is ever prevented, so one tap opens the page.
 *  - "+ Add" / the Add tile open the Add sheet (a <dialog>): search, app chips, and a + per page that adds it at
 *    once (labs:toggle_tile, by fetch) - it turns into a check; tap again to remove. New tiles go to the end.
 *  - Edit mode (the Edit button, or ?edit=1): a remove (x) per tile (saved at once), drag to reorder with pointer
 *    events (mouse and touch), arrow keys on a focused tile; Done saves the order and the settings by fetch
 *    (labs:save_picker) and leaves Edit mode without a reload. Cancel reloads (the saved order comes back).
 */
(function () {
    "use strict";

    var root = document.getElementById("labs-picker");
    if (!root) return;
    var form = document.getElementById("picker-form");
    var grid = document.getElementById("ht-grid");
    var note = document.getElementById("picker-note");
    var editingInput = document.getElementById("picker-editing");
    var sheet = document.getElementById("tile-sheet");
    var sheetForm = document.getElementById("ht-sheet-form");
    var csrf = form.querySelector("[name=csrfmiddlewaretoken]").value;

    // "Show completed tasks" only while "Show Today panel" is ticked.
    var showToday = document.getElementById("show-today"), todaySub = document.getElementById("show-completed-row");
    if (showToday && todaySub) showToday.addEventListener("change", function () { todaySub.hidden = !showToday.checked; });

    function say(text) { if (note) note.textContent = text; }
    function editing() { return root.classList.contains("is-editing"); }
    function tiles() { return Array.prototype.slice.call(grid.querySelectorAll(".ht:not([data-fixed])")); }
    function addTile() { return grid.querySelector(".ht--add"); }

    function post(url, body) {
        body.append("csrfmiddlewaretoken", csrf);
        return fetch(url, {method: "POST", body: body, credentials: "same-origin",
                           headers: {"X-Requested-With": "XMLHttpRequest"}})
            .then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); });
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
    var search = document.getElementById("ht-search");
    var chip = "";
    function filterRows() {
        var q = (search.value || "").trim().toLowerCase(), shown = 0;
        sheet.querySelectorAll(".ht-row").forEach(function (row) {
            var ok = (!chip || row.dataset.app === chip) && (!q || row.dataset.search.indexOf(q) !== -1);
            row.hidden = !ok;
            if (ok) shown += 1;
        });
        document.getElementById("ht-rows-empty").hidden = shown > 0;
    }
    function openSheet() {
        if (!sheet) return;
        if (sheet.open) sheet.close();
        if (typeof sheet.showModal === "function") sheet.showModal(); else sheet.setAttribute("open", "");
        if (search && window.matchMedia("(min-width: 720px)").matches) search.focus();
    }
    function closeSheet() {
        if (!sheet) return;
        if (typeof sheet.close === "function") sheet.close(); else sheet.removeAttribute("open");
    }
    if (sheet) {
        if (sheet.hasAttribute("open")) {                 // opened as ?add=1 - make it the real modal
            sheet.removeAttribute("open");
            var clean = new URL(window.location.href);
            clean.searchParams.delete("add");
            history.replaceState(null, "", clean.pathname + clean.search);
            openSheet();
        }
        search.addEventListener("input", filterRows);
        sheet.addEventListener("click", function (e) {
            if (e.target === sheet) { closeSheet(); return; }           // the backdrop
            if (e.target.closest("[data-close-sheet]")) { e.preventDefault(); closeSheet(); return; }
            var c = e.target.closest("[data-chip]");
            if (c) {
                chip = c.dataset.chip;
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
                    .catch(function () { say("Couldn't save that - try again."); })
                    .then(function () { t.disabled = false; t.focus(); });
            }
        });
    }
    root.addEventListener("click", function (e) {
        var opener = e.target.closest("[data-open-sheet]");
        if (opener && sheet) { e.preventDefault(); openSheet(); }
    });

    // Edit mode ----------------------------------------------------------------------------------------------
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
                var focus = next && next.querySelector("[data-tile-remove]") || next && next.querySelector("a");
                if (focus) focus.focus();
            }).catch(function () { say("Couldn't remove that - try again."); });
            return;
        }
        // In Edit mode a tile is something to arrange, not a link (the Add tile still opens the sheet).
        if (e.target.closest("[data-tile-link]")) e.preventDefault();
    }

    function onKey(e) {
        if (!editing() || (e.key !== "ArrowLeft" && e.key !== "ArrowRight")) return;
        var li = e.target.closest(".ht:not([data-fixed])");
        if (!li || !e.target.matches("[data-tile-link]")) return;
        e.preventDefault();
        if (e.key === "ArrowLeft" && li.previousElementSibling) grid.insertBefore(li, li.previousElementSibling);
        if (e.key === "ArrowRight" && li.nextElementSibling && !li.nextElementSibling.hasAttribute("data-fixed")) {
            grid.insertBefore(li.nextElementSibling, li);
        }
        e.target.focus();
        say("Moved - press Done to keep this order.");
    }

    // Drag to reorder: pointer events, so mouse, pen and touch work the same.
    var drag = null;
    function onPointerDown(e) {
        if (!editing() || e.button > 0 || e.target.closest("[data-tile-remove]")) return;
        var li = e.target.closest(".ht:not([data-fixed])");
        if (!li) return;
        drag = {li: li, id: e.pointerId, x: e.clientX, y: e.clientY, moving: false};
    }
    function onPointerMove(e) {
        if (!drag || e.pointerId !== drag.id) return;
        var dx = e.clientX - drag.x, dy = e.clientY - drag.y;
        if (!drag.moving) {
            if (Math.abs(dx) + Math.abs(dy) < 6) return;
            drag.moving = true;
            drag.li.classList.add("is-dragging");
            try { drag.li.setPointerCapture(e.pointerId); } catch (err) { /* not capturable - still works */ }
        }
        e.preventDefault();
        drag.li.style.transform = "translate(" + dx + "px," + dy + "px)";
        drag.li.style.pointerEvents = "none";
        var under = document.elementFromPoint(e.clientX, e.clientY);
        drag.li.style.pointerEvents = "";
        var over = under && under.closest(".ht:not([data-fixed])");
        if (!over || over === drag.li || over.parentNode !== grid) return;
        var before = drag.li.getBoundingClientRect();
        var rect = over.getBoundingClientRect();
        var sameRow = e.clientY >= rect.top && e.clientY <= rect.bottom;
        var after = sameRow ? e.clientX > rect.left + rect.width / 2 : e.clientY > rect.top + rect.height / 2;
        grid.insertBefore(drag.li, after ? over.nextElementSibling : over);
        // Keep the tile under the finger after it moved in the grid.
        var moved = drag.li.getBoundingClientRect();
        drag.x += moved.left - before.left;
        drag.y += moved.top - before.top;
        drag.li.style.transform = "translate(" + (e.clientX - drag.x) + "px," + (e.clientY - drag.y) + "px)";
    }
    function onPointerUp(e) {
        if (!drag || e.pointerId !== drag.id) return;
        var li = drag.li, moved = drag.moving;
        drag = null;
        li.classList.remove("is-dragging");
        li.style.transform = "";
        if (moved) {
            say("Moved - press Done to keep this order.");
            justDragged = true;                      // the click that follows pointerup isn't a tap
            setTimeout(function () { justDragged = false; }, 0);
        }
    }

    function startEditing() {
        root.classList.add("is-editing");
        if (editingInput) editingInput.value = "1";
        var url = new URL(window.location.href);
        url.searchParams.set("picker", "1");
        url.searchParams.set("edit", "1");
        history.replaceState(null, "", url.pathname + url.search);
        if (!root.dataset.editReady) {
            root.dataset.editReady = "1";
            grid.addEventListener("click", onEditClick);
            grid.addEventListener("keydown", onKey);
            grid.addEventListener("pointerdown", onPointerDown);
            grid.addEventListener("pointermove", onPointerMove);
            grid.addEventListener("pointerup", onPointerUp);
            grid.addEventListener("pointercancel", onPointerUp);
            grid.addEventListener("dragstart", function (e) { if (editing()) e.preventDefault(); });
        }
        say("");
    }

    function stopEditing() {
        root.classList.remove("is-editing");
        if (editingInput) editingInput.value = "";
        var url = new URL(window.location.href);
        url.searchParams.delete("edit");
        history.replaceState(null, "", url.pathname + url.search);
    }

    var editButton = document.getElementById("picker-edit");
    if (editButton) {
        editButton.addEventListener("click", function (e) {
            e.preventDefault();
            startEditing();
            var first = grid.querySelector("[data-tile-remove]");
            if (first) first.focus();
        });
    }

    var done = document.getElementById("picker-done");
    if (done) {
        form.addEventListener("submit", function (e) {
            if (!editing() || (e.submitter && e.submitter.hasAttribute("formaction"))) return;
            e.preventDefault();
            done.disabled = true;
            var body = new FormData(form);
            body.delete("csrfmiddlewaretoken");
            post(form.getAttribute("action"), body).then(function (data) {
                stopEditing();
                say(data.note || "Saved.");
                if (editButton) editButton.focus();
            }).catch(function () {
                say("Couldn't save - try again.");
            }).then(function () { done.disabled = false; });
        });
    }

    if (editing()) startEditing();      // opened as ?edit=1
})();
