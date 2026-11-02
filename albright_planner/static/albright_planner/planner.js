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
 *   checklists  ticking a subtask saves at once, for that occurrence only; checking a task with subtasks still
 *               open asks "Mark all subtasks done too?"; the form's Checklist editor: Enter adds the next line,
 *               Enter on an empty one finishes, drag or arrows reorder, x deletes
 *   view panel  clicking an item opens its view panel in a <dialog> over the calendar (its own URL while open);
 *               x, Esc or the backdrop close it, "E" opens the form; checking off and ticks there show on the
 *               calendar underneath too. #pl-view=<id>-<date> (back from the form) opens it on load.
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
    // "Mark all subtasks done too?" - Yes / Just the task (null: cancelled). Without <dialog>: confirm().
    function askSubtasks(open, near) {
        if (!document.createElement("dialog").showModal) {
            return Promise.resolve(window.confirm("Mark all subtasks done too? (Cancel checks just the task.)") ? "all" : "task");
        }
        var dialog = document.getElementById("pl-subtasks");
        if (!dialog) {
            dialog = document.createElement("dialog");
            dialog.className = "pl-dialog";
            dialog.id = "pl-subtasks";
            dialog.setAttribute("aria-labelledby", "pl-subtasks-h");
            dialog.innerHTML = '<form method="dialog"><h2 id="pl-subtasks-h">Mark all subtasks done too?</h2>' +
                '<p class="pl-muted" id="pl-subtasks-n"></p><div class="pl-dialog__actions">' +
                '<button type="submit" class="pl-btn pl-btn--primary" value="all">Yes</button>' +
                '<button type="submit" class="pl-btn" value="task">Just the task</button>' +
                '<button type="submit" class="pl-btn pl-btn--ghost" value="cancel">Cancel</button></div></form>';
            ((near && near.closest(".pl")) || document.querySelector(".pl") || document.body).appendChild(dialog);   // the Planner's button styles
        }
        dialog.querySelector("#pl-subtasks-n").textContent = open + " subtask" + (open === 1 ? " is" : "s are") + " still open.";
        return new Promise(function (resolve) {
            dialog.returnValue = "";
            dialog.addEventListener("close", function done() {
                dialog.removeEventListener("close", done);
                resolve(dialog.returnValue && dialog.returnValue !== "cancel" ? dialog.returnValue : null);
            });
            dialog.showModal();
        });
    }
    // The same occurrence elsewhere on the page: the calendar under the view panel, or the panel over it.
    function withTwins(el) {
        if (!el || !el.dataset.item) return el ? [el] : [];
        return [el].concat(Array.prototype.filter.call(document.querySelectorAll(
            '[data-item="' + el.dataset.item + '"][data-date="' + el.dataset.date + '"]'), function (t) { return t !== el; }));
    }
    function showProgress(el, data) {
        if (data.progress) el.querySelectorAll("[data-cl-progress]").forEach(function (p) { p.textContent = data.progress; });
        if (data.progress_title) el.querySelectorAll("[data-cl-progress-title]").forEach(function (p) { p.textContent = data.progress_title; });
    }
    function showTask(el, done) {
        var box = el.querySelector(".pl-check");
        if (box) box.setAttribute("aria-checked", done ? "true" : "false");
        el.classList.toggle("is-done", !!done);
    }
    function setRows(item, checked) {
        item.querySelectorAll(".pl-cl__row").forEach(function (row) {
            row.classList.toggle("is-done", checked);
            var box = row.querySelector(".pl-cl__check");
            if (box) box.setAttribute("aria-checked", checked ? "true" : "false");
        });
    }
    document.addEventListener("submit", function (event) {
        var form = event.target;
        if (!form.matches || !form.matches(".pl-check-form") || !window.fetch) return;
        event.preventDefault();
        var item = form.closest(".pl-item, .pl-view"), box = form.querySelector(".pl-check");
        var was = box.getAttribute("aria-checked") === "true";
        var open = item ? parseInt(item.dataset.openSubtasks || "0", 10) : 0;
        var show = function (done) {
            box.setAttribute("aria-checked", done ? "true" : "false");
            if (item) item.classList.toggle("is-done", done);
        };
        (was || !open ? Promise.resolve("task") : askSubtasks(open, form)).then(function (choice) {
            if (!choice) return;
            var body = new FormData(form);
            if (choice === "all") body.append("all_subtasks", "1");
            show(!was);
            return post(form.action, body).then(function (res) {
                if (!res.ok) { show(was); return; }
                show(res.data.done);
                withTwins(item).forEach(function (el) {
                    showTask(el, res.data.done);
                    if (choice === "all") { setRows(el, true); el.dataset.openSubtasks = "0"; }
                    showProgress(el, res.data);
                    if (res.data.done && el.classList.contains("is-overdue")) el.remove();   // done: no longer overdue
                });
            });
        }).catch(function () { show(was); });
    });

    // ---------- Checklist rows: a tick saves at once (that occurrence only) ----------
    document.addEventListener("submit", function (event) {
        var form = event.target;
        if (!form.matches || !form.matches("[data-checklist-toggle]") || !window.fetch) return;
        event.preventDefault();
        var row = form.closest(".pl-cl__row"), box = form.querySelector(".pl-cl__check");
        var was = box.getAttribute("aria-checked") === "true";
        var show = function (checked) {
            box.setAttribute("aria-checked", checked ? "true" : "false");
            row.classList.toggle("is-done", checked);
        };
        show(!was);
        post(form.action, new FormData(form)).then(function (res) {
            if (!res.ok) { show(was); return; }
            show(res.data.checked);
            var item = form.closest(".pl-item, .pl-view");
            if (!item) {                                              // the edit page's list
                showProgress(document, res.data);
                var by = row.querySelector("[data-cl-by]");
                if (by) by.textContent = res.data.done_by ? "Done by " + res.data.done_by : "";
                return;
            }
            withTwins(item).forEach(function (el) {
                el.querySelectorAll('.pl-cl__row[data-row="' + row.dataset.row + '"]').forEach(function (r) {
                    r.classList.toggle("is-done", !!res.data.checked);
                    var b = r.querySelector(".pl-cl__check");
                    if (b) b.setAttribute("aria-checked", res.data.checked ? "true" : "false");
                    var by = r.querySelector("[data-cl-by]");
                    if (by) by.textContent = res.data.done_by ? "Done by " + res.data.done_by : "";   // shared tasks only
                });
                showProgress(el, res.data);
                el.dataset.openSubtasks = String(res.data.open);
                showTask(el, res.data.task_done);
            });
        }).catch(function () { show(was); });
    });

    // ---------- Forms that ask first (Delete) ----------
    document.addEventListener("submit", function (event) {
        var form = event.target;
        if (form.matches && form.matches("form[data-confirm]") && !window.confirm(form.dataset.confirm)) event.preventDefault();
    }, true);

    // ---------- The view panel ----------
    var viewDialog = null, viewOpener = null, viewPushed = false;
    var canDialog = !!(window.fetch && document.createElement("dialog").showModal);
    function viewBox() {
        if (viewDialog) return viewDialog;
        viewDialog = document.createElement("dialog");
        viewDialog.className = "pl-view-dialog";
        viewDialog.id = "pl-view";
        viewDialog.setAttribute("aria-labelledby", "pl-view-title");
        (document.querySelector(".pl") || document.body).appendChild(viewDialog);   // the Planner's control styles
        viewDialog.addEventListener("click", function (event) {
            if (event.target === viewDialog) { viewDialog.close(); return; }          // the backdrop
            var close = event.target.closest("[data-view-close]");
            if (close) { event.preventDefault(); viewDialog.close(); }
        });
        viewDialog.addEventListener("close", function () {
            viewDialog.innerHTML = "";
            if (viewPushed) { viewPushed = false; history.back(); }                  // the URL back to the calendar's
            if (viewOpener && document.contains(viewOpener)) viewOpener.focus();
        });
        return viewDialog;
    }
    window.addEventListener("popstate", function () {                                // Back (a phone's too) closes it
        if (viewDialog && viewDialog.open) { viewPushed = false; viewDialog.close(); }
    });
    function clampNotes(scope) {
        scope.querySelectorAll("[data-clamp]").forEach(function (notes) {
            var more = notes.parentNode.querySelector("[data-show-more]");
            notes.classList.add("is-clamped");
            if (!more || notes.scrollHeight <= notes.clientHeight + 2) { notes.classList.remove("is-clamped"); return; }
            more.hidden = false;
            more.addEventListener("click", function () {
                var clamped = notes.classList.toggle("is-clamped");
                more.textContent = clamped ? "Show more" : "Show less";
                more.setAttribute("aria-expanded", clamped ? "false" : "true");
            });
        });
    }
    function openView(url, opener) {
        var box = viewBox();
        viewOpener = opener || null;
        return fetch(url + (url.indexOf("?") < 0 ? "?" : "&") + "panel=1", {
            credentials: "same-origin", headers: {"X-Requested-With": "XMLHttpRequest"}
        }).then(function (r) {
            if (!r.ok) throw new Error(r.status);
            return r.text();
        }).then(function (html) {
            box.innerHTML = html;
            var edit = box.querySelector("[data-view-edit]");
            if (edit) edit.href = edit.href.replace("then=view", "then=panel");     // saving comes back to this dialog
            if (!box.open) box.showModal();
            clampNotes(box);
            var close = box.querySelector(".pl-view__close");
            if (close) close.focus();
            if (history.pushState && !viewPushed) { history.pushState({plView: true}, "", url); viewPushed = true; }
        }).catch(function () { window.location.href = url; });                       // the panel's own page
    }
    document.addEventListener("click", function (event) {
        var link = event.target.closest && event.target.closest("a[data-view]");
        if (!link || !canDialog || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
        event.preventDefault();
        openView(link.getAttribute("href"), link);
    });
    document.addEventListener("click", function (event) {                            // a menu closes on a click outside it
        document.querySelectorAll(".pl-menu[open]").forEach(function (menu) {
            if (!menu.contains(event.target)) menu.open = false;
        });
    });
    document.addEventListener("keydown", function (event) {
        if (event.metaKey || event.ctrlKey || event.altKey || event.defaultPrevented) return;
        var page = document.querySelector(".pl-view-page");
        var panel = viewDialog && viewDialog.open ? viewDialog : (!document.querySelector("dialog[open]") && page);
        if (!panel) return;
        if (event.key === "Escape" && panel === page) {                              // the page: Esc is its Close
            var close = page.querySelector("[data-view-close]");
            if (close) window.location.href = close.href;
            return;
        }
        if ((event.key !== "e" && event.key !== "E") || event.target.closest("input, textarea, select, [contenteditable]")) return;
        var edit = panel.querySelector("[data-view-edit]");
        if (edit) { event.preventDefault(); window.location.href = edit.href; }
    });
    var viewPage = document.querySelector(".pl-view-page");
    if (viewPage) clampNotes(viewPage);
    var opening = /^#pl-view=(\d+)-(\d{4}-\d{2}-\d{2})$/.exec(window.location.hash), viewBase = document.querySelector("[data-view-url]");
    if (opening && viewBase && canDialog) {
        var here = window.location.pathname + window.location.search;
        history.replaceState(history.state, "", here);
        openView(viewBase.dataset.viewUrl.replace(/0\/$/, opening[1] + "/") + "?date=" + opening[2] + "&next=" + encodeURIComponent(here));
    }

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

    // The Checklist editor: one subtask per line.
    var clRows = document.getElementById("pl-cl-edit-rows");
    if (clRows) {
        var blankRow = function () {
            var last = clRows.lastElementChild, copy = last.cloneNode(true);
            copy.querySelector("input[name=cl_id]").value = "";
            var text = copy.querySelector("input[name=cl_text]");
            text.value = "";
            text.readOnly = false;
            text.removeAttribute("title");
            text.placeholder = "Add an item";
            return copy;
        };
        var rowOf = function (el) { return el.closest(".pl-cl-edit__row"); };
        var keepBlankLast = function () {
            var last = clRows.lastElementChild;
            if (!last || last.querySelector("input[name=cl_text]").value.trim()) clRows.appendChild(blankRow());
        };
        clRows.addEventListener("keydown", function (event) {
            if (event.key !== "Enter" || !event.target.matches("input[name=cl_text]")) return;
            event.preventDefault();                                   // never submits the form from here
            var row = rowOf(event.target);
            if (!event.target.value.trim()) {                         // Enter on an empty line: done
                if (row !== clRows.lastElementChild) row.remove();
                var after = form.querySelector("#pl-scope-field input, #pl-share-with select, .pl-actions button");
                if (after) after.focus();
                return;
            }
            var next = row.nextElementSibling;
            if (!next || next.querySelector("input[name=cl_text]").value.trim()) {
                next = blankRow();
                row.after(next);
            }
            next.querySelector("input[name=cl_text]").focus();
        });
        clRows.addEventListener("input", keepBlankLast);
        clRows.addEventListener("click", function (event) {
            var button = event.target.closest("button");
            if (!button) return;
            var row = rowOf(button);
            if (button.hasAttribute("data-cl-delete")) {
                var focusTo = row.nextElementSibling || row.previousElementSibling;
                row.remove();
                keepBlankLast();
                if (focusTo) focusTo.querySelector("input[name=cl_text]").focus();
            } else if (button.dataset.clMove === "-1" && row.previousElementSibling) {
                row.previousElementSibling.before(row);
                button.focus();
            } else if (button.dataset.clMove === "1" && row.nextElementSibling &&
                       row.nextElementSibling !== clRows.lastElementChild) {
                row.nextElementSibling.after(row);
                button.focus();
            }
        });
        var draggedRow = null;
        clRows.addEventListener("dragstart", function (event) {
            draggedRow = rowOf(event.target);
            if (!draggedRow || draggedRow === clRows.lastElementChild) { draggedRow = null; event.preventDefault(); return; }
            draggedRow.classList.add("is-dragging");
            event.dataTransfer.effectAllowed = "move";
            event.dataTransfer.setData("text/plain", "");
        });
        clRows.addEventListener("dragover", function (event) {
            var over = draggedRow && rowOf(event.target);
            if (!over || over === draggedRow) return;
            event.preventDefault();
            var box = over.getBoundingClientRect();
            if (event.clientY < box.top + box.height / 2) over.before(draggedRow);
            else if (over !== clRows.lastElementChild) over.after(draggedRow);
        });
        clRows.addEventListener("dragend", function () {
            if (draggedRow) draggedRow.classList.remove("is-dragging");
            draggedRow = null;
        });
    }
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
        form.querySelectorAll("[data-kind-only]").forEach(function (el) { el.hidden = el.dataset.kindOnly !== value("kind"); });
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
    // Not saved (the server said why): bring the first problem into view with its field focused - what you typed is
    // still in the form.
    var problem = form.querySelector(".pl-field .pl-error");
    if (problem) {
        var field = problem.closest(".pl-field").querySelector("input:not([type=hidden]), select, textarea");
        problem.scrollIntoView({block: "center"});
        if (field) field.focus({preventScroll: true});
    }
})();
