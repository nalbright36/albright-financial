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
})();
