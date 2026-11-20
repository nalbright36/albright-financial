/*
 * QuickSave - background saves for check-offs (note checklists, Planner tasks and subtasks, grocery lines).
 *
 *   QuickSave.request(url, {body, json, keepalive})
 *       POST with a short timeout (8s) and one retry on a timeout, network error or 5xx. Resolves
 *       {ok, status, data}; a 4xx resolves (not rejects) so the caller can read the error.
 *
 *   QuickSave.set(key, state, send, {apply, revert, done})
 *       The box has already flipped on screen. Per key (one item) only the latest intended state counts: one request
 *       at a time, and when it returns, the newest state is sent if it differs from what the server now has - so
 *       rapid taps never land out of order. send(state) returns a promise of {ok, data}. On a final failure
 *       revert(serverState) puts the box back and a small "Couldn't save - tap to retry" message shows; tapping it
 *       sends the intended state again. done(data, state) runs after each successful save.
 */
(function () {
    "use strict";

    var TIMEOUT_MS = 8000;

    function csrfToken() {
        var match = document.cookie.match(/csrftoken=([^;]+)/);
        if (match) return match[1];
        var input = document.querySelector("input[name=csrfmiddlewaretoken]");
        return input ? input.value : "";
    }

    function once(url, opts) {
        var headers = {"X-CSRFToken": csrfToken(), "X-Requested-With": "XMLHttpRequest"};
        var body = opts.body;
        if (opts.json) { headers["Content-Type"] = "application/json"; body = JSON.stringify(body || {}); }
        var ctrl = window.AbortController ? new AbortController() : null;
        var timer = ctrl ? setTimeout(function () { ctrl.abort(); }, opts.timeout || TIMEOUT_MS) : null;
        return fetch(url, {method: "POST", credentials: "same-origin", headers: headers, body: body,
                           keepalive: !!opts.keepalive, signal: ctrl ? ctrl.signal : undefined})
            .then(function (r) {
                clearTimeout(timer);
                if (r.status >= 500) throw new Error("HTTP " + r.status);
                if (r.status === 204) return {ok: true, status: 204, data: {}};
                return r.json().catch(function () { return {}; }).then(function (d) { return {ok: r.ok, status: r.status, data: d}; });
            }, function (err) { clearTimeout(timer); throw err; });
    }

    function request(url, opts) {
        opts = opts || {};
        return once(url, opts).catch(function () {
            return new Promise(function (resolve) { setTimeout(resolve, 600); }).then(function () { return once(url, opts); });
        });
    }

    // ---------- "Couldn't save - tap to retry" ----------
    var bar = null, retries = [];
    function showRetry(retry) {
        retries.push(retry);
        if (!bar) {
            bar = document.createElement("button");
            bar.type = "button";
            bar.className = "quicksave-retry";
            bar.setAttribute("role", "status");
            bar.addEventListener("click", function () {
                var all = retries;
                retries = [];
                bar.hidden = true;
                all.forEach(function (fn) { fn(); });
            });
            document.body.appendChild(bar);
        }
        bar.textContent = "Couldn't save - tap to retry";
        bar.hidden = false;
    }

    // ---------- Latest state per item ----------
    var items = {};      // key -> {server, want, busy, send, hooks}

    function pump(key) {
        var it = items[key];
        if (!it || it.busy || it.want === it.server) return;
        var state = it.want;
        it.busy = true;
        Promise.resolve().then(function () { return it.send(state); }).then(function (res) {
            it.busy = false;
            if (!res || !res.ok) throw res || new Error("failed");
            it.server = state;
            if (it.hooks.done) it.hooks.done(res.data || {}, state, it.want === state);
            pump(key);
        }).catch(function (res) {
            it.busy = false;
            var intended = it.want;
            it.want = it.server;
            if (it.hooks.revert) it.hooks.revert(it.server, res && res.data);
            if (res && res.status >= 400 && res.status < 500) return;    // refused (not a network problem) - no retry
            showRetry(function () {
                if (it.hooks.apply) it.hooks.apply(intended);
                set(key, intended, it.send, it.hooks);
            });
        });
    }

    function set(key, state, send, hooks) {
        // hooks.initial: what the box showed before this tap. While nothing is in flight that's the best guess of the
        // server's state (the page may have been refreshed with someone else's change since our last save).
        var it = items[key] || (items[key] = {busy: false});
        if (!it.busy) it.server = hooks && "initial" in hooks ? hooks.initial : !state;
        it.want = state;
        it.send = send;
        it.hooks = hooks || {};
        pump(key);
    }

    window.QuickSave = {request: request, set: set, csrfToken: csrfToken};
})();
