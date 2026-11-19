/* The floating AI assistant (albright_assistant/_widget.html). Sends a message (POST /assistant/send/), then polls
   /assistant/runs/<id>/ about once a second - each poll advances the run one step on the server (runner.py) and
   returns the new messages, action chips and any approval card. Assistant replies arrive as sanitized HTML from the
   server; everything else goes in as text. Close with X, Esc, or a swipe down on a phone. */
(function () {
    "use strict";
    var root = document.getElementById("assistant");
    if (!root) return;
    var fab = document.getElementById("assistant-fab");
    var connected = root.dataset.connected === "1";
    var target = document.getElementById(connected ? "assistant-panel" : "assistant-connect-card");
    var POLL_MS = 1000;

    // The button sits in the floating dock (index.html); js/fab_dock.js lifts the dock over fixed bottom bars.

    // ---------- open / close ----------
    function isOpen() { return target && !target.hidden; }
    function open() {
        if (!target) return;
        target.hidden = false;
        root.classList.add("is-open");
        fab.setAttribute("aria-expanded", "true");
        if (connected) {
            loadState();
            var input = document.getElementById("assistant-input");
            if (input) input.focus();
        } else {
            var link = target.querySelector("a, button");
            if (link) link.focus();
        }
    }
    function close() {
        if (!target || target.hidden) return;
        target.hidden = true;
        root.classList.remove("is-open");
        fab.setAttribute("aria-expanded", "false");
        fab.focus();
    }
    fab.addEventListener("click", function () { isOpen() ? close() : open(); });
    Array.prototype.forEach.call(root.querySelectorAll("[data-assistant-close]"), function (b) {
        b.addEventListener("click", close);
    });
    document.addEventListener("keydown", function (e) {
        if (e.key === "Escape" && isOpen()) { e.preventDefault(); close(); }
    });
    if (!connected) return;

    // ---------- swipe down to close (phones) ----------
    var head = document.getElementById("assistant-head"), startY = null;
    head.addEventListener("touchstart", function (e) { startY = e.touches[0].clientY; }, { passive: true });
    head.addEventListener("touchmove", function (e) {
        if (startY !== null && e.touches[0].clientY - startY > 80 && window.innerWidth <= 640) { startY = null; close(); }
    }, { passive: true });
    head.addEventListener("touchend", function () { startY = null; });

    // ---------- helpers ----------
    var log = document.getElementById("assistant-log"), empty = document.getElementById("assistant-empty");
    var form = document.getElementById("assistant-form"), input = document.getElementById("assistant-input");
    var send = document.getElementById("assistant-send"), chips = document.getElementById("assistant-chips");
    var costEl = document.getElementById("assistant-cost"), modelEl = document.getElementById("assistant-model");
    var historyBtn = document.getElementById("assistant-history-btn"), historyBox = document.getElementById("assistant-history");
    var historyList = document.getElementById("assistant-history-list");
    var refreshBox = document.getElementById("assistant-refresh");
    var csrf = document.getElementById("assistant-csrf").value;
    var page = {};
    try { page = JSON.parse(document.getElementById("assistant-page").textContent) || {}; } catch (e) { page = {}; }
    var state = { chat: null, lastId: 0, run: null, polling: false, busy: false };
    try { state.chat = parseInt(sessionStorage.getItem("assistant-chat"), 10) || null; } catch (e) { state.chat = null; }

    function url(name, id) { return root.dataset[name].replace("/0/", "/" + id + "/"); }
    function remember() { try { if (state.chat) sessionStorage.setItem("assistant-chat", state.chat); else sessionStorage.removeItem("assistant-chat"); } catch (e) {} }
    function api(method, address, body) {
        var opts = { method: method, credentials: "same-origin", headers: { "Accept": "application/json" } };
        if (method !== "GET") {
            opts.headers["X-CSRFToken"] = csrf;
            opts.headers["Content-Type"] = "application/json";
            opts.body = JSON.stringify(body || {});
        }
        return fetch(address, opts).then(function (r) {
            return r.json().catch(function () { return {}; }).then(function (data) { data._status = r.status; return data; });
        });
    }
    function pageContext() {
        var html = document.documentElement;
        return { app: html.getAttribute("data-app-key") || "", page: document.title || "", path: page.path || location.pathname,
                 item: page.item || null };
    }
    function scrollDown() { log.scrollTop = log.scrollHeight; }
    function el(tag, cls, text) {
        var n = document.createElement(tag);
        if (cls) n.className = cls;
        if (text != null) n.textContent = text;
        return n;
    }
    function header(data) {
        if (data.provider && modelEl) modelEl.textContent = data.provider + " · " + data.model;
        var parts = [];
        if (data.cost) parts.push("estimate $" + data.cost);
        if (data.tokens_in || data.tokens_out) {
            var total = (data.tokens_in || 0) + (data.tokens_out || 0);
            parts.push((total >= 1000 ? (total / 1000).toFixed(1) + "k" : total) + " tokens");
        }
        costEl.textContent = parts.join(" · ");
        costEl.title = data.cost ? "An estimate from the provider's token counts - your provider's bill is what counts." : "";
    }

    // ---------- rendering ----------
    var typing = null;
    function showTyping(on) {
        if (on && !typing) { typing = el("div", "as-typing"); typing.setAttribute("aria-label", "Thinking");
            typing.innerHTML = "<span></span><span></span><span></span>"; log.appendChild(typing); scrollDown(); }
        if (!on && typing) { typing.remove(); typing = null; }
    }
    function actionChip(a) {
        var row = el("div", "as-action" + (a.undone ? " is-undone" : ""));
        row.dataset.action = a.id;
        row.appendChild(el("span", "as-action__text", a.summary));
        if (a.can_undo) {
            var b = el("button", "as-action__undo", "Undo");
            b.type = "button";
            b.setAttribute("aria-label", "Undo: " + a.summary);
            b.addEventListener("click", function () { undo(a, row, b); });
            row.appendChild(b);
            if (a.undo_until) {
                var ms = new Date(a.undo_until).getTime() - Date.now();
                if (ms > 0) setTimeout(function () { if (b.parentNode) b.remove(); }, ms);
            }
        }
        return row;
    }
    function render(m) {
        if (state.lastId < m.id) state.lastId = m.id;
        if (empty) { empty.remove(); empty = null; }
        if (m.role === "user") { log.appendChild(el("div", "as-msg as-msg--user", m.text)); }
        else if (m.role === "assistant") {
            if (!m.html) return;
            var box = el("div", "as-msg as-msg--assistant");
            box.innerHTML = m.html;                      // sanitized on the server (albright_assistant/render.py)
            log.appendChild(box);
        } else if (m.role === "tool") {
            if (m.actions && m.actions.length) {
                var list = el("div", "as-actions");
                m.actions.forEach(function (a) { list.appendChild(actionChip(a)); });
                log.appendChild(list);
            }
        } else if (m.role === "notice") {
            var isErr = ["limit", "cancelled"].indexOf(m.kind) === -1;
            var n = el("div", "as-msg as-msg--notice" + (isErr ? " as-msg--error" : ""), m.text);
            if (isErr) n.setAttribute("role", "alert");
            log.appendChild(n);
        }
    }
    var approveCard = null;
    function renderPending(run) {
        if (approveCard) { approveCard.remove(); approveCard = null; }
        if (!run || run.status !== "waiting" || !run.pending.length) return;
        approveCard = el("div", "as-approve");
        approveCard.setAttribute("role", "group");
        approveCard.setAttribute("aria-label", "Needs your OK");
        approveCard.appendChild(el("p", "as-approve__title", "Needs your OK"));
        var ul = el("ul");
        run.pending.forEach(function (p) {
            var li = el("li", null, p.description + " ");
            li.appendChild(el("span", "as-approve__why", "(" + p.reason + ")"));
            ul.appendChild(li);
        });
        approveCard.appendChild(ul);
        var row = el("div", "as-approve__buttons");
        var ok = el("button", "btn btn-primary btn-sm", "Approve"), no = el("button", "btn btn-secondary btn-sm", "Cancel");
        ok.type = no.type = "button";
        ok.addEventListener("click", function () { decide(run, "approve"); });
        no.addEventListener("click", function () { decide(run, "cancel"); });
        row.appendChild(ok); row.appendChild(no);
        approveCard.appendChild(row);
        log.appendChild(approveCard);
        scrollDown();
        ok.focus();
    }
    function changed(apps) {
        if (!apps || !apps.length) return;
        var here = document.documentElement.getAttribute("data-app-key") || "";
        try { document.dispatchEvent(new CustomEvent("assistant:changed", { detail: { apps: apps } })); } catch (e) {}
        if (apps.indexOf(here) !== -1 || here === "home") {
            refreshBox.hidden = false;
            document.getElementById("assistant-refresh-link").href = location.href;
        }
    }
    function setBusy(on) {
        state.busy = on;
        send.disabled = on;
        showTyping(on);
    }

    // ---------- loading ----------
    function reset() {
        state.chat = null; state.lastId = 0; state.run = null;
        remember();
        log.textContent = "";
        empty = el("p", "as-empty", "Ask about your plans, meals, workouts or notes - or ask it to add or change something.");
        log.appendChild(empty);
        chips.hidden = false;
        costEl.textContent = "";
        refreshBox.hidden = true;
        if (approveCard) { approveCard.remove(); approveCard = null; }
    }
    function loadChat(id) {
        return api("GET", url("chatUrl", id)).then(function (data) {
            if (data._status !== 200) { reset(); return; }
            reset();
            state.chat = id; remember();
            header(data);
            chips.hidden = !!(data.messages && data.messages.length);
            (data.messages || []).forEach(render);
            scrollDown();
            if (data.run && (data.run.status === "queued" || data.run.status === "running")) { state.run = data.run.id; setBusy(true); poll(); }
            else if (data.run && data.run.status === "waiting") { state.run = data.run.id; renderPending(data.run); }
        });
    }
    var loaded = false;
    function loadState() {
        if (loaded) return;
        loaded = true;
        if (state.chat) loadChat(state.chat);
    }
    function loadHistory() {
        api("GET", root.dataset.stateUrl).then(function (data) {
            historyList.textContent = "";
            if (!data.chats || !data.chats.length) { historyList.appendChild(el("li", "as-history__empty", "No chats yet.")); return; }
            data.chats.forEach(function (c) {
                var li = el("li");
                var openBtn = el("button", "as-history__open", c.title);
                openBtn.type = "button";
                if (c.id === state.chat) openBtn.setAttribute("aria-current", "true");
                openBtn.addEventListener("click", function () { loadChat(c.id); toggleHistory(false); input.focus(); });
                var del = el("button", "as-icon-btn", "×");
                del.type = "button";
                del.setAttribute("aria-label", "Delete chat: " + c.title);
                del.addEventListener("click", function () {
                    if (!window.confirm("Delete this chat?")) return;
                    api("POST", url("chatDeleteUrl", c.id)).then(function () {
                        if (state.chat === c.id) reset();
                        loadHistory();
                    });
                });
                li.appendChild(openBtn); li.appendChild(del);
                historyList.appendChild(li);
            });
        });
    }
    function toggleHistory(force) {
        var show = typeof force === "boolean" ? force : historyBox.hidden;
        historyBox.hidden = !show;
        historyBtn.setAttribute("aria-expanded", show ? "true" : "false");
        if (show) loadHistory();
    }
    historyBtn.addEventListener("click", function () { toggleHistory(); });
    document.getElementById("assistant-delete-all").addEventListener("click", function () {
        if (!window.confirm("Delete all your assistant chats? This can't be undone.")) return;
        api("POST", root.dataset.deleteAllUrl).then(function () { reset(); loadHistory(); });
    });
    document.getElementById("assistant-new").addEventListener("click", function () { reset(); toggleHistory(false); input.focus(); });

    // ---------- sending and polling ----------
    function poll() {
        if (state.polling || !state.run) return;
        state.polling = true;
        api("GET", url("runUrl", state.run) + "?after=" + state.lastId).then(function (data) {
            state.polling = false;
            if (data._status !== 200) { setBusy(false); return; }
            header(data);
            showTyping(false);
            (data.messages || []).forEach(render);
            var run = data.run || {};
            changed(run.changed);
            if (run.status === "queued" || run.status === "running") {
                showTyping(true);
                scrollDown();
                setTimeout(poll, POLL_MS);
            } else {
                setBusy(false);
                renderPending(run);
                scrollDown();
            }
        }, function () {
            state.polling = false;
            setTimeout(poll, POLL_MS * 3);                // offline for a moment: try again
        });
    }
    function decide(run, how) {
        if (approveCard) approveCard.querySelectorAll("button").forEach(function (b) { b.disabled = true; });
        api("POST", url(how === "approve" ? "approveUrl" : "cancelUrl", run.id)).then(function () {
            if (approveCard) { approveCard.remove(); approveCard = null; }
            state.run = run.id;
            setBusy(true);
            poll();
        });
    }
    function undo(a, row, button) {
        button.disabled = true;
        api("POST", url("undoUrl", a.id)).then(function (data) {
            if (data._status === 200) {
                row.classList.add("is-undone");
                button.remove();
                changed([data.app]);
            } else {
                button.remove();
                row.appendChild(el("span", "as-approve__why", data.error || "Couldn't undo it."));
            }
        });
    }
    function submit(text) {
        text = (text || "").trim();
        if (!text || state.busy) return;
        setBusy(true);
        chips.hidden = true;
        refreshBox.hidden = true;
        api("POST", root.dataset.sendUrl, { chat: state.chat, text: text, page: pageContext() }).then(function (data) {
            if (data._status !== 200) {
                setBusy(false);
                var n = el("div", "as-msg as-msg--notice as-msg--error", data.error || "Couldn't send that. Try again.");
                n.setAttribute("role", "alert");
                if (empty) { empty.remove(); empty = null; }
                log.appendChild(n);
                scrollDown();
                return;
            }
            input.value = "";
            grow();
            state.chat = data.chat; remember();
            header(data);
            if (approveCard) { approveCard.remove(); approveCard = null; }
            (data.messages || []).forEach(render);
            state.run = data.run.id;
            showTyping(true);
            scrollDown();
            setTimeout(poll, 250);
        }, function () { setBusy(false); });
    }
    function grow() { input.style.height = "auto"; input.style.height = Math.min(input.scrollHeight, 160) + "px"; }
    input.addEventListener("input", grow);
    input.addEventListener("keydown", function (e) {
        if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); submit(input.value); }
    });
    form.addEventListener("submit", function (e) { e.preventDefault(); submit(input.value); });
    Array.prototype.forEach.call(chips.querySelectorAll("[data-chip]"), function (b) {
        b.addEventListener("click", function () {
            var text = b.getAttribute("data-chip");
            if (/…$/.test(text)) { input.value = text.replace(/…$/, " "); input.focus(); grow(); }
            else submit(text);
        });
    });

    // ---------- speech to text, where the browser has it ----------
    var Speech = window.SpeechRecognition || window.webkitSpeechRecognition;
    var mic = document.getElementById("assistant-mic");
    if (Speech && mic) {
        mic.hidden = false;
        var rec = null;
        mic.addEventListener("click", function () {
            if (rec) { rec.stop(); return; }
            rec = new Speech();
            rec.lang = document.documentElement.lang || "en-US";
            rec.interimResults = false;
            var before = input.value;
            rec.onresult = function (e) {
                var said = Array.prototype.map.call(e.results, function (r) { return r[0].transcript; }).join(" ");
                input.value = (before ? before.replace(/\s*$/, " ") : "") + said;
                grow();
            };
            rec.onend = function () { rec = null; mic.classList.remove("is-listening"); mic.setAttribute("aria-pressed", "false"); input.focus(); };
            rec.onerror = rec.onend;
            mic.classList.add("is-listening");
            mic.setAttribute("aria-pressed", "true");
            rec.start();
        });
    }
})();
