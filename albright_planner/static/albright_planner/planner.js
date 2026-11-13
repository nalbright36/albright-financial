/*
 * Albright Planner - included on every Planner page by templates/index.html (apps_registry: Planner's script)
 * and by Home for its Today panel. Vanilla JS; everything also works without it (forms post normally).
 *
 *   workouts    checking off a Fitness workout task (data-log-url) first asks "Log this workout?" - Log now opens
 *               Fitness for that occurrence, pre-filled (finishing it there checks it off); Mark done without
 *               logging just checks it off; Cancel leaves it
 *   rows        every item is one line; its checklist chevron opens the subtasks under it (and closes them) - kept
 *               open only while you stay on the page; a click anywhere else on the row opens it like its title does
 *   check-off   a task's box records (or undoes) THIS occurrence only - saved in the background, struck through.
 *               In Home's Today panel a checked-off task leaves the list at once with "Done · Undo" (or, with
 *               "Show completed tasks" on, drops to the bottom struck through); the count line follows.
 *   toolbar     the date range, Day / Week / Month (plain links that keep the date), "‹ Today ›", a date jump,
 *               View options and New item. View options (a <dialog>; a bottom sheet on phones): the filters,
 *               Show meals and default sharing apply live while it's open (the page underneath is fetched with
 *               them), Save posts them to the account, Cancel / Esc / the backdrop put the page back; its button's
 *               dot shows while something is filtered out. New item: a <dialog> with quick add first and "More
 *               options" bringing in the full form (new/?panel=1). The jump form submits on change (a Go button
 *               without JS). Keys: D / W / M switch views, T today, N New item, Left / Right previous or next
 *               (not while typing or with a dialog open).
 *   Week        drag an item onto another day; a repeating one asks "This occurrence only / This and
 *               following / All occurrences" first
 *   the form    shows only the repeat fields that apply, reads the choice back in plain English, and ties
 *               "Can be checked off" to the type (tasks always can)
 *   checklists  ticking a subtask saves at once, for that occurrence only; checking a task with subtasks still
 *               open asks "Mark all subtasks done too?"; the form's Checklist editor: Enter adds the next line,
 *               Enter on an empty one finishes, drag or arrows reorder, x deletes
 *   view panel  clicking an item opens its view panel in a <dialog> over the calendar (its own URL while open);
 *               x, Esc or the backdrop close it, "E" opens the form; checking off and ticks there show on the
 *               calendar underneath too. #pl-view=<id>-<date> (back from the form) opens it on load. Only item links
 *               (data-view) open it - the Day / Week / Month tabs (data-tab) are ordinary page links. Its linked
 *               note shows in full; ticking a box there saves to the note (Notes' check endpoint).
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
    // A Fitness workout task, checked without logging: "Log this workout?" - "log" / "task" / null (cancelled).
    function askLog(near) {
        if (!document.createElement("dialog").showModal) {
            return Promise.resolve(window.confirm("Log this workout in Fitness now? (Cancel marks it done without logging.)") ? "log" : "task");
        }
        var dialog = document.getElementById("pl-log-workout");
        if (!dialog) {
            dialog = document.createElement("dialog");
            dialog.className = "pl-dialog";
            dialog.id = "pl-log-workout";
            dialog.setAttribute("aria-labelledby", "pl-log-workout-h");
            dialog.innerHTML = '<form method="dialog"><h2 id="pl-log-workout-h">Log this workout?</h2>' +
                '<p class="pl-muted">Logging it in Fitness keeps your sets and records - and checks this off when you finish.</p>' +
                '<div class="pl-dialog__actions">' +
                '<button type="submit" class="pl-btn pl-btn--primary" value="log">Log now</button>' +
                '<button type="submit" class="pl-btn" value="task">Mark done without logging</button>' +
                '<button type="submit" class="pl-btn pl-btn--ghost" value="cancel">Cancel</button></div></form>';
            ((near && near.closest(".pl")) || document.querySelector(".pl") || document.body).appendChild(dialog);
        }
        return new Promise(function (resolve) {
            dialog.returnValue = "";
            dialog.addEventListener("close", function done() {
                dialog.removeEventListener("close", done);
                resolve(dialog.returnValue === "log" ? "log" : dialog.returnValue === "task" ? "task" : null);
            });
            dialog.showModal();
        });
    }
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
        if (data.progress_title) el.querySelectorAll("[data-cl-toggle], .pl-cl-count").forEach(function (b) { b.title = "Checklist: " + data.progress_title; });
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
        var logUrl = item && item.getAttribute("data-log-url");
        var first = !was && logUrl ? askLog(form) : Promise.resolve("task");
        first.then(function (logChoice) {
            if (logChoice === "log") { window.location.href = logUrl; return null; }
            if (!logChoice) return null;
            return was || !open ? "task" : askSubtasks(open, form);
        }).then(function (choice) {
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
                    if (todayBox && todayBox.contains(el)) { todayChecked(el, res.data.done); return; }
                    if (res.data.done && el.classList.contains("is-overdue")) el.remove();   // done: no longer overdue
                });
            });
        }).catch(function () { show(was); });
    });

    // ---------- One-line rows: the checklist chevron, and the whole row as a target ----------
    document.addEventListener("click", function (event) {
        var toggle = event.target.closest && event.target.closest("[data-cl-toggle]");
        if (toggle) {
            var list = document.getElementById(toggle.getAttribute("aria-controls"));
            if (!list) return;
            list.hidden = !list.hidden;
            toggle.setAttribute("aria-expanded", String(!list.hidden));
            return;
        }
        var row = event.target.closest && event.target.closest(".pl-item");
        if (!row || event.defaultPrevented || event.target.closest("a, button, form, input, label, select, .pl-item__cl")) return;
        var link = row.querySelector(".pl-item__body");
        if (link) link.click();                       // the view panel (or the meal / record it links to)
    });

    // ---------- Home's Today panel: a checked-off task leaves the list ("Done · Undo"), or drops to the bottom ----------
    var todayBox = document.getElementById("planner-today"), lastDone = null, toastTimer = null;
    function todayCount(change) {
        var c = document.getElementById("planner-today-count");
        if (!c) return;
        ["open", "overdue", "done"].forEach(function (k) {
            c.dataset[k] = Math.max(0, parseInt(c.dataset[k] || "0", 10) + (change[k] || 0));
        });
        var open = +c.dataset.open, overdue = +c.dataset.overdue, done = +c.dataset.done;
        c.textContent = (open ? open + " open today" : "Nothing open today") + (overdue ? " · " + overdue + " overdue" : "") +
            (done ? " · " + done + " done" : "");
    }
    function todayEmpty(list) {
        var empty = list.querySelector(".pl-empty"), any = list.querySelector(".pl-item");
        if (any && empty) empty.parentNode.removeChild(empty);
        if (!any && !empty) {
            empty = document.createElement("li");
            empty.className = "pl-empty";
            empty.textContent = list.getAttribute("data-empty") || "No tasks today.";
            list.appendChild(empty);
        }
    }
    function todayToast(text, undo) {
        var toast = document.getElementById("planner-today-toast");
        document.getElementById("planner-today-toast-text").textContent = text;
        document.getElementById("planner-today-undo").hidden = !undo;
        toast.hidden = false;
        clearTimeout(toastTimer);
        toastTimer = setTimeout(function () { toast.hidden = true; lastDone = null; }, 6000);
    }
    function todayChecked(el, done) {
        var list = el.parentNode, overdue = el.classList.contains("is-overdue");
        var keep = todayBox.getAttribute("data-show-completed") === "1";
        if (done) {
            todayCount({open: -1, overdue: overdue ? -1 : 0, done: keep ? 1 : 0});
            if (keep) list.appendChild(el);                                   // struck through, at the bottom
            else {
                lastDone = {el: el, list: list, next: el.nextSibling, overdue: overdue};
                list.removeChild(el);
                todayToast("Done", true);
            }
        } else if (keep) {
            todayCount({open: 1, done: -1});                                  // a finished one, open again
        }
        todayEmpty(list);
    }
    if (todayBox) {
        document.getElementById("planner-today-undo").addEventListener("click", function () {
            var d = lastDone, form = d && d.el.querySelector(".pl-check-form");
            if (!d || !form) return;
            lastDone = null;
            document.getElementById("planner-today-toast").hidden = true;
            d.list.insertBefore(d.el, d.next && d.next.parentNode === d.list ? d.next : null);
            showTask(d.el, false);
            todayCount({open: 1, overdue: d.overdue ? 1 : 0});
            todayEmpty(d.list);
            post(form.action, new FormData(form)).then(function (res) {      // the box toggles it back to open
                if (res.ok && !res.data.done) return;
                throw new Error("not undone");
            }).catch(function () {
                showTask(d.el, true);
                d.list.removeChild(d.el);
                todayCount({open: -1, overdue: d.overdue ? -1 : 0});
                todayEmpty(d.list);
                todayToast("Couldn't undo – it's still done.", false);
            });
        });
    }

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
            // Only ever an item's view panel - anything else (a whole page) is followed as a link instead.
            if (html.indexOf('id="pl-view-panel"') < 0) throw new Error("not a view panel");
            box.innerHTML = html;
            if (window.AlbrightShare && typeof window.AlbrightShare.enhance === "function") window.AlbrightShare.enhance(box);
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
        // An item's link (data-view, no value) - not the Day / Week / Month tabs (data-tab), which are page links.
        var link = event.target.closest && event.target.closest('a[data-view=""]');
        if (!link || !canDialog || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
        event.preventDefault();
        openView(link.getAttribute("href"), link);
    });
    // The linked note's checklist (the panel shows the whole note): a box ticks that "- [ ]" line in the note
    // itself - Notes' check endpoint, with the version the panel shows - and the note's fresh HTML comes back. If
    // someone changed the note meanwhile, their version shows and nothing is overwritten.
    document.addEventListener("change", function (event) {
        var box = event.target;
        var note = box.matches && box.matches("input.nt-check[data-task]") && box.closest("[data-note-check-url]");
        if (!note) return;
        var status = note.parentNode.querySelector(".pl-view__note-status"), item = box.closest("li"), checked = box.checked;
        function say(text) { if (status) status.textContent = text; }
        function undo(text) { box.checked = !checked; if (item) item.classList.toggle("done", !checked); say(text); }
        if (item) item.classList.toggle("done", checked);
        say("Saving…");
        post(note.dataset.noteCheckUrl, {index: parseInt(box.dataset.task, 10), checked: checked,
                                         version: parseInt(note.dataset.noteVersion, 10)}, true).then(function (res) {
            var d = res.data || {};
            if (typeof d.html === "string" && d.version) { note.innerHTML = d.html; note.dataset.noteVersion = d.version; }
            if (res.ok) say("Saved to the note");
            else if (d.conflict) say("The note was changed elsewhere – this is the latest. Tick again if you still need to.");
            else undo("Not saved – try again.");
        }).catch(function () { undo("Offline – not saved."); });
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

    // ---------- Selects that save on change ----------
    document.addEventListener("change", function (event) {
        if (event.target.matches && event.target.matches("select[data-autosubmit]")) event.target.form.submit();
    });

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

    // ---------- The toolbar: View options ----------
    var head = document.getElementById("pl-head"), plannerBox = document.getElementById("planner");
    var options = document.getElementById("pl-options"), optionsForm = document.getElementById("pl-options-form");
    var optionsBtn = document.getElementById("pl-options-btn"), dot = document.getElementById("pl-options-dot");
    var saved = null, previewing = 0;
    // The page under the toolbar: everything in the Planner box but the toolbar and the dialogs.
    function content(box) {
        return Array.prototype.filter.call(box.children, function (n) { return n !== head && n.id !== "pl-head" && n.tagName !== "DIALOG"; });
    }
    function chosen(name) {
        var el = optionsForm.querySelector("input[name=" + name + "]:checked");
        return el ? el.value : "";
    }
    // The dot: anything filtered out compared with the defaults (views.is_filtered).
    function filtered() {
        var meals = optionsForm.elements.show_meals;
        return !!(optionsForm.elements.kind.value || optionsForm.elements.hide_done.checked || chosen("who") ||
                  (meals && !meals.checked));
    }
    function previewUrl() {
        var tab = document.querySelector("#pl-views a[aria-current='page']");
        var url = new URL((tab && tab.getAttribute("href")) || window.location.href, window.location.origin);
        url.searchParams.set("kind", optionsForm.elements.kind.value);
        url.searchParams.set("done", optionsForm.elements.hide_done.checked ? "0" : "1");
        url.searchParams.set("who", chosen("who"));
        if (optionsForm.elements.show_meals) url.searchParams.set("meals", optionsForm.elements.show_meals.checked ? "1" : "0");
        return url.pathname + url.search;
    }
    function headLinks(doc) {
        var todayBtn = doc.getElementById("pl-today"), jumpBtn = doc.getElementById("pl-jump-btn"),
            jumpDate = doc.getElementById("pl-jump-date");
        return {title: doc.getElementById("pl-title").textContent, prev: doc.getElementById("pl-prev").getAttribute("href"),
                today: todayBtn.getAttribute("href"), todayHidden: todayBtn.hidden,
                next: doc.getElementById("pl-next").getAttribute("href"), page: doc.title,
                jump: jumpBtn ? jumpBtn.textContent : "", jumpValue: jumpDate ? jumpDate.value : "",
                jumpToday: !!(jumpBtn && jumpBtn.classList.contains("is-today"))};
    }
    function showHead(links) {
        document.getElementById("pl-title").textContent = links.title;
        ["prev", "today", "next"].forEach(function (k) { document.getElementById("pl-" + k).setAttribute("href", links[k]); });
        document.getElementById("pl-today").hidden = links.todayHidden;
        document.title = links.page;
        var jumpBtn = document.getElementById("pl-jump-btn"), jumpDate = document.getElementById("pl-jump-date");
        if (jumpBtn && "jump" in links) {
            jumpBtn.textContent = links.jump;
            jumpBtn.classList.toggle("is-today", links.jumpToday);
        }
        if (jumpDate && "jumpValue" in links) jumpDate.value = links.jumpValue;
    }
    function putContent(nodes) {
        content(plannerBox).forEach(function (n) { n.parentNode.removeChild(n); });
        var after = head;
        nodes.forEach(function (n) { after.parentNode.insertBefore(n, after.nextSibling); after = n; });
        window.dispatchEvent(new Event("navfit"));                  // the layout changed without a reload
    }
    // Live: the page underneath, fetched with the choices so far (nothing is saved until Save).
    function preview() {
        dot.hidden = !filtered();
        var n = ++previewing;
        plannerBox.setAttribute("aria-busy", "true");
        fetch(previewUrl(), {credentials: "same-origin"}).then(function (r) {
            if (!r.ok) throw new Error(r.status);
            return r.text();
        }).then(function (html) {
            if (n !== previewing || !options.open) return;
            var doc = new DOMParser().parseFromString(html, "text/html"), box = doc.getElementById("planner");
            if (!box || !doc.getElementById("pl-head")) return;
            showHead(headLinks(doc));
            putContent(content(box).map(function (node) { return document.adoptNode(node); }));
        }).catch(function () { /* the page stays as it was; Save still stores the choices */ }).then(function () {
            if (n === previewing) plannerBox.removeAttribute("aria-busy");
        });
    }
    function cancelOptions() {
        previewing++;
        if (saved) {
            if (saved.changed) { putContent(saved.nodes); showHead(saved.links); }
            dot.hidden = saved.dot;
        }
        optionsForm.reset();
        plannerBox.removeAttribute("aria-busy");
        if (options.open) options.close();
        saved = null;
    }
    if (options && optionsForm && options.showModal) {
        optionsBtn.addEventListener("click", function (event) {
            event.preventDefault();
            saved = {nodes: content(plannerBox), links: headLinks(document), dot: dot.hidden, changed: false};
            options.showModal();
        });
        optionsForm.addEventListener("change", function (event) {
            if (event.target.name === "share") return;              // a saved default, not part of the page
            saved.changed = true;
            preview();
        });
        optionsForm.addEventListener("click", function (event) {
            if (event.target.closest("[data-options-cancel]")) cancelOptions();
        });
        options.addEventListener("cancel", function (event) { event.preventDefault(); cancelOptions(); });   // Esc
        options.addEventListener("click", function (event) { if (event.target === options) cancelOptions(); });  // backdrop
        optionsForm.addEventListener("submit", function () { previewing++; });
    }

    // ---------- The toolbar: New item (quick add, then "More options") ----------
    var newDialog = document.getElementById("pl-new-dialog"), newBtn = document.getElementById("pl-new");
    var more = document.getElementById("pl-new-more"), full = document.getElementById("pl-new-full");
    function openNew() {
        if (!newDialog || !newDialog.showModal) { if (newBtn) window.location.href = newBtn.href; return; }
        newDialog.showModal();
        document.getElementById("pl-quick-text").focus();
    }
    if (newDialog && newDialog.showModal && newBtn) {
        newBtn.addEventListener("click", function (event) { event.preventDefault(); openNew(); });
        newDialog.addEventListener("click", function (event) {
            if (event.target === newDialog || event.target.closest("[data-new-close]")) { newDialog.close(); return; }
            if (event.target.closest("#pl-new-full [data-form-cancel]")) { event.preventDefault(); newDialog.close(); }
        });
        more.addEventListener("click", function (event) {
            event.preventDefault();
            if (full.firstElementChild) {                                // already here: show / hide it
                full.hidden = !full.hidden;
                more.setAttribute("aria-expanded", String(!full.hidden));
                return;
            }
            var url = more.getAttribute("href");
            fetch(url + (url.indexOf("?") < 0 ? "?" : "&") + "panel=1", {credentials: "same-origin"}).then(function (r) {
                if (!r.ok) throw new Error(r.status);
                return r.text();
            }).then(function (html) {
                if (html.indexOf('id="pl-form"') < 0) throw new Error("not the form");
                full.innerHTML = html;
                var form = full.querySelector("#pl-form"), typed = document.getElementById("pl-quick-text").value.trim();
                form.setAttribute("action", url);                       // posts to the New item page, like the page itself
                if (typed && form.elements.title && !form.elements.title.value) form.elements.title.value = typed;
                initForm(form);
                if (window.MdEditor && window.MdEditor.init) full.querySelectorAll("[data-md-editor]").forEach(window.MdEditor.init);
                full.hidden = false;
                more.setAttribute("aria-expanded", "true");
                form.elements.title.focus();
            }).catch(function () { window.location.href = url; });       // the New item page
        });
    }

    // ---------- Date jump: the date button opens the native picker; submit on change (Go is there without JS)
    var jumpForm = document.getElementById("pl-jump"), jumpInput = document.getElementById("pl-jump-date");
    var jumpBtn = document.getElementById("pl-jump-btn");
    function openJump() {
        if (!jumpInput) return;
        if (jumpInput.showPicker) {
            try { jumpInput.showPicker(); return; } catch (err) { /* hidden, or no user gesture */ }
        }
        jumpInput.focus();
        if (typeof jumpInput.click === "function") jumpInput.click();
    }
    if (jumpForm && jumpInput) {
        jumpInput.addEventListener("change", function () { if (jumpInput.value) jumpForm.submit(); });
        if (jumpBtn) jumpBtn.addEventListener("click", openJump);
    }

    function goNav(id) {
        var el = document.getElementById(id);
        if (el && el.getAttribute("href")) window.location.href = el.getAttribute("href");
    }

    // ---------- Keys (desktop): D / W / M views, T today, N New item, Left / Right prev / next ----------
    document.addEventListener("keydown", function (event) {
        if (!head || event.defaultPrevented || event.metaKey || event.ctrlKey || event.altKey || event.repeat) return;
        if (event.target.closest && event.target.closest("input, textarea, select, [contenteditable]")) return;
        if (document.querySelector("dialog[open]")) return;
        var key = (event.key || "").toLowerCase(), views = {d: "day", w: "week", m: "month"};
        if (views[key]) {
            var tab = document.querySelector('#pl-views a[data-tab="' + views[key] + '"]');
            if (tab && tab.getAttribute("aria-current") !== "page") { event.preventDefault(); window.location.href = tab.getAttribute("href"); }
        } else if (key === "t") {
            event.preventDefault();
            goNav("pl-today");
        } else if (key === "n") {
            event.preventDefault();
            openNew();
        } else if (key === "arrowleft") {
            event.preventDefault();
            goNav("pl-prev");
        } else if (key === "arrowright") {
            event.preventDefault();
            goNav("pl-next");
        }
    });

    // ---------- The item form (its page, and the New item dialog's "More options") ----------
    var pageForm = document.getElementById("pl-form");
    if (pageForm) initForm(pageForm);

    function initForm(form) {

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
    }
})();
