/*
 * Albright Fitness - on every Fitness page via the app registry (apps_registry: Fitness's script). Vanilla JS;
 * every page works without it (each form posts normally).
 *
 *   sets       each change saves at once (views.update_set); the set number cycles working -> warm-up -> drop;
 *              the done box saves and starts the rest timer
 *   + Set      a copy of the set before, added in place (views.add_set)
 *   rest timer at the bottom: 90 s by default, ±15 s (remembered on this device), Skip; vibrates at zero where
 *              the phone can
 *   filters    the exercise library's menus apply on change
 *   chart      hover / tap a point (or move along the line) for its value
 *   board      the challenge leaderboard (#fx-board) refreshes every 15s
 *   rules      on Edit rules / New challenge, a two-step activity + measure picker
 *              (the grouped <select name="activity"> still works without JS)
 *   log card   "Log this exercise": + Set copies the set before it, x removes one, Save posts in the background
 *              and shows what was saved and any new records; on the exercise page Progress and Records update
 *   sheet      a Log card opened from a list (Exercises' Log buttons, the Today page's recent chips) in a bottom
 *              sheet (<dialog>); without JS those links open the exercise page
 *   quick log  the Today page's tabs (the last one used is remembered on this device), the workout text's
 *              preview as you type (with pickers for names it doesn't know), and the food list's search
 *   schedule   the Schedule form shows only the repeat fields that apply
 *   terms      the "?" next to a term (fitness_extras.term) opens its note; a tap elsewhere or Escape closes it
 *   weeks      Insights' sets-per-muscle table starts scrolled to the latest weeks
 *   delete     a program's "Delete program" (its "…" menu) opens the confirmation dialog with that program's name
 *              (the checkbox checked again); without JS the link opens the confirmation page
 */
