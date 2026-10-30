/*
 * Admin > Ideas and Admin > Bot suggestions (albright_control/review_list.html).
 * One script for both pages. Vanilla JS, no libraries.
 *
 * The inbox (Option C of docs/design/ideas_layout_options.html): clicking a
 * title on the left shows its details on the right (below it on a phone).
 * The detail's status buttons - or C / P / I / X for the selected item - save
 * without a reload: the item leaves the current tab at once, the next one is
 * selected, the tab counts and the Admin nav's New badges update, and a
 * toast offers "Undo" for a few seconds. Notes save in place too.
 *
 * Daily review (the New tab's "Start review"): New items one at a time,
 * oldest first, with the reseller review queue's card, legend and help
 * overlay (review_queue.js's pattern):
 *   C considering   P planned   I in progress   X declined   (each saves, then next)
 *   N add a note    Y copy as Claude Code prompt
 *   →/Space skip    ← back      U undo          ? help
 *
 * Without JS the first item's details show, and the status buttons, notes and
 * filters post or submit normally (the page reloads).
 */
(function () {
    "use strict";

    var root = document.getElementById("review-list");
    if (!root) return;
    var TAB = root.dataset.status;
    var UNDO_MS = 6000;
    var LABELS = {};
    root.querySelectorAll("#rl-tabs [data-tab]").forEach(function (tab) {
        LABELS[tab.dataset.tab] = tab.firstChild.textContent.trim();
    });
    var KEY_STATUSES = {c: "considering", p: "planned", i: "in_progress", x: "declined"};

    function byId(id) { return document.getElementById(id); }
    function csrfToken() {
        var match = document.cookie.match(/csrftoken=([^;]+)/);
        if (match) return match[1];
        var input = document.querySelector("input[name=csrfmiddlewaretoken]");
        return input ? input.value : "";
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
    function el(tag, className, text) {
        var node = document.createElement(tag);
        if (className) node.className = className;
        if (text != null) node.textContent = text;
        return node;
    }
    function copyText(text, button) {
        if (!navigator.clipboard) return;
        navigator.clipboard.writeText(text).then(function () {
            if (!button) return;
            var label = button.textContent;
            button.textContent = "Copied";
            setTimeout(function () { button.textContent = label; }, 1500);
        });
    }
    function isTyping(target) {
        return target && (target.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(target.tagName));
    }

    // ---------- Counts: the tabs and the Admin nav's New badges ----------
    function updateCounts(counts) {
        Object.keys(counts || {}).forEach(function (key) {
            var badge = root.querySelector('#rl-tabs [data-count="' + key + '"]');
            if (badge) badge.textContent = counts[key];
        });
    }

    function setItemBadge(link, count) {
        var badge = link.querySelector(".navbar__badge");
        if (!count) { if (badge) badge.remove(); return; }
        if (!badge) {
            link.appendChild(document.createTextNode(" "));
            badge = link.appendChild(el("span", "badge warn navbar__badge"));
        }
        badge.textContent = count;
    }

    function setGroupBadge(group) {
        var trigger = group.querySelector(".navbar__dropdown-trigger");
        var total = 0;
        group.querySelectorAll(".navbar__dropdown-menu .navbar__badge").forEach(function (b) {
            total += parseInt(b.textContent, 10) || 0;
        });
        var badge = trigger.querySelector(".navbar__badge");
        if (!total) { if (badge) badge.remove(); return; }
        if (!badge) {
            badge = el("span", "badge warn navbar__badge");
            trigger.insertBefore(badge, trigger.querySelector(".navbar__dropdown-caret"));
        }
        badge.textContent = total;
        badge.appendChild(el("span", "visually-hidden", " need attention"));
    }

    function updateNav(newCounts) {
        if (!newCounts) return;
        var urls = {ideas: root.dataset.navIdeas, suggestions: root.dataset.navSuggestions};
        Object.keys(urls).forEach(function (kind) {
            var link = document.querySelector('.navbar__dropdown-menu a[href="' + urls[kind] + '"]');
            if (!link || newCounts[kind] == null) return;
            setItemBadge(link, newCounts[kind]);
            setGroupBadge(link.closest(".navbar__dropdown"));
        });
    }

    // ---------- Toast with Undo (the site's toast styles) ----------
    function toast(message, onUndo) {
        var container = byId("toast-container");
        if (!container) {
            container = el("div", "toast-container");
            container.id = "toast-container";
            container.setAttribute("role", "status");
            container.setAttribute("aria-live", "polite");
            document.body.appendChild(container);
        }
        var item = el("div", "toast toast--success ib-toast");
        item.appendChild(el("span", "", message));
        if (onUndo) {
            item.appendChild(el("span", "", "·"));
            var undo = el("button", "ib-toast__undo", "Undo");
            undo.type = "button";
            undo.addEventListener("click", function () { item.remove(); onUndo(); });
            item.appendChild(undo);
        }
        container.appendChild(item);
        setTimeout(function () { item.remove(); }, UNDO_MS);
        return item;
    }

    // ---------- The inbox: items on the left, the selected one's detail on the right ----------
    function rowFor(id) { return root.querySelector('.ib-item[data-id="' + id + '"]'); }
    function detailFor(id) { return byId("rl-detail-" + id); }
    function items() { return Array.prototype.slice.call(root.querySelectorAll(".ib-item")); }
    function visibleItems() { return items().filter(function (item) { return !item.hidden; }); }
    function selectedItem() { return root.querySelector(".ib-item.sel:not([hidden])"); }

    function select(item, reveal) {
        items().forEach(function (other) {
            var on = other === item;
            other.classList.toggle("sel", on);
            other.setAttribute("aria-pressed", on ? "true" : "false");
            var detail = detailFor(other.dataset.id);
            if (detail) detail.hidden = !on;
        });
        // On a phone the detail is below the list - bring it into view when picked.
        if (item && reveal && window.matchMedia("(max-width: 760px)").matches) {
            detailFor(item.dataset.id).scrollIntoView({behavior: "smooth", block: "start"});
        }
    }

    // The first status button still showing is the suggested one (the mockup's "primary").
    function markPrimary(detail) {
        var first = true;
        detail.querySelectorAll("[data-status-form] button[name=status]").forEach(function (b) {
            b.classList.toggle("primary", !b.hidden && first);
            if (!b.hidden) first = false;
        });
    }

    function refreshEmpty() {
        var count = visibleItems().length;
        byId("rl-split").hidden = count === 0;
        byId("rl-empty").hidden = count > 0;
        byId("rl-count").hidden = count === 0;
        var start = byId("rl-start-review");
        if (start) start.hidden = TAB !== "new" || count === 0;
    }

    function adjustTotal(delta) {
        var total = root.querySelector("#rl-count [data-total]");
        if (total) total.textContent = Math.max(0, (parseInt(total.textContent, 10) || 0) + delta);
    }

    // Shows an item as having this status: on a status tab it leaves (the next one is selected) or comes
    // back; on All its status badges change.
    function showRowStatus(id, status) {
        var item = rowFor(id), detail = detailFor(id);
        if (!item) return;
        var wasIn = !item.hidden;
        item.dataset.status = status;
        detail.querySelectorAll("[data-status-form] button[name=status]").forEach(function (b) {
            b.hidden = b.value === status;
        });
        markPrimary(detail);
        [item, detail].forEach(function (node) {
            var badge = node.querySelector("[data-row-status]");
            if (badge) badge.textContent = LABELS[status] || status;
        });
        if (TAB !== "all") {
            var list = visibleItems(), at = list.indexOf(item);
            item.hidden = status !== TAB;
            if (wasIn !== !item.hidden) adjustTotal(item.hidden ? -1 : 1);
            if (item.hidden && item.classList.contains("sel")) {
                select(list[at + 1] || list[at - 1] || null);
            } else if (!item.hidden && !selectedItem()) {
                select(item);
            }
            if (item.hidden) detail.hidden = true;
        }
        refreshEmpty();
    }

    // The one place a status changes - the detail's buttons, its keys, review mode and every Undo.
    // Shown at once, saved in the background, put back if the save fails.
    function changeStatus(id, url, status, previous, isUndo) {
        showRowStatus(id, status);
        return post(url, {status: status, undo: !!isUndo}).then(function (res) {
            if (!res.ok) throw new Error(res.data.error || "save failed");
            updateCounts(res.data.counts);
            updateNav(res.data.new_counts);
            return res.data;
        }).catch(function () {
            showRowStatus(id, previous);
            toast("Couldn't save that - try again.");
            throw new Error("save failed");
        });
    }

    function moveTo(id, status) {
        var item = rowFor(id);
        var previous = item.dataset.status, url = item.dataset.statusUrl;
        if (status === previous) return;
        changeStatus(id, url, status, previous).then(function (data) {
            toast("Moved to " + data.label, function () { changeStatus(id, url, previous, status, true).catch(function () {}); });
        }).catch(function () {});
    }

    root.addEventListener("click", function (event) {
        var item = event.target.closest(".ib-item");
        if (item) { select(item, true); return; }
        var copy = event.target.closest(".js-rl-copy");
        if (copy) {
            var source = byId(copy.dataset.copyTarget);
            if (source) copyText(source.textContent, copy);
        }
    });

    root.addEventListener("submit", function (event) {
        var form = event.target;
        if (!window.fetch) return;
        if (form.matches("[data-status-form]") && event.submitter && event.submitter.name === "status") {
            event.preventDefault();
            moveTo(form.closest(".ib-detail").dataset.id, event.submitter.value);
        } else if (form.matches("[data-notes-form]")) {
            event.preventDefault();
            var id = form.closest(".ib-detail").dataset.id;
            saveNotes(id, rowFor(id).dataset.notesUrl, form.elements.my_notes.value, form.querySelector("[data-notes-status]"));
        }
    });

    // C / P / I / X - the key hints on the detail's buttons - for the selected item (not while reviewing,
    // which has its own keys, or while typing).
    document.addEventListener("keydown", function (event) {
        if (reviewing || event.ctrlKey || event.metaKey || event.altKey || isTyping(event.target)) return;
        if (byId("rl-list-view").hidden) return;
        var status = KEY_STATUSES[event.key.length === 1 ? event.key.toLowerCase() : ""];
        var item = selectedItem();
        if (!status || !item || item.dataset.status === status) return;
        event.preventDefault();
        moveTo(item.dataset.id, status);
    });

    // Filters apply as soon as one changes (search: on Enter or leaving the box); the Filter button is
    // only for no-JS use (inbox.css hides it when JS runs).
    var filters = byId("rl-filters");
    if (filters) filters.addEventListener("change", function () { filters.submit(); });

    root.querySelectorAll(".ib-detail").forEach(markPrimary);

    function saveNotes(id, url, value, statusEl) {
        if (statusEl) statusEl.textContent = "Saving…";
        return post(url, {my_notes: value}).then(function (res) {
            if (statusEl) statusEl.textContent = res.ok ? "Saved" : (res.data.error || "Couldn't save");
            if (!res.ok) return false;
            var detail = detailFor(id);
            if (detail) detail.querySelector("textarea[name=my_notes]").value = value;
            QUEUE.forEach(function (item) { if (String(item.id) === String(id)) item.notes = value; });
            return true;
        }).catch(function () {
            if (statusEl) statusEl.textContent = "Couldn't save";
            return false;
        });
    }

    // ---------- Daily review ----------
    var queueEl = byId("rl-queue");
    var QUEUE = queueEl ? JSON.parse(queueEl.textContent) : [];
    if (!queueEl) return;

    var queue = [], index = 0, decided = {}, undoStack = [], reviewing = false;

    function currentItem() { return queue[index]; }
    function setText(id, text) { byId(id).textContent = text == null ? "" : text; }

    function start() {
        // Anything already moved out of New from the list stays out.
        queue = QUEUE.filter(function (item) {
            var row = rowFor(item.id);
            return !row || row.dataset.status === "new";
        });
        index = 0;
        decided = {};
        undoStack = [];
        reviewing = true;
        byId("rl-list-view").hidden = true;
        byId("rl-start-review").hidden = true;
        byId("rl-review").hidden = false;
        window.scrollTo(0, 0);
        render();
    }

    function exit() {
        reviewing = false;
        byId("rv-help").hidden = true;
        byId("rl-review").hidden = true;
        byId("rl-list-view").hidden = false;
        refreshEmpty();
    }

    function renderStatus(item) {
        var box = byId("rv-status");
        var st = decided[item.id];
        box.hidden = !st;
        box.textContent = st ? "Moved to " + (LABELS[st] || st) + " - press U to undo" : "";
    }

    function render() {
        var item = currentItem();
        if (!item) { showDone(); return; }
        byId("rv-card").hidden = false;
        byId("rv-done").hidden = true;
        setText("rv-progress", (index + 1) + " of " + queue.length);

        var badges = byId("rv-badges");
        badges.textContent = "";
        badges.appendChild(el("span", "badge neutral", item.app));
        badges.appendChild(el("span", "badge " + (item.priority === "urgent" ? "loss" : item.priority === "high" ? "warn" : "neutral"),
                              item.priority_label.toLowerCase() + " priority"));
        if (item.impact_label) {
            badges.appendChild(el("span", "badge neutral", item.impact_label.toLowerCase() + " impact · " +
                                                            item.effort_label.toLowerCase() + " effort"));
        }
        badges.appendChild(el("span", "badge neutral", item.source));
        if (item.category) badges.appendChild(el("span", "badge neutral", item.category));
        if (item.area) badges.appendChild(el("span", "badge neutral", item.area));

        var title = byId("rv-title");
        title.textContent = "";
        if (item.urls.detail) {
            var link = el("a", "", item.title);
            link.href = item.urls.detail;
            title.appendChild(link);
        } else {
            title.textContent = item.title;
        }
        setText("rv-meta", "Created " + item.created + " · " + item.created_full +
                (item.duplicate_of ? " · possible duplicate of #" + item.duplicate_of : ""));
        // description_html is review_list_tags.rich_text's output - escaped on the server.
        byId("rv-desc").innerHTML = item.description_html || "<p>(no description)</p>";
        byId("rv-original-box").hidden = !item.original_message;
        setText("rv-original", item.original_message);

        var links = byId("rv-links");
        links.textContent = "";
        (item.links || []).forEach(function (l) {
            var li = el("li");
            if (l.href) {
                var a = el("a", "crypto-link" + (l.verified ? "" : " crypto-link--unverified"), l.text);
                a.href = l.href;
                if (l.verified) { a.target = "_blank"; a.rel = "noopener noreferrer"; }
                else { a.title = "Not on your verified list - you'll see a warning first"; a.appendChild(el("span", "badge warn", "unverified")); }
                li.appendChild(a);
            } else {
                li.appendChild(el("span", "crypto-nolink", l.text));
            }
            links.appendChild(li);
        });
        byId("rv-links-box").hidden = !(item.links && item.links.length);

        byId("rv-notes-box").hidden = !item.notes;
        byId("rv-notes").value = item.notes || "";
        setText("rv-notes-status", "Ctrl+Enter saves · Esc goes back to the keys");
        renderStatus(item);
    }

    function showDone() {
        var pending = queue.filter(function (item) { return !decided[item.id]; }).length;
        byId("rv-card").hidden = true;
        byId("rv-done").hidden = false;
        setText("rv-progress", queue.length + " of " + queue.length);
        if (pending === 0) {
            setText("rv-done-title", "All caught up");
            setText("rv-done-text", "Everything that was New has a status now.");
            byId("rv-review-skipped").hidden = true;
        } else {
            setText("rv-done-title", "End of the review");
            setText("rv-done-text", pending + " you skipped " + (pending === 1 ? "is" : "are") + " still in New.");
            byId("rv-review-skipped").hidden = false;
        }
    }

    function goTo(newIndex) {
        index = Math.max(0, Math.min(newIndex, queue.length));
        render();
    }
    function next() { goTo(index + 1); }
    function back() { if (index > 0) goTo(index - 1); }

    function flash(action) {
        var row = document.querySelector('#rv-legend [data-legend="' + action + '"]');
        if (!row) return;
        row.classList.remove("is-flash");
        void row.offsetWidth;
        row.classList.add("is-flash");
        window.setTimeout(function () { row.classList.remove("is-flash"); }, 700);
    }

    function decide(status) {
        var item = currentItem();
        if (!item) return;
        flash(status);
        var previous = decided[item.id] || "new";
        if (previous === status) { next(); return; }
        decided[item.id] = status;
        undoStack.push({item: item, index: index, previous: previous, status: status});
        changeStatus(item.id, item.urls.status, status, previous).catch(function () {
            if (previous === "new") delete decided[item.id]; else decided[item.id] = previous;
            undoStack.pop();
            if (currentItem() === item) renderStatus(item);
        });
        next();
    }

    function undo() {
        flash("undo");
        var last = undoStack.pop();
        if (!last) return;
        if (last.previous === "new") delete decided[last.item.id]; else decided[last.item.id] = last.previous;
        changeStatus(last.item.id, last.item.urls.status, last.previous, last.status, true).catch(function () {});
        goTo(last.index);
    }

    function openNote() {
        var item = currentItem();
        if (!item) return;
        flash("note");
        byId("rv-notes-box").hidden = false;
        var box = byId("rv-notes");
        box.focus();
        box.setSelectionRange(box.value.length, box.value.length);
    }

    function saveNote() {
        var item = currentItem();
        if (!item) return;
        var value = byId("rv-notes").value;
        saveNotes(item.id, item.urls.notes, value, byId("rv-notes-status")).then(function (ok) {
            if (ok) byId("rv-notes").blur();
        });
    }

    function copyPrompt() {
        var item = currentItem();
        if (!item) return;
        flash("copy");
        copyText(item.prompt, byId("rv-actions").querySelector('[data-action="copy"] span:last-child'));
    }

    function toggleHelp(force) {
        var help = byId("rv-help");
        help.hidden = force === undefined ? !help.hidden : !force;
        flash("help");
    }

    var ACTIONS = {
        considering: function () { decide("considering"); }, planned: function () { decide("planned"); },
        in_progress: function () { decide("in_progress"); }, declined: function () { decide("declined"); },
        note: openNote, copy: copyPrompt, skip: function () { flash("skip"); next(); },
        back: function () { flash("back"); back(); }, undo: undo, help: function () { toggleHelp(); },
    };
    var KEYS = {c: "considering", p: "planned", i: "in_progress", x: "declined", n: "note", y: "copy",
                ArrowRight: "skip", " ": "skip", ArrowLeft: "back", u: "undo", "?": "help"};

    document.addEventListener("keydown", function (event) {
        if (!reviewing || event.ctrlKey || event.metaKey || event.altKey || isTyping(event.target)) return;
        var help = byId("rv-help");
        if (event.key === "Escape") {
            if (!help.hidden) toggleHelp(false);
            return;
        }
        var action = KEYS[event.key.length === 1 ? event.key.toLowerCase() : event.key];
        if (!action) return;
        event.preventDefault();
        if (!help.hidden && action !== "help") return;
        if (!currentItem() && action !== "back" && action !== "undo" && action !== "help") return;
        ACTIONS[action]();
    });

    byId("rv-notes").addEventListener("keydown", function (event) {
        if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) { event.preventDefault(); saveNote(); }
        else if (event.key === "Escape") { event.preventDefault(); byId("rv-notes").blur(); }
    });
    byId("rv-notes-save").addEventListener("click", saveNote);
    byId("rv-actions").addEventListener("click", function (event) {
        var button = event.target.closest("[data-action]");
        if (button) ACTIONS[button.dataset.action]();
    });
    byId("rv-review-skipped").addEventListener("click", function () {
        var first = queue.findIndex(function (item) { return !decided[item.id]; });
        if (first !== -1) goTo(first);
    });
    byId("rl-start-review").addEventListener("click", start);
    byId("rv-exit").addEventListener("click", exit);
    byId("rv-done-exit").addEventListener("click", exit);
    byId("rv-help-close").addEventListener("click", function () { toggleHelp(false); });
    byId("rv-help").addEventListener("click", function (event) {
        if (event.target === byId("rv-help")) toggleHelp(false);
    });
})();
