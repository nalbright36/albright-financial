/*
 * Groceries (albright_meals/groceries.html). Vanilla JS.
 *  - Checking off, removing and quick-adding happen in place (fetch POST);
 *    the server answers with the whole list's HTML, which replaces it.
 *  - Every 20 seconds the list is re-fetched (grocery_list?version=...) and
 *    replaced only when something changed - so two people shopping see each
 *    other's changes. Only #grocery-list is ever replaced: the quick-add box
 *    lives outside it, so whatever someone is typing is never touched.
 *  - Paused while the tab is hidden, and while one of our own requests is
 *    still on its way; checked again the moment the tab comes back.
 *  - Select mode: a box on each row (they belong to #grocery-task-form); the
 *    picks survive the 20-second refresh, and "Create task (N)" counts them.
 */
(function () {
    "use strict";
    var list = document.getElementById("grocery-list");
    if (!list) return;
    var addForm = document.getElementById("grocery-add");
    var input = document.getElementById("grocery-line");
    var count = document.getElementById("grocery-count");
    var clearButton = document.getElementById("grocery-clear");
    var sync = document.getElementById("grocery-sync");
    var REFRESH_MS = 20000;
    var busy = 0;
    var page = document.getElementById("grocery-page");
    var taskForm = document.getElementById("grocery-task-form");
    var picked = {};      // grocery line pk -> true, kept across list swaps

    function selectBoxes() { return Array.prototype.slice.call(list.querySelectorAll("[data-select-item]")); }

    function countPicked() {
        var n = selectBoxes().filter(function (b) { return b.checked; }).length;
        document.querySelectorAll("[data-selected-count]").forEach(function (el) { el.textContent = n; });
        var label = document.getElementById("grocery-selected");
        if (label) label.textContent = n;
        var submit = document.getElementById("grocery-task-submit");
        if (submit) submit.disabled = n === 0;
    }

    function restorePicks() {
        selectBoxes().forEach(function (b) { b.checked = !!picked[b.value]; });
        countPicked();
    }

    function setSelecting(on) {
        if (!page) return;
        page.classList.toggle("is-selecting", on);
        if (!on) { picked = {}; restorePicks(); }
        var url = new URL(window.location.href);
        if (on) url.searchParams.set("select", "1"); else url.searchParams.delete("select");
        history.replaceState(null, "", url.pathname + url.search);
        if (on && taskForm) {
            var first = list.querySelector("[data-select-item]");
            if (first) first.focus();
        }
    }

    document.addEventListener("click", function (e) {
        if (e.target.closest("[data-select-start]")) { e.preventDefault(); setSelecting(true); return; }
        if (e.target.closest("[data-select-cancel]")) { e.preventDefault(); setSelecting(false); return; }
        var all = e.target.closest("[data-select-all]"), none = e.target.closest("[data-select-none]");
        if (all || none) {
            e.preventDefault();
            picked = {};
            selectBoxes().forEach(function (b) {
                b.checked = !!all && !b.hasAttribute("data-checked");     // "All to get": the unchecked lines
                if (b.checked) picked[b.value] = true;
            });
            countPicked();
        }
    });
    list.addEventListener("change", function (e) {
        if (!e.target.matches("[data-select-item]")) return;
        if (e.target.checked) picked[e.target.value] = true; else delete picked[e.target.value];
        countPicked();
    });

    function csrf(form) {
        var field = form.querySelector("input[name=csrfmiddlewaretoken]");
        return field ? field.value : "";
    }

    function show(data) {
        if (!data || !data.html) return;
        // Keep keyboard focus on the same item (or its row) across the swap.
        var focused = document.activeElement && list.contains(document.activeElement)
            ? document.activeElement.closest("[data-item]") : null;
        var focusedId = focused ? focused.dataset.item : null;
        list.innerHTML = data.html;
        list.dataset.version = data.version;
        restorePicks();
        if (count && typeof data.unchecked === "number") count.textContent = data.unchecked;
        if (clearButton && typeof data.checked === "number") clearButton.disabled = data.checked === 0;
        if (focusedId) {
            var again = list.querySelector('[data-item-button="' + focusedId + '"]');
            if (again) again.focus();
        }
        if (sync) sync.textContent = "Updated " + new Date().toLocaleTimeString([], {hour: "numeric", minute: "2-digit"});
    }

    function post(form, body) {
        busy += 1;
        return fetch(form.action, {
            method: "POST", credentials: "same-origin", body: body || new FormData(form),
            headers: {"X-Requested-With": "XMLHttpRequest", "X-CSRFToken": csrf(form)}
        }).then(function (r) {
            if (!r.ok) throw new Error("HTTP " + r.status);
            return r.json();
        }).finally(function () { busy -= 1; });
    }

    // Check off / remove: one listener for every row, including rows added later.
    list.addEventListener("submit", function (e) {
        var form = e.target.closest("form[data-list-action]");
        if (!form) return;
        e.preventDefault();
        var row = form.closest(".meals-item");
        if (row && form.classList.contains("meals-item__toggle-form")) row.classList.toggle("is-checked");  // instant feedback
        post(form).then(show).catch(function () { form.submit(); });   // fall back to a normal post
    });

    if (addForm && input) {
        addForm.addEventListener("submit", function (e) {
            if (!input.value.trim()) { e.preventDefault(); input.focus(); return; }
            e.preventDefault();
            var body = new FormData(addForm);
            input.value = "";
            input.focus();
            post(addForm, body).then(show).catch(function () { input.value = body.get("line"); addForm.submit(); });
        });
    }

    function refresh() {
        if (document.hidden || busy) return;
        fetch(list.dataset.listUrl + "?version=" + encodeURIComponent(list.dataset.version || ""),
              {credentials: "same-origin", headers: {"X-Requested-With": "XMLHttpRequest"}})
            .then(function (r) { return r.ok ? r.json() : null; })
            .then(function (data) {
                if (!data || busy) return;
                if (data.changed) show(data);
                else if (sync) sync.textContent = "Up to date " + new Date().toLocaleTimeString([], {hour: "numeric", minute: "2-digit"});
            })
            .catch(function () { if (sync) sync.textContent = "Couldn't refresh - retrying."; });
    }
    selectBoxes().forEach(function (b) { if (b.checked) picked[b.value] = true; });   // kept by the browser on reload
    countPicked();
    setInterval(refresh, REFRESH_MS);
    document.addEventListener("visibilitychange", function () { if (!document.hidden) refresh(); });
})();