(function () {
    "use strict";

    function csrf() {
        var m = document.cookie.match(/csrftoken=([^;]+)/);
        if (m) return m[1];
        var input = document.querySelector("input[name=csrfmiddlewaretoken]");
        return input ? input.value : "";
    }
    function post(url, body) {
        return fetch(url, {method: "POST", credentials: "same-origin", body: body,
                           headers: {"X-CSRFToken": csrf(), "X-Requested-With": "XMLHttpRequest"}})
            .then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); });
    }
    function store(key, value) {
        try { if (value === undefined) return window.localStorage.getItem(key); window.localStorage.setItem(key, value); }
        catch (e) { return null; }
        return null;
    }

    // ---------- Sets: save on change ----------
    function save(form) {
        form.classList.add("is-saving");
        return post(form.action, new FormData(form)).catch(function () {
            form.closest(".fx-set").classList.add("is-error");
        }).then(function (res) { form.classList.remove("is-saving"); return res; });
    }
    document.addEventListener("change", function (event) {
        var form = event.target.closest && event.target.closest("[data-set-form]");
        if (form && window.fetch) { save(form); return; }
        if (event.target.matches && event.target.matches("[data-autosubmit]")) event.target.form.submit();
    });

    var TYPES = ["working", "warmup", "drop"];
    document.addEventListener("click", function (event) {
        var type = event.target.closest && event.target.closest("[data-set-type]");
        if (type && window.fetch) {
            var form = type.closest("form"), row = type.closest(".fx-set");
            var next = TYPES[(TYPES.indexOf(type.getAttribute("data-set-type")) + 1) % TYPES.length];
            type.setAttribute("data-set-type", next);
            form.elements.set_type.value = next;
            row.classList.remove("is-warmup", "is-drop");
            if (next !== "working") row.classList.add("is-" + next);
            type.textContent = next === "warmup" ? "W" : next === "drop" ? "D" : String(Array.prototype.indexOf.call(row.parentNode.children, row) + 1);
            save(form);
            return;
        }
        var done = event.target.closest && event.target.closest("[data-done]");
        if (done && window.fetch) {
            event.preventDefault();
            var f = done.closest("form"), now = done.getAttribute("aria-checked") !== "true";
            f.elements.done.value = now ? "1" : "0";
            done.setAttribute("aria-checked", String(now));
            done.closest(".fx-set").classList.toggle("is-done", now);
            save(f).then(function (res) {
                if (!res || !res.ok) {
                    done.setAttribute("aria-checked", String(!now));
                    done.closest(".fx-set").classList.toggle("is-done", !now);
                    f.elements.done.value = now ? "0" : "1";
                }
            });
            if (now) startTimer();
            return;
        }
        var timerButton = event.target.closest && event.target.closest("[data-timer]");
        if (timerButton) timerAction(timerButton.getAttribute("data-timer"));
    });

    // ---------- + Set ----------
    document.addEventListener("submit", function (event) {
        var form = event.target;
        if (!form.matches || !form.matches("[data-add-set]") || !window.fetch) return;
        event.preventDefault();
        post(form.action, new FormData(form)).then(function (res) {
            var list = form.parentNode.querySelector("[data-sets]");
            list.insertAdjacentHTML("beforeend", res.html);
            var first = list.lastElementChild.querySelector(".fx-input");
            if (first) first.focus({preventScroll: false});
        }).catch(function () { form.submit(); });
    });
    // A set's own form (no JS needed for Enter): save in place instead of reloading.
    document.addEventListener("submit", function (event) {
        var form = event.target;
        if (!form.matches || !form.matches("[data-set-form]") || !window.fetch) return;
        event.preventDefault();
        save(form);
    });

    // ---------- Rest timer ----------
    var timer = document.getElementById("fx-timer"), timeText = document.getElementById("fx-timer-time");
    var announce = document.getElementById("fx-timer-done");
    var rest = parseInt(store("fx-rest") || "90", 10) || 90, endAt = 0, ticking = null;
    function show(seconds) {
        var s = Math.max(0, seconds);
        timeText.textContent = Math.floor(s / 60) + ":" + ("0" + (s % 60)).slice(-2);
    }
    function tick() {
        var left = Math.round((endAt - Date.now()) / 1000);
        show(left);
        if (left <= 0) {
            clearInterval(ticking);
            ticking = null;
            timer.classList.add("is-over");
            if (announce) announce.textContent = "Rest over";
            if (navigator.vibrate) navigator.vibrate([250, 120, 250]);
            setTimeout(function () { if (!ticking) timer.hidden = true; }, 4000);
        }
    }
    function startTimer() {
        if (!timer) return;
        endAt = Date.now() + rest * 1000;
        timer.hidden = false;
        timer.classList.remove("is-over");
        if (announce) announce.textContent = "";
        clearInterval(ticking);
        ticking = setInterval(tick, 250);
        tick();
    }
    function timerAction(what) {
        if (what === "skip") { clearInterval(ticking); ticking = null; timer.hidden = true; return; }
        var delta = parseInt(what, 10);
        rest = Math.min(600, Math.max(15, rest + delta));
        store("fx-rest", String(rest));
        endAt += delta * 1000;
        if (!ticking) startTimer(); else tick();
    }

    // ---------- The progress chart: the nearest point to the pointer ----------
    function initChart(box) {
        var plot = box.querySelector("[data-plot]"), tip = box.querySelector("[data-tip]"), cross = box.querySelector("[data-cross]");
        var dots = Array.prototype.slice.call(plot.querySelectorAll("[data-x]"));
        function pick(clientX) {
            var rect = plot.getBoundingClientRect(), x = (clientX - rect.left) / rect.width * 100, best = dots[0];
            dots.forEach(function (d) { if (Math.abs(d.getAttribute("data-x") - x) < Math.abs(best.getAttribute("data-x") - x)) best = d; });
            dots.forEach(function (d) { d.classList.toggle("is-on", d === best); });
            cross.style.left = best.style.left;
            cross.hidden = false;
            tip.textContent = best.getAttribute("data-label");
            tip.style.left = best.style.left;
            tip.style.top = best.style.top;
            tip.hidden = false;
        }
        plot.addEventListener("pointermove", function (e) { pick(e.clientX); });
        plot.addEventListener("pointerdown", function (e) { pick(e.clientX); });
        plot.addEventListener("pointerleave", function () {
            tip.hidden = true;
            cross.hidden = true;
            dots.forEach(function (d) { d.classList.remove("is-on"); });
        });
    }
    document.querySelectorAll("[data-chart]").forEach(initChart);

    // A USDA food's amount (food_usda.html): the macros for what's typed, as you type. data-per100 is calories,
    // protein, carbs, fat, fiber per 100 g; each portion option carries its grams.
    var amountForm = document.getElementById("fx-amount");
    if (amountForm) {
        var per100 = amountForm.getAttribute("data-per100").split(",").map(function (v) { return v === "" ? null : parseFloat(v); });
        var amount = document.getElementById("fx-amount-n"), portion = document.getElementById("fx-portion");
        var preview = document.getElementById("fx-amount-preview");
        var showPreview = function () {
            var option = portion.options[portion.selectedIndex];
            var grams = (parseFloat(amount.value) || 0) * (parseFloat(option.getAttribute("data-grams")) || 0);
            if (!grams) { preview.textContent = ""; return; }
            var part = function (i, unit, label) {
                return per100[i] === null ? null : Math.round(per100[i] * grams / 100) + unit + label;
            };
            preview.textContent = [Math.round(grams) + " g", part(0, " kcal", ""), part(1, " g", " protein"), part(2, " g", " carbs"),
                                   part(3, " g", " fat")].filter(Boolean).join(" \u00b7 ");
        };
        amount.addEventListener("input", showPreview);
        portion.addEventListener("change", function () {
            if (portion.value === "g" && parseFloat(amount.value) < 10) amount.value = 100;
            else if (portion.value !== "g" && parseFloat(amount.value) >= 10) amount.value = 1;
            showPreview();
        });
        showPreview();
    }

    // ---------- Challenge leaderboard poll ----------
    var board = document.getElementById("fx-board");
    if (board && board.dataset.url) {
        var refreshBoard = function () {
            if (document.hidden) return;
            fetch(board.dataset.url, {credentials: "same-origin", headers: {"X-Requested-With": "XMLHttpRequest"}})
                .then(function (r) { return r.ok ? r.json() : null; })
                .then(function (data) {
                    if (data && data.html) board.innerHTML = data.html;
                })
                .catch(function () {});
        };
        setInterval(refreshBoard, 15000);
        document.addEventListener("visibilitychange", function () { if (!document.hidden) refreshBoard(); });
    }

    // ---------- Challenge rule picker (activity, then measure) ----------
    // The form still posts a single <select name="activity"> of catalog keys
    // (optgroups, no-JS). With JS we hide that select and drive it from two
    // dropdowns, prefilling suggested points, per-units, unit and daily cap.
    function setupRulePicker(form) {
        var fallback = form.querySelector("[data-rule-fallback]");
        var activity = form.querySelector("[name=activity]");
        if (!fallback || !activity || activity.tagName !== "SELECT") return;
        var unit = form.querySelector("[name=unit]");
        var points = form.querySelector("[name=points]") || form.querySelector("[name=steps_points]");
        var per = form.querySelector("[name=per_units]") || form.querySelector("[name=steps_per]");
        var cap = form.querySelector("[name=daily_cap]") || form.querySelector("[name=steps_cap]");
        var prefillCap = form.hasAttribute("data-prefill-cap");

        function fillFrom(opt) {
            if (!opt) return;
            if (unit && opt.dataset.unit) unit.value = opt.dataset.unit;
            if (points && opt.dataset.points) points.value = opt.dataset.points;
            if (per && opt.dataset.per) per.value = opt.dataset.per;
            // New-challenge keeps a blank cap (no cap) unless the user types one.
            if (cap && prefillCap) cap.value = opt.dataset.cap || "";
        }

        var kindOrder = [], byKind = {};
        Array.prototype.forEach.call(activity.options, function (opt) {
            if (!opt.value || !opt.dataset.kind) return;
            var kind = opt.dataset.kind;
            if (!byKind[kind]) {
                var text = (opt.textContent || "").trim();
                var cut = text.lastIndexOf(" - ");
                var label = cut === -1 ? text : text.slice(0, cut);
                var group = (opt.parentNode && opt.parentNode.label) || "";
                byKind[kind] = {label: label, group: group, measures: []};
                kindOrder.push(kind);
            }
            byKind[kind].measures.push(opt);
        });
        if (!kindOrder.length) return;

        var kindWrap = document.createElement("div");
        kindWrap.innerHTML = '<label class="fx-label" for="fx-add-kind">Activity</label>' +
            '<select id="fx-add-kind" class="ledger-input"></select>';
        var measWrap = document.createElement("div");
        measWrap.innerHTML = '<label class="fx-label" for="fx-add-measure">Score by</label>' +
            '<select id="fx-add-measure" class="ledger-input"></select>';
        var kindSel = kindWrap.querySelector("#fx-add-kind");
        var measSel = measWrap.querySelector("#fx-add-measure");
        var currentGroup = null, groupEl = null;
        kindOrder.forEach(function (kind) {
            var info = byKind[kind];
            if (info.group && info.group !== currentGroup) {
                currentGroup = info.group;
                groupEl = document.createElement("optgroup");
                groupEl.label = info.group;
                kindSel.appendChild(groupEl);
            }
            var o = document.createElement("option");
            o.value = kind;
            o.textContent = info.label;
            (groupEl || kindSel).appendChild(o);
        });

        function selectedActivity() {
            return activity.options[activity.selectedIndex];
        }
        function syncMeasure() {
            var kind = kindSel.value, opts = (byKind[kind] && byKind[kind].measures) || [];
            measSel.innerHTML = "";
            opts.forEach(function (opt) {
                var o = document.createElement("option");
                o.value = opt.value;
                o.textContent = opt.dataset.measure || opt.textContent;
                measSel.appendChild(o);
            });
            var current = selectedActivity();
            if (current && current.dataset.kind === kind) measSel.value = current.value;
            activity.value = measSel.value;
            fillFrom(selectedActivity());
        }
        kindSel.addEventListener("change", syncMeasure);
        measSel.addEventListener("change", function () {
            activity.value = measSel.value;
            fillFrom(selectedActivity());
        });

        var start = selectedActivity();
        if (start && start.dataset.kind) kindSel.value = start.dataset.kind;
        fallback.hidden = true;
        fallback.parentNode.insertBefore(kindWrap, fallback);
        fallback.parentNode.insertBefore(measWrap, fallback);
        syncMeasure();
        // Re-apply the starting option's suggestions in case the form had
        // generic defaults (create-challenge starts on walk_steps).
        fillFrom(selectedActivity());
    }
    document.querySelectorAll("[data-rule-picker]").forEach(setupRulePicker);

    // ---------- The Log card: + Set, remove, save in the background ----------
    function renumber(list) {
        var rows = list.querySelectorAll("[data-log-row]");
        rows.forEach(function (row, i) {
            var n = row.querySelector("[data-log-n]");
            if (n) n.textContent = String(i + 1);
            var remove = row.querySelector("[data-log-remove]");
            if (remove) remove.hidden = rows.length < 2;
        });
    }
    function initLogCard(card) {
        if (!card || card.getAttribute("data-bound") === "1") return;
        card.setAttribute("data-bound", "1");
        var list = card.querySelector("[data-log-sets]"), add = card.querySelector("[data-log-add]");
        var spare = list && list.querySelector("[data-log-spare]");
        if (spare && list.querySelectorAll("[data-log-row]").length > 1) spare.parentNode.removeChild(spare);
        else if (spare) spare.removeAttribute("data-log-spare");
        if (add) add.hidden = false;
        renumber(list);
    }
    function addSet(card) {
        var list = card.querySelector("[data-log-sets]"), rows = list.querySelectorAll("[data-log-row]");
        var last = rows[rows.length - 1], row;
        if (last) {
            row = last.cloneNode(true);                       // "+ Set" copies the set before it
            var from = last.querySelectorAll("input"), to = row.querySelectorAll("input");
            for (var i = 0; i < to.length; i++) to[i].value = from[i].value;
        } else {
            var tpl = card.querySelector("[data-log-template]");
            row = tpl.content.firstElementChild.cloneNode(true);
        }
        list.appendChild(row);
        renumber(list);
        var first = row.querySelector("input");
        if (first) first.focus();
    }
    function logStatus(card, html, error) {
        var box = card.querySelector("[data-log-status]");
        if (!box) return;
        box.innerHTML = html;
        box.classList.toggle("is-error", !!error);
        box.hidden = false;
    }
    function escapeHtml(text) {
        var d = document.createElement("div");
        d.textContent = text;
        return d.innerHTML;
    }
    document.addEventListener("click", function (event) {
        var addBtn = event.target.closest && event.target.closest("[data-log-add]");
        if (addBtn) { addSet(addBtn.closest("[data-log-card]")); return; }
        var remove = event.target.closest && event.target.closest("[data-log-remove]");
        if (remove) {
            var list = remove.closest("[data-log-sets]"), row = remove.closest("[data-log-row]");
            if (list.querySelectorAll("[data-log-row]").length > 1) { list.removeChild(row); renumber(list); }
        }
    });
    document.addEventListener("submit", function (event) {
        var form = event.target;
        if (!form.matches || !form.matches("[data-log-form]") || !window.fetch) return;
        event.preventDefault();
        var card = form.closest("[data-log-card]"), save = form.querySelector("[type=submit]");
        if (save) save.disabled = true;
        fetch(form.action, {method: "POST", credentials: "same-origin", body: new FormData(form),
                            headers: {"X-CSRFToken": csrf(), "X-Requested-With": "XMLHttpRequest"}})
            .then(function (r) { return r.json().catch(function () { return {ok: false, error: "Not saved - try again."}; }); })
            .then(function (data) {
                if (!data.ok) { logStatus(card, escapeHtml(data.error || "Not saved - try again."), true); return; }
                var html = '<p class="fx-log__done">' + escapeHtml(data.message) +
                    ' <a href="' + data.workout_url + '">Open the workout</a></p>';
                if (data.records && data.records.length) {
                    html += '<ul class="fx-log__prs" aria-label="New records">' + data.records.map(function (t) {
                        return '<li><span aria-hidden="true">&#x1F3C6;</span> ' + escapeHtml(t) + "</li>";
                    }).join("") + "</ul>";
                }
                logStatus(card, html, false);
                if (save && data.added_to_open === false) save.textContent = "Add to today's workout";
                var progress = document.getElementById("fx-progress-box"), records = document.getElementById("fx-records-box");
                if (progress && data.progress_html && !card.closest("#fx-sheet")) {
                    progress.innerHTML = data.progress_html;
                    progress.querySelectorAll("[data-chart]").forEach(initChart);
                }
                if (records && data.records_html && !card.closest("#fx-sheet")) records.innerHTML = data.records_html;
            })
            .catch(function () { logStatus(card, "Not saved - check your connection and try again.", true); })
            .then(function () { if (save) save.disabled = false; });
    });
    document.querySelectorAll("[data-log-card]").forEach(initLogCard);

    // ---------- The bottom sheet: a Log card opened from a list ----------
    var sheet = document.getElementById("fx-sheet");
    document.addEventListener("click", function (event) {
        var opener = event.target.closest && event.target.closest("[data-sheet-url]");
        if (!opener || !sheet || !sheet.showModal || !window.fetch) return;
        event.preventDefault();
        var body = sheet.querySelector("[data-sheet-body]");
        fetch(opener.getAttribute("data-sheet-url"), {credentials: "same-origin", headers: {"X-Requested-With": "XMLHttpRequest"}})
            .then(function (r) { if (!r.ok) throw new Error(r.status); return r.text(); })
            .then(function (html) {
                body.innerHTML = html;
                body.querySelectorAll("[data-log-card]").forEach(initLogCard);
                sheet.showModal();
                var first = body.querySelector(".fx-input");
                if (first) first.focus();
            })
            .catch(function () { window.location.href = opener.href; });
    });
    if (sheet) {
        sheet.addEventListener("click", function (event) {
            if (event.target === sheet || (event.target.closest && event.target.closest("[data-sheet-close]"))) sheet.close();
        });
    }

    // ---------- Quick log: tabs, the workout preview, the food search ----------
    var quick = document.querySelector("[data-quicklog]");
    if (quick) {
        var tabs = Array.prototype.slice.call(quick.querySelectorAll("[data-quick-tab]"));
        var choose = function (key, focus) {
            if (!quick.querySelector('[data-quick-panel="' + key + '"]')) key = "workout";
            tabs.forEach(function (t) {
                var on = t.getAttribute("data-quick-tab") === key;
                t.classList.toggle("is-on", on);
                t.setAttribute("aria-selected", String(on));
                t.tabIndex = on ? 0 : -1;
                if (on && focus) t.focus();
            });
            quick.querySelectorAll("[data-quick-panel]").forEach(function (p) { p.hidden = p.getAttribute("data-quick-panel") !== key; });
            store("fx-quick-tab", key);
        };
        quick.querySelector("[data-quick-tabs]").hidden = false;
        quick.classList.add("has-tabs");
        choose(store("fx-quick-tab") || "workout");
        tabs.forEach(function (t, i) {
            t.addEventListener("click", function () { choose(t.getAttribute("data-quick-tab")); });
            t.addEventListener("keydown", function (e) {
                if (e.key !== "ArrowRight" && e.key !== "ArrowLeft") return;
                var next = tabs[(i + (e.key === "ArrowRight" ? 1 : tabs.length - 1)) % tabs.length];
                choose(next.getAttribute("data-quick-tab"), true);
            });
        });

        var qform = quick.querySelector("[data-quick-form]"), preview = quick.querySelector("[data-quick-preview]"), wait = null;
        var showPreview = function () {
            var text = qform.elements.text.value.trim();
            if (!text) { preview.innerHTML = ""; return; }
            fetch(qform.getAttribute("data-preview-url"), {method: "POST", credentials: "same-origin", body: new FormData(qform),
                                                           headers: {"X-CSRFToken": csrf(), "X-Requested-With": "XMLHttpRequest"}})
                .then(function (r) { return r.ok ? r.json() : null; })
                .then(function (data) { if (data) preview.innerHTML = data.html; })
                .catch(function () {});
        };
        if (qform && preview && window.fetch) {
            qform.elements.text.addEventListener("input", function () { clearTimeout(wait); wait = setTimeout(showPreview, 350); });
            preview.addEventListener("change", function (e) { if (e.target.matches("[data-quick-match]")) showPreview(); });
        }

        var filter = quick.querySelector("[data-food-filter]"), foods = quick.querySelector("[data-food-list]");
        if (filter && foods) {
            filter.addEventListener("input", function () {
                var q = filter.value.trim().toLowerCase(), shown = 0;
                foods.querySelectorAll("[data-food-name]").forEach(function (li) {
                    var hit = !q || li.getAttribute("data-food-name").indexOf(q) !== -1;
                    li.hidden = !hit;
                    if (hit) shown += 1;
                });
                var none = quick.querySelector("[data-food-none]");
                if (none) none.hidden = shown > 0 || !q;
            });
        }
    }

    // ---------- The Schedule form: only the repeat fields that apply ----------
    function syncRepeat(box) {
        var repeat = box.querySelector("[data-repeat]");
        if (!repeat) return;
        box.querySelectorAll("[data-show-for]").forEach(function (el) {
            el.hidden = (" " + el.getAttribute("data-show-for") + " ").indexOf(" " + repeat.value + " ") === -1;
        });
    }
    document.querySelectorAll("[data-repeat-form]").forEach(syncRepeat);
    document.addEventListener("change", function (event) {
        if (event.target.matches && event.target.matches("[data-repeat]")) syncRepeat(event.target.closest("[data-repeat-form]"));
    });

    // ---------- Plain-words help: the "?" next to a term ----------
    // One shared note on <body> (a card can be a containing block that would trap or clip it): under the "?" on a
    // wide screen, a card at the bottom on a phone (fitness.css).
    var termPop = null, termBtn = null;
    function closeTerm() {
        if (termBtn) termBtn.setAttribute("aria-expanded", "false");
        if (termPop) termPop.hidden = true;
        termBtn = null;
    }
    document.addEventListener("click", function (event) {
        var btn = event.target.closest && event.target.closest("[data-term]");
        if (!btn) { if (!(termPop && termPop.contains(event.target))) closeTerm(); return; }
        event.preventDefault();
        if (btn === termBtn) { closeTerm(); return; }
        closeTerm();
        if (!termPop) {
            termPop = document.createElement("div");
            termPop.className = "fx-term__body fx-term__pop";
            termPop.setAttribute("role", "note");
            termPop.id = "fx-term-pop";
            document.body.appendChild(termPop);
        }
        termPop.innerHTML = btn.nextElementSibling.innerHTML;
        termPop.hidden = false;
        btn.setAttribute("aria-expanded", "true");
        btn.setAttribute("aria-controls", "fx-term-pop");
        termBtn = btn;
        var r = btn.getBoundingClientRect();
        termPop.style.top = (r.bottom + 6) + "px";
        termPop.style.left = Math.max(12, Math.min(r.left - 8, window.innerWidth - termPop.offsetWidth - 12)) + "px";
    });
    document.addEventListener("keydown", function (event) { if (event.key === "Escape") closeTerm(); });
    window.addEventListener("scroll", function () { if (window.innerWidth > 600) closeTerm(); }, { passive: true });

    // ---------- Delete a program: the confirmation dialog ----------
    var confirmBox = document.getElementById("fx-program-delete");
    if (confirmBox && confirmBox.showModal) {
        var confirmFrom = null;
        document.addEventListener("click", function (event) {
            var link = event.target.closest && event.target.closest("[data-program-delete]");
            if (!link) return;
            event.preventDefault();
            // Focus goes back to the menu's "…" button - found through the panel (menu.js moves an open panel out of its
            // <details>; the button's aria-controls names it).
            var panel = link.closest("[data-menu-panel]");
            confirmFrom = (panel && panel.id && document.querySelector('[aria-controls="' + panel.id + '"]')) || link;
            confirmBox.querySelector("[data-program-delete-form]").action = link.getAttribute("href");
            confirmBox.querySelector("[data-program-delete-name]").textContent = link.getAttribute("data-name") || "this program";
            confirmBox.querySelector("input[name=remove_scheduled]").checked = true;
            confirmBox.showModal();
            var cancel = confirmBox.querySelector("[data-program-delete-cancel]");
            if (cancel) cancel.focus();                    // the safe choice first
        });
        confirmBox.addEventListener("click", function (event) {
            var r = confirmBox.getBoundingClientRect();
            var outside = event.target === confirmBox && event.clientX !== 0 &&     // the backdrop, not the dialog's own padding
                (event.clientX < r.left || event.clientX > r.right || event.clientY < r.top || event.clientY > r.bottom);
            if (outside || (event.target.closest && event.target.closest("[data-program-delete-cancel]"))) confirmBox.close();
        });
        confirmBox.addEventListener("close", function () {
            if (confirmFrom && document.contains(confirmFrom)) confirmFrom.focus();
        });
    }

    // ---------- Insights' weeks table: start scrolled to the latest weeks (they're on the right) ----------
    document.querySelectorAll(".fx-heat-wrap").forEach(function (box) { box.scrollLeft = box.scrollWidth; });

    // ---------- Fitness Today: "Log weight" opens the weigh-in sheet ----------
    var openWi = null;
    function closeWi() {
        if (!openWi) return;
        var box = openWi.closest(".wi"), button = box && box.querySelector("[data-wi-open]"), back = box && box.querySelector("[data-wi-close]");
        openWi.classList.remove("is-open");
        if (button) button.setAttribute("aria-expanded", "false");
        if (back) back.hidden = true;
        openWi = null;
    }
    function openWiPanel(panel) {
        if (openWi === panel) return;
        closeWi();
        var box = panel.closest(".wi"), button = box.querySelector("[data-wi-open]"), back = box.querySelector("[data-wi-close]");
        panel.classList.add("is-open");
        if (button) button.setAttribute("aria-expanded", "true");
        if (back) back.hidden = false;
        openWi = panel;
        var input = panel.querySelector("input[name=weight]");
        if (input) input.focus();
    }
    document.addEventListener("click", function (event) {
        var opener = event.target.closest && event.target.closest("#fx-today [data-wi-open]");
        if (opener) {
            var panel = document.getElementById(opener.getAttribute("aria-controls"));
            if (panel === openWi) closeWi(); else if (panel) openWiPanel(panel);
            return;
        }
        if (event.target.closest && event.target.closest("#fx-today [data-wi-close]")) { closeWi(); return; }
        if (openWi && !openWi.contains(event.target)) closeWi();
    });
    document.addEventListener("keydown", function (event) {
        if (event.key === "Escape" && openWi) closeWi();
    });
    document.addEventListener("submit", function (event) {
        var form = event.target;
        if (!form.matches || !form.matches("#fx-today [data-wi-form]") || !window.fetch || !window.FormData) return;
        event.preventDefault();
        var body = new FormData(form);
        if (event.submitter && event.submitter.name === "skip") body.set("skip", "1");
        else if (!String(body.get("weight") || "").trim()) {
            var empty = form.querySelector("[data-wi-msg]");
            if (empty) empty.textContent = "Enter your weight, or Skip today.";
            return;
        }
        var msg = form.querySelector("[data-wi-msg]");
        if (msg) msg.textContent = "";
        post(form.action, body).then(function (res) {
            if (res.data && res.data.confirm) {
                if (!window.confirm(res.data.confirm)) return;
                body.set("replace", "1");
                return post(form.action, body);
            }
            return res;
        }).then(function (res) {
            if (!res) return;
            if (!res.ok) { if (msg) msg.textContent = (res.data && res.data.error) || "Couldn't save that - try again."; return; }
            window.location.reload();
        }).catch(function () { if (msg) msg.textContent = "Couldn't save that - try again."; });
    });
})();

