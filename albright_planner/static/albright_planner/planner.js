/*
 * Albright Planner - included on every Planner page by templates/index.html (apps_registry: Planner's script)
 * and by Home for its Today panel. Vanilla JS; everything also works without it (forms post normally).
 *
 *   check-off   a task's box records (or undoes) THIS occurrence only - saved in the background, struck through
 *   filters     apply as soon as one changes
 *   Week        drag an item onto another day; a repeating one asks "This occurrence only / This and
 *               following / All occurrences" first
 *   the form    shows only the repeat fields that apply, reads the choice back in plain English, and ties
 *               "Can be checked off" to the type (tasks always can)
 */
(function () {
    "use strict";

    function csrfToken() {
        var match = document.cookie.match(/csrftoken=([^;]+)/);
        if (match) return match[1];
        var input = document.querySelector("input[name=csrfmiddlewaretoken]");
        return input ? input.value : "";
    }
    function post(url, body, json) {
        var headers = {"X-CSRFToken": csrfToken(), "X-Requested-With": "XMLHttpRequest"};
        if (json) headers["Content-Type"] = "application/json";
        return fetch(url, {method: "POST", credentials: "same-origin", headers: headers,
                           body: json ? JSON.stringify(body) : body})
            .then(function (r) { return r.json().catch(function () { return {}; }).then(function (d) { return {ok: r.ok, data: d}; }); });
    }

    // ---------- Check-off ----------
    document.addEventListener("submit", function (event) {
        var form = event.target;
        if (!form.matches || !form.matches(".pl-check-form") || !window.fetch) return;
        event.preventDefault();
        var item = form.closest(".pl-item"), box = form.querySelector(".pl-check");
        var was = box.getAttribute("aria-checked") === "true";
        var show = function (done) {
            box.setAttribute("aria-checked", done ? "true" : "false");
            if (item) item.classList.toggle("is-done", done);
        };
        show(!was);
        post(form.action, new FormData(form)).then(function (res) {
            if (!res.ok) { show(was); return; }
            show(res.data.done);
            if (res.data.done && item && item.classList.contains("is-overdue")) item.remove();   // done: no longer overdue
        }).catch(function () { show(was); });
    });

    // ---------- Filters, and selects that save on change ----------
    var filters = document.getElementById("pl-filters");
    if (filters) filters.addEventListener("change", function () { filters.submit(); });
    document.addEventListener("change", function (event) {
        if (event.target.matches && event.target.matches("select[data-autosubmit]")) event.target.form.submit();
    });

    // ---------- Quick add: the "Shared with <name>" chip follows its choices ----------
    var quickShare = document.getElementById("pl-quick-share");
    if (quickShare) {
        quickShare.addEventListener("change", function () {
            var names = Array.prototype.filter.call(quickShare.querySelectorAll("select[data-share-name]"),
                function (sel) { return sel.value; }).map(function (sel) { return sel.dataset.shareName; });
            document.getElementById("pl-quick-share-label").textContent = names.length
                ? "Shared with " + names[0] + (names.length > 1 ? " +" + (names.length - 1) : "") : "Only me";
        });
    }

    // ---------- Week: drag to another day ----------
    var week = document.getElementById("pl-week"), dialog = document.getElementById("pl-scope"), dragged = null;
    function askScope(repeats) {
        if (!repeats) return Promise.resolve("all");
        if (!dialog || !dialog.showModal) {
            return Promise.resolve(window.confirm("Move only this occurrence? (Cancel moves every occurrence.)") ? "this" : "all");
        }
        return new Promise(function (resolve) {
            dialog.returnValue = "";
            dialog.addEventListener("close", function done() {
                dialog.removeEventListener("close", done);
                resolve(dialog.returnValue && dialog.returnValue !== "cancel" ? dialog.returnValue : null);
            });
            dialog.showModal();
        });
    }
    if (week) {
        week.addEventListener("dragstart", function (event) {
            var item = event.target.closest && event.target.closest(".pl-item[draggable=true]");
            if (!item) return;
            dragged = item;
            item.classList.add("is-dragging");
            event.dataTransfer.effectAllowed = "move";
            event.dataTransfer.setData("text/plain", item.dataset.item);
        });
        week.addEventListener("dragend", function () {
            if (dragged) dragged.classList.remove("is-dragging");
            week.querySelectorAll(".is-drop").forEach(function (c) { c.classList.remove("is-drop"); });
        });
        week.addEventListener("dragover", function (event) {
            var col = dragged && event.target.closest(".pl-col");
            if (!col) return;
            event.preventDefault();
            week.querySelectorAll(".is-drop").forEach(function (c) { if (c !== col) c.classList.remove("is-drop"); });
            col.classList.add("is-drop");
        });
        week.addEventListener("drop", function (event) {
            var col = event.target.closest(".pl-col"), item = dragged;
            dragged = null;
            if (!col || !item) return;
            event.preventDefault();
            col.classList.remove("is-drop");
            var to = col.dataset.dropDate;
            if (to === item.closest(".pl-col").dataset.dropDate) return;
            askScope(item.dataset.repeats === "1").then(function (scope) {
                if (!scope) return;
                return post(item.dataset.moveUrl, {date: item.dataset.date, to: to, scope: scope}, true).then(function (res) {
                    if (res.ok) window.location.reload();
                    else window.alert(res.data.error || "Couldn't move that item.");
                });
            });
        });
    }

    // ---------- The item form ----------
    var form = document.getElementById("pl-form");
    if (!form) return;
    var DAY_NAMES = {MO: "Mon", TU: "Tue", WE: "Wed", TH: "Thu", FR: "Fri", SA: "Sat", SU: "Sun"};
    var LONG = {MO: "Monday", TU: "Tuesday", WE: "Wednesday", TH: "Thursday", FR: "Friday", SA: "Saturday", SU: "Sunday"};
    var ORD = {"1": "1st", "2": "2nd", "3": "3rd", "4": "4th", "-1": "last"};
    var MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
    var CODES = ["SU", "MO", "TU", "WE", "TH", "FR", "SA"];        // Date.getDay() order

    function value(name) {
        var el = form.elements[name];
        if (!el) return "";
        if (el.length !== undefined && el.tagName !== "SELECT") {
            var picked = Array.prototype.filter.call(el, function (e) { return e.checked; });
            return picked.length ? picked[0].value : "";
        }
        return el.value;
    }
    function chosenDays() {
        return Array.prototype.filter.call(form.querySelectorAll("input[name=days]"), function (e) { return e.checked; })
            .map(function (e) { return e.value; });
    }
    function parseDate(text) {
        var m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(text || "");
        return m ? new Date(+m[1], +m[2] - 1, +m[3]) : null;
    }
    function shortDate(d) {
        return MONTHS[d.getMonth()] + " " + d.getDate() + (d.getFullYear() !== new Date().getFullYear() ? ", " + d.getFullYear() : "");
    }
    function summary() {
        var repeat = value("repeat"), start = parseDate(value("date"));
        if (repeat === "none" || !start) return "";
        var days = chosenDays();
        if (!days.length) days = [CODES[start.getDay()]];
        var names = days.map(function (d) { return DAY_NAMES[d]; }).join(", ");
        var text = {
            daily: "Every day",
            every_n_days: "Every " + (value("day_interval") || 3) + " days",
            weekdays: "Every weekday",
            weekly: "Every " + names,
            every_n_weeks: "Every " + (value("interval") || 2) + " weeks on " + names,
            monthly_day: "Monthly on day " + (value("month_day") || start.getDate()),
            monthly_nth: "Monthly on the " + (ORD[value("nth")] || ORD[String(Math.min(4, Math.ceil(start.getDate() / 7)))]) + " " +
                (LONG[value("nth_weekday")] || LONG[CODES[start.getDay()]]),
            yearly: "Every year on " + MONTHS[start.getMonth()] + " " + start.getDate()
        }[repeat] || "";
        var ends = value("ends"), until = parseDate(value("until")), count = value("count");
        if (ends === "on" && until) text += " until " + shortDate(until);
        else if (ends === "after" && count) text += ", " + count + " time" + (count === "1" ? "" : "s");
        else if (value("kind") === "task") text += " \u00b7 one at a time";
        return text;
    }
    function refresh() {
        var repeat = value("repeat");
        form.querySelectorAll("[data-show-for]").forEach(function (el) {
            el.hidden = el.dataset.showFor.split(" ").indexOf(repeat) === -1;
        });
        var ends = value("ends");
        form.querySelectorAll("[data-ends]").forEach(function (el) {
            el.hidden = el.dataset.ends !== ends || (el.dataset.kind && el.dataset.kind !== value("kind"));
        });
        var out = document.getElementById("pl-summary");
        if (out) out.textContent = summary();
        var checkable = document.getElementById("pl-checkable"), box = form.elements.checkable;
        if (checkable && box) {
            var task = value("kind") === "task";
            if (task) box.checked = true;
            box.disabled = task || form.querySelector("fieldset").disabled;
        }
    }
    form.addEventListener("change", refresh);
    form.addEventListener("input", refresh);
    // A disabled box isn't submitted: re-enable it on submit so a task stays checkable.
    form.addEventListener("submit", function () { if (form.elements.checkable) form.elements.checkable.disabled = false; });
    refresh();
})();
