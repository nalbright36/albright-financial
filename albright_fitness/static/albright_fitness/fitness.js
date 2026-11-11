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
    document.querySelectorAll("[data-chart]").forEach(function (box) {
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
    });

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
})();
