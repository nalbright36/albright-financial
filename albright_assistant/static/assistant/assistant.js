/* The AI assistant (albright_assistant/_widget.html), opened from its header button (_nav_button.html). Sends a message (POST /assistant/send/), then polls
   /assistant/runs/<id>/ about once a second - each poll advances the run one step on the server (runner.py) and
   returns the new messages, action chips and any approval card. Assistant replies arrive as sanitized HTML from the
   server; everything else goes in as text. Close with X, Esc, or a swipe down on a phone.
   Photos (the camera button; drag-and-drop or paste on a computer): up to 4 per message, each shrunk here to 2048px
   on the long side and re-encoded as JPEG - which also drops EXIF/GPS - before upload (the server checks again:
   albright_assistant/photos.py). Disabled when the model can't read images. */
(function () {
    "use strict";
    var root = document.getElementById("assistant");
    if (!root) return;
    var btn = document.getElementById("assistant-button");
    var VISUALS_JS = document.currentScript && document.currentScript.getAttribute("data-visuals-js");
    var connected = root.dataset.connected === "1";
    var target = document.getElementById(connected ? "assistant-panel" : "assistant-connect-card");
    var POLL_MS = 1000;

    // ---------- open / close ----------
    function isOpen() { return target && !target.hidden; }
    function open() {
        if (!target) return;
        target.hidden = false;
        root.classList.add("is-open");
        if (btn) btn.setAttribute("aria-expanded", "true");
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
        if (btn) btn.setAttribute("aria-expanded", "false");
        var back = opener && opener.getClientRects().length ? opener : btn;
        opener = null;
        if (back) back.focus();
    }
    if (btn) btn.addEventListener("click", function () { opener = null; isOpen() ? close() : open(); });
    Array.prototype.forEach.call(root.querySelectorAll("[data-assistant-close]"), function (b) {
        b.addEventListener("click", close);
    });
    document.addEventListener("keydown", function (e) {
        if (e.key === "Escape" && isOpen()) { e.preventDefault(); close(); }
    });

    // ---------- A page's own "Ask AI" buttons ([data-assistant-open]) ----------
    // E.g. Notes' toolbar, phone bar and More menu: they open this same drawer. data-assistant-prompt starts the
    // message (the open page - a note - is already in the assistant's context: prompt.page_context).
    var opener = null;
    document.addEventListener("click", function (e) {
        var b = e.target.closest && e.target.closest("[data-assistant-open]");
        if (!b) return;
        e.preventDefault();
        opener = b;
        if (!isOpen()) open();
        var text = b.getAttribute("data-assistant-prompt"), box = document.getElementById("assistant-input");
        if (connected && text && box && !box.value.trim()) {
            box.value = text;
            box.dispatchEvent(new Event("input"));
            box.focus();
            try { box.setSelectionRange(text.length, text.length); } catch (err) {}
        }
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
        if (typeof data.vision === "boolean") setVision(data.vision);
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
        if (m.role === "user") {
            if (m.images && m.images.length) {
                var pics = el("div", "as-pics");
                m.images.forEach(function (p) {
                    var b = el("button", "as-pics__btn");
                    b.type = "button";
                    b.setAttribute("aria-label", "View photo full size");
                    var img = el("img");
                    img.src = p.thumb; img.alt = ""; img.loading = "lazy";
                    b.appendChild(img);
                    b.addEventListener("click", function () { viewPhoto(p.url); });
                    pics.appendChild(b);
                });
                log.appendChild(pics);
            }
            if (m.text) log.appendChild(el("div", "as-msg as-msg--user", m.text));
        }
        else if (m.role === "assistant") {
            if (!m.html) return;
            var box = el("div", "as-msg as-msg--assistant");
            box.innerHTML = m.html;                      // sanitized on the server (albright_assistant/render.py)
            log.appendChild(box);
        } else if (m.role === "tool") {
            if (m.visual) {
                var slot = el("div", "as-visual");
                log.appendChild(slot);
                visualCard(m.visual, slot);
            }
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
        var label = "Approve";
        run.pending.forEach(function (p) {
            var li = el("li", null, p.description + " ");
            li.appendChild(el("span", "as-approve__why", "(" + p.reason + ")"));
            if (p.preview && p.preview.kind === "recipe") li.appendChild(recipePreview(p.preview));
            if (p.approve_label && run.pending.length === 1) label = p.approve_label;
            ul.appendChild(li);
        });
        approveCard.appendChild(ul);
        var row = el("div", "as-approve__buttons");
        var ok = el("button", "btn btn-primary btn-sm", label), no = el("button", "btn btn-secondary btn-sm", "Cancel");
        ok.type = no.type = "button";
        ok.addEventListener("click", function () { decide(run, "approve"); });
        no.addEventListener("click", function () { decide(run, "cancel"); });
        row.appendChild(ok); row.appendChild(no);
        approveCard.appendChild(row);
        log.appendChild(approveCard);
        scrollDown();
        ok.focus();
    }
    // A Visual (a chart from their data): visuals.js draws it - loaded the first time one is shown.
    var visualsReady = null;
    function visualCard(info, slot) {
        if (!visualsReady) {
            visualsReady = window.AlbrightVisuals ? Promise.resolve() : new Promise(function (resolve, reject) {
                var s = document.createElement("script");
                s.src = VISUALS_JS; s.onload = resolve; s.onerror = reject;
                document.head.appendChild(s);
            });
        }
        visualsReady.then(function () {
            slot.appendChild(window.AlbrightVisuals.card(info, { height: 220 }));
            scrollDown();
        }, function () { slot.textContent = "Couldn't load the chart."; });
    }
    function recipePreview(r) {
        var card = el("div", "as-recipe");
        card.appendChild(el("p", "as-recipe__title", r.title || "Untitled recipe"));
        if (r.servings) card.appendChild(el("p", "as-recipe__meta", "Serves " + r.servings));
        if (r.ingredients && r.ingredients.length) {
            card.appendChild(el("p", "as-recipe__h", "Ingredients"));
            var ing = el("ul");
            r.ingredients.forEach(function (t) { ing.appendChild(el("li", null, t)); });
            card.appendChild(ing);
        }
        if (r.steps && r.steps.length) {
            card.appendChild(el("p", "as-recipe__h", "Steps"));
            var steps = el("ol");
            r.steps.forEach(function (t) { steps.appendChild(el("li", null, t)); });
            card.appendChild(steps);
        }
        return card;
    }
    var viewer = null;
    function viewPhoto(src) {
        if (!viewer) {
            viewer = el("dialog", "as-viewer");
            viewer.setAttribute("aria-label", "Photo");
            var img = el("img");
            img.alt = "Your photo, full size";
            var shut = el("button", "as-icon-btn as-viewer__close", "×");
            shut.type = "button";
            shut.setAttribute("aria-label", "Close photo");
            shut.addEventListener("click", function () { viewer.close(); });
            viewer.addEventListener("click", function (e) { if (e.target === viewer || e.target.tagName === "IMG") viewer.close(); });
            viewer.appendChild(img); viewer.appendChild(shut);
            document.body.appendChild(viewer);
        }
        viewer.querySelector("img").src = src;
        if (typeof viewer.showModal === "function") viewer.showModal(); else window.open(src, "_blank", "noopener");
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
        clearPhotos();
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
        if ((!text && !photos.length) || state.busy || preparing) return;
        setBusy(true);
        chips.hidden = true;
        refreshBox.hidden = true;
        var request;
        if (photos.length) {
            var fd = new FormData();
            if (state.chat) fd.append("chat", state.chat);
            fd.append("text", text);
            fd.append("page", JSON.stringify(pageContext()));
            photos.forEach(function (p, i) { fd.append("photos", p.blob, p.name || ("photo-" + (i + 1) + ".jpg")); });
            request = fetch(root.dataset.sendUrl, { method: "POST", credentials: "same-origin", body: fd,
                                                    headers: { "Accept": "application/json", "X-CSRFToken": csrf } })
                .then(function (r) { return r.json().catch(function () { return {}; }).then(function (d) { d._status = r.status; return d; }); });
        } else {
            request = api("POST", root.dataset.sendUrl, { chat: state.chat, text: text, page: pageContext() });
        }
        request.then(function (data) {
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
            clearPhotos();
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

    // ---------- photos ----------
    var MAX_PHOTOS = 4, MAX_SIDE = 2048, MAX_BYTES = 5 * 1024 * 1024, NO_VISION = root.dataset.noVision || "";
    var photoBtn = document.getElementById("assistant-photo"), photoInput = document.getElementById("assistant-photo-input");
    var photoWrap = document.getElementById("assistant-photo-wrap"), tray = document.getElementById("assistant-photos");
    var photoNote = document.getElementById("assistant-photo-note");
    var photos = [], preparing = 0, vision = root.dataset.vision === "1";

    function sayPhoto(text) { if (photoNote) { photoNote.textContent = text || ""; photoNote.hidden = !text; } }
    function setVision(on) {
        vision = !!on;
        if (!photoBtn) return;
        photoBtn.disabled = !vision;
        photoBtn.setAttribute("aria-disabled", vision ? "false" : "true");
        if (photoWrap) photoWrap.title = vision ? "Add photos" : NO_VISION;
        photoBtn.setAttribute("aria-label", vision ? "Add photos (camera or library)" : "Add photos - " + NO_VISION);
        if (!vision && photos.length) { clearPhotos(); sayPhoto(NO_VISION); }
    }
    function drawTray() {
        if (!tray) return;
        tray.textContent = "";
        tray.hidden = !photos.length && !preparing;
        photos.forEach(function (p, i) {
            var item = el("div", "as-tray__item");
            var img = el("img");
            img.src = p.url; img.alt = "Photo " + (i + 1);
            var x = el("button", "as-tray__remove", "×");
            x.type = "button";
            x.setAttribute("aria-label", "Remove photo " + (i + 1));
            x.addEventListener("click", function () {
                URL.revokeObjectURL(p.url);
                photos.splice(photos.indexOf(p), 1);
                drawTray();
                input.focus();
            });
            item.appendChild(img); item.appendChild(x);
            tray.appendChild(item);
        });
        for (var n = 0; n < preparing; n++) tray.appendChild(el("div", "as-tray__item is-loading"));
    }
    function clearPhotos() {
        photos.forEach(function (p) { URL.revokeObjectURL(p.url); });
        photos = [];
        sayPhoto("");
        drawTray();
    }
    function decode(file) {
        // createImageBitmap honours the EXIF rotation (so the photo comes out upright); an <img> is the fallback.
        if (window.createImageBitmap) {
            return createImageBitmap(file, { imageOrientation: "from-image" }).catch(function () { return viaImg(file); });
        }
        return viaImg(file);
    }
    function viaImg(file) {
        return new Promise(function (resolve, reject) {
            var img = new Image(), src = URL.createObjectURL(file);
            img.onload = function () { URL.revokeObjectURL(src); resolve(img); };
            img.onerror = function () { URL.revokeObjectURL(src); reject(new Error("decode")); };
            img.src = src;
        });
    }
    function shrink(file) {
        return decode(file).then(function (pic) {
            var w = pic.width, h = pic.height, scale = Math.min(1, MAX_SIDE / Math.max(w, h));
            var canvas = document.createElement("canvas");
            canvas.width = Math.max(1, Math.round(w * scale));
            canvas.height = Math.max(1, Math.round(h * scale));
            var ctx = canvas.getContext("2d");
            ctx.fillStyle = "#fff";                      // a transparent PNG gets a white background, not black
            ctx.fillRect(0, 0, canvas.width, canvas.height);
            ctx.drawImage(pic, 0, 0, canvas.width, canvas.height);
            if (pic.close) pic.close();
            return new Promise(function (resolve, reject) {
                canvas.toBlob(function (blob) { blob ? resolve(blob) : reject(new Error("encode")); }, "image/jpeg", 0.85);
            });
        }, function () {
            // This browser can't open it (HEIC outside Safari): send it as it is - the server converts HEIC.
            if (file.size > MAX_BYTES) throw new Error("That photo is too big to send (over 5 MB) and this browser can't shrink it.");
            return file;
        });
    }
    function addFiles(list) {
        if (!vision) { sayPhoto(NO_VISION); return; }
        var files = Array.prototype.filter.call(list || [], function (f) {
            return /^image\//.test(f.type) || /\.(heic|heif)$/i.test(f.name || "");
        });
        if (!files.length) { if (list && list.length) sayPhoto("Only photos can be added."); return; }
        sayPhoto("");
        var room = MAX_PHOTOS - photos.length - preparing;
        if (files.length > room) sayPhoto("Up to " + MAX_PHOTOS + " photos per message.");
        files.slice(0, Math.max(0, room)).forEach(function (file) {
            preparing += 1;
            drawTray();
            shrink(file).then(function (blob) {
                var name = blob === file ? file.name : (file.name || "photo").replace(/\.[^.]*$/, "") + ".jpg";
                photos.push({ blob: blob, name: name, url: URL.createObjectURL(blob) });
            }, function (err) {
                sayPhoto(err && err.message && err.message.length > 12 ? err.message : "Couldn't read that photo.");
            }).then(function () { preparing -= 1; drawTray(); });
        });
    }
    if (photoBtn && photoInput) {
        photoBtn.addEventListener("click", function () { if (vision) photoInput.click(); });
        photoInput.addEventListener("change", function () { addFiles(photoInput.files); photoInput.value = ""; });
        // Paste a screenshot or copied image (desktop).
        input.addEventListener("paste", function (e) {
            var items = (e.clipboardData && e.clipboardData.files) || [];
            if (items.length && Array.prototype.some.call(items, function (f) { return /^image\//.test(f.type); })) {
                e.preventDefault();
                addFiles(items);
            }
        });
        // Drag and drop onto the panel (desktop).
        var panel = document.getElementById("assistant-panel"), depth = 0;
        function hasFiles(e) { return e.dataTransfer && Array.prototype.indexOf.call(e.dataTransfer.types || [], "Files") !== -1; }
        panel.addEventListener("dragenter", function (e) { if (!hasFiles(e)) return; e.preventDefault(); depth += 1; panel.classList.add("is-dropping"); });
        panel.addEventListener("dragover", function (e) { if (hasFiles(e)) { e.preventDefault(); e.dataTransfer.dropEffect = vision ? "copy" : "none"; } });
        panel.addEventListener("dragleave", function () { depth = Math.max(0, depth - 1); if (!depth) panel.classList.remove("is-dropping"); });
        panel.addEventListener("drop", function (e) {
            if (!hasFiles(e)) return;
            e.preventDefault();
            depth = 0;
            panel.classList.remove("is-dropping");
            addFiles(e.dataTransfer.files);
        });
        setVision(vision);
    }

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