// ---------- Programs' guided setup: the exercise type-ahead and the Planner time ----------
(function () {
    "use strict";
    var box = document.querySelector("[data-ex-search]"), list = document.querySelector("[data-ex-results]");
    if (box && list && window.fetch) {
        var input = box.querySelector("input[type=search]"), wait = null, kinds = list.querySelector("select[name=new_kind]");
        var kindOptions = kinds ? kinds.innerHTML : '<option value="strength">Strength</option><option value="cardio">Cardio</option>' +
            '<option value="bodyweight">Bodyweight</option><option value="mobility">Mobility</option>';
        var esc = function (t) { var d = document.createElement("div"); d.textContent = t; return d.innerHTML; };
        var run = function () {
            var q = input.value.trim();
            if (!q) { list.innerHTML = ""; return; }
            fetch(box.getAttribute("data-url") + "?q=" + encodeURIComponent(q), {credentials: "same-origin"})
                .then(function (r) { return r.ok ? r.json() : null; })
                .then(function (data) {
                    if (!data || input.value.trim() !== q) return;
                    var html = data.results.map(function (e) {
                        return '<li class="fx-row"><span class="fx-row__title">' + esc(e.name) + ' <span class="fx-muted fx-small">' +
                            esc(e.kind_label) + (e.mine ? " &middot; mine" : "") + '</span></span><button type="submit" name="action" value="add:' +
                            e.id + '" class="btn btn-secondary btn-sm" aria-label="Add ' + esc(e.name) + '">Add</button></li>';
                    }).join("");
                    if (data.create) {
                        html += '<li class="fx-row fx-ex-create"><input type="hidden" name="new_name" value="' + esc(data.create) +
                            '"><span class="fx-row__title">Not listed? Make “' + esc(data.create) + '”</span><label class="visually-hidden" ' +
                            'for="fx-new-kind">Kind</label><select name="new_kind" id="fx-new-kind" class="ledger-input fx-select fx-select--sm">' +
                            kindOptions + '</select><button type="submit" name="action" value="create" class="btn btn-secondary btn-sm">Add mine</button></li>';
                    }
                    list.innerHTML = html;
                })
                .catch(function () {});
        };
        input.addEventListener("input", function () { clearTimeout(wait); wait = setTimeout(run, 250); });
    }
    document.addEventListener("change", function (event) {
        if (!event.target.matches || !event.target.matches("[data-planner-toggle]")) return;
        var time = event.target.closest("form").querySelector("[data-planner-time]");
        if (time) time.hidden = !event.target.checked;
    });
})();

