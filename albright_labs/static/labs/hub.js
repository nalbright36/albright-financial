/*
 * Home, the app picker (albright_labs/hub.html). Vanilla JS; the page works without it (every button submits).
 *  - Status lines: one request to the launcher's endpoint (labs:launcher_lines) after the tiles show.
 *  - Outside Edit mode a tile is just its link: no handlers are attached to tiles and no click is ever
 *    prevented, so one click or tap opens the app.
 *  - Edit mode (the Edit button, or ?edit=1): only then are the tile handlers attached - drag to reorder, the
 *    arrows (the keyboard way), Show, and the pin (saved at once, labs:toggle_pin); Done submits the form
 *    (labs:save_picker). Admin stays last. Leaving Edit mode is Done or Cancel, both a page load.
 */
(function () {
    "use strict";

    var root = document.getElementById("labs-picker");
    if (!root) return;
    // "Show completed tasks" only while "Show Today panel" is ticked.
    var showToday = document.getElementById("show-today"), todaySub = document.getElementById("show-completed-row");
    if (showToday && todaySub) showToday.addEventListener("change", function () { todaySub.hidden = !showToday.checked; });

    var form = document.getElementById("picker-form");
    var favorites = root.querySelector('[data-list="favorites"]');
    var others = root.querySelector('[data-list="others"]');
    var note = document.getElementById("picker-note");
    var editingInput = document.getElementById("picker-editing");

    function say(text) { if (note) note.textContent = text; }

    // Status lines -----------------------------------------------------------------------------------------
    fetch(root.dataset.linesUrl, {headers: {"X-Requested-With": "XMLHttpRequest"}, credentials: "same-origin"})
        .then(function (r) { return r.ok ? r.json() : {lines: {}}; })
        .then(function (data) {
            var lines = data.lines || {};
            root.querySelectorAll("[data-line-for]").forEach(function (el) {
                var text = lines[el.dataset.lineFor];
                if (text) el.textContent = text;
            });
        })
        .catch(function () { /* the tiles still work; the lines just stay empty */ });

    // Edit mode only ---------------------------------------------------------------------------------------
    function keepFixedLast(list) {
        list.querySelectorAll("[data-fixed]").forEach(function (li) { list.appendChild(li); });
    }

    function refresh() {
        [favorites, others].forEach(function (list) {
            keepFixedLast(list);
            var movable = list.querySelectorAll(".labs-pick:not([data-fixed])");
            movable.forEach(function (li, i) {
                var up = li.querySelector('[data-move="up"]'), down = li.querySelector('[data-move="down"]');
                if (up) up.disabled = i === 0;
                if (down) down.disabled = i === movable.length - 1;
                li.draggable = true;
            });
        });
        var hasFavorites = !!favorites.querySelector(".labs-pick");
        document.getElementById("picker-favorites").hidden = !hasFavorites;
        document.getElementById("picker-others-label").hidden = !hasFavorites;
    }

    function onClick(e) {
        // In Edit mode a tile is something to arrange, not a link.
        if (e.target.closest("[data-pick-link]")) { e.preventDefault(); return; }

        var move = e.target.closest("[data-move]");
        if (move) {
            e.preventDefault();
            var li = move.closest(".labs-pick");
            var list = li.parentNode;
            if (move.dataset.move === "up") {
                if (li.previousElementSibling) list.insertBefore(li, li.previousElementSibling);
            } else {
                var next = li.nextElementSibling;
                if (next && !next.hasAttribute("data-fixed")) list.insertBefore(next, li);
            }
            refresh();
            var again = li.querySelector('[data-move="' + move.dataset.move + '"]');
            (again && !again.disabled ? again : li.querySelector("[data-move]:not([disabled])") || li.querySelector("[data-visible]")).focus();
            say("Moved - press Done to keep this order.");
            return;
        }

        var pin = e.target.closest("[data-pin]");
        if (pin) {
            e.preventDefault();
            togglePin(pin);
        }
    }

    function togglePin(button) {
        var li = button.closest(".labs-pick");
        var body = new FormData();
        body.append("key", li.dataset.key);
        body.append("csrfmiddlewaretoken", form.querySelector("[name=csrfmiddlewaretoken]").value);
        fetch(root.dataset.pinUrl, {method: "POST", body: body, credentials: "same-origin",
                                    headers: {"X-Requested-With": "XMLHttpRequest"}})
            .then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); })
            .then(function (data) {
                var name = li.querySelector(".labs-pick__name").textContent;
                li.classList.toggle("is-pinned", data.pinned);
                button.setAttribute("aria-pressed", data.pinned ? "true" : "false");
                button.setAttribute("aria-label", (data.pinned ? "Unpin " : "Pin ") + name);
                button.title = data.pinned ? "Unpin from Favorites" : "Pin to Favorites";
                var target = data.pinned ? favorites : others;
                target.insertBefore(li, li.hasAttribute("data-fixed") ? null : target.querySelector("[data-fixed]"));
                refresh();
                button.focus();
            })
            .catch(function () { say("Couldn't save that pin - try again."); });
    }

    function onChange(e) {
        if (!e.target.matches("[data-visible]")) return;
        if (!e.target.checked && !root.querySelector("[data-visible]:checked")) {
            e.target.checked = true;
            say("Keep at least one app shown.");
            return;
        }
        e.target.closest(".labs-pick").classList.toggle("is-off", !e.target.checked);
        say(e.target.checked ? "Shown - press Done to save." : "Hidden - press Done to save.");
    }

    // Drag to reorder, within a row group.
    var dragged = null;
    function onDragStart(e) {
        var li = e.target.closest(".labs-pick");
        if (!li || li.hasAttribute("data-fixed")) { e.preventDefault(); return; }
        dragged = li;
        li.classList.add("is-dragging");
        e.dataTransfer.effectAllowed = "move";
        e.dataTransfer.setData("text/plain", li.dataset.key);
    }
    function onDragOver(e) {
        if (!dragged) return;
        var over = e.target.closest(".labs-pick");
        if (!over || over === dragged || over.parentNode !== dragged.parentNode || over.hasAttribute("data-fixed")) return;
        e.preventDefault();
        var rect = over.getBoundingClientRect();
        over.parentNode.insertBefore(dragged, (e.clientX - rect.left) > rect.width / 2 ? over.nextElementSibling : over);
    }
    function onDrop(e) { if (dragged) e.preventDefault(); }
    function onDragEnd() {
        if (!dragged) return;
        dragged.classList.remove("is-dragging");
        dragged = null;
        refresh();
        say("Moved - press Done to keep this order.");
    }

    function startEditing() {
        if (root.dataset.editReady) return;
        root.dataset.editReady = "1";
        root.classList.add("is-editing");
        if (editingInput) editingInput.value = "1";
        var url = new URL(window.location.href);
        url.searchParams.set("picker", "1");
        url.searchParams.set("edit", "1");
        history.replaceState(null, "", url.pathname + url.search);
        root.addEventListener("click", onClick);
        root.addEventListener("change", onChange);
        root.addEventListener("dragstart", onDragStart);
        root.addEventListener("dragover", onDragOver);
        root.addEventListener("drop", onDrop);
        root.addEventListener("dragend", onDragEnd);
        refresh();
    }

    var editButton = document.getElementById("picker-edit");
    if (editButton) {
        editButton.addEventListener("click", function (e) {
            e.preventDefault();
            startEditing();
            var first = root.querySelector(".labs-pick [data-visible]");
            if (first) first.focus();
        });
    }
    if (root.classList.contains("is-editing")) startEditing();      // opened as ?edit=1
})();
