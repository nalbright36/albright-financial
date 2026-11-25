// Offline viewing, read-only - on every page (templates/index.html). The service worker (/sw.js) saves pages and the app
// shell; albright_labs.offline has the server's half. This script:
//   snapshot   keeps /offline/snapshot/ (Planner's next 7 days, groceries, the week's meals, recipes, notes, today's food,
//              recent weights) in IndexedDB "albright-offline" with the user's id: fetched when the app opens online,
//              after a change (the server's albright_changed cookie is newer than the copy) and every 15 minutes while
//              open. A different user's copy is deleted first; signed out or "Offline data" off deletes it.
//   offline    "Offline · showing your data from <time>" at the top; buttons that change data are disabled with
//              "Available when you're back online", and any post is stopped. Back online: the banner goes, the
//              buttons come back, the snapshot is refreshed - and a saved copy of a page is reloaded live. On a
//              section that can be changed offline (Planner, Groceries, Notes, Fitness, Meals - push_views.edit_paths)
//              the banner offers "Edit offline": the offline page draws it from the snapshot, editable
//              (labs/offline_view.js; the changes go through labs/sync.js's outbox).
//   update     a newer worker waiting: "New version available - Refresh" (it takes over, then the page reloads).
//   account    My account > Offline data: when it was last saved, the space it uses, "Clear offline data".
// window.AlbrightOffline: {get, save, clear, refresh, stamp, editable} for labs/offline_view.js and labs/sync.js. A fresh
// snapshot gets the changes that haven't synced yet laid over it (AlbrightSync.overlay) and fires "albright:snapshot".
(function () {
    var body = document.body, meta = document.querySelector('meta[name="albright-offline"]');
    if (!meta) return;
    var user = meta.getAttribute("data-user") || "";
    var on = meta.getAttribute("data-state") === "on";
    var shell = meta.hasAttribute("data-shell");                   // the offline page: shared, holds nobody's data
    var copy = body.hasAttribute("data-offline-copy");             // a saved page the worker showed (offline)
    var snapUrl = meta.getAttribute("data-snapshot") || "/offline/snapshot/";
    if (shell) document.documentElement.classList.add("is-offline-shell");
    var DB = "albright-offline", STORE = "kv", KEY = "snapshot";
    var EVERY = 15 * 60 * 1000, SOON = 20 * 1000;
    var TIP = "Available when you're back online";
    var lastTry = 0, busy = false, offline = false;

    // ---------- IndexedDB ----------
    function open() {
        return new Promise(function (resolve, reject) {
            if (!window.indexedDB) { reject(new Error("no indexedDB")); return; }
            var req = indexedDB.open(DB, 1);
            req.onupgradeneeded = function () { req.result.createObjectStore(STORE); };
            req.onsuccess = function () { resolve(req.result); };
            req.onerror = function () { reject(req.error); };
        });
    }
    function get() {
        return open().then(function (db) {
            return new Promise(function (resolve) {
                var req = db.transaction(STORE, "readonly").objectStore(STORE).get(KEY);
                req.onsuccess = function () { db.close(); resolve(req.result || null); };
                req.onerror = function () { db.close(); resolve(null); };
            });
        }).catch(function () { return null; });
    }
    function put(value) {
        return open().then(function (db) {
            return new Promise(function (resolve) {
                var tx = db.transaction(STORE, "readwrite");
                tx.objectStore(STORE).put(value, KEY);
                tx.oncomplete = tx.onerror = function () { db.close(); resolve(); };
            });
        }).catch(function () {});
    }
    function dropDb() {
        return new Promise(function (resolve) {
            try {
                var req = indexedDB.deleteDatabase(DB);
                req.onsuccess = req.onerror = req.onblocked = function () { resolve(); };
            } catch (e) { resolve(); }
        });
    }
    function worker(msg) {
        if (!navigator.serviceWorker) return;
        navigator.serviceWorker.ready.then(function (reg) { if (reg.active) reg.active.postMessage(msg); }).catch(function () {});
    }
    // Everything this device saved: the snapshot, and the worker's pages and files (the shell stays - it's nobody's).
    function clear() {
        worker({type: "CLEAR"});
        var gone = window.caches ? caches.keys().then(function (keys) {
            return Promise.all(keys.filter(function (k) { return /^albright-(pages|files|meta)$/.test(k); })
                .map(function (k) { return caches.delete(k); }));
        }).catch(function () {}) : Promise.resolve();
        return Promise.all([dropDb(), gone]);
    }

    // ---------- the snapshot ----------
    function changedAt() {
        var m = document.cookie.match(/(?:^|; )albright_changed=(\d+)/);
        return m ? parseInt(m[1], 10) : 0;
    }
    function refresh(force) {
        if (!user || !on || shell || busy) return Promise.resolve(null);
        if (!force && Date.now() - lastTry < SOON) return Promise.resolve(null);
        busy = true;
        lastTry = Date.now();
        return fetch(snapUrl, {credentials: "same-origin", cache: "no-store", headers: {"X-Requested-With": "XMLHttpRequest"}})
            .then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); })
            .then(function (data) {
                busy = false;
                if (data.off) { return clear().then(function () { return null; }); }
                if (String(data.user) !== user) return null;          // signed in as someone else in another tab
                var value = {user: user, saved_at: data.saved_at, saved_ms: Date.now(), data: data};
                if (offline) goOnline();                              // the site answered: we're back
                var laid = window.AlbrightSync ? window.AlbrightSync.overlay(data) : Promise.resolve(data);
                return laid.then(function () { return put(value); }).then(function () {
                    showAccount();
                    document.dispatchEvent(new CustomEvent("albright:snapshot", {detail: value}));
                    return value;
                });
            })
            .catch(function () { busy = false; return null; });
    }
    function check() {
        if (!user || !on || shell || !navigator.onLine) return;
        get().then(function (snap) {
            var stale = !snap || snap.user !== user || changedAt() > (snap.saved_ms || 0) || Date.now() - (snap.saved_ms || 0) >= EVERY;
            if (stale) refresh(false);
        });
    }

    // ---------- offline: banner and disabled buttons ----------
    var EDIT = {exact: [], prefix: []};
    try { EDIT = JSON.parse(meta.getAttribute("data-edit") || "{}"); } catch (e) {}
    function editable(path) {
        return (EDIT.exact || []).indexOf(path) >= 0 || (EDIT.prefix || []).some(function (p) { return path.indexOf(p) === 0; });
    }
    var CONTROLS = ['form[method="post"] button:not([type="button"])', 'form[method="POST"] button:not([type="button"])',
        'form[method="post"] input[type="submit"]', 'form[method="post"] input[type="checkbox"]',
        'form[method="POST"] input[type="checkbox"]', 'input[type="checkbox"][data-url]', '[data-quick-url]',
        '[data-offline-disable]', '.ht__remove', '.fab-dock button', '.fab-dock a', '[data-assistant-open]',
        'a[href$="/new/"]', 'a[href*="/new/?"]', 'a[href*="/edit/"]', 'a[href$="/delete/"]'].join(",");
    function stamp(at) {
        var d = at ? new Date(at) : null;
        if (!d || isNaN(d)) return "earlier";
        var today = new Date();
        var time = d.toLocaleTimeString([], {hour: "numeric", minute: "2-digit"});
        if (d.toDateString() === today.toDateString()) return time;
        return d.toLocaleDateString([], {month: "short", day: "numeric"}) + ", " + time;
    }
    function banner(at) {
        var el = document.getElementById("offline-banner");
        if (!el) {
            el = document.createElement("div");
            el.id = "offline-banner";
            el.className = "offline-banner";
            el.setAttribute("role", "status");
            body.insertBefore(el, body.firstChild);
        }
        el.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" ' +
            'stroke-linecap="round" aria-hidden="true" focusable="false"><path d="M2 8.8A16 16 0 0118.2 6.2M5 12.2a11 11 0 0111.3-1.4M8.4 15.5a6 6 0 015.6-.6"/>' +
            '<circle cx="12" cy="19" r="1.2" fill="currentColor" stroke="none"/><path d="M3 3l18 18"/></svg>' +
            "<span>Offline &middot; showing your data from <time></time></span>";
        el.querySelector("time").textContent = stamp(at);
        if (at) el.querySelector("time").setAttribute("datetime", at);
        if (!shell && editable(window.location.pathname)) {
            var go = document.createElement("button");
            go.type = "button";
            go.className = "offline-banner__edit";
            go.textContent = "Edit offline";
            go.addEventListener("click", function () { window.location.reload(); });   // the worker shows the editable copy
            el.appendChild(go);
        }
        el.hidden = false;
    }
    function disable() {
        document.querySelectorAll(CONTROLS).forEach(function (el) {
            if (el.hasAttribute("data-offline-off")) return;
            el.setAttribute("data-offline-off", el.disabled ? "1" : "0");
            el.setAttribute("data-offline-title", el.getAttribute("title") || "");
            el.setAttribute("title", TIP);
            el.setAttribute("aria-disabled", "true");
            if ("disabled" in el && el.tagName !== "A") el.disabled = true;
        });
    }
    function enable() {
        document.querySelectorAll("[data-offline-off]").forEach(function (el) {
            if ("disabled" in el && el.tagName !== "A") el.disabled = el.getAttribute("data-offline-off") === "1";
            var title = el.getAttribute("data-offline-title");
            if (title) el.setAttribute("title", title); else el.removeAttribute("title");
            el.removeAttribute("aria-disabled");
            el.removeAttribute("data-offline-off");
            el.removeAttribute("data-offline-title");
        });
    }
    function goOffline() {
        offline = true;
        document.documentElement.classList.add("is-offline");
        // The time: a saved page's own; the offline page's comes from the snapshot (offline_view.js sets it).
        var at = meta.getAttribute("data-rendered-at");
        if (shell) get().then(function (snap) { banner(snap && snap.saved_at); });
        else banner(at);
        var toasts = document.getElementById("toast-container");          // a saved page's "Saved" toast is old news
        if (copy && toasts) toasts.parentNode.removeChild(toasts);
        disable();
    }
    function goOnline() {
        if (!offline) return;
        offline = false;
        document.documentElement.classList.remove("is-offline");
        var el = document.getElementById("offline-banner");
        if (el) el.hidden = true;
        enable();
        if (copy || shell) { window.location.reload(); return; }        // a saved copy: show the live page
        refresh(true);
    }
    function blocked(e) {
        var t = document.getElementById("toast-container");
        if (!t) {
            t = document.createElement("div");
            t.className = "toast-container";
            t.id = "toast-container";
            t.setAttribute("role", "status");
            t.setAttribute("aria-live", "polite");
            body.appendChild(t);
        }
        var toast = document.createElement("div");
        toast.className = "toast";
        toast.textContent = TIP;
        t.appendChild(toast);
        setTimeout(function () { if (toast.parentNode) toast.parentNode.removeChild(toast); }, 3500);
        e.preventDefault();
        e.stopImmediatePropagation();
    }
    document.addEventListener("submit", function (e) {
        if (!offline) return;
        var form = e.target;
        if ((form.getAttribute("method") || "get").toLowerCase() === "post") blocked(e);
    }, true);
    document.addEventListener("click", function (e) {
        if (!offline) return;
        var el = e.target.closest && e.target.closest("[data-offline-off]");
        if (el) blocked(e);
    }, true);
    window.addEventListener("offline", goOffline);
    // "online" can fire for nothing (Chrome sends one as a navigation starts, with navigator.onLine still false) and a
    // reload then would cancel the page being opened. So: wait a moment, check again, and ask the site (a HEAD - the
    // worker never answers those) before reloading a saved copy or bringing the buttons back.
    var leaving = false;
    window.addEventListener("pagehide", function () { leaving = true; });
    window.addEventListener("online", function () {
        setTimeout(function () {
            if (leaving || !navigator.onLine) return;
            fetch(window.location.pathname, {method: "HEAD", cache: "no-store", credentials: "same-origin"})
                .then(function (r) { return r.status < 500; }).catch(function () { return false; })
                .then(function (up) {
                    if (!up || leaving) return;
                    if (copy || shell) window.location.reload(); else goOnline();
                });
        }, 800);
    });

    // ---------- My account > Offline data ----------
    function size(bytes) {
        if (!bytes) return "nothing yet";
        if (bytes < 1024 * 1024) return Math.max(1, Math.round(bytes / 1024)) + " KB";
        return (bytes / 1024 / 1024).toFixed(1) + " MB";
    }
    function showAccount() {
        var box = document.getElementById("offline-data");
        if (!box) return;
        var saved = box.querySelector("[data-offline-saved]"), used = box.querySelector("[data-offline-size]");
        get().then(function (snap) {
            if (saved) saved.textContent = snap && snap.user === user ? stamp(snap.saved_at) : "not saved on this device yet";
        });
        if (used) {
            if (navigator.storage && navigator.storage.estimate) {
                navigator.storage.estimate().then(function (e) { used.textContent = size(e.usage); }).catch(function () { used.textContent = "unknown"; });
            } else used.textContent = "unknown";
        }
    }
    var clearBtn = document.querySelector("[data-offline-clear]");
    if (clearBtn) {
        clearBtn.addEventListener("click", function () {
            clearBtn.disabled = true;
            clear().then(function () {
                clearBtn.disabled = false;
                var note = document.querySelector("[data-offline-cleared]");
                if (note) note.hidden = false;
                showAccount();
            });
        });
    }

    // ---------- a new version ----------
    function offerUpdate(waiting) {
        if (document.getElementById("sw-update")) return;
        var bar = document.createElement("div");
        bar.id = "sw-update";
        bar.className = "sw-update";
        bar.setAttribute("role", "status");
        bar.innerHTML = '<span>New version available</span><button type="button" class="btn btn-primary btn-sm">Refresh</button>';
        bar.querySelector("button").addEventListener("click", function () {
            bar.querySelector("button").disabled = true;
            reloadOnChange = true;
            waiting.postMessage({type: "SKIP_WAITING"});
        });
        body.appendChild(bar);
    }
    var reloadOnChange = false;
    if (navigator.serviceWorker) {
        navigator.serviceWorker.addEventListener("controllerchange", function () {
            if (reloadOnChange) { reloadOnChange = false; window.location.reload(); }
        });
        navigator.serviceWorker.addEventListener("message", function (e) {
            if (e.data && e.data.type === "offline-wiped") showAccount();
        });
        navigator.serviceWorker.ready.then(function (reg) {
            if (reg.waiting && navigator.serviceWorker.controller) offerUpdate(reg.waiting);
            reg.addEventListener("updatefound", function () {
                var w = reg.installing;
                if (!w) return;
                w.addEventListener("statechange", function () {
                    if (w.state === "installed" && navigator.serviceWorker.controller) offerUpdate(w);
                });
            });
        }).catch(function () {});
    }

    // ---------- start ----------
    window.AlbrightOffline = {get: get, save: put, clear: clear, refresh: refresh, stamp: stamp, editable: editable};
    if (!shell) {
        if (!user || !on) {
            // Signed out, or Offline data off: nothing of anyone's stays on this device.
            if (window.indexedDB) dropDb();
            if (user && !on) worker({type: "CLEAR"});
        } else {
            worker({type: "USER", user: user});
            get().then(function (snap) {
                if (snap && snap.user !== user) return dropDb();            // someone else's: gone before anything is saved
            }).then(function () {
                var opened = false;
                try { opened = sessionStorage.getItem("albright-offline-opened") === "1"; sessionStorage.setItem("albright-offline-opened", "1"); } catch (e) {}
                if (!opened && navigator.onLine) refresh(true); else check();
            });
            setInterval(function () {
                if (document.visibilityState === "visible") {
                    if (offline && copy) refresh(true); else check();      // a saved copy: is the site back?
                }
            }, 60 * 1000);
            document.addEventListener("visibilitychange", function () { if (document.visibilityState === "visible") check(); });
        }
    }
    if (!navigator.onLine || copy) goOffline();
    showAccount();
})();
