/*
 * The review queue (review_queue.html): one lot at a time, keyboard
 * first. The whole queue arrives as JSON (#review-queue-data, built by
 * scanner/review_queue.py); every action posts to review_views' small
 * JSON endpoints without a reload. Vanilla JS, no libraries.
 *
 *   W watch -> next     D dismiss (+ 1-4 reason within 3s) -> next
 *   C calculator        A AI review (lot stays put)
 *   →/Space skip        ← back       O open listing
 *   U undo last W/D     ? help overlay
 *
 * Shortcuts are ignored while typing in a field, and while the calculator
 * dialog or the photo lightbox is open (their own Esc closes them).
 * Position is remembered per browser for the rest of the day.
 */
(function () {
    "use strict";

    var dataEl = document.getElementById("review-queue-data");
    if (!dataEl) return;
    var QUEUE = JSON.parse(dataEl.textContent);
    var TODAY = document.getElementById("review-config").dataset.today;
    var POSITION_KEY = "review-queue-position";
    var LEGEND_KEY = "review-queue-legend-open";
    var REASON_WINDOW_MS = 3000;

    function byId(id) { return document.getElementById(id); }
    function csrfToken() {
        var match = document.cookie.match(/csrftoken=([^;]+)/);
        return match ? match[1] : "";
    }
    function money(value) {
        if (value === null || value === undefined) return "—";
        return (value < 0 ? "-$" : "$") + Math.abs(value).toFixed(2);
    }
    function post(url, body) {
        return fetch(url, {
            method: "POST",
            headers: {"Content-Type": "application/json", "X-CSRFToken": csrfToken(), "X-Requested-With": "XMLHttpRequest"},
            body: JSON.stringify(body || {}),
        }).then(function (r) {
            return r.json().catch(function () { return {}; }).then(function (data) { return {ok: r.ok, data: data}; });
        });
    }
    function storageGet(key) { try { return window.localStorage.getItem(key); } catch (e) { return null; } }
    function storageSet(key, value) { try { window.localStorage.setItem(key, value); } catch (e) {} }

    // ---------- State ----------
    var index = 0;
    var status = {};      // lot_id -> {type: "watch"|"dismiss", reason}
    var undoStack = [];   // [{lotId, type, index}]
    var reasonTimer = null;
    var reasonCountdown = null;
    var reasonLotId = null;

    // Same-day resume: back to the lot you were on (by id, since the queue
    // itself may have changed since).
    try {
        var saved = JSON.parse(storageGet(POSITION_KEY) || "null");
        if (saved && saved.date === TODAY) {
            var found = QUEUE.findIndex(function (item) { return item.lot_id === saved.lotId; });
            if (found !== -1) index = found;
        }
    } catch (e) {}

    function savePosition() {
        var item = QUEUE[index];
        if (item) storageSet(POSITION_KEY, JSON.stringify({date: TODAY, lotId: item.lot_id}));
    }

    function pendingCount() {
        return QUEUE.filter(function (item) { return !status[item.lot_id]; }).length;
    }

    // ---------- Rendering ----------
    function setText(id, text) { byId(id).textContent = text == null ? "" : text; }

    function renderAI(item) {
        var box = byId("rq-ai");
        var ai = item.ai_review;
        if (item.aiLoading) {
            box.hidden = false;
            box.innerHTML = '<span class="spinner" aria-hidden="true"></span> Running AI review…';
            return;
        }
        if (item.aiError) {
            box.hidden = false;
            box.textContent = "AI review: " + item.aiError;
            return;
        }
        if (!ai) { box.hidden = true; box.textContent = ""; return; }
        box.hidden = false;
        box.textContent = "";
        var head = document.createElement("div");
        head.className = "review-ai__head";
        var range = ai.resale_low !== null ? money(ai.resale_low) + "–" + money(ai.resale_high) : "no estimate";
        var verdict = "";
        if (ai.suggested_max_bid !== null) {
            verdict = item.current_bid <= ai.suggested_max_bid
                ? "Under the AI's max bid (" + money(ai.suggested_max_bid) + ")"
                : "Over the AI's max bid (" + money(ai.suggested_max_bid) + ")";
        }
        head.textContent = "AI: " + range + (ai.confidence ? " · " + ai.confidence + " confidence" : "") +
            (verdict ? " · " + verdict : "");
        box.appendChild(head);
        if (ai.summary) {
            var summary = document.createElement("p");
            summary.textContent = ai.summary;
            box.appendChild(summary);
        }
        var link = document.createElement("a");
        link.href = ai.detail_url;
        link.target = "_blank";
        link.rel = "noopener";
        link.textContent = "Full review";
        box.appendChild(link);
    }

    function renderStatus(item) {
        var el = byId("rq-status");
        var st = status[item.lot_id];
        if (!st) { el.hidden = true; el.textContent = ""; return; }
        el.hidden = false;
        el.textContent = st.type === "watch"
            ? "Added to Watchlist - press U to undo"
            : "Dismissed" + (st.reasonLabel ? " (" + st.reasonLabel + ")" : "") + " - press U to undo";
    }

    function render() {
        var item = QUEUE[index];
        if (!item) { showDone(); return; }
        byId("rq-card").hidden = false;
        byId("rq-done").hidden = true;
        setText("rq-progress", (index + 1) + " of " + QUEUE.length);

        var photo = byId("rq-photo");
        var empty = byId("rq-photo-empty");
        if (item.image_url) {
            byId("rq-photo-img").src = item.image_url;
            byId("rq-photo-img").alt = "Photo of " + item.title;
            photo.dataset.fullSrc = item.image_url;
            photo.dataset.caption = item.title;
            photo.hidden = false;
            empty.hidden = true;
        } else {
            byId("rq-photo-img").removeAttribute("src");
            delete photo.dataset.fullSrc;
            photo.hidden = true;
            empty.hidden = false;
        }

        setText("rq-kind", item.kind_display);
        setText("rq-source", item.source_display);
        setText("rq-category", item.category ? item.category.charAt(0).toUpperCase() + item.category.slice(1) : "");
        byId("rq-title").href = item.url;
        setText("rq-title", item.title);
        setText("rq-bid", money(item.current_bid));
        setText("rq-time", item.time_remaining);
        byId("rq-max-wrap").hidden = item.max_bid === null;
        byId("rq-headroom-wrap").hidden = item.headroom === null;
        setText("rq-max", money(item.max_bid));
        var headroomEl = byId("rq-headroom");
        headroomEl.textContent = money(item.headroom);
        headroomEl.style.color = item.headroom === null ? "" : (item.headroom >= 0 ? "var(--gain)" : "var(--loss)");

        var why = byId("rq-why");
        why.textContent = "";
        item.why.forEach(function (reason) {
            var li = document.createElement("li");
            li.textContent = reason;
            why.appendChild(li);
        });

        var desc = byId("rq-desc");
        var more = byId("rq-desc-more");
        var expanded = !!item.descExpanded;
        desc.textContent = (item.description_preview || "(no description)") +
            (item.description_more ? (expanded ? item.description_more : "…") : "");
        more.hidden = !item.description_more;
        more.textContent = expanded ? "Show less" : "Show more";

        renderAI(item);
        renderStatus(item);
        updateCalcButton(item);
        savePosition();
    }

    function showDone() {
        var pending = pendingCount();
        byId("rq-card").hidden = true;
        byId("rq-done").hidden = false;
        setText("rq-progress", QUEUE.length + " of " + QUEUE.length);
        if (pending === 0) {
            setText("rq-done-title", "All caught up");
            setText("rq-done-text", "Every lot in the queue has been watched or dismissed.");
            byId("rq-review-skipped").hidden = true;
        } else {
            setText("rq-done-title", "End of the queue");
            setText("rq-done-text", pending + " skipped lot" + (pending === 1 ? "" : "s") + " still waiting.");
            byId("rq-review-skipped").hidden = false;
            setText("rq-review-skipped", "Review skipped (" + pending + ")");
        }
    }

    function goTo(newIndex) {
        index = Math.max(0, Math.min(newIndex, QUEUE.length));
        render();
    }
    function next() { goTo(index + 1); }
    function back() { if (index > 0) goTo(index - 1); }

    // ---------- Legend: flash the row for what just happened ----------
    function flash(id) {
        var row = document.querySelector('#rq-legend [data-legend="' + id + '"]');
        if (!row) return;
        row.classList.remove("is-flash");
        void row.offsetWidth;  // restart the animation on a repeat press
        row.classList.add("is-flash");
        window.setTimeout(function () { row.classList.remove("is-flash"); }, 700);
    }

    // ---------- Actions ----------
    function currentItem() { return QUEUE[index]; }

    function watch() {
        var item = currentItem();
        if (!item) return;
        flash("watch");
        if (status[item.lot_id] && status[item.lot_id].type === "watch") { next(); return; }
        status[item.lot_id] = {type: "watch"};
        undoStack.push({lotId: item.lot_id, type: "watch", index: index, item: item});
        post(item.urls.watch).then(function (res) {
            if (!res.ok) { delete status[item.lot_id]; undoStack.pop(); if (currentItem() === item) render(); }
        });
        next();
    }

    function dismiss() {
        var item = currentItem();
        if (!item) return;
        flash("dismiss");
        status[item.lot_id] = {type: "dismiss"};
        undoStack.push({lotId: item.lot_id, type: "dismiss", index: index, item: item});
        post(item.urls.dismiss, {});
        renderStatus(item);
        openReasonPicker(item);
    }

    function openReasonPicker(item) {
        reasonLotId = item.lot_id;
        byId("rq-reasons").hidden = false;
        byId("rq-legend-main").hidden = true;
        byId("rq-legend-reasons").hidden = false;
        var remaining = REASON_WINDOW_MS / 1000;
        setText("rq-reasons-timer", remaining + "s");
        window.clearInterval(reasonCountdown);
        reasonCountdown = window.setInterval(function () {
            remaining -= 1;
            setText("rq-reasons-timer", Math.max(0, remaining) + "s");
        }, 1000);
        window.clearTimeout(reasonTimer);
        reasonTimer = window.setTimeout(function () { closeReasonPicker(true); }, REASON_WINDOW_MS);
    }

    function closeReasonPicker(advance) {
        if (reasonLotId === null) return;
        window.clearTimeout(reasonTimer);
        window.clearInterval(reasonCountdown);
        reasonLotId = null;
        byId("rq-reasons").hidden = true;
        byId("rq-legend-main").hidden = false;
        byId("rq-legend-reasons").hidden = true;
        if (advance) next();
    }

    function pickReason(button) {
        var item = QUEUE.find(function (q) { return q.lot_id === reasonLotId; });
        if (!item) return;
        var reason = button.dataset.reason;
        var label = button.textContent.replace(/^\s*\d\s*/, "").trim();
        status[item.lot_id] = {type: "dismiss", reasonLabel: label};
        post(item.urls.dismiss, {reason: reason});
        closeReasonPicker(true);
    }

    function undo() {
        flash("undo");
        closeReasonPicker(false);
        var last = undoStack.pop();
        if (!last) return;
        delete status[last.lotId];
        post(last.item.urls.undo, {action: last.type});
        goTo(last.index);
    }

    function updateCalcButton(item) {
        var button = byId("rq-calc-button");
        button.dataset.lotId = item.lot_id;
        button.dataset.estimateUrl = item.urls.estimate;
        button.dataset.title = item.title;
        button.dataset.bid = item.current_bid.toFixed(2);
        button.dataset.source = item.source;
        button.dataset.expectedSale = item.expected_sale === null ? "" : item.expected_sale;
    }

    function calculator() {
        if (!currentItem()) return;
        flash("calc");
        byId("rq-calc-button").click();
    }

    function aiReview() {
        var item = currentItem();
        if (!item || item.aiLoading) return;
        flash("ai");
        item.aiLoading = true;
        item.aiError = null;
        renderAI(item);
        post(item.urls.ai_review).then(function (res) {
            item.aiLoading = false;
            if (res.ok) item.ai_review = res.data.ai_review;
            else item.aiError = res.data.error || "failed";
            if (currentItem() === item) renderAI(item);
        }).catch(function () {
            item.aiLoading = false;
            item.aiError = "failed";
            if (currentItem() === item) renderAI(item);
        });
    }

    function openListing() {
        var item = currentItem();
        if (!item) return;
        flash("open");
        window.open(item.url, "_blank", "noopener");
    }

    function toggleHelp(force) {
        var help = byId("rq-help");
        help.hidden = force === undefined ? !help.hidden : !force;
        flash("help");
    }

    var ACTIONS = {
        watch: watch, dismiss: dismiss, calc: calculator, ai: aiReview,
        skip: function () { flash("skip"); next(); }, back: function () { flash("back"); back(); },
        open: openListing, undo: undo, help: function () { toggleHelp(); },
    };

    // ---------- Keyboard ----------
    var KEYS = {
        w: "watch", d: "dismiss", c: "calc", a: "ai", o: "open", u: "undo",
        ArrowRight: "skip", " ": "skip", ArrowLeft: "back", "?": "help",
    };

    function isTyping(target) {
        return target && (target.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(target.tagName));
    }
    function overlayOpen() {
        var calc = byId("calc-dialog");
        return (calc && calc.open) || (window.lotLightbox && window.lotLightbox.isOpen());
    }

    document.addEventListener("keydown", function (event) {
        if (event.ctrlKey || event.metaKey || event.altKey) return;
        if (overlayOpen() || isTyping(event.target)) return;  // their own Esc still works natively

        var help = byId("rq-help");
        if (event.key === "Escape") {
            if (!help.hidden) toggleHelp(false);
            return;
        }

        if (reasonLotId !== null && /^[1-4]$/.test(event.key)) {
            event.preventDefault();
            flash("reason-" + event.key);
            var button = byId("rq-reasons").querySelectorAll("[data-reason]")[parseInt(event.key, 10) - 1];
            if (button) pickReason(button);
            return;
        }

        var action = KEYS[event.key.length === 1 ? event.key.toLowerCase() : event.key];
        if (!action) return;
        event.preventDefault();  // e.g. Space would otherwise scroll the page

        if (!help.hidden && action !== "help") return;
        if (reasonLotId !== null) {
            // Anything but a reason number ends the picker: saved with no
            // reason, then on to the next lot (or the undo/back asked for).
            if (action === "undo") { undo(); return; }
            closeReasonPicker(action !== "back");
            if (action === "back") back();
            return;
        }
        if (!currentItem() && action !== "back" && action !== "undo" && action !== "help") return;
        ACTIONS[action]();
    });

    // ---------- Buttons ----------
    byId("rq-actions").addEventListener("click", function (event) {
        var button = event.target.closest("[data-action]");
        if (!button) return;
        if (reasonLotId !== null && button.dataset.action !== "undo") closeReasonPicker(button.dataset.action !== "back");
        else ACTIONS[button.dataset.action]();
    });
    byId("rq-reasons").addEventListener("click", function (event) {
        var button = event.target.closest("[data-reason]");
        if (button) pickReason(button);
    });
    byId("rq-desc-more").addEventListener("click", function () {
        var item = currentItem();
        if (!item) return;
        item.descExpanded = !item.descExpanded;
        render();
    });
    byId("rq-review-skipped").addEventListener("click", function () {
        var first = QUEUE.findIndex(function (item) { return !status[item.lot_id]; });
        if (first !== -1) goTo(first);
    });
    byId("rq-help-close").addEventListener("click", function () { toggleHelp(false); });
    byId("rq-help").addEventListener("click", function (event) {
        if (event.target === byId("rq-help")) toggleHelp(false);
    });

    // ---------- Legend collapse (remembered, default open) ----------
    var legend = byId("rq-legend");
    var legendTab = byId("rq-legend-tab");
    function setLegendOpen(open) {
        legend.hidden = !open;
        legendTab.hidden = open;
        byId("review-layout").classList.toggle("is-legend-collapsed", !open);
        storageSet(LEGEND_KEY, open ? "1" : "0");
    }
    byId("rq-legend-collapse").addEventListener("click", function () { setLegendOpen(false); });
    legendTab.addEventListener("click", function () { setLegendOpen(true); });
    setLegendOpen(storageGet(LEGEND_KEY) !== "0");

    render();
})();
