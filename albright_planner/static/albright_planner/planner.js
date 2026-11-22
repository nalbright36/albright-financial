/*
 * Albright Planner - included on every Planner page by templates/index.html (apps_registry: Planner's script)
 * and by Home for its Today panel. Vanilla JS; everything also works without it (forms post normally).
 *
 *   workouts    checking off a Fitness workout task (data-log-url) first asks "Log this workout?" - Log now opens
 *               Fitness for that occurrence, pre-filled (finishing it there checks it off); Mark done without
 *               logging just checks it off; Cancel leaves it
 *   weigh-ins   a Fitness weigh-in task's "Log weight" opens a small form (a bottom sheet on phones): Save logs the
 *               weight for that day (asks "Replace today's ...?" when there's one already) and checks it off, showing
 *               "182.0 lb (−0.4 since last)" in place; "Skip today" checks it off with no weight. Checking its box
 *               without a weight opens that form instead; unchecking keeps the weight ("Weight kept - ...").
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
        var weighIn = item && item.querySelector("[data-wi-open-task] [data-wi-form]");
        if (!was && weighIn) { openWeighIn(weighIn); return; }    // a weigh-in is checked off by logging it
        var logUrl = item && item.getAttribute("data-log-url");
        var first = !was && logUrl ? askLog(form) : Promise.resolve("task");
        first.then(function (logChoice) {
            if (logChoice === "log") { window.location.href = logUrl; return null; }
            if (!logChoice) return null;
            return was || !open ? "task" : askSubtasks(open, form);
        }).then(function (choice) {
            if (!choice) return;
            var want = !was;
            show(want);
            if (!window.QuickSave) return;
            // The state is sent (checked=1/0), not "flip it", so a retry or a second tap can't undo the first. Per
            // occurrence only the latest tap is sent, one request at a time; a failure puts the box back and offers
            // "Couldn't save - tap to retry" (labs/quicksave.js).
            QuickSave.set("task:" + form.action + ":" + (form.querySelector("[name=date]") || {}).value, want, function (state) {
                var body = new FormData(form);
                body.set("checked", state ? "1" : "0");
                if (state && choice === "all") body.append("all_subtasks", "1");
                return QuickSave.request(form.action, {body: body});
            }, {
                initial: was, apply: show,
                revert: function (state) { show(state); },
                done: function (data, state, latest) {
                    if (!latest) return;                              // another tap is on its way: wait for it
                    withTwins(item).forEach(function (el) {
                        showTask(el, data.done);
                        if (data.done && choice === "all") { setRows(el, true); el.dataset.openSubtasks = "0"; }
                        showProgress(el, data);
                        if (data.note) weighInNote(el, data.note);
                        else el.querySelectorAll(".pl-kept-note").forEach(function (n) { n.remove(); });
                        if (todayBox && todayBox.contains(el)) { todayChecked(el, data.done); return; }
                        if (data.done && el.classList.contains("is-overdue")) el.remove();   // done: no longer overdue
                    });
                },
            });
        }).catch(function () { show(was); });
    });

    // ---------- Weigh-ins: "Log weight" (albright_fitness/_weigh_in_box.html) ----------
    var openPanel = null;
    function closeWeighIn() {
        if (!openPanel) return;
        var box = openPanel.closest(".wi"), button = box && box.querySelector("[data-wi-open]"), back = box && box.querySelector("[data-wi-close]");
        openPanel.classList.remove("is-open");
        if (button) button.setAttribute("aria-expanded", "false");
        if (back) back.hidden = true;
        openPanel = null;
    }
    function openWeighIn(panel) {
        if (openPanel === panel) return;
        closeWeighIn();
        var box = panel.closest(".wi"), button = box.querySelector("[data-wi-open]"), back = box.querySelector("[data-wi-close]");
        panel.classList.add("is-open");
        if (button) button.setAttribute("aria-expanded", "true");
        if (back) back.hidden = false;
        openPanel = panel;
        var input = panel.querySelector("input[name=weight]");
        if (input) input.focus();
    }
    function weighInNote(el, text) {          // also a workout's "Workout kept - edit it in Fitness."
        var box = el.querySelector(".wi");
        if (!box) {
            var kept = el.querySelector(".pl-kept-note");
            if (!kept) {
                kept = document.createElement("span");
                kept.className = "wi-result wi-note pl-kept-note";
                kept.setAttribute("role", "status");
                el.appendChild(kept);
            }
            kept.textContent = text;
            return;
        }
        box.classList.remove("is-done");
        box.innerHTML = "";
        var note = document.createElement("span");
        note.className = "wi-result wi-note";
        note.textContent = text;
        box.appendChild(note);
    }
    function weighInDone(el, data) {
        var box = el.querySelector(".wi");
        if (box) {
            box.classList.add("is-done");
            box.removeAttribute("data-wi-open-task");
            box.innerHTML = "";
            var result = document.createElement("span");
            result.className = "wi-result" + (data.skipped ? " is-skipped" : "");
            result.textContent = data.result;
            box.appendChild(result);
        }
        showTask(el, true);
    }
    function saveWeighIn(form, body) {
        var msg = form.querySelector("[data-wi-msg]"), item = form.closest(".pl-item, .pl-view");
        if (msg) msg.textContent = "";
        return post(form.action, body).then(function (res) {
            if (res.data.confirm) {                                   // that day already has a weight
                if (!window.confirm(res.data.confirm)) return;
                body.set("replace", "1");
                return saveWeighIn(form, body);
            }
            if (!res.ok) { if (msg) msg.textContent = res.data.error || "Couldn't save that - try again."; return; }
            closeWeighIn();
            withTwins(item).forEach(function (el) {
                weighInDone(el, res.data);
                if (todayBox && todayBox.contains(el)) {
                    todayChecked(el, true);
                    if (todayBox.getAttribute("data-show-completed") !== "1") todayToast("Weigh-in: " + res.data.result, true);
                }
            });
        });
    }
    document.addEventListener("click", function (event) {
        var opener = event.target.closest && event.target.closest("[data-wi-open]");
        if (opener) {
            var panel = document.getElementById(opener.getAttribute("aria-controls"));
            if (panel === openPanel) closeWeighIn(); else if (panel) openWeighIn(panel);
            return;
        }
        if (event.target.closest && event.target.closest("[data-wi-close]")) { closeWeighIn(); return; }
        if (openPanel && !openPanel.contains(event.target) && !(event.target.closest && event.target.closest(".pl-check-form"))) closeWeighIn();
    });
    document.addEventListener("keydown", function (event) {
        if (event.key === "Escape" && openPanel) { closeWeighIn(); }
    });
    document.addEventListener("submit", function (event) {
        var form = event.target;
        if (!form.matches || !form.matches("[data-wi-form]") || !window.fetch || !window.FormData) return;
        event.preventDefault();
        var body = new FormData(form);
        if (event.submitter && event.submitter.name === "skip") body.set("skip", "1");
        else if (!String(body.get("weight") || "").trim()) {
            var msg = form.querySelector("[data-wi-msg]");
            if (msg) msg.textContent = "Enter your weight, or Skip today.";
            return;
        }
        saveWeighIn(form, body).catch(function () {
            var msg = form.querySelector("[data-wi-msg]");
            if (msg) msg.textContent = "Couldn't save that - try again.";
        });
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
        if (!row || event.defaultPrevented || event.target.closest("a, button, form, input, label, select, .pl-item__cl, .wi")) return;
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
            var undoBody = new FormData(form);
            undoBody.set("checked", "0");                                     // back to open, whatever the server has
            (window.QuickSave ? QuickSave.request(form.action, {body: undoBody}) : post(form.action, undoBody)).then(function (res) {
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
        var was = box.getAttribute("aria-checked") === "true", want = !was;
        var show = function (checked) {
            box.setAttribute("aria-checked", checked ? "true" : "false");
            row.classList.toggle("is-done", checked);
        };
        show(want);
        if (!window.QuickSave) return;
        QuickSave.set("subtask:" + form.action + ":" + (form.querySelector("[name=date]") || {}).value, want, function (state) {
            var body = new FormData(form);
            body.set("checked", state ? "1" : "0");                  // the state, not "flip it"
            return QuickSave.request(form.action, {body: body});
        }, {
            initial: was, apply: show,
            revert: function (state) { show(state); },
            done: function (data, state, latest) {
                if (!latest) return;
                var item = form.closest(".pl-item, .pl-view");
                if (!item) {                                              // the edit page's list
                    showProgress(document, data);
                    var by = row.querySelector("[data-cl-by]");
                    if (by) by.textContent = data.done_by ? "Done by " + data.done_by : "";
                    return;
                }
                withTwins(item).forEach(function (el) {
                    el.querySelectorAll('.pl-cl__row[data-row="' + row.dataset.row + '"]').forEach(function (r) {
                        r.classList.toggle("is-done", !!data.checked);
                        var b = r.querySelector(".pl-cl__check");
                        if (b) b.setAttribute("aria-checked", data.checked ? "true" : "false");
                        var by = r.querySelector("[data-cl-by]");
                        if (by) by.textContent = data.done_by ? "Done by " + data.done_by : "";   // shared tasks only
                    });
                    showProgress(el, data);
                    el.dataset.openSubtasks = String(data.open);
                    showTask(el, data.task_done);
                });
            },
        });
    });

    // ---------- Forms that ask first (Delete) ----------
    document.addEventListener("submit", function (event) {
        var form = event.target;
        if (form.matches && form.matches("form[data-confirm]") && !window.confirm(form.dataset.confirm)) event.preventDefault();
    }, true);

    // ---------- The view panel ----------
    var viewDialog = null, viewOpener = null, viewPushed = false, reloadOnPop = false;
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
            // Something was changed in the quick view: the calendar under it reloads to show it.
            var changed = viewDialog.dataset.changed === "1";
            viewDialog.dataset.changed = "";
            viewDialog.innerHTML = "";
            viewDialog._token = null;
            document.documentElement.classList.remove("pl-scroll-lock");            // the page scrolls again
            if (viewPushed) {                                                        // the URL back to the calendar's
                viewPushed = false;
                reloadOnPop = changed;
                history.back();
            } else if (changed) window.location.reload();
            if (viewOpener && document.contains(viewOpener)) viewOpener.focus();
        });
        return viewDialog;
    }
    window.addEventListener("popstate", function () {                                // Back (a phone's too) closes it
        if (reloadOnPop) { reloadOnPop = false; window.location.reload(); return; }
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
    // The full panel for a link (views.view_item ?panel=1) - fetched once, already on pointerdown, so it's usually here
    // by the time the tap ends; kept 30 seconds, and forgotten after any change (a check-off, a quick edit...).
    var panelCache = {};
    function forgetPanels() { panelCache = {}; }
    function fetchPanel(url) {
        var hit = panelCache[url];
        if (hit && Date.now() - hit.at < 30000) return hit.promise;
        var promise = fetch(url + (url.indexOf("?") < 0 ? "?" : "&") + "panel=1", {
            credentials: "same-origin", headers: {"X-Requested-With": "XMLHttpRequest"}
        }).then(function (r) {
            if (!r.ok) throw new Error(r.status);
            return r.text();
        }).then(function (html) {
            // Only ever an item's view panel - anything else (a whole page) is followed as a link instead.
            if (html.indexOf('id="pl-view-panel"') < 0) throw new Error("not a view panel");
            return html;
        });
        promise.catch(function () { if (panelCache[url] && panelCache[url].promise === promise) delete panelCache[url]; });
        panelCache[url] = {at: Date.now(), promise: promise};
        return promise;
    }
    document.addEventListener("submit", forgetPanels, true);

    // The instant first paint, from the link's data-qv (planner/quickview.py): the same panel, read-only for the moment;
    // no data (a link inside a panel): a skeleton.
    function esc(text) {
        return String(text == null ? "" : text).replace(/[&<>"']/g, function (c) {
            return {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c];
        });
    }
    var QV_ICONS = {
        date: '<rect x="3.5" y="5" width="17" height="15.5" rx="2.5"/><path d="M3.5 10h17M8 3v4M16 3v4"/>',
        time: '<circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3 2"/>',
        repeat: '<path d="M17 2.5l3 3-3 3"/><path d="M4 11.5v-1a5 5 0 015-5h11"/><path d="M7 21.5l-3-3 3-3"/><path d="M20 12.5v1a5 5 0 01-5 5H4"/>',
        shared: '<circle cx="9" cy="8" r="3.5"/><path d="M2.5 20a6.5 6.5 0 0113 0M16 4.5a3.5 3.5 0 010 7M18 14a6 6 0 013.5 6"/>',
        note: '<path d="M6 3h9l4 4v14H6z"/><path d="M15 3v4h4M9 12h7M9 16h5"/>'
    };
    function qvIcon(name) {
        return '<svg class="pl-prop__icon" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">' + QV_ICONS[name] + "</svg>";
    }
    var TICK = '<svg width="10" height="10" viewBox="0 0 12 12" aria-hidden="true" focusable="false"><path d="M2.5 6.2l2.3 2.3 4.7-4.9" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>';
    var CLOSE_X = '<svg width="18" height="18" viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path d="M6 6l12 12M18 6L6 18" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"/></svg>';
    function instantPanel(data, url) {
        var back = window.location.pathname + window.location.search;
        try { back = new URL(url, window.location.href).searchParams.get("next") || back; } catch (e) { /* old browsers */ }
        var skel = '<span class="pl-skel" aria-hidden="true"></span>';
        function row(icon, label, value) {
            return '<div class="pl-qv__prop"><div class="pl-prop__row"><span class="pl-prop__label">' + qvIcon(icon) + label +
                '</span><span class="pl-prop__value">' + (value === null ? skel : esc(value)) + "</span></div></div>";
        }
        var d = data || {};
        var html = '<article class="pl-view pl-qv is-instant' + (d.done ? " is-done" : "") + '" aria-busy="true" aria-labelledby="pl-view-title">' +
            '<header class="pl-view__head">' +
            (data ? (d.checkable ? '<span class="pl-check pl-check--lg is-readonly" role="checkbox" aria-checked="' + (d.done ? "true" : "false") + '" aria-disabled="true">' + TICK.replace(/10/g, "16") + "</span>"
                                 : '<span class="pl-dot pl-view__dot" aria-hidden="true"></span>') : "") +
            '<h2 class="pl-view__title" id="pl-view-title">' + (data ? esc(d.title) : '<span class="pl-skel pl-skel--title" aria-hidden="true"></span><span class="visually-hidden">Loading</span>') + "</h2>" +
            '<span class="pl-menu__btn is-placeholder" aria-hidden="true"></span>' +
            '<a href="' + esc(back) + '" class="pl-view__close" data-view-close aria-label="Close" title="Close (Esc)">' + CLOSE_X + "</a></header>" +
            '<div class="pl-view__body"><div class="pl-props pl-qv__props">' +
            row("date", "Date", data ? d.dateText : null) + row("time", "Time", data ? d.timeText : null) +
            row("repeat", "Repeat", data ? d.repeat : null) + row("shared", "Shared", data ? d.shared : null) +
            row("note", "Linked note", data ? d.note : null) + "</div>";
        if (d.checklist && d.checklist.length) {
            var done = d.checklist.filter(function (r) { return r.c; }).length;
            html += '<section class="pl-view__section"><h3 class="pl-view__label">Checklist <span class="pl-view__count">' + done + "/" + d.checklist.length +
                '</span></h3><ul class="pl-cl__rows pl-cl__rows--full">' + d.checklist.map(function (r) {
                    return '<li class="pl-cl__row' + (r.c ? " is-done" : "") + '"><span class="pl-cl__check is-readonly" role="checkbox" aria-checked="' +
                        (r.c ? "true" : "false") + '" aria-disabled="true">' + TICK + '</span><span class="pl-cl__text">' + esc(r.t) + "</span></li>";
                }).join("") + "</ul></section>";
        }
        if (d.notes) {
            html += '<section class="pl-view__section"><h3 class="pl-view__label">Notes</h3><div class="pl-view__notes pl-qv__plain">' +
                esc(d.notes) + (d.more ? "…" : "") + "</div></section>";
        }
        if (d.files && d.files.length) {
            html += '<section class="pl-view__section"><h3 class="pl-view__label">Attachments <span class="pl-view__count">' + d.files.length +
                '</span></h3><ul class="pl-qv__files">' + d.files.map(function (f) { return "<li>" + esc(f) + "</li>"; }).join("") + "</ul></section>";
        }
        html += "</div>" +
            '<footer class="pl-view__foot pl-qv__foot">' +
            (d.edit ? '<a href="' + esc(d.edit + "?date=" + encodeURIComponent(d.date) + "&next=" + encodeURIComponent(back) + "&then=panel") +
                      '" class="pl-qv__editor" data-view-edit>Open full editor &rarr;</a>' : "<span></span>") +
            '<a href="' + esc(back) + '" class="pl-btn" data-view-close>Close</a></footer></article>';
        return html;
    }
    function showPanel(box, html) {
        var body = box.querySelector(".pl-view__body"), top = body ? body.scrollTop : 0;
        var focusClose = !document.activeElement || document.activeElement === document.body || box.contains(document.activeElement);
        box.innerHTML = html;
        if (window.AlbrightShare && typeof window.AlbrightShare.enhance === "function") window.AlbrightShare.enhance(box);
        var edit = box.querySelector("[data-view-edit]");
        if (edit) edit.href = edit.href.replace("then=view", "then=panel");     // saving comes back to this dialog
        box.querySelectorAll("[data-qv-title]").forEach(qvGrow);                  // a long title wraps
        clampNotes(box);
        var newBody = box.querySelector(".pl-view__body");
        if (newBody) newBody.scrollTop = top;
        var close = box.querySelector(".pl-view__close");
        if (close && focusClose) close.focus();
    }
    function openView(url, opener, data) {
        var box = viewBox(), token = {};
        viewOpener = opener || null;
        box._token = token;
        var request = fetchPanel(url);
        box.innerHTML = instantPanel(data, url);                                 // at once - no waiting on the server
        if (!box.open) box.showModal();
        document.documentElement.classList.add("pl-scroll-lock");                // the page behind stays put
        var close = box.querySelector(".pl-view__close");
        if (close) close.focus();
        if (history.pushState && !viewPushed) { history.pushState({plView: true}, "", url); viewPushed = true; }
        request.then(function (html) {
            if (box._token !== token || !box.open) return;                       // closed, or another task opened
            showPanel(box, html);
        }).catch(function () {
            if (box._token === token) window.location.href = url;                // the panel's own page
        });
    }
    document.addEventListener("pointerdown", function (event) {                  // prefetch before the tap completes
        var link = event.target.closest && event.target.closest('a[data-view=""]');
        if (link && canDialog && event.button === 0) fetchPanel(link.getAttribute("href"));
    }, {passive: true});
    document.addEventListener("click", function (event) {
        // An item's link (data-view, no value) - not the Day / Week / Month tabs (data-tab), which are page links.
        var link = event.target.closest && event.target.closest('a[data-view=""]');
        if (!link || !canDialog || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
        event.preventDefault();
        var data = null;
        try { data = link.dataset.qv ? JSON.parse(link.dataset.qv) : null; } catch (e) { data = null; }
        openView(link.getAttribute("href"), link, data);
    });
    // The linked note's checklist (the panel shows the whole note): a box ticks that one "- [ ]" line in the note
    // itself - Notes' check endpoint, by the item's stable id (data-item-id), on the note's latest text - so someone
    // else's edit meanwhile is never overwritten. The box flips at once; per item only the latest tap is sent
    // (labs/quicksave.js). When the note had changed, its fresh HTML comes back and replaces the panel's copy.
    document.addEventListener("change", function (event) {
        var box = event.target;
        var note = box.matches && box.matches("input.nt-check[data-item-id]") && box.closest("[data-note-check-url]");
        if (!note || !window.QuickSave) return;
        var status = note.parentNode.querySelector(".pl-view__note-status"), id = box.dataset.itemId, checked = box.checked;
        function say(text) { if (status) status.textContent = text; }
        function show(state) {
            var b = note.querySelector('input.nt-check[data-item-id="' + id + '"]'), li = b && b.closest("li");
            if (b) b.checked = state;
            if (li) li.classList.toggle("done", state);
        }
        show(checked);
        QuickSave.set("note:" + note.dataset.noteCheckUrl + ":" + id, checked, function (state) {
            return QuickSave.request(note.dataset.noteCheckUrl, {json: true, body: {item_id: id, checked: state,
                version: parseInt(note.dataset.noteVersion, 10)}});
        }, {
            initial: !checked, apply: show,
            done: function (d) {
                if (typeof d.html === "string") note.innerHTML = d.html;
                if (d.version) note.dataset.noteVersion = d.version;
                say("Saved to the note");
            },
            revert: function (state, d) {
                show(state);
                if (d && d.missing && typeof d.html === "string") {
                    note.innerHTML = d.html;
                    note.dataset.noteVersion = d.version;
                    say("That item was changed elsewhere – this is the latest.");
                }
            },
        });
    });
    document.addEventListener("click", function (event) {                            // a menu closes on a click outside it
        document.querySelectorAll(".pl-menu[open]:not([data-menu])").forEach(function (menu) {   // data-menu ones: js/menu.js
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
    // ---------- The quick view: every change saves on its own (views.quick_edit) ----------
    // The panel carries data-quick-url for someone who can edit the item. A change shows at once; the request runs
    // in the background ("Saving…" / "Saved" in the corner); if it fails, the old value comes back with the reason.
    // Closing the dialog after a change reloads the calendar under it, so it shows the change too.
    var DAYS3 = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
    var MONTHS3 = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
    function qvPanel(el) { return el && el.closest ? el.closest("[data-quick-url]") : null; }
    function qvStatus(panel, text, state) {
        var s = panel.querySelector("[data-qv-status]");
        if (!s) return;
        s.className = "pl-qv__saved" + (state ? " is-" + state : "");
        s.textContent = text;
    }
    function qvValue(panel, key, text) {
        var v = panel.querySelector('[data-qv-value="' + key + '"]');
        if (v && text !== undefined && text !== null) v.textContent = text;
        return v ? v.textContent : "";
    }
    function qvSave(panel, fields) {
        var body = new FormData();
        body.append("date", panel.dataset.date);
        Object.keys(fields).forEach(function (k) { body.append(k, fields[k]); });
        qvStatus(panel, "Saving…", "saving");
        return post(panel.dataset.quickUrl, body).then(function (res) {
            if (!res.ok || !res.data.ok) throw new Error((res.data && res.data.error) || "check your connection and try again.");
            var dialog = panel.closest("dialog");
            if (dialog) dialog.dataset.changed = "1";
            forgetPanels();
            if (res.data.occurrence && res.data.occurrence !== panel.dataset.date) {
                panel.dataset.date = res.data.occurrence;
                panel.querySelectorAll('input[name=date]').forEach(function (i) { i.value = res.data.occurrence; });
            }
            ["date", "time", "repeat"].forEach(function (k) { if (res.data[k]) qvValue(panel, k, res.data[k]); });
            qvStatus(panel, "Saved", "saved");
            return res.data;
        }).catch(function (err) {
            qvStatus(panel, "Couldn’t save – " + (err && err.message ? err.message : "try again."), "error");
            throw err;
        });
    }
    function qvDate(iso) {
        var p = /^(\d{4})-(\d{2})-(\d{2})$/.exec(iso || "");
        if (!p) return iso;
        var d = new Date(+p[1], +p[2] - 1, +p[3]);
        return DAYS3[d.getDay()] + ", " + MONTHS3[d.getMonth()] + " " + d.getDate() + (d.getFullYear() !== new Date().getFullYear() ? ", " + d.getFullYear() : "");
    }
    function qvClock(hm) {
        var p = /^(\d{1,2}):(\d{2})/.exec(hm || "");
        if (!p) return "";
        var h = +p[1], ampm = h < 12 ? "AM" : "PM";
        return ((h % 12) || 12) + ":" + p[2] + " " + ampm;
    }
    function qvTimeText(start, end) {
        if (!start) return "All day";
        return qvClock(start) + (end ? "–" + qvClock(end) : "");
    }
    function qvClosePickers(panel, except) {
        panel.querySelectorAll("[data-qv-pick][aria-expanded=true]").forEach(function (b) {
            if (b === except) return;
            b.setAttribute("aria-expanded", "false");
            var picker = b.parentNode.querySelector(".pl-qv__picker");
            if (picker) picker.hidden = true;
        });
    }
    function qvGrow(area) { area.style.height = "auto"; area.style.height = area.scrollHeight + "px"; }
    // Pickers whose contents cost the server most load only when first opened: the Share control (data-lazy-html)
    // and the notes you can link (data-lazy-options).
    function qvLazy(picker) {
        var box = picker.querySelector("[data-lazy-html]");
        if (box && !box.dataset.loaded) {
            box.dataset.loaded = "1";
            fetch(box.dataset.lazyHtml, {credentials: "same-origin", headers: {"X-Requested-With": "XMLHttpRequest"}})
                .then(function (r) { if (!r.ok) throw new Error(r.status); return r.text(); })
                .then(function (html) {
                    box.innerHTML = html;
                    if (window.AlbrightShare && typeof window.AlbrightShare.enhance === "function") window.AlbrightShare.enhance(box);
                }).catch(function () { box.dataset.loaded = ""; box.innerHTML = '<p class="pl-error">Couldn\u2019t load sharing \u2013 try again.</p>'; });
        }
        var select = picker.querySelector("select[data-lazy-options]");
        if (select && !select.dataset.loaded) {
            select.dataset.loaded = "1";
            fetch(select.dataset.lazyOptions, {credentials: "same-origin", headers: {"X-Requested-With": "XMLHttpRequest"}})
                .then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); })
                .then(function (data) {
                    var newOpt = select.querySelector('option[value="new"]');
                    (data.notes || []).forEach(function (n) {
                        if (select.querySelector('option[value="' + n.id + '"]')) return;
                        var opt = document.createElement("option");
                        opt.value = n.id; opt.textContent = n.title;
                        select.insertBefore(opt, newOpt);
                    });
                }).catch(function () { select.dataset.loaded = ""; });
        }
    }

    document.addEventListener("click", function (event) {
        var panel = qvPanel(event.target);
        if (!panel) return;
        var pick = event.target.closest("[data-qv-pick]");
        if (pick) {                                                    // a property row: its picker
            var picker = pick.parentNode.querySelector(".pl-qv__picker"), open = pick.getAttribute("aria-expanded") !== "true";
            qvClosePickers(panel, pick);
            pick.setAttribute("aria-expanded", open ? "true" : "false");
            if (picker) {
                picker.hidden = !open;
                if (open) qvLazy(picker);
                var first = open && picker.querySelector("input:not([type=hidden]), select");
                if (first) first.focus();
            }
            return;
        }
        if (event.target.closest("[data-qv-allday]")) {                // Time: All day
            var box = event.target.closest(".pl-qv__picker"), before = qvValue(panel, "time");
            var inputs = box.querySelectorAll("[data-qv-time]"), old = [inputs[0].value, inputs[1].value];
            inputs.forEach(function (i) { i.value = ""; });
            qvValue(panel, "time", "All day");
            qvSave(panel, {field: "time", start: "", end: ""}).catch(function () {
                inputs[0].value = old[0]; inputs[1].value = old[1];
                qvValue(panel, "time", before);
            });
            return;
        }
        var add = event.target.closest("[data-qv-notes-add]");
        var notesView = !add && event.target.closest("[data-qv-notes-view]");
        if ((add || notesView) && !event.target.closest("a, input")) { qvEditNotes(panel); return; }
        var rowText = event.target.closest("[data-qv-row-text]");
        if (rowText && !rowText.querySelector("input")) qvRenameRow(panel, rowText);
    });
    document.addEventListener("keydown", function (event) {
        var panel = qvPanel(event.target);
        if (!panel || (event.key !== "Enter" && event.key !== " ")) return;
        if (event.target.matches("[data-qv-notes-view]")) { event.preventDefault(); qvEditNotes(panel); }
        else if (event.target.matches("[data-qv-row-text]") && !event.target.querySelector("input")) { event.preventDefault(); qvRenameRow(panel, event.target); }
    });

    // Date, repeat and the linked note save on change; the time a moment after the last change.
    var qvTimer = null;
    document.addEventListener("change", function (event) {
        var panel = qvPanel(event.target), el = event.target;
        if (!panel) return;
        var field = el.getAttribute("data-qv-field");
        if (field === "date") {
            if (!el.value) return;
            var was = qvValue(panel, "date"), oldDate = el.defaultValue;
            qvValue(panel, "date", qvDate(el.value));
            qvSave(panel, {field: "date", value: el.value}).then(function () { el.defaultValue = el.value; }).catch(function () {
                el.value = oldDate; qvValue(panel, "date", was);
            });
        } else if (field === "repeat") {
            var wasRepeat = qvValue(panel, "repeat"), oldIndex = Array.prototype.findIndex.call(el.options, function (o) { return o.defaultSelected; });
            qvValue(panel, "repeat", el.value === "none" ? "Doesn't repeat" : el.options[el.selectedIndex].text);
            qvSave(panel, {field: "repeat", value: el.value}).then(function () {
                Array.prototype.forEach.call(el.options, function (o) { o.defaultSelected = o.selected; });
            }).catch(function () { if (oldIndex >= 0) el.selectedIndex = oldIndex; qvValue(panel, "repeat", wasRepeat); });
        } else if (field === "note") {
            var wasNote = qvValue(panel, "note"), oldNote = Array.prototype.findIndex.call(el.options, function (o) { return o.defaultSelected; });
            qvValue(panel, "note", el.value === "none" ? "None" : el.value === "new" ? "New note…" : el.options[el.selectedIndex].text);
            qvSave(panel, {field: "note", value: el.value}).then(function (data) {
                qvValue(panel, "note", data.note);
                if (el.value === "new" && data.note_id) {                // the new note joins the list, chosen
                    var opt = document.createElement("option");
                    opt.value = data.note_id; opt.textContent = data.note;
                    el.insertBefore(opt, el.options[1] || null);
                    el.value = String(data.note_id);
                }
                Array.prototype.forEach.call(el.options, function (o) { o.defaultSelected = o.selected; });
            }).catch(function () { if (oldNote >= 0) el.selectedIndex = oldNote; qvValue(panel, "note", wasNote); });
        }
    });
    document.addEventListener("input", function (event) {
        var panel = qvPanel(event.target), el = event.target;
        if (!panel) return;
        if (el.matches("[data-qv-title]")) { qvGrow(el); return; }
        if (!el.matches("[data-qv-time]")) return;
        var box = el.closest(".pl-qv__picker"), start = box.querySelector('[data-qv-time="start"]'), end = box.querySelector('[data-qv-time="end"]');
        clearTimeout(qvTimer);
        qvTimer = setTimeout(function () {
            if (!start.value && end.value) return;                       // waits for a start time
            var was = qvValue(panel, "time"), old = [start.defaultValue, end.defaultValue];
            qvValue(panel, "time", qvTimeText(start.value, end.value));
            qvSave(panel, {field: "time", start: start.value, end: end.value}).then(function () {
                start.defaultValue = start.value; end.defaultValue = end.value;
            }).catch(function () { start.value = old[0]; end.value = old[1]; qvValue(panel, "time", was); });
        }, 700);
    });

    // The title: Enter (or leaving it) saves; Esc puts it back.
    document.addEventListener("keydown", function (event) {
        var el = event.target;
        if (!el.matches || !el.matches("[data-qv-title]")) return;
        if (event.key === "Enter") { event.preventDefault(); el.blur(); }
        else if (event.key === "Escape") { el.value = el.dataset.saved; el.blur(); event.stopPropagation(); }
    });
    document.addEventListener("focusout", function (event) {
        var el = event.target, panel = qvPanel(el);
        if (!panel || !el.matches("[data-qv-title]")) return;
        var text = el.value.replace(/\s+/g, " ").trim();
        if (!text) { el.value = el.dataset.saved; qvGrow(el); return; }
        if (text === el.dataset.saved) return;
        var was = el.dataset.saved;
        el.dataset.saved = text;
        qvSave(panel, {field: "title", value: text}).catch(function () { el.value = el.dataset.saved = was; qvGrow(el); });
    });

    // Notes: tap them (or "Add notes") to edit the Markdown; leaving the box saves and shows them rendered again.
    function qvEditNotes(panel) {
        var area = panel.querySelector("[data-qv-notes]"), view = panel.querySelector("[data-qv-notes-view]");
        var add = panel.querySelector("[data-qv-notes-add]"), more = panel.querySelector("#pl-view-notes [data-show-more]");
        if (!area) return;
        area.hidden = false;
        if (view) view.hidden = true;
        if (add) add.hidden = true;
        if (more) more.hidden = true;
        area.dataset.saved = area.value;
        qvGrow(area);
        area.focus();
    }
    document.addEventListener("focusout", function (event) {
        var area = event.target, panel = qvPanel(area);
        if (!panel || !area.matches("[data-qv-notes]")) return;
        var view = panel.querySelector("[data-qv-notes-view]"), add = panel.querySelector("[data-qv-notes-add]");
        function show(html) {
            area.hidden = true;
            if (typeof html === "string") view.innerHTML = html;
            view.hidden = !area.value.trim();
            if (add) add.hidden = !!area.value.trim();
        }
        if (area.value === area.dataset.saved) { show(); return; }
        var was = area.dataset.saved;
        show();
        qvSave(panel, {field: "notes", value: area.value}).then(function (data) {
            area.value = area.dataset.saved = data.notes || "";
            show(data.notes_html || "");
        }).catch(function () { area.value = area.dataset.saved = was; show(); });
    });
    document.addEventListener("input", function (event) { if (event.target.matches && event.target.matches("[data-qv-notes]")) qvGrow(event.target); });

    // The checklist: tap an item's text to rename it (empty removes it); "Add item" + Enter adds one.
    function qvRenameRow(panel, span) {
        var row = span.closest(".pl-cl__row"), old = span.textContent;
        var input = document.createElement("input");
        input.type = "text"; input.className = "pl-qv__rename"; input.maxLength = 200; input.value = old;
        input.setAttribute("aria-label", "Rename checklist item");
        span.textContent = "";
        span.appendChild(input);
        input.focus();
        input.select();
        var done = false;
        function finish(save) {
            if (done) return;
            done = true;
            var text = input.value.replace(/\s+/g, " ").trim();
            span.textContent = save ? (text || old) : old;
            if (!save || text === old) return;
            if (!text) row.hidden = true;
            qvSave(panel, {field: "cl_text", row: row.dataset.row, value: text}).then(function (data) {
                if (!text) row.remove();
                showProgress(panel, data);
            }).catch(function () { span.textContent = old; row.hidden = false; });
        }
        input.addEventListener("keydown", function (e) {
            if (e.key === "Enter") { e.preventDefault(); finish(true); }
            else if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); finish(false); }
        });
        input.addEventListener("blur", function () { finish(true); });
    }
    document.addEventListener("keydown", function (event) {
        var input = event.target, panel = qvPanel(input);
        if (!panel || event.key !== "Enter" || !input.matches("[data-qv-add]")) return;
        event.preventDefault();
        var text = input.value.replace(/\s+/g, " ").trim(), list = panel.querySelector("[data-qv-checklist]");
        if (!text || !list) return;
        var ghost = document.createElement("li");
        ghost.className = "pl-cl__row is-pending";
        ghost.textContent = text;
        list.appendChild(ghost);
        input.value = "";
        qvSave(panel, {field: "cl_add", value: text}).then(function (data) {
            if (data.row_html) {
                var tmp = document.createElement("ul");
                tmp.innerHTML = data.row_html.trim();
                if (tmp.firstElementChild) list.replaceChild(tmp.firstElementChild, ghost);
            } else ghost.remove();
            showProgress(panel, data);
            var title = panel.querySelector("[data-cl-progress-title]");
            if (title) title.hidden = !data.progress_title;
            panel.dataset.openSubtasks = String(data.open || 0);
        }).catch(function () { ghost.remove(); input.value = text; });
    });

    // ---------- The edit page: explicit Save (the floating button), and nothing lost on the way out ----------
    var edForm = document.querySelector("[data-edit-form]");
    // The dock (with the Save button) comes after this script on the page: set up once the page is all there.
    if (edForm) (document.readyState === "loading" ? function (fn) { document.addEventListener("DOMContentLoaded", fn); }
                                                    : function (fn) { fn(); })(function () {
        var saveBtn = document.getElementById("pl-save"), saveMsg = document.getElementById("pl-save-msg");
        var unsaved = document.getElementById("pl-unsaved"), shareDialog = document.getElementById("pl-ed-share");
        var baseline = "", saving = false, leaving = false, savedTimer = null, pendingHref = null;

        // The rows' values, from their fields.
        function picked(name) {
            var els = edForm.querySelectorAll('[name="' + name + '"]');
            for (var i = 0; i < els.length; i++) if (els[i].checked) return els[i];
            return null;
        }
        function labelOf(input) { var l = input && input.closest("label"); return l ? l.textContent.trim() : ""; }
        function selectText(name) {
            var s = edForm.querySelector('select[name="' + name + '"]');
            return s && s.selectedIndex >= 0 ? s.options[s.selectedIndex].text : "";
        }
        function setRow(key, text) {
            var v = edForm.querySelector('[data-prop="' + key + '"] [data-prop-value]');
            if (v && text) v.textContent = text;
        }
        function rowValues() {
            var f = edForm.elements;
            if (f.date && f.date.value) setRow("date", qvDate(f.date.value));
            setRow("time", qvTimeText(f.start_time && f.start_time.value, f.end_time && f.end_time.value));
            var repeat = f.repeat && f.repeat.value, summary = document.getElementById("pl-summary");
            setRow("repeat", repeat === "none" ? "Doesn't repeat" : (summary && summary.textContent.trim()) || selectText("repeat"));
            var scope = picked("scope");
            if (scope) setRow("scope", labelOf(scope));
            setRow("place", (f.location && f.location.value.trim()) || "Home");
            var kind = picked("kind");
            setRow("kind", labelOf(kind) + (kind && kind.value !== "task" && f.checkable && f.checkable.checked ? " · can be checked off" : ""));
            setRow("priority", selectText("priority"));
            setRow("color", selectText("color"));
            var note = edForm.querySelector("#pl-note-pick");
            if (note && note.selectedIndex >= 0) setRow("note", note.options[note.selectedIndex].dataset.label || note.options[note.selectedIndex].text);
        }

        // What's changed: the form's data (the title too - form="pl-form"), leaving out the empty "Add an item" line.
        function snapshot() {
            var data = new FormData(edForm), out = [], ids = data.getAll("cl_id"), texts = data.getAll("cl_text");
            data.forEach(function (value, key) {
                if (key !== "cl_id" && key !== "cl_text" && key !== "csrfmiddlewaretoken" && typeof value === "string") out.push(key + "=" + value);
            });
            ids.forEach(function (id, i) { if (id || (texts[i] || "").trim()) out.push("cl=" + id + ":" + texts[i]); });
            return out.join("&");
        }
        function dirty() { return snapshot() !== baseline; }
        function setState(state, text) {
            if (!saveBtn) return;
            saveBtn.dataset.state = state;
            saveBtn.disabled = state !== "dirty";
            saveBtn.querySelector(".pl-save-fab__label").textContent = text;
        }
        function refresh() {
            rowValues();
            if (saving) return;
            if (dirty()) { clearTimeout(savedTimer); setState("dirty", "Save"); }
            else if (saveBtn && saveBtn.dataset.state !== "done") setState("saved", "Saved");
        }
        function say(text) {
            if (!saveMsg) return;
            saveMsg.textContent = text || "";
            saveMsg.hidden = !text;
        }
        function clearErrors() {
            edForm.querySelectorAll(".pl-error[data-js-error]").forEach(function (e) { e.remove(); });
            document.querySelectorAll(".pl-ed__titlebox .pl-error[data-js-error]").forEach(function (e) { e.remove(); });
            var banner = document.getElementById("pl-form-errors");
            if (banner) banner.hidden = true;
        }
        function showErrors(errors) {
            var first = null;
            Object.keys(errors || {}).forEach(function (name) {
                var field = document.querySelector('[form="pl-form"][name="' + name + '"]') || edForm.querySelector('[name="' + name + '"]');
                if (!field) return;
                var holder = field.closest(".pl-prop__body, .pl-ed__blk, .pl-ed__titlebox") || field.parentNode;
                var details = field.closest("details");
                if (details) details.open = true;
                var msg = document.createElement("span");
                msg.className = "pl-error"; msg.setAttribute("data-js-error", "");
                msg.textContent = errors[name].join(" ");
                holder.appendChild(msg);
                if (!first) first = field;
            });
            var banner = document.getElementById("pl-form-errors");
            if (banner) banner.hidden = false;
            if (first) { first.scrollIntoView({block: "center"}); first.focus({preventScroll: true}); }
        }

        function save() {
            if (saving || !saveBtn) return Promise.resolve(false);
            if (!dirty()) return Promise.resolve(true);
            saving = true;
            var sent = snapshot();
            setState("saving", "Saving…");
            say("");
            clearErrors();
            return post(window.location.pathname + window.location.search, new FormData(edForm)).then(function (res) {
                saving = false;
                if (!res.ok || !res.data.ok) {
                    setState("dirty", "Save");
                    say((res.data && res.data.message) || "Couldn’t save – check your connection and try again.");
                    if (res.data && res.data.errors) showErrors(res.data.errors);
                    return false;
                }
                var d = res.data;
                if (d.redirect) { leaving = true; window.location.href = d.redirect; return true; }
                var typed = snapshot() !== sent;                         // changed again while saving: stays unsaved
                document.querySelectorAll("[data-ed-occurrence]").forEach(function (i) { i.value = d.occurrence; });
                var rows = document.getElementById("pl-cl-edit-rows");
                if (rows && typeof d.checklist_html === "string") rows.innerHTML = d.checklist_html;
                var note = edForm.querySelector("#pl-note-pick");
                if (note) {                                              // the choice is applied: back to "keep"
                    var keep = note.querySelector('option[value="keep"]'), none = note.querySelector('option[value="none"]');
                    keep.textContent = keep.dataset.label = d.note;
                    note.value = "keep";
                    if (d.note_id && !none) {
                        none = document.createElement("option"); none.value = "none"; none.textContent = "No note"; none.dataset.label = "None";
                        keep.after(none);
                    } else if (!d.note_id && none) none.remove();
                }
                var link = document.querySelector("[data-note-link]");
                if (link) { link.hidden = !d.note_url; if (d.note_url) link.href = d.note_url; }
                setRow("note", d.note);
                if (d.url && history.replaceState) history.replaceState(history.state, "", d.url);
                var check = document.getElementById("pl-ed-check");
                if (check && d.title) check.setAttribute("aria-label", "Done: " + d.title);
                if (d.title) document.title = document.title.replace(/^[^·]*·/, d.title + " ·");
                baseline = typed ? sent : snapshot();                    // new checklist rows now have their ids
                if (dirty()) { setState("dirty", "Save"); return true; }
                setState("done", "Saved ✓");
                clearTimeout(savedTimer);
                savedTimer = setTimeout(function () { if (!dirty() && !saving) setState("saved", "Saved"); }, 2000);
                return true;
            }).catch(function () {
                saving = false;
                setState("dirty", "Save");
                say("Couldn’t save – check your connection and try again.");
                return false;
            });
        }

        edForm.addEventListener("submit", function (event) { event.preventDefault(); save(); });
        document.addEventListener("input", function (event) {
            if (edForm.contains(event.target) || event.target.getAttribute("form") === "pl-form") refresh();
        });
        document.addEventListener("change", function (event) {
            if (edForm.contains(event.target) || event.target.getAttribute("form") === "pl-form") refresh();
        });
        edForm.addEventListener("click", function (event) {
            var allDay = event.target.closest("[data-all-day]");
            if (allDay) {
                edForm.elements.start_time.value = "";
                edForm.elements.end_time.value = "";
                refresh();
            }
            if (event.target.closest("[data-cl-delete], [data-cl-move]")) setTimeout(refresh, 0);
            if (event.target.closest("[data-share-open]") && shareDialog && shareDialog.showModal) shareDialog.showModal();
        });
        new MutationObserver(function () { refresh(); }).observe(document.getElementById("pl-cl-edit-rows") || edForm, {childList: true});
        if (shareDialog) shareDialog.addEventListener("click", function (event) {
            if (event.target === shareDialog || event.target.closest("[data-share-close]")) shareDialog.close();
        });

        // A checklist tick saves at once (this occurrence) - it's checking something off, not editing the task.
        edForm.addEventListener("click", function (event) {
            var tick = event.target.closest("[data-ed-tick]");
            if (!tick) return;
            var was = tick.getAttribute("aria-checked") === "true", row = tick.closest(".pl-cl-edit__row");
            var show = function (on) { tick.setAttribute("aria-checked", on ? "true" : "false"); row.classList.toggle("is-done", on); };
            show(!was);
            var body = new FormData();
            body.append("date", document.querySelector("input[name=occurrence]").value);
            body.append("checked", was ? "0" : "1");
            forgetPanels();
            post(tick.dataset.edTick, body).then(function (res) {
                if (!res.ok || !res.data.ok) throw new Error("not saved");
                show(!!res.data.checked);
                showProgress(document, res.data);
                var box = document.getElementById("pl-ed-check");
                if (box) box.setAttribute("aria-checked", res.data.task_done ? "true" : "false");
            }).catch(function () { show(was); say("Couldn’t save that tick – try again."); });
        });

        // Ctrl/Cmd+S saves.
        document.addEventListener("keydown", function (event) {
            if ((event.ctrlKey || event.metaKey) && !event.altKey && (event.key === "s" || event.key === "S")) {
                event.preventDefault();
                save();
            }
        });

        // Leaving with unsaved changes: links and Back ask here (Save / Discard / Keep editing); closing the tab or the
        // browser's Back gets the browser's own question.
        function go(href) { leaving = true; window.location.href = href; }
        document.addEventListener("click", function (event) {
            var link = event.target.closest && event.target.closest("a[href]");
            if (!link || leaving || event.defaultPrevented || !dirty() || saving) return;
            if (link.target === "_blank" || link.hasAttribute("download") || event.metaKey || event.ctrlKey || event.shiftKey || event.button !== 0) return;
            var href = link.getAttribute("href");
            if (!href || href.charAt(0) === "#" || /^(mailto|tel|javascript):/i.test(href)) return;
            event.preventDefault();
            pendingHref = link.href;
            if (unsaved && unsaved.showModal) unsaved.showModal();
            else if (window.confirm("You have unsaved changes. Leave without saving?")) go(pendingHref);
        }, true);
        if (unsaved) unsaved.addEventListener("click", function (event) {
            var choice = event.target.closest("[data-unsaved]");
            if (!choice && event.target === unsaved) { unsaved.close(); return; }
            if (!choice) return;
            var what = choice.dataset.unsaved;
            if (what === "keep") { unsaved.close(); return; }
            if (what === "discard") { unsaved.close(); go(pendingHref); return; }
            unsaved.close();
            save().then(function (ok) { if (ok && !dirty()) go(pendingHref); });
        });
        // A form posting elsewhere (Duplicate, Delete, Skip...) leaves the page: the browser's question covers it.
        window.addEventListener("beforeunload", function (event) {
            if (leaving || !dirty()) return;
            event.preventDefault();
            event.returnValue = "You have unsaved changes";
            return "You have unsaved changes";
        });

        // The starting point: after planner.js has set the form up (its blank checklist line, repeat fields).
        setTimeout(function () {
            var banner = document.getElementById("pl-form-errors");
            // Back with errors (a save without JavaScript): what's on the page isn't saved yet.
            baseline = banner && !banner.hidden ? "unsaved" : snapshot();
            rowValues();
            if (dirty()) setState("dirty", "Save");
            else setState("saved", "Saved");
        }, 0);

        // The big title: a text box that grows with it (a long title wraps); Enter never adds a line.
        var bigTitle = document.querySelector(".pl-ed__title");
        if (bigTitle) {
            qvGrow(bigTitle);
            bigTitle.addEventListener("input", function () { qvGrow(bigTitle); });
            bigTitle.addEventListener("keydown", function (event) { if (event.key === "Enter") event.preventDefault(); });
            window.addEventListener("resize", function () { qvGrow(bigTitle); });
        }
    });
    document.querySelectorAll(".pl-view-page [data-qv-title]").forEach(qvGrow);   // the quick view as a page
})();
