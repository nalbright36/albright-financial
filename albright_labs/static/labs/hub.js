/*
 * Home, the app picker (albright_labs/hub.html). Vanilla JS; the page works without it (every button submits).
 *  - Status lines: one request to the launcher's endpoint (labs:launcher_lines) after the tiles show.
 *  - Edit: toggles Edit mode in place (no reload); tiles drag to reorder, the arrows move them too (the keyboard
 *    way), Show hides/shows; Done submits the form (labs:save_picker). Admin stays last.
 *  - Pin: saved at once (labs:toggle_pin); the tile moves to or from the Favorites row. On touch screens a long
 *    press shows a tile's pin.
 */
(function () {
    "use strict";

    var root = document.getElementById("labs-picker");
    if (!root) return;
    var form = document.getElementById("picker-form");
    var favorites = root.querySelector('[data-list="favorites"]');
    var others = root.querySelector('[data-list="others"]');
    var note = document.getElementById("picker-note");
    var editingInput = document.getElementById("picker-editing");

    function say(text) { if (note) note.textContent = text; }
    function editing() { return root.classList.contains("is-editing"); }

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

    // Lists ------------------------------------------------------------------------------------------------
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
                li.draggable = editing();
            });
        });
        var hasFavorites = !!favorites.querySelector(".labs-pick");
        document.getElementById("picker-favorites").hidden = !hasFavorites;
        document.getElementById("picker-others-label").hidden = !hasFavorites;
    }

    function setEditing(on) {
        root.classList.toggle("is-editing", on);
        if (editingInput) editingInput.value = on ? "1" : "";
        var url = new URL(window.location.href);
        url.searchParams.set("picker", "1");
        if (on) url.searchParams.set("edit", "1"); else url.searchParams.delete("edit");
        history.replaceState(null, "", url.pathname + url.search);
        refresh();
        if (on) {
            var first = root.querySelector('.labs-pick [data-visible]');
            if (first) first.focus();
        }
    }

    var editButton = document.getElementById("picker-edit");
    if (editButton) {
        editButton.addEventListener("click", function (e) {
            e.preventDefault();
            setEditing(true);
        });
    }

    // Arrows (and Enter/Space on them): move within the tile's own row group.
    root.addEventListener("click", function (e) {
        var button = e.target.closest("[data-move]");
        if (!button) return;
        e.preventDefault();
        var li = button.closest(".labs-pick");
        var list = li.parentNode;
        if (button.dataset.move === "up") {
            var prev = li.previousElementSibling;
            if (prev) list.insertBefore(li, prev);
        } else {
            var next = li.nextElementSibling;
            if (next && !next.hasAttribute("data-fixed")) list.insertBefore(next, li);
        }
        refresh();
        var again = li.querySelector('[data-move="' + button.dataset.move + '"]');
        (again && !again.disabled ? again : li.querySelector("[data-move]:not([disabled])") || li.querySelector("[data-visible]")).focus();
        say("Moved - press Done to keep this order.");
    });

    // In Edit mode a tile is something to arrange, not a link.
    root.addEventListener("click", function (e) {
        var link = e.target.closest("[data-pick-link]");
        if (link && (editing() || link.dataset.suppressClick)) {
            e.preventDefault();
            delete link.dataset.suppressClick;
        }
    });

    // Show / hide - at least one app stays shown.
    root.addEventListener("change", function (e) {
        if (!e.target.matches("[data-visible]")) return;
        var li = e.target.closest(".labs-pick");
        if (!e.target.checked && !root.querySelector("[data-visible]:checked")) {
            e.target.checked = true;
            say("Keep at least one app shown.");
            return;
        }
        li.classList.toggle("is-off", !e.target.checked);
        say(e.target.checked ? "Shown - press Done to save." : "Hidden - press Done to save.");
    });

    // Drag to reorder (Edit mode), within a row group.
    var dragged = null;
    root.addEventListener("dragstart", function (e) {
        var li = e.target.closest(".labs-pick");
        if (!editing() || !li || li.hasAttribute("data-fixed")) { e.preventDefault(); return; }
        dragged = li;
        li.classList.add("is-dragging");
        e.dataTransfer.effectAllowed = "move";
        e.dataTransfer.setData("text/plain", li.dataset.key);
    });
    root.addEventListener("dragover", function (e) {
        if (!dragged) return;
        var over = e.target.closest(".labs-pick");
        if (!over || over === dragged || over.parentNode !== dragged.parentNode || over.hasAttribute("data-fixed")) return;
        e.preventDefault();
        var rect = over.getBoundingClientRect();
        var after = (e.clientX - rect.left) > rect.width / 2;
        over.parentNode.insertBefore(dragged, after ? over.nextElementSibling : over);
    });
    root.addEventListener("drop", function (e) { if (dragged) e.preventDefault(); });
    root.addEventListener("dragend", function () {
        if (!dragged) return;
        dragged.classList.remove("is-dragging");
        dragged = null;
        refresh();
        say("Moved - press Done to keep this order.");
    });

    // Pins -------------------------------------------------------------------------------------------------
    root.addEventListener("click", function (e) {
        var button = e.target.closest("[data-pin]");
        if (!button) return;
        e.preventDefault();
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
                li.classList.remove("show-pin");
                button.setAttribute("aria-pressed", data.pinned ? "true" : "false");
                button.setAttribute("aria-label", (data.pinned ? "Unpin " : "Pin ") + name);
                button.title = data.pinned ? "Unpin from Favorites" : "Pin to Favorites";
                var target = data.pinned ? favorites : others;
                var fixed = target.querySelector("[data-fixed]");
                target.insertBefore(li, li.hasAttribute("data-fixed") ? null : fixed);
                refresh();
                button.focus();
            })
            .catch(function () { say("Couldn't save that pin - try again."); });
    });

    // Long press (touch): show that tile's pin instead of opening the app.
    var pressTimer = null;
    root.addEventListener("touchstart", function (e) {
        var link = e.target.closest("[data-pick-link]");
        if (!link) return;
        pressTimer = setTimeout(function () {
            root.querySelectorAll(".labs-pick.show-pin").forEach(function (li) { li.classList.remove("show-pin"); });
            link.closest(".labs-pick").classList.add("show-pin");
            link.dataset.suppressClick = "1";
        }, 500);
    }, {passive: true});
    ["touchend", "touchmove", "touchcancel"].forEach(function (type) {
        root.addEventListener(type, function () { clearTimeout(pressTimer); }, {passive: true});
    });
    document.addEventListener("click", function (e) {
        if (!e.target.closest(".labs-pick.show-pin")) {
            root.querySelectorAll(".labs-pick.show-pin").forEach(function (li) { li.classList.remove("show-pin"); });
        }
    });
    root.addEventListener("contextmenu", function (e) {
        if (e.target.closest("[data-pick-link]") && pressTimer !== null && "ontouchstart" in window) e.preventDefault();
    });

    refresh();
})();
