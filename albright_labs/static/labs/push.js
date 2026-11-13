// Web Push + service worker (templates/index.html, My account > Notifications).
// Registers /sw.js on every page so the offline fallback works. On the Notifications
// page, the This device switch (albright_labs.push_views) turns banners on or off and
// marks "This device" in the list. iPhone in a Safari tab is told to Add to Home Screen
// first; permission denied, unsupported browsers, and missing VAPID keys have their own copy.
(function () {
    var SW_URL = "/sw.js";

    if (navigator.serviceWorker) {
        navigator.serviceWorker.register(SW_URL, { scope: "/" }).catch(function () {});
    }

    var box = document.getElementById("notif-device");
    if (!box) return;

    var switchBtn = document.getElementById("notif-push-switch");
    var statusEl = box.querySelector("[data-push-status]");
    var vapid = (box.getAttribute("data-vapid") || "").trim();
    var subscribeUrl = box.getAttribute("data-subscribe");
    var unsubscribeUrl = box.getAttribute("data-unsubscribe");

    function csrf() {
        var input = document.querySelector("[name=csrfmiddlewaretoken]");
        if (input && input.value) return input.value;
        var match = document.cookie.match(/(?:^|; )csrftoken=([^;]+)/);
        return match ? decodeURIComponent(match[1]) : "";
    }

    function isIos() {
        var ua = navigator.userAgent || "";
        return /iPhone|iPad|iPod/.test(ua) || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
    }

    function isStandalone() {
        return (window.navigator.standalone === true) ||
            (window.matchMedia && window.matchMedia("(display-mode: standalone)").matches);
    }

    function supported() {
        return !!(navigator.serviceWorker && window.PushManager && window.Notification);
    }

    function b64ToBytes(b64) {
        b64 = b64.replace(/-/g, "+").replace(/_/g, "/");
        var pad = "=".repeat((4 - b64.length % 4) % 4);
        var raw = atob(b64 + pad);
        var out = new Uint8Array(raw.length);
        for (var i = 0; i < raw.length; i++) out[i] = raw.charCodeAt(i);
        return out;
    }

    function hidePanels() {
        box.querySelectorAll(".notif-push-panel").forEach(function (el) { el.hidden = true; });
    }

    function show(name) {
        hidePanels();
        var panel = box.querySelector("[data-push-" + name + "]");
        if (panel) panel.hidden = false;
    }

    function setSwitch(on, disabled) {
        if (!switchBtn) return;
        switchBtn.classList.toggle("is-on", !!on);
        switchBtn.setAttribute("aria-pressed", on ? "true" : "false");
        switchBtn.disabled = !!disabled;
        if (statusEl) {
            statusEl.className = "notif-device-status " + (on ? "is-on" : "is-off");
        }
    }

    function setStatus(on, text) {
        if (!statusEl) return;
        statusEl.innerHTML = '<span class="notif-device-status__dot"></span>' + text;
        statusEl.className = "notif-device-status " + (on ? "is-on" : "is-off");
    }

    function markThisDevice(endpoint) {
        box.querySelectorAll(".notif-device").forEach(function (row) {
            var isThis = !!(endpoint && row.getAttribute("data-endpoint") === endpoint);
            row.classList.toggle("is-current", isThis);
            var tag = row.querySelector("[data-this-device]");
            if (tag) tag.hidden = !isThis;
        });
    }

    function post(url, body) {
        return fetch(url, {
            method: "POST",
            credentials: "same-origin",
            headers: {
                "Content-Type": "application/json",
                "Accept": "application/json",
                "X-Requested-With": "XMLHttpRequest",
                "X-CSRFToken": csrf()
            },
            body: JSON.stringify(body || {})
        }).then(function (r) {
            if (!r.ok) throw new Error(r.status);
            return r.json();
        });
    }

    function registration() {
        return navigator.serviceWorker.ready;
    }

    function currentSubscription() {
        if (!supported()) return Promise.resolve(null);
        return registration().then(function (reg) { return reg.pushManager.getSubscription(); });
    }

    function applyState(sub) {
        var endpoint = sub && sub.endpoint;
        markThisDevice(endpoint);
        if (!vapid) {
            setSwitch(false, true);
            setStatus(false, "Not available here");
            show("unavailable");
            return;
        }
        if (isIos() && !isStandalone()) {
            setSwitch(false, true);
            setStatus(false, "Needs the home-screen app");
            show("ios");
            return;
        }
        if (!supported()) {
            setSwitch(false, true);
            setStatus(false, "Not available here");
            show("unsupported");
            return;
        }
        if (window.Notification && Notification.permission === "denied") {
            setSwitch(false, true);
            setStatus(false, "Permission blocked");
            show("denied");
            return;
        }
        if (sub) {
            setSwitch(true, false);
            setStatus(true, "On for this device");
            show("on");
        } else {
            setSwitch(false, false);
            setStatus(false, "Off on this device");
            show("off");
        }
    }

    function turnOn() {
        if (!vapid || !supported()) return Promise.reject();
        return registration().then(function (reg) {
            return Notification.requestPermission().then(function (permission) {
                if (permission !== "granted") {
                    applyState(null);
                    return null;
                }
                return reg.pushManager.subscribe({
                    userVisibleOnly: true,
                    applicationServerKey: b64ToBytes(vapid)
                });
            });
        }).then(function (sub) {
            if (!sub) return;
            return post(subscribeUrl, { endpoint: sub.endpoint, keys: {
                p256dh: b64encode(sub.getKey("p256dh")),
                auth: b64encode(sub.getKey("auth"))
            }}).then(function () { return sub; });
        }).then(function (sub) {
            applyState(sub || null);
            if (sub) location.reload();  // refresh the device list with this device on it
        }).catch(function () {
            applyState(null);
        });
    }

    function b64encode(buf) {
        if (!buf) return "";
        var bytes = new Uint8Array(buf);
        var s = "";
        for (var i = 0; i < bytes.length; i++) s += String.fromCharCode(bytes[i]);
        return btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
    }

    function turnOff() {
        return currentSubscription().then(function (sub) {
            var endpoint = sub && sub.endpoint;
            var unsub = sub ? sub.unsubscribe() : Promise.resolve();
            return unsub.then(function () {
                if (endpoint) return post(unsubscribeUrl, { endpoint: endpoint });
            });
        }).then(function () {
            applyState(null);
            location.reload();
        }).catch(function () { applyState(null); });
    }

    if (switchBtn) {
        switchBtn.addEventListener("click", function () {
            if (switchBtn.disabled) return;
            var on = switchBtn.classList.contains("is-on");
            switchBtn.disabled = true;
            (on ? turnOff() : turnOn()).finally(function () { switchBtn.disabled = false; });
        });
    }

    box.querySelectorAll("[data-push-remove]").forEach(function (form) {
        form.addEventListener("submit", function (e) {
            var row = form.closest(".notif-device");
            if (row && row.classList.contains("is-current")) {
                e.preventDefault();
                turnOff();
                return;
            }
            e.preventDefault();
            post(form.action, {}).then(function () { row && row.remove(); }).catch(function () { form.submit(); });
        });
    });

    // First paint: decide the state (async if we can read the current subscription).
    if (!vapid) {
        applyState(null);
        return;
    }
    if (isIos() && !isStandalone()) {
        applyState(null);
        return;
    }
    if (!supported()) {
        applyState(null);
        return;
    }
    currentSubscription().then(applyState).catch(function () { applyState(null); });
})();
