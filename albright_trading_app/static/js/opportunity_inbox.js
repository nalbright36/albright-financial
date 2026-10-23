/*
 * The Opportunities Inbox (albright_crypto_app/inbox.html): one opportunity
 * at a time, keyboard first - the reseller review queue's pattern
 * (review_queue.js) and CSS. The queue arrives as JSON (#inbox-data, built by
 * inbox_views.card); every action posts to inbox_views' JSON endpoints
 * without a reload. Vanilla JS, no libraries.
 *
 *   K keep -> Pipeline "Saved", next     S skip (+ 1-4 reason within 3s), next
 *   W walk me through it                 P draft proposal (then: tailor resume)
 *   O open posting                       →/← move     U undo     ? help
 *
 * Shortcuts are ignored while typing in a field. "Open" uses the link the
 * server checked: verified domains directly, anything else via the
 * "not on your verified list" page.
 */
(function () {
    "use strict";

    var dataEl = document.getElementById("inbox-data");
    if (!dataEl) return;
    var QUEUE = JSON.parse(dataEl.textContent);
    var LEGEND_KEY = "opportunity-inbox-legend-open";
    var REASON_WINDOW_MS = 3000;

    function byId(id) { return document.getElementById(id); }
    function csrfToken() {
        var match = document.cookie.match(/csrftoken=([^;]+)/);
        return match ? match[1] : "";
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
    function setText(id, text) { byId(id).textContent = text == null ? "" : text; }
    function el(tag, className, text) {
        var node = document.createElement(tag);
        if (className) node.className = className;
        if (text != null) node.textContent = text;
        return node;
    }

    // ---------- State ----------
    var index = 0;
    var status = {};      // id -> {type: "keep"|"skip", reasonLabel}
    var undoStack = [];   // [{id, type, index, item}]
    var reasonTimer = null, reasonCountdown = null, reasonId = null;

    function pendingCount() {
        return QUEUE.filter(function (item) { return !status[item.id]; }).length;
    }
    function currentItem() { return QUEUE[index]; }

    // ---------- Rendering ----------
    function renderList(id, items, boxId) {
        var list = byId(id);
        list.textContent = "";
        (items || []).forEach(function (text) { list.appendChild(el("li", "", text)); });
        byId(boxId).hidden = !(items && items.length);
    }

    function renderAI(item) {
        var box = byId("in-ai");
        var ai = item.ai;
        box.textContent = "";
        if (!ai) { box.hidden = true; return; }
        box.hidden = false;
        if (ai.loading) {
            box.innerHTML = '<span class="spinner" aria-hidden="true"></span> ';
            box.appendChild(document.createTextNode(ai.loading));
            return;
        }
        if (ai.error) { box.textContent = ai.error; return; }
        if (ai.walkthrough) {
            var w = ai.walkthrough;
            box.appendChild(el("div", "review-ai__head", "Walkthrough" + (w.difficulty ? " · " + w.difficulty : "") +
                (w.hours ? " · ~" + w.hours + " h" : "")));
            (w.warnings || []).forEach(function (text) { box.appendChild(el("p", "crypto-warning", text)); });
            (w.sections || []).forEach(function (section) {
                box.appendChild(el("div", "row-count", section.title));
                var ul = el("ul");
                (section.items || []).forEach(function (text) { ul.appendChild(el("li", "", text)); });
                box.appendChild(ul);
            });
            var more = el("a", "", "Full walkthrough");
            more.href = w.detail_url;
            box.appendChild(more);
        }
        if (ai.draft) {
            var d = ai.draft;
            box.appendChild(el("div", "review-ai__head", d.label + (ai.reused ? " (saved earlier)" : "")));
            if (d.unmet && d.unmet.length) box.appendChild(el("p", "crypto-warning", "Requirements you don't show: " + d.unmet.join("; ")));
            if (d.removed && d.removed.length) box.appendChild(el("p", "crypto-danger", "Removed (not in your profile): " + d.removed.join(" · ")));
            var pre = el("pre", "crypto-source-text", d.text);
            box.appendChild(pre);
            var row = el("div");
            row.style.cssText = "display: flex; gap: var(--space-2); flex-wrap: wrap; margin-top: var(--space-2);";
            var copy = el("button", "btn btn-secondary btn-sm", "Copy");
            copy.type = "button";
            copy.addEventListener("click", function () {
                if (navigator.clipboard) navigator.clipboard.writeText(d.text).then(function () { copy.textContent = "Copied"; });
            });
            row.appendChild(copy);
            var download = el("a", "btn btn-secondary btn-sm", "Download");
            download.href = d.download_url;
            row.appendChild(download);
            if (d.kind === "proposal") {
                var resume = el("button", "btn btn-secondary btn-sm", "Tailor resume");
                resume.type = "button";
                resume.addEventListener("click", function () { draft("resume"); });
                row.appendChild(resume);
            }
            var open = el("a", "btn btn-secondary btn-sm", "Open drafts");
            open.href = d.detail_url;
            row.appendChild(open);
            box.appendChild(row);
        }
    }

    function renderStatus(item) {
        var box = byId("in-status");
        var st = status[item.id];
        if (!st) { box.hidden = true; box.textContent = ""; return; }
        box.hidden = false;
        box.textContent = st.type === "keep"
            ? "Kept - in the Pipeline as Saved. Press U to undo"
            : "Skipped" + (st.reasonLabel ? " (" + st.reasonLabel + ")" : "") + " - press U to undo";
    }

    function render() {
        var item = currentItem();
        if (!item) { showDone(); return; }
        byId("in-card").hidden = false;
        byId("in-done").hidden = true;
        setText("inbox-progress", (index + 1) + " of " + QUEUE.length);

        setText("in-type", item.type);
        // Job boards' terms: credit them as "via <site>", linking to the job on their site.
        var source = byId("in-source");
        source.textContent = "";
        if (item.via && item.open_url) {
            var via = el("a", "crypto-link crypto-via", "via " + item.source);
            via.href = item.open_url;
            via.target = "_blank";
            via.rel = "noopener";
            source.appendChild(via);
        } else {
            source.textContent = item.source;
        }
        var safety = byId("in-safety");
        safety.textContent = item.safety_verdict;
        safety.className = "badge " + (item.safety_warnings.length ? "warn" : "gain");
        var title = byId("in-title");
        title.href = item.detail_url;
        title.textContent = item.title;
        setText("in-subtitle", [item.company, item.location].filter(Boolean).join(" · ") || "Company and location not stated");
        setText("in-pay", item.pay || "not stated");
        setText("in-deadline", item.deadline || "none");
        setText("in-posted", item.posted);

        var why = byId("in-why"), skills = byId("in-skills");
        skills.textContent = "";
        if (item.uses_fit) {
            setText("in-score-label", "Fit score");
            setText("in-score", item.fit_score === null ? "pending" : item.fit_score + "/100" +
                (item.fit_method === "Keywords" ? " (keywords)" : ""));
            setText("in-why-label", "Why it fits" + (item.role_match ? " · " + item.role_match : ""));
            why.textContent = item.fit_reason || (item.fit_score === null ? "Not scored yet - waiting for the next scan." : "");
            item.matched_skills.forEach(function (skill) {
                var chip = el("span", "badge gain", skill);
                chip.style.margin = "0 4px 4px 0";
                skills.appendChild(chip);
            });
        } else {
            setText("in-score-label", "EV/hour");
            setText("in-score", item.ev_per_hour === null ? "hours unknown" : "$" + item.ev_per_hour.toFixed(2) + "/h");
            setText("in-why-label", "The score");
            why.textContent = "Expected value $" + item.ev_mid.toFixed(2) + " per job, " + item.confidence + " confidence.";
        }
        renderList("in-missing", item.missing_requirements, "in-missing-box");
        renderList("in-warnings", item.safety_warnings, "in-warnings-box");

        var also = byId("in-also");
        also.textContent = "";
        also.hidden = !item.also_on.length;
        if (item.also_on.length) {
            also.appendChild(document.createTextNode("Also posted on: "));
            item.also_on.forEach(function (link, i) {
                if (i) also.appendChild(document.createTextNode(", "));
                if (link.href) {
                    var a = el("a", "crypto-link", link.source);
                    a.href = link.href;
                    a.target = "_blank";
                    a.rel = "noopener";
                    also.appendChild(a);
                } else {
                    also.appendChild(document.createTextNode(link.source));
                }
            });
        }
        byId("in-actions").querySelector('[data-action="draft"]').hidden = !item.is_income;
        renderAI(item);
        renderStatus(item);
    }

    function showDone() {
        var pending = pendingCount();
        byId("in-card").hidden = true;
        byId("in-done").hidden = false;
        setText("inbox-progress", QUEUE.length + " of " + QUEUE.length);
        if (pending === 0) {
            setText("in-done-title", "All caught up");
            setText("in-done-text", "Everything here has been kept or skipped.");
            byId("in-review-passed").hidden = true;
        } else {
            setText("in-done-title", "End of the Inbox");
            setText("in-done-text", pending + " you moved past without deciding " + (pending === 1 ? "is" : "are") + " still here.");
            byId("in-review-passed").hidden = false;
        }
    }

    function goTo(newIndex) {
        index = Math.max(0, Math.min(newIndex, QUEUE.length));
        render();
    }
    function next() { goTo(index + 1); }
    function back() { if (index > 0) goTo(index - 1); }

    function flash(id) {
        var row = document.querySelector('#in-legend [data-legend="' + id + '"]');
        if (!row) return;
        row.classList.remove("is-flash");
        void row.offsetWidth;
        row.classList.add("is-flash");
        window.setTimeout(function () { row.classList.remove("is-flash"); }, 700);
    }

    // ---------- Actions ----------
    function keep() {
        var item = currentItem();
        if (!item) return;
        flash("keep");
        if (status[item.id] && status[item.id].type === "keep") { next(); return; }
        status[item.id] = {type: "keep"};
        undoStack.push({id: item.id, type: "keep", index: index, item: item});
        post(item.urls.keep).then(function (res) {
            if (!res.ok) { delete status[item.id]; undoStack.pop(); if (currentItem() === item) render(); }
        });
        next();
    }

    function skip() {
        var item = currentItem();
        if (!item) return;
        flash("skip");
        status[item.id] = {type: "skip"};
        undoStack.push({id: item.id, type: "skip", index: index, item: item});
        post(item.urls.skip, {});
        renderStatus(item);
        openReasonPicker(item);
    }

    function openReasonPicker(item) {
        reasonId = item.id;
        byId("in-reasons").hidden = false;
        byId("in-legend-main").hidden = true;
        byId("in-legend-reasons").hidden = false;
        var remaining = REASON_WINDOW_MS / 1000;
        setText("in-reasons-timer", remaining + "s");
        window.clearInterval(reasonCountdown);
        reasonCountdown = window.setInterval(function () {
            remaining -= 1;
            setText("in-reasons-timer", Math.max(0, remaining) + "s");
        }, 1000);
        window.clearTimeout(reasonTimer);
        reasonTimer = window.setTimeout(function () { closeReasonPicker(true); }, REASON_WINDOW_MS);
    }

    function closeReasonPicker(advance) {
        if (reasonId === null) return;
        window.clearTimeout(reasonTimer);
        window.clearInterval(reasonCountdown);
        reasonId = null;
        byId("in-reasons").hidden = true;
        byId("in-legend-main").hidden = false;
        byId("in-legend-reasons").hidden = true;
        if (advance) next();
    }

    function pickReason(button) {
        var item = QUEUE.find(function (q) { return q.id === reasonId; });
        if (!item) return;
        var label = button.textContent.replace(/^\s*\d\s*/, "").trim();
        status[item.id] = {type: "skip", reasonLabel: label};
        post(item.urls.skip, {reason: button.dataset.reason});
        closeReasonPicker(true);
    }

    function undo() {
        flash("undo");
        closeReasonPicker(false);
        var last = undoStack.pop();
        if (!last) return;
        delete status[last.id];
        post(last.item.urls.undo, {action: last.type});
        goTo(last.index);
    }

    function runAI(url, body, loadingText, key) {
        var item = currentItem();
        if (!item || (item.ai && item.ai.loading)) return;
        item.ai = {loading: loadingText};
        renderAI(item);
        post(url, body).then(function (res) {
            if (!res.ok) item.ai = {error: res.data.error || "That didn't work - try again."};
            else if (key === "walkthrough") item.ai = {walkthrough: res.data.walkthrough};
            else item.ai = {draft: res.data.draft, reused: res.data.reused};
            if (currentItem() === item) renderAI(item);
        }).catch(function () {
            item.ai = {error: "That didn't work - try again."};
            if (currentItem() === item) renderAI(item);
        });
    }

    function walkthrough() {
        var item = currentItem();
        if (!item) return;
        flash("walkthrough");
        runAI(item.urls.walkthrough, {}, "Writing a walkthrough…", "walkthrough");
    }

    function draft(kind) {
        var item = currentItem();
        if (!item || !item.is_income) return;
        flash("draft");
        kind = kind === "resume" ? "resume" : "proposal";
        runAI(item.urls.draft, {kind: kind}, kind === "resume" ? "Tailoring your resume…" : "Drafting a proposal…", "draft");
    }

    function openPosting() {
        var item = currentItem();
        if (!item || !item.open_url) return;
        flash("open");
        window.open(item.open_url, "_blank", "noopener");
    }

    function toggleHelp(force) {
        var help = byId("in-help");
        help.hidden = force === undefined ? !help.hidden : !force;
        flash("help");
    }

    var ACTIONS = {
        keep: keep, skip: skip, walkthrough: walkthrough, draft: function () { draft("proposal"); },
        open: openPosting, next: function () { flash("next"); next(); }, back: function () { flash("back"); back(); },
        undo: undo, help: function () { toggleHelp(); },
    };
    var KEYS = {k: "keep", s: "skip", w: "walkthrough", p: "draft", o: "open", u: "undo",
                ArrowRight: "next", ArrowLeft: "back", "?": "help"};

    function isTyping(target) {
        return target && (target.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(target.tagName));
    }

    document.addEventListener("keydown", function (event) {
        if (event.ctrlKey || event.metaKey || event.altKey || isTyping(event.target)) return;
        var help = byId("in-help");
        if (event.key === "Escape") {
            if (!help.hidden) toggleHelp(false);
            return;
        }
        if (reasonId !== null && /^[1-4]$/.test(event.key)) {
            event.preventDefault();
            flash("reason-" + event.key);
            var button = byId("in-reasons").querySelectorAll("[data-reason]")[parseInt(event.key, 10) - 1];
            if (button) pickReason(button);
            return;
        }
        var action = KEYS[event.key.length === 1 ? event.key.toLowerCase() : event.key];
        if (!action) return;
        event.preventDefault();
        if (!help.hidden && action !== "help") return;
        if (reasonId !== null) {
            // Anything but a reason number ends the picker (saved with no reason).
            if (action === "undo") { undo(); return; }
            closeReasonPicker(action !== "back");
            if (action === "back") back();
            return;
        }
        if (!currentItem() && action !== "back" && action !== "undo" && action !== "help") return;
        ACTIONS[action]();
    });

    // ---------- Buttons ----------
    byId("in-actions").addEventListener("click", function (event) {
        var button = event.target.closest("[data-action]");
        if (!button) return;
        if (reasonId !== null && button.dataset.action !== "undo") closeReasonPicker(button.dataset.action !== "back");
        else ACTIONS[button.dataset.action]();
    });
    byId("in-reasons").addEventListener("click", function (event) {
        var button = event.target.closest("[data-reason]");
        if (button) pickReason(button);
    });
    byId("in-review-passed").addEventListener("click", function () {
        var first = QUEUE.findIndex(function (item) { return !status[item.id]; });
        if (first !== -1) goTo(first);
    });
    byId("inbox-type").addEventListener("change", function () { byId("inbox-type-form").submit(); });
    byId("in-help-close").addEventListener("click", function () { toggleHelp(false); });
    byId("in-help").addEventListener("click", function (event) {
        if (event.target === byId("in-help")) toggleHelp(false);
    });

    // ---------- Legend collapse (remembered, default open) ----------
    var legend = byId("in-legend");
    var legendTab = byId("in-legend-tab");
    function setLegendOpen(open) {
        legend.hidden = !open;
        legendTab.hidden = open;
        byId("review-layout").classList.toggle("is-legend-collapsed", !open);
        storageSet(LEGEND_KEY, open ? "1" : "0");
    }
    byId("in-legend-collapse").addEventListener("click", function () { setLegendOpen(false); });
    legendTab.addEventListener("click", function () { setLegendOpen(true); });
    setLegendOpen(storageGet(LEGEND_KEY) !== "0");

    render();
})();
