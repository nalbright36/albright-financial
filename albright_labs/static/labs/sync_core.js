// Offline editing: the outbox, shared by the page (labs/sync.js) and the service worker (/sw.js importScripts - its
// Background Sync sends it with no page open). No DOM here. albright_labs.sync is the server's half.
//
// The outbox lives in IndexedDB "albright-offline" (the snapshot's database, store "kv", key "outbox"), tied to the
// user it belongs to - so it goes with the snapshot when someone else signs in, on sign-out and when Offline data is
// turned off. {user, csrf, entries: [...], notices: [...]}; an entry:
//   {id (client_change_id, a UUID), user, action, target (an id, or the client_id of something made offline),
//    client_id (a new item's UUID), payload, base (the note version the edit started from), at (when it was made,
//    ms), label, key (what it changed - the pending dot), restore ({action, payload}: how to make it again if it was
//    deleted elsewhere), failed ({status, message}: kept for Retry / Discard)}
// flush() sends every waiting entry in order to POST /sync/ and reads one result per entry.
(function (root) {
    var DB = "albright-offline", STORE = "kv", KEY = "outbox";
    var URL = "/sync/";

    function open() {
        return new Promise(function (resolve, reject) {
            if (!root.indexedDB) { reject(new Error("no indexedDB")); return; }
            var req = root.indexedDB.open(DB, 1);
            req.onupgradeneeded = function () { req.result.createObjectStore(STORE); };
            req.onsuccess = function () { resolve(req.result); };
            req.onerror = function () { reject(req.error); };
        });
    }
    function read(key) {
        return open().then(function (db) {
            return new Promise(function (resolve) {
                var req = db.transaction(STORE, "readonly").objectStore(STORE).get(key);
                req.onsuccess = function () { db.close(); resolve(req.result || null); };
                req.onerror = function () { db.close(); resolve(null); };
            });
        }).catch(function () { return null; });
    }
    function write(key, value) {
        return open().then(function (db) {
            return new Promise(function (resolve) {
                var tx = db.transaction(STORE, "readwrite");
                tx.objectStore(STORE).put(value, key);
                tx.oncomplete = tx.onerror = function () { db.close(); resolve(); };
            });
        }).catch(function () {});
    }
    function load(user) {
        return read(KEY).then(function (box) {
            if (!box || (user && String(box.user) !== String(user))) return {user: user ? String(user) : "", csrf: "", entries: [], notices: []};
            box.entries = box.entries || [];
            box.notices = box.notices || [];
            return box;
        });
    }
    function save(box) { return write(KEY, box); }

    function uuid() {
        if (root.crypto && root.crypto.randomUUID) return root.crypto.randomUUID();
        var b = new Uint8Array(16);
        (root.crypto || {getRandomValues: function (a) { for (var i = 0; i < a.length; i++) a[i] = Math.random() * 256; return a; }}).getRandomValues(b);
        b[6] = (b[6] & 15) | 64; b[8] = (b[8] & 63) | 128;
        var h = Array.prototype.map.call(b, function (x) { return (x + 256).toString(16).slice(1); }).join("");
        return h.slice(0, 8) + "-" + h.slice(8, 12) + "-" + h.slice(12, 16) + "-" + h.slice(16, 20) + "-" + h.slice(20);
    }
    function waiting(e) { return !e.failed && !e.sending; }

    // One more change: folded into a waiting one where that's the same thing (a second tick, a newer edit, an edit
    // of something made offline), so the outbox stays short and nothing is sent twice.
    var REPLACES = {"planner.check": ["planner.check", "planner.skip"], "planner.skip": ["planner.check", "planner.skip"],
                    "grocery.check": ["grocery.check"], "recipe.rate": ["recipe.rate"], "meal.move": ["meal.move"],
                    "note.edit": ["note.edit", "note.check"]};
    var CREATES = {"planner": "planner.add", "grocery": "grocery.add", "note": "note.create"};
    function queue(entries, entry) {
        var kind = entry.action.split(".")[0], verb = entry.action.split(".")[1];
        var made = entries.filter(function (e) { return waiting(e) && e.action === CREATES[kind] && e.client_id && e.client_id === entry.target; })[0];
        if (made && verb === "delete") {                    // made and deleted offline: nothing to send at all
            return entries.filter(function (e) { return e !== made && !(waiting(e) && e.target === entry.target); });
        }
        if (made && (entry.action === "grocery.edit" || entry.action === "note.edit")) {
            Object.keys(entry.payload || {}).forEach(function (k) { made.payload[k] = entry.payload[k]; });
            return entries;
        }
        var same = REPLACES[entry.action];
        if (same) {
            entries = entries.filter(function (e) {
                if (!waiting(e) || same.indexOf(e.action) < 0 || String(e.target) !== String(entry.target)) return true;
                if (kind === "planner") return (e.payload || {}).date !== (entry.payload || {}).date;
                // The first edit's version stays the base: a newer snapshot must not hide someone else's change.
                if (entry.action === "note.edit" && e.action === "note.edit" && e.base != null) entry.base = e.base;
                return false;
            });
        }
        if (entry.action === "planner.edit") {
            entries = entries.filter(function (e) {
                return !(waiting(e) && e.action === "planner.edit" && String(e.target) === String(entry.target) &&
                         e.payload.field === entry.payload.field && e.payload.date === entry.payload.date);
            });
        }
        entries.push(entry);
        return entries;
    }

    // Send what's waiting. opts: {user, csrf, fetch}. Resolves {sent, applied, conflicts, failed, error, box}.
    function flush(opts) {
        var run = function () { return send(opts); };
        if (root.navigator && root.navigator.locks && root.navigator.locks.request) {
            return root.navigator.locks.request("albright-sync", {ifAvailable: true}, function (lock) {
                return lock ? run() : {sent: 0, busy: true};
            });
        }
        return run();
    }
    function send(opts) {
        return load(opts.user).then(function (box) {
            var batch = box.entries.filter(function (e) { return !e.failed; }).slice(0, 100);
            if (!batch.length || !box.user) return {sent: 0, box: box};
            var csrf = opts.csrf || box.csrf || "";
            var ids = {};
            batch.forEach(function (e) { e.sending = true; ids[e.id] = true; });   // nothing folds into these now
            return save(box).then(function () { return (opts.fetch || root.fetch)(URL, {
                method: "POST", credentials: "same-origin", cache: "no-store", redirect: "error",
                headers: {"Content-Type": "application/json", "X-CSRFToken": csrf, "X-Requested-With": "XMLHttpRequest"},
                body: JSON.stringify({sent_at: Date.now(), changes: batch.map(function (e) {
                    return {id: e.id, user: e.user, action: e.action, target: e.target, client_id: e.client_id,
                            payload: e.payload, base: e.base, at: e.at};
                })})
            }); }).then(function (r) {
                if (!r.ok) throw new Error("status " + r.status);
                return r.json();
            }).catch(function (err) {
                return load(opts.user).then(function (now) {
                    now.entries.forEach(function (e) { if (ids[e.id]) delete e.sending; });
                    return save(now);
                }).then(function () { throw err; });
            }).then(function (reply) {
                if (String(reply.user) !== String(box.user)) throw new Error("someone else is signed in");
                return load(opts.user).then(function (now) {        // re-read: changes made while this was on its way
                    var byId = {}, out = {sent: batch.length, applied: 0, conflicts: [], failed: [], error: 0};
                    (reply.results || []).forEach(function (res) { if (res && res.id) byId[res.id] = res; });
                    now.entries = now.entries.filter(function (e) {
                        var res = byId[e.id];
                        if (ids[e.id]) delete e.sending;
                        if (!res) return true;
                        if (res.status === "applied" || res.status === "skipped") { out.applied++; return false; }
                        if (res.status === "conflict") {
                            out.applied++;
                            var notice = {type: "conflict", id: uuid(), note_id: res.note_id, copy_id: res.copy_id, title: res.title};
                            now.notices.push(notice);
                            out.conflicts.push(notice);
                            return false;
                        }
                        if (res.status === "error") { out.error++; return true; }
                        e.failed = {status: res.status, message: res.message || ""};
                        out.failed.push(e);
                        return true;
                    });
                    out.box = now;
                    return save(now).then(function () { return out; });
                });
            });
        });
    }

    root.AlbrightSyncCore = {load: load, save: save, queue: queue, flush: flush, uuid: uuid, read: read, write: write};
})(typeof self !== "undefined" ? self : window);