// ---------- Food: the one search box (live), and the amount panel's numbers as you type ----------
(function () {
    "use strict";
    var FRACTIONS = {"½": " 1/2", "¼": " 1/4", "¾": " 3/4", "⅓": " 1/3", "⅔": " 2/3", "⅛": " 1/8"};
    // The same rules as amounts.parse_quantity: 2, 1.5, .3, 1/4, 1 1/2, 1½ -> a number, else null.
    function parseQuantity(text) {
        text = String(text || "").trim().toLowerCase();
        Object.keys(FRACTIONS).forEach(function (c) { text = text.split(c).join(FRACTIONS[c]); });
        text = text.replace(/,/g, ".").replace(/\s+/g, " ").trim();
        var m = text.match(/^(?:(\d+)(?:\s+|-))?(\d+)\s*\/\s*(\d+)$/), value = null;
        if (m) { if (+m[3]) value = (+(m[1] || 0)) + (+m[2]) / (+m[3]); }
        else if (/^(\d+(\.\d*)?|\.\d+)$/.test(text)) value = parseFloat(text);
        return value && value > 0 && value <= 5000 ? value : null;
    }
    function updatePanel(panel) {
        var menuEl = panel.querySelector("[data-menu]"), qty = panel.querySelector("[data-qty]"), unit = panel.querySelector("[data-unit]");
        if (!menuEl || !qty || !unit) return;
        var menu = JSON.parse(menuEl.textContent), q = parseQuantity(qty.value), row = null;
        menu.forEach(function (u) { if (u.key === unit.value) row = u; });
        panel.querySelectorAll("[data-live]").forEach(function (el) {
            var per = row ? row.per[el.getAttribute("data-live")] : null;
            el.textContent = q && per !== null && per !== undefined ? Math.round(per * q) : "-";
        });
        qty.setAttribute("aria-invalid", q ? "false" : "true");
        var save = panel.querySelector("[data-amount-save]");
        if (save) save.disabled = !q;
    }
    document.addEventListener("input", function (e) {
        var panel = e.target.closest && e.target.closest("[data-amount-panel]");
        if (panel && e.target.matches("[data-qty]")) updatePanel(panel);
    });
    document.addEventListener("change", function (e) {
        var panel = e.target.closest && e.target.closest("[data-amount-panel]");
        if (panel && e.target.matches("[data-unit]")) updatePanel(panel);
    });
    document.addEventListener("click", function (e) {
        var chip = e.target.closest && e.target.closest("[data-qty-chip]");
        if (!chip) return;
        var panel = chip.closest("[data-amount-panel]"), qty = panel.querySelector("[data-qty]");
        qty.value = chip.getAttribute("data-qty-chip");
        updatePanel(panel);
    });

    var find = document.querySelector("[data-food-find]"), results = document.querySelector("[data-find-results]");
    if (!find || !results || !window.fetch) return;
    var input = find.querySelector("input[type=search]"), wait = null;
    var esc = function (t) { var d = document.createElement("div"); d.textContent = t == null ? "" : t; return d.innerHTML; };
    var slot = function () { var c = find.querySelector("[data-find-slot]:checked"); return c ? c.value : ""; };
    var extra = function () { return "?date=" + encodeURIComponent(find.elements.date.value) + "&slot=" + encodeURIComponent(slot()); };
    var run = function () {
        var q = input.value.trim();
        fetch(find.getAttribute("data-url") + extra() + "&format=json&q=" + encodeURIComponent(q), {credentials: "same-origin"})
            .then(function (r) { return r.ok ? r.json() : null; })
            .then(function (data) {
                if (!data || input.value.trim() !== q) return;
                var html = data.groups.map(function (g) {
                    return '<section class="card fx-card fx-find__group"><h2 class="fx-card__title">' + esc(g.title) + '</h2><ul class="fx-list">' +
                        g.items.map(function (i) {
                            var href = esc(i.url + extra());
                            return '<li><a href="' + href + '" class="fx-row fx-row--link" data-sheet-url="' + href + '" data-find-item>' +
                                '<span class="fx-row__title">' + esc(i.label) + '</span><span class="fx-row__ind">' + esc(i.detail) + '</span></a></li>';
                        }).join("") + "</ul></section>";
                }).join("");
                if (!data.groups.length && q) html = '<p class="fx-empty">Nothing called “' + esc(q) + '” yet.</p>';
                if (data.note) html += '<p class="fx-help" id="fx-find-note">' + esc(data.note) + "</p>";
                results.innerHTML = html;
            })
            .catch(function () {});
    };
    input.addEventListener("input", function () { clearTimeout(wait); wait = setTimeout(run, 300); });
    // The meal chips: the links already listed (and "Enter it yourself") follow the chosen meal.
    find.addEventListener("change", function (e) {
        if (!e.target.matches("[data-find-slot]")) return;
        document.querySelectorAll("[data-find-item], [data-find-carry]").forEach(function (a) {
            ["href", "data-sheet-url"].forEach(function (attr) {
                var v = a.getAttribute(attr);
                if (v) a.setAttribute(attr, v.replace(/([?&]slot=)[^&]*/, "$1" + slot()));
            });
        });
    });
})();

