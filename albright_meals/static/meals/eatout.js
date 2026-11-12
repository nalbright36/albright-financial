/*
 * Eat out (albright_meals/eatout.html). Vanilla JS, progressive enhancement.
 *  - Changing a chip or the open-now toggle submits the GET form.
 *  - The "Find places" button still works without this file.
 *  - When weather follows the device, a rough GPS fix is posted to
 *    eat-out/location/ (the same PlannerSettings fields Planner weather uses).
 */
(function () {
    "use strict";
    var page = document.querySelector(".eo-page");
    if (!page) return;
    var form = document.getElementById("eo-form");
    if (form) {
        form.addEventListener("change", function (event) {
            var target = event.target;
            if (!target || !form.contains(target)) return;
            if (target.name === "other") return;
            form.submit();
        });
    }

    if (page.dataset.eoAuto !== "1" || !navigator.geolocation || !window.fetch) return;
    var deniedKey = "eo-geo-denied";
    function token() {
        var match = document.cookie.match(/csrftoken=([^;]+)/);
        var input = document.querySelector("input[name=csrfmiddlewaretoken]");
        return match ? match[1] : (input ? input.value : "");
    }
    function rememberedDenied() {
        try { return localStorage.getItem(deniedKey) === "1"; } catch (e) { return false; }
    }
    function rememberDenied() {
        try { localStorage.setItem(deniedKey, "1"); } catch (e) {}
    }
    function forgetDenied() {
        try { localStorage.removeItem(deniedKey); } catch (e) {}
    }
    function post(lat, lon) {
        var url = page.dataset.eoLocationUrl;
        if (!url) return;
        fetch(url, {
            method: "POST",
            credentials: "same-origin",
            headers: {
                "Content-Type": "application/json",
                "X-CSRFToken": token(),
                "X-Requested-With": "XMLHttpRequest"
            },
            body: JSON.stringify({
                lat: Math.round(lat * 100) / 100,
                lon: Math.round(lon * 100) / 100
            })
        }).then(function (r) { return r.ok ? r.json() : null; })
          .then(function (data) { if (data && data.changed) location.reload(); })
          .catch(function () {});
    }
    function onError(err) {
        if (err && err.code === 1) rememberDenied();
    }
    function ask() {
        navigator.geolocation.getCurrentPosition(
            function (pos) {
                forgetDenied();
                post(pos.coords.latitude, pos.coords.longitude);
            },
            onError,
            {enableHighAccuracy: false, maximumAge: 30 * 60 * 1000, timeout: 10000}
        );
    }
    function maybeAsk(state) {
        if (state === "denied") return;
        if (state === "granted") { forgetDenied(); ask(); return; }
        if (rememberedDenied()) return;
        ask();
    }
    if (navigator.permissions && navigator.permissions.query) {
        navigator.permissions.query({name: "geolocation"}).then(function (p) {
            maybeAsk(p.state);
        }).catch(function () { maybeAsk("prompt"); });
    } else {
        maybeAsk("prompt");
    }
})();
