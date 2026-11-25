// Offline editing, the page's half - on every page after labs/offline.js (the outbox itself: labs/sync_core.js; the
// server: albright_labs.sync). window.AlbrightSync.write(action, {target, client_id, payload, base, label, key,
// restore}) is the one way a change is made offline (labs/offline_view.js): it shows at once - applied to the
// IndexedDB snapshot (APPLY below, also re-applied over every fresh snapshot until it has synced) - goes into the
// outbox, and is sent right away when online.
//   sync      on "online", when the app opens or comes back into view, after each change, every so often (with
//             backoff) while anything waits, and by Background Sync (/sw.js) where the browser has it. After a sync
//             the snapshot is fetched again.
//   status    the header pill: "Offline · 3 changes waiting", "Syncing…", "2 didn't sync", nothing when all synced;
//             tap it for the list (Retry / Discard; Restore / Discard for something deleted on another device).
//   conflicts a note changed elsewhere while edited offline comes back as "<title> (offline copy)": a prompt to
//             compare and keep mine, theirs or both (note.resolve).
//   sign-out  with changes still waiting: "3 changes haven't synced yet" - Sync now / Log out anyway.
(function () {
    var Core = window.AlbrightSyncCore, Offline = window.AlbrightOffline;
    var meta = document.querySelector('meta[name="albright-offline"]');
    var api = {write: function () { return Promise.reject(new Error("offline editing is off")); }, overlay: function (d) { return Promise.resolve(d); },
               pending: function () { return Promise.resolve([]); }, keys: function () { return {}; }, enabled: false};
    window.AlbrightSync = api;
    if (!Core || !Offline || !meta || !window.indexedDB) return;
    var shell = meta.hasAttribute("data-shell");
    var user = meta.getAttribute("data-user") || "";
    if (!shell && (!user || meta.getAttribute("data-state") !== "on")) return;     // signed out / Offline data off
    var notesUrl = meta.getAttribute("data-notes") || "/notes/";
    var ready = shell ? Offline.get().then(function (snap) { user = snap && snap.user ? String(snap.user) : ""; }) : Promise.resolve();
    var chain = Promise.resolve(), box = {entries: [], notices: []}, syncing = false, retryTimer = null, tries = 0;
    var keys = {};

    function csrf() {
        var m = document.cookie.match(/(?:^|; )csrftoken=([^;]+)/);
        return m ? decodeURIComponent(m[1]) : "";
    }
    function online() { return navigator.onLine !== false; }
    function plural(n, word) { return window.plural ? window.plural(n, word) : n + " " + word + (n === 1 ? "" : "s"); }

    // ---------- what each change does to the snapshot (so the screen shows it before it syncs) ----------
    function find(list, id) { return (list || []).filter(function (x) { return String(x.id) === String(id); }); }
    function toggleLine(body, index, checked) {
        var n = -1, fenced = false;
        return (body || "").split("\n").map(function (line) {
            if (/^\s*(```|~~~)/.test(line)) fenced = !fenced;
            if (!fenced && /^\s*[-*+]\s+\[[ xX]\]/.test(line) && ++n === index) {
                return line.replace(/\[[ xX]\]/, checked ? "[x]" : "[ ]");
            }
            return line;
        }).join("\n");
    }
    var APPLY = {
        "planner.add": function (d, e) {
            if (!d.planner) return;
            var p = e.payload;
            d.planner.items.push({id: e.client_id, occurrence: p.date, date: p.date, title: p.title, start: p.time || null,
                end: null, kind: "task", checkable: true, done: false, skipped: false, notes: p.notes || "", repeats: false,
                workout: false, weigh_in: false, editable: true, owner: true, checklist: []});
        },
        "planner.check": function (d, e) {
            if (!d.planner) return;
            d.planner.items.forEach(function (o) {
                if (String(o.id) === String(e.target) && o.occurrence === e.payload.date) { o.done = !!e.payload.done; if (!o.done) o.skipped = false; }
            });
        },
        "planner.skip": function (d, e) {
            if (!d.planner) return;
            d.planner.items = d.planner.items.filter(function (o) {
                if (String(o.id) !== String(e.target) || o.occurrence !== e.payload.date) return true;
                if (o.workout || o.weigh_in) { o.done = true; o.skipped = true; return true; }
                return false;
            });
        },
        "planner.edit": function (d, e) {
            if (!d.planner) return;
            var p = e.payload;
            d.planner.items.forEach(function (o) {
                if (String(o.id) !== String(e.target)) return;
                if (p.field === "title" || p.field === "notes") o[p.field] = p.value;
                else if (o.occurrence === p.date) {
                    if (p.field === "date") { o.date = p.value; if (!o.repeats) o.occurrence = p.value; }
                    if (p.field === "time") { o.start = p.start || null; o.end = p.end || null; }
                }
            });
        },
        "planner.checklist": function (d, e) {
            if (!d.planner) return;
            d.planner.items.forEach(function (o) {
                if (String(o.id) === String(e.target) && o.occurrence === e.payload.date) {
                    find(o.checklist, e.payload.row).forEach(function (r) { r.done = !!e.payload.done; });
                }
            });
        },
        "planner.delete": function (d, e) {
            if (!d.planner) return;
            d.planner.items = d.planner.items.filter(function (o) {
                return String(o.id) !== String(e.target) || (e.payload.scope === "this" && o.occurrence !== e.payload.date);
            });
        },
        "grocery.add": function (d, e) {
            if (!d.groceries) return;
            d.groceries.push({id: e.client_id, name: e.payload.line, quantity: "", unit: "", aisle: "other", note: "", checked: false});
        },
        "grocery.check": function (d, e) { find(d.groceries, e.target).forEach(function (i) { i.checked = !!e.payload.checked; }); },
        "grocery.edit": function (d, e) {
            find(d.groceries, e.target).forEach(function (i) { i.name = e.payload.line; i.quantity = ""; i.unit = ""; i.note = ""; });
        },
        "grocery.delete": function (d, e) {
            if (d.groceries) d.groceries = d.groceries.filter(function (i) { return String(i.id) !== String(e.target); });
        },
        "note.create": function (d, e) {
            if (!d.notes) return;
            d.notes.unshift({id: e.client_id, title: e.payload.title || "Untitled", body: e.payload.body || "", pinned: false,
                tags: [], updated_at: new Date(e.at).toISOString(), version: 0, complete: true, check_ids: []});
        },
        "note.edit": function (d, e) {
            find(d.notes, e.target).forEach(function (n) {
                if (typeof e.payload.title === "string") n.title = e.payload.title;
                if (typeof e.payload.body === "string") n.body = e.payload.body;
            });
        },
        "note.check": function (d, e) {
            find(d.notes, e.target).forEach(function (n) { n.body = toggleLine(n.body, e.payload.index, !!e.payload.checked); });
        },
        "note.delete": function (d, e) {
            if (d.notes) d.notes = d.notes.filter(function (n) { return String(n.id) !== String(e.target); });
        },
        "food.log": function (d, e) {
            var p = e.payload;
            if (!d.food || d.food.date !== p.date) return;
            var slot = (d.food.slots || []).filter(function (s) { return s[0] === p.slot; })[0];
            d.food.entries.push({id: e.client_id, slot: slot ? slot[1] : p.slot, label: p.label || "Quick add",
                calories: p.calories ? Math.round(+p.calories) : null, protein: p.protein ? Math.round(+p.protein) : null});
            d.food.calories = (d.food.calories || 0) + (p.calories ? Math.round(+p.calories) : 0);
            d.food.protein = (d.food.protein || 0) + (p.protein ? Math.round(+p.protein) : 0);
        },
        "weight.log": function (d, e) {
            if (!d.weights) return;
            var p = e.payload;
            d.weights.entries = d.weights.entries.filter(function (w) { return w.date !== p.date; });
            d.weights.entries.unshift({date: p.date, weight: String(p.weight)});
            d.weights.entries.sort(function (a, b) { return a.date < b.date ? 1 : -1; });
        },
        "recipe.rate": function (d, e) {
            var level = e.payload.level ? +e.payload.level : null;
            var info = (d.rating_levels || []).filter(function (l) { return l.level === level; })[0];
            find(d.recipes, e.target).forEach(function (r) { r.rating_level = level; r.rating = info ? info.label : null; });
        },
        "meal.move": function (d, e) {
            if (!d.meal_plan) return;
            find(d.meal_plan.entries, e.target).forEach(function (m) { m.date = e.payload.date; });
        }
    };
    APPLY["planner.restore"] = APPLY["planner.add"];
    APPLY["grocery.restore"] = APPLY["grocery.add"];
    APPLY["note.restore"] = function (d, e) {
        if (!d.notes || find(d.notes, e.target).length) return;
        APPLY["note.create"](d, {client_id: e.target, payload: e.payload, at: e.at});
    };
    function applyOne(data, e) {
        var fn = APPLY[e.action];
        if (!fn || e.failed) return;
        try { fn(data, e); } catch (err) { /* a change for a part this snapshot doesn't have */ }
    }
    // A fresh snapshot (labs/offline.js) with what hasn't synced yet laid over it.
    function overlay(data) {
        return ready.then(function () { return Core.load(user); }).then(function (b) {
            if (data && String(data.user) === String(user)) b.entries.forEach(function (e) { applyOne(data, e); });
            return data;
        });
    }

    // ---------- writing ----------
    function write(action, o) {
        o = o || {};
        var made = chain.then(function () { return ready; }).then(function () {
            if (!user) throw new Error("nothing saved on this device");
            var entry = {id: Core.uuid(), user: user, action: action, target: o.target != null ? o.target : null,
                         client_id: o.client_id || null, payload: o.payload || {}, base: o.base != null ? o.base : null,
                         at: Date.now(), label: o.label || action, key: o.key || "", restore: o.restore || null};
            return Promise.all([Offline.get(), Core.load(user)]).then(function (r) {
                var snap = r[0], b = r[1];
                b.user = user;
                b.csrf = csrf() || b.csrf;
                b.entries = Core.queue(b.entries, entry);
                var jobs = [Core.save(b)];
                if (snap && snap.data && String(snap.user) === String(user)) {
                    applyOne(snap.data, entry);
                    jobs.push(Offline.save(snap));
                }
                return Promise.all(jobs).then(function () {
                    changed(b);
                    if (online()) soon(400);
                    background();
                    return snap;
                });
            });
        });
        chain = made.catch(function () {});
        return made;
    }

    // ---------- syncing ----------
    function background() {
        if (!navigator.serviceWorker) return;
        navigator.serviceWorker.ready.then(function (reg) {
            if (reg.sync && reg.sync.register) return reg.sync.register("albright-sync");
        }).catch(function () {});
    }
    var soonTimer = null;
    function soon(ms) {
        clearTimeout(soonTimer);
        soonTimer = setTimeout(syncNow, ms || 0);
    }
    function backoff() {
        clearTimeout(retryTimer);
        var delay = Math.min(2000 * Math.pow(2, tries++), 5 * 60 * 1000);
        retryTimer = setTimeout(syncNow, delay);
    }
    function syncNow() {
        return ready.then(function () {
            if (syncing || !user) return null;
            var waitingNow = box.entries.filter(function (e) { return !e.failed; }).length;
            if (!waitingNow) return null;
            if (!online()) { paint(); return null; }
            syncing = true;
            paint();
            return Core.flush({user: user, csrf: csrf()}).then(function (out) {
                syncing = false;
                if (out && out.busy) { soon(3000); return out; }         // another tab is sending it
                if (out && out.box) changed(out.box);
                if (out && out.error) backoff(); else tries = 0;
                if (out && out.applied) Offline.refresh(true);
                if (out && out.failed && out.failed.length) {
                    toast(out.failed.length === 1 ? (out.failed[0].failed.message || "A change didn't sync.")
                        : plural(out.failed.length, "change") + " didn't sync - tap the status to see them.");
                }
                if (out && out.conflicts && out.conflicts.length) showNotices();
                return out;
            }).catch(function () {
                syncing = false;
                paint();
                backoff();
                return null;
            });
        });
    }

    // ---------- the outbox's state on the page ----------
    function changed(b) {
        box = b || box;
        keys = {};
        box.entries.forEach(function (e) { if (e.key) keys[e.key] = e.failed ? "failed" : "pending"; });
        paint();
        document.dispatchEvent(new CustomEvent("albright:outbox", {detail: {keys: keys}}));
    }
    var pill = null;
    function paint() {
        var waitingN = box.entries.filter(function (e) { return !e.failed; }).length;
        var failedN = box.entries.length - waitingN, review = box.notices.length;
        var text = "", short = "";
        if (syncing) { text = "Syncing…"; short = "Syncing"; }
        else if (waitingN) { text = (online() ? "" : "Offline · ") + plural(waitingN, "change") + " waiting"; short = waitingN + " waiting"; }
        if (!syncing && failedN) { text = (text ? text + " · " : "") + failedN + " didn't sync"; short = failedN + " failed"; }
        if (!syncing && review) { text = (text ? text + " · " : "") + review + " to review"; short = short || "Review"; }
        if (!pill) {
            pill = document.createElement("button");
            pill.type = "button";
            pill.className = "sync-pill";
            pill.id = "sync-pill";
            pill.setAttribute("aria-haspopup", "dialog");
            pill.addEventListener("click", showList);
            var row = document.querySelector(".navbar__main-row"), toggle = document.getElementById("nav-toggle");
            if (row && toggle) row.insertBefore(pill, toggle); else if (row) row.appendChild(pill); else document.body.appendChild(pill);
        }
        pill.hidden = !text;
        pill.textContent = "";
        pill.appendChild(el("span", "sync-pill__long", text));
        pill.appendChild(el("span", "sync-pill__short", short));     // phones: the header has no room for the sentence
        pill.setAttribute("aria-label", text);
        pill.title = text;
        pill.classList.toggle("is-syncing", syncing);
        pill.classList.toggle("is-failed", !syncing && (failedN > 0 || review > 0));
    }

    // ---------- dialogs ----------
    function el(tag, cls, text) {
        var e = document.createElement(tag);
        if (cls) e.className = cls;
        if (text != null) e.textContent = text;
        return e;
    }
    function button(text, cls, fn) {
        var b = el("button", "btn btn-sm " + (cls || "btn-secondary"), text);
        b.type = "button";
        b.addEventListener("click", fn);
        return b;
    }
    function dialog(title) {
        var old = document.getElementById("sync-dialog");
        if (old) old.parentNode.removeChild(old);
        var d = el("dialog", "sync-dialog");
        d.id = "sync-dialog";
        var head = el("div", "sync-dialog__head");
        head.appendChild(el("h2", "sync-dialog__title", title));
        var x = button("×", "sync-dialog__close", function () { d.close(); });
        x.setAttribute("aria-label", "Close");
        head.appendChild(x);
        d.appendChild(head);
        var bodyEl = el("div", "sync-dialog__body");
        d.appendChild(bodyEl);
        d.addEventListener("close", function () { if (d.parentNode) d.parentNode.removeChild(d); });
        document.body.appendChild(d);
        return {dialog: d, body: bodyEl, open: function () { if (d.showModal) d.showModal(); else d.setAttribute("open", ""); }};
    }
    function update(fn) {
        return Core.load(user).then(function (b) { fn(b); return Core.save(b).then(function () { changed(b); return b; }); });
    }
    function retry(e) {
        return update(function (b) {
            b.entries.forEach(function (x) { if (x.id === e.id) { delete x.failed; x.id = Core.uuid(); } });   // a new id: the old one is recorded
        }).then(function () { soon(0); });
    }
    function discard(e) {
        return update(function (b) { b.entries = b.entries.filter(function (x) { return x.id !== e.id; }); })
            .then(function () { if (online()) Offline.refresh(true); });
    }
    function restore(e) {
        if (!e.restore) return discard(e);
        return update(function (b) { b.entries = b.entries.filter(function (x) { return x.id !== e.id; }); }).then(function () {
            return write(e.restore.action, {target: e.restore.target != null ? e.restore.target : null, client_id: Core.uuid(),
                                            payload: e.restore.payload, label: "Restore " + (e.restore.name || "item"), key: e.key});
        });
    }
    function showList() {
        var d = dialog("Changes made offline");
        var list = el("ul", "sync-list");
        box.entries.forEach(function (e) {
            var li = el("li", "sync-list__item" + (e.failed ? " is-failed" : ""));
            var main = el("div", "sync-list__main");
            main.appendChild(el("span", "sync-list__label", e.label));
            var when = new Date(e.at);
            var status = e.failed ? (e.failed.message || "Didn't sync")
                : (syncing ? "Syncing…" : online() ? "Waiting to sync" : "Waiting for a connection");
            main.appendChild(el("span", "sync-list__meta", when.toLocaleTimeString([], {hour: "numeric", minute: "2-digit"}) + " · " + status));
            li.appendChild(main);
            if (e.failed) {
                var acts = el("div", "sync-list__acts");
                if (e.failed.status === "deleted") acts.appendChild(button("Restore", "btn-primary", function () { restore(e).then(showList); }));
                else acts.appendChild(button("Retry", "btn-primary", function () { retry(e).then(showList); }));
                acts.appendChild(button("Discard", "btn-secondary", function () { discard(e).then(showList); }));
                li.appendChild(acts);
            }
            list.appendChild(li);
        });
        if (!box.entries.length) d.body.appendChild(el("p", "sync-dialog__text", box.notices.length ? "Everything has synced." : "Everything has synced."));
        else d.body.appendChild(list);
        if (box.notices.length) d.body.appendChild(button("Review " + plural(box.notices.length, "note"), "btn-secondary", function () { d.dialog.close(); showNotices(); }));
        if (box.entries.some(function (e) { return !e.failed; }) && online() && !syncing) {
            d.body.appendChild(button("Sync now", "btn-primary", function () { syncNow().then(showList); }));
        }
        d.open();
    }
    function showNotices() {
        var n = box.notices[0];
        if (!n || !online()) return;
        var d = dialog("Edited in two places");
        d.body.appendChild(el("p", "sync-dialog__text", "“" + n.title + "” changed on another device while you were offline. " +
            "Your offline edit was saved as “" + n.title + " (offline copy)” so nothing was lost."));
        var links = el("p", "sync-dialog__links");
        [["Open the current note", n.note_id], ["Open your offline copy", n.copy_id]].forEach(function (pair) {
            var a = el("a", "", pair[0]);
            a.href = notesUrl + pair[1] + "/";
            a.target = "_blank";
            a.rel = "noopener";
            links.appendChild(a);
        });
        d.body.appendChild(links);
        var acts = el("div", "sync-dialog__acts");
        function resolve(keep) {
            return function () {
                update(function (b) { b.notices = b.notices.filter(function (x) { return x.id !== n.id; }); }).then(function () {
                    return write("note.resolve", {payload: {note_id: n.note_id, copy_id: n.copy_id, keep: keep, title: n.title},
                                                  label: "Keep " + (keep === "both" ? "both versions" : keep === "mine" ? "my version" : "the other version") + " of “" + n.title + "”"});
                }).then(function () { d.dialog.close(); if (box.notices.length) setTimeout(showNotices, 300); });
            };
        }
        acts.appendChild(button("Keep mine", "btn-primary", resolve("mine")));
        acts.appendChild(button("Keep theirs", "btn-secondary", resolve("theirs")));
        acts.appendChild(button("Keep both", "btn-secondary", resolve("both")));
        d.body.appendChild(acts);
        d.open();
    }
    function toast(text) {
        var t = document.getElementById("toast-container");
        if (!t) {
            t = el("div", "toast-container");
            t.id = "toast-container";
            t.setAttribute("role", "status");
            t.setAttribute("aria-live", "polite");
            document.body.appendChild(t);
        }
        var item = el("div", "toast", text);
        t.appendChild(item);
        setTimeout(function () { if (item.parentNode) item.parentNode.removeChild(item); }, 5000);
    }

    // ---------- signing out with changes waiting ----------
    document.addEventListener("click", function (ev) {
        var a = ev.target.closest && ev.target.closest('a[href*="user_logout"]');
        if (!a || !box.entries.length) return;
        ev.preventDefault();
        ev.stopImmediatePropagation();
        var n = box.entries.length;
        var d = dialog("Changes haven't synced");
        d.body.appendChild(el("p", "sync-dialog__text", plural(n, "change") + (n === 1 ? " hasn't" : " haven't") +
            " synced yet. Logging out clears this device's offline data, so they would be lost."));
        var acts = el("div", "sync-dialog__acts");
        if (online()) {
            acts.appendChild(button("Sync now", "btn-primary", function () {
                syncNow().then(function () {
                    if (!box.entries.length) window.location.href = a.href;
                    else { d.dialog.close(); showList(); }
                });
            }));
        }
        acts.appendChild(button("Log out anyway", "btn-secondary", function () { window.location.href = a.href; }));
        d.body.appendChild(acts);
        d.open();
    }, true);

    // ---------- start ----------
    api.write = write;
    api.overlay = overlay;
    api.pending = function () { return Core.load(user).then(function (b) { return b.entries; }); };
    api.keys = function () { return keys; };
    // Is a change of these kinds still waiting for `key`? (A note made or edited offline is ticked by editing its text.)
    api.waiting = function (key, actions) {
        return box.entries.some(function (e) { return !e.failed && e.key === key && (!actions || actions.indexOf(e.action) >= 0); });
    };
    api.syncNow = syncNow;
    api.enabled = true;
    ready.then(function () { return Core.load(user); }).then(function (b) {
        if (!user) return;
        if (b.user && String(b.user) !== String(user)) b = {entries: [], notices: []};   // someone else's: never sent
        changed(b);
        soon(500);
        if (b.notices.length) setTimeout(showNotices, 800);
    });
    window.addEventListener("online", function () { tries = 0; setTimeout(syncNow, 1200); });
    window.addEventListener("offline", paint);
    document.addEventListener("visibilitychange", function () { if (document.visibilityState === "visible") soon(300); });
    setInterval(function () { if (box.entries.some(function (e) { return !e.failed; })) syncNow(); }, 60 * 1000);
    if (navigator.serviceWorker) {
        navigator.serviceWorker.addEventListener("message", function (ev) {
            if (ev.data && ev.data.type === "SYNC") syncNow();
            if (ev.data && ev.data.type === "SYNCED") Core.load(user).then(function (b) { changed(b); Offline.refresh(true); });
        });
    }
})();