// ---------- Body > Weigh-in schedule: only the fields the frequency needs, and a live preview of the next 3 dates ----------
(function () {
    "use strict";
    var form = document.getElementById("fx-wis-form");
    if (!form) return;
    var preview = document.getElementById("fx-wis-preview"), wait = null;
    function frequency() {
        var on = form.querySelector("input[name=frequency]:checked");
        return on ? on.value : "";
    }
    function showFields() {
        var f = frequency();
        form.querySelectorAll("[data-wi-for]").forEach(function (el) {
            var mine = el.getAttribute("data-wi-for") === f;
            el.hidden = !mine;
            el.querySelectorAll("input").forEach(function (i) { i.disabled = !mine; });   // left out of the post
        });
    }
    function refresh() {
        if (!window.fetch || !preview) return;
        var params = new URLSearchParams(new FormData(form));
        params.delete("csrfmiddlewaretoken");
        params.delete("mode");
        fetch(form.getAttribute("data-preview-url") + "?" + params.toString(), {credentials: "same-origin"})
            .then(function (r) { return r.json(); })
            .then(function (d) {
                preview.textContent = d.ok ? (d.dates.length ? "Next: " + d.dates.join(" · ") : "No weigh-ins left before the end date.") : (d.error || "");
            })
            .catch(function () {});
    }
    form.addEventListener("change", function () { showFields(); clearTimeout(wait); wait = setTimeout(refresh, 150); });
    form.addEventListener("input", function () { clearTimeout(wait); wait = setTimeout(refresh, 300); });
    showFields();
    refresh();
})();

/* Swipe left on the Today weigh-in row to reveal Skip. */
(function () {
    var THRESH = 48, RAIL = 88, IGNORE = "a, button, input, textarea, select, label, summary, .skip-pop, .wi";
    var startX = 0, startY = 0, active = null, tracking = false, opened = false;
    function rowOf(node) { return node && node.closest ? node.closest("[data-skip-swipe]") : null; }
    function closeAll(except) {
        document.querySelectorAll("[data-skip-swipe].is-open").forEach(function (row) {
            if (row !== except) row.classList.remove("is-open");
        });
    }
    function onDown(event) {
        if (event.pointerType === "mouse" && event.button !== 0) return;
        var row = rowOf(event.target);
        if (!row || event.target.closest(IGNORE)) return;
        startX = event.clientX; startY = event.clientY;
        active = row; tracking = true; opened = row.classList.contains("is-open");
        try { event.target.setPointerCapture(event.pointerId); } catch (e) {}
    }
    function onMove(event) {
        if (!tracking || !active) return;
        var dx = event.clientX - startX, dy = event.clientY - startY;
        if (Math.abs(dx) < 8 && Math.abs(dy) < 8) return;
        if (Math.abs(dy) > Math.abs(dx)) { tracking = false; return; }
        event.preventDefault();
        var shift = opened ? Math.min(0, Math.max(-RAIL, dx - RAIL)) : Math.min(0, Math.max(-RAIL, dx));
        var content = active.querySelector(".fx-swipe__content, .pl-swipe__content");
        if (content) content.style.transform = "translateX(" + shift + "px)";
    }
    function onUp(event) {
        if (!active) return;
        var dx = event.clientX - startX;
        var content = active.querySelector(".fx-swipe__content, .pl-swipe__content");
        if (content) content.style.transform = "";
        if (tracking && dx < -THRESH) { closeAll(active); active.classList.add("is-open"); }
        else if (tracking && dx > THRESH) active.classList.remove("is-open");
        tracking = false; active = null;
    }
    document.addEventListener("pointerdown", onDown);
    document.addEventListener("pointermove", onMove, { passive: false });
    document.addEventListener("pointerup", onUp);
    document.addEventListener("pointercancel", onUp);
    document.addEventListener("pointerdown", function (event) {
        if (!rowOf(event.target)) closeAll();
    }, true);
})();
