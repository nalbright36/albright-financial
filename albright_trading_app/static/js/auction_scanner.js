/*
 * Auction Scanner dashboard: column sorting (with paired detail rows),
 * expandable row detail, tabs, a shared filter bar, and collapsible
 * section headers. Vanilla JS, no libraries.
 *
 * Every table renders complete on the server first, and all three tab
 * groups render stacked and visible - this file only reorders/hides
 * things already in the DOM, so the page works (just without these
 * controls) if JavaScript is disabled: every row is visible, and the tab
 * nav links still work as plain in-page anchors.
 */
(function () {
    "use strict";

    var CONFIDENCE_RANK = { high: 3, medium: 2, low: 1, "": 0 };

    function compareRaw(key, rawA, rawB) {
        if (key === "confidence") {
            return (CONFIDENCE_RANK[rawA] || 0) - (CONFIDENCE_RANK[rawB] || 0);
        }
        var numA = parseFloat(rawA);
        var numB = parseFloat(rawB);
        if (!isNaN(numA) && !isNaN(numB)) {
            return numA - numB;
        }
        return (rawA || "").localeCompare(rawB || "");
    }

    // A main row's detail row (if any) is always rendered as the very next
    // <tr> in the markup - no id matching needed to keep them paired.
    function detailOf(row) {
        var next = row.nextElementSibling;
        return next && next.classList.contains("scanner-detail") ? next : null;
    }

    // ---------- Column sorting (per table, independent of the others) ----------
    function initSortableTable(table) {
        var tbody = table.querySelector("tbody");
        var headers = Array.prototype.slice.call(table.querySelectorAll("th[data-sort-key]"));
        if (!tbody || !headers.length) return;

        var sortKey = null;
        var sortAsc = true;

        function sortBy(key) {
            sortAsc = sortKey === key ? !sortAsc : true;
            sortKey = key;

            var rows = Array.prototype.slice.call(tbody.querySelectorAll("tr.scanner-row"));
            var pairs = rows.map(function (row) { return [row, detailOf(row)]; });
            pairs.sort(function (a, b) {
                var cmp = compareRaw(key, a[0].dataset[key], b[0].dataset[key]);
                return sortAsc ? cmp : -cmp;
            });
            pairs.forEach(function (pair) {
                tbody.appendChild(pair[0]);
                if (pair[1]) tbody.appendChild(pair[1]);
            });

            headers.forEach(function (h) { h.classList.remove("sorted-asc", "sorted-desc"); });
            var active = headers.filter(function (h) { return h.dataset.sortKey === key; })[0];
            if (active) active.classList.add(sortAsc ? "sorted-asc" : "sorted-desc");
        }

        headers.forEach(function (h) {
            h.addEventListener("click", function () { sortBy(h.dataset.sortKey); });
        });
    }

    // ---------- Expandable row detail (click the chevron cell, or anywhere
    // on the row that isn't a link/button/form; Enter/Space when the
    // chevron cell itself has keyboard focus) ----------
    function toggleRow(row) {
        var detail = detailOf(row);
        if (!detail) return;
        detail.hidden = !detail.hidden;
        row.classList.toggle("is-expanded", !detail.hidden);
        var toggle = row.querySelector(".row-toggle");
        if (toggle) toggle.setAttribute("aria-expanded", String(!detail.hidden));
    }

    function initExpandableRows() {
        document.querySelectorAll("tr.scanner-row").forEach(function (row) {
            var toggle = row.querySelector(".row-toggle");
            if (!toggle || !detailOf(row)) return;
            toggle.setAttribute("tabindex", "0");
            toggle.setAttribute("role", "button");
            toggle.setAttribute("aria-expanded", "false");
            toggle.setAttribute("aria-label", "Toggle details");
        });

        document.querySelectorAll("table.holdings tbody").forEach(function (tbody) {
            tbody.addEventListener("click", function (event) {
                if (event.target.closest("a, button, form")) return;
                var row = event.target.closest("tr.scanner-row");
                if (!row) return;
                toggleRow(row);
            });
            tbody.addEventListener("keydown", function (event) {
                if (event.key !== "Enter" && event.key !== " ") return;
                if (!event.target.classList.contains("row-toggle")) return;
                event.preventDefault();
                var row = event.target.closest("tr.scanner-row");
                if (row) toggleRow(row);
            });
        });
    }

    // ---------- Collapsible card headers ----------
    // A header carrying data-collapse-key (currently just the Watchlist
    // section) remembers its open/closed state per browser via
    // localStorage; every other collapsible card stays session-only
    // (defaults open on every load), same as before. Storage can throw or
    // come back empty (private windows, blocked site data) - never lets
    // that break the toggle itself, just falls back to "always starts
    // open" in that case.
    function readCollapseState(key) {
        try {
            return window.localStorage.getItem("scanner-collapse:" + key);
        } catch (e) {
            return null;
        }
    }

    function writeCollapseState(key, collapsed) {
        try {
            window.localStorage.setItem("scanner-collapse:" + key, collapsed ? "1" : "0");
        } catch (e) {}
    }

    function initCollapsible() {
        var headers = document.querySelectorAll(".js-collapsible-header");
        headers.forEach(function (header) {
            var body = header.nextElementSibling;
            if (!body) return;
            header.setAttribute("tabindex", "0");
            header.setAttribute("role", "button");

            var storageKey = header.dataset.collapseKey;
            if (storageKey && readCollapseState(storageKey) === "1") {
                body.hidden = true;
                header.classList.add("is-collapsed");
            }
            header.setAttribute("aria-expanded", String(!body.hidden));

            function toggle() {
                var isHidden = body.hidden;
                body.hidden = !isHidden;
                header.classList.toggle("is-collapsed", !isHidden);
                header.setAttribute("aria-expanded", String(isHidden));
                if (storageKey) writeCollapseState(storageKey, !isHidden);
            }

            header.addEventListener("click", function (event) {
                // A link/button inside the header (e.g. "Start review")
                // does its own thing - never also collapses the card.
                if (event.target.closest("a, button")) return;
                toggle();
            });
            header.addEventListener("keydown", function (event) {
                if (event.key !== "Enter" && event.key !== " ") return;
                if (event.target !== header) return;  // Enter on a link inside it follows the link
                event.preventDefault();
                toggle();
            });
        });
    }

    // ---------- AI review buttons: disable + "Reviewing..." on submit ----------
    // The request itself can take up to a minute (web search + an LLM
    // call), so this is just feedback for a real page navigation (POST ->
    // redirect), not an AJAX call - no JS means the form still submits
    // normally, just without this feedback.
    function initAIReviewForms() {
        var forms = document.querySelectorAll(".js-ai-review-form");
        forms.forEach(function (form) {
            form.addEventListener("submit", function () {
                var button = form.querySelector("button");
                if (!button) return;
                button.disabled = true;
                button.textContent = "Reviewing...";
            });
        });
    }

    // ---------- Tabs: Act now / Watch / Performance ----------
    // The server renders all three panels stacked and visible (the no-JS
    // fallback - the nav links are plain in-page anchors to each panel's
    // heading). This only adds show/hide-one-at-a-time plus a #hash so the
    // active tab survives a bookmark or refresh.
    var TAB_NAMES = ["act-now", "watch", "performance"];

    function initTabs() {
        var nav = document.getElementById("scanner-tabs");
        if (!nav) return;
        var links = Array.prototype.slice.call(nav.querySelectorAll(".scanner-tab"));
        var panels = TAB_NAMES.map(function (name) { return document.getElementById(name); }).filter(Boolean);
        if (!links.length || !panels.length) return;

        function activate(name, updateHash) {
            if (TAB_NAMES.indexOf(name) === -1) name = TAB_NAMES[0];
            panels.forEach(function (panel) { panel.hidden = panel.id !== name; });
            links.forEach(function (link) {
                link.classList.toggle("is-active", link.dataset.tab === name);
            });
            if (updateHash) window.history.replaceState(null, "", "#" + name);
        }

        links.forEach(function (link) {
            link.addEventListener("click", function (event) {
                event.preventDefault();
                activate(link.dataset.tab, true);
            });
        });

        var initial = (window.location.hash || "").replace("#", "");
        activate(TAB_NAMES.indexOf(initial) !== -1 ? initial : TAB_NAMES[0], false);
    }

    // ---------- Tab row-count badges (reflect the current filters) ----------
    // Counts every non-hidden main row in each panel's tables, whether or
    // not that table participates in the shared filter bar - a table with
    // no filter bar membership (MaxSold estates, results, runs) just never
    // gets a row hidden, so it always counts in full.
    function updateTabCounts() {
        TAB_NAMES.forEach(function (name) {
            var panel = document.getElementById(name);
            var badge = document.getElementById("tab-badge-" + name);
            if (!panel || !badge) return;
            badge.textContent = panel.querySelectorAll("tr.scanner-row:not([hidden])").length;
        });
    }

    // Recomputes each filterable table's own "N of M shown" count straight
    // from the DOM's current hidden/dismissed state, with no dependency on
    // the current filter field values (initFilters()'s own applyFilters()
    // already keeps this current whenever a filter changes) - just what's
    // needed after a row is added to or removed from a table outside of
    // filtering, e.g. initWatchToggles() moving a row to the Watchlist.
    function refreshFilterCounts() {
        document.querySelectorAll("table[data-filterable]").forEach(function (table) {
            var tbody = table.querySelector("tbody");
            if (!tbody) return;
            var rows = Array.prototype.slice.call(tbody.querySelectorAll("tr.scanner-row:not(.is-dismissed)"));
            var shown = rows.filter(function (row) { return !row.hidden; }).length;
            var countEl = document.getElementById(table.dataset.countTarget);
            if (countEl) countEl.textContent = shown + " of " + rows.length + " shown";
        });
    }

    // ---------- Shared filter bar (source / category / confidence / search /
    // status / numeric ranges) ----------
    // Every element is optional, so a page only needs to render the ones
    // relevant to it (the dashboard uses search+source+category+confidence+
    // the bid/max bid/headroom ranges; the Ledger page uses only status+
    // source) - this bails out entirely only when none of them are present.
    function initFilters() {
        var searchInput = document.getElementById("af-search");
        var sourceSelect = document.getElementById("af-source");
        var categorySelect = document.getElementById("af-category");
        var confidenceSelect = document.getElementById("af-confidence");
        var statusSelect = document.getElementById("af-status");
        var bidMin = document.getElementById("af-bid-min");
        var bidMax = document.getElementById("af-bid-max");
        var maxBidMin = document.getElementById("af-maxbid-min");
        var maxBidMax = document.getElementById("af-maxbid-max");
        var headroomMin = document.getElementById("af-headroom-min");
        var headroomMax = document.getElementById("af-headroom-max");
        var clearButton = document.getElementById("af-clear");
        var presetButtons = Array.prototype.slice.call(document.querySelectorAll(".af-preset"));
        var elements = [
            searchInput, sourceSelect, categorySelect, confidenceSelect, statusSelect,
            bidMin, bidMax, maxBidMin, maxBidMax, headroomMin, headroomMax,
        ].filter(Boolean);
        if (!elements.length) return;

        var tables = Array.prototype.slice.call(document.querySelectorAll("table[data-filterable]"));

        function parsedValue(el) {
            if (!el || el.value === "") return null;
            var num = parseFloat(el.value);
            return isNaN(num) ? null : num;
        }

        // A row whose table doesn't carry this attribute at all (e.g. a
        // leads row has no data-maxbid/data-headroom, a closed-results row
        // has no data-bid/data-headroom) always passes - the range filter
        // simply doesn't apply there, per its own table's columns.
        function inRange(raw, min, max) {
            if (raw === undefined || raw === "") return true;
            var num = parseFloat(raw);
            if (isNaN(num)) return true;
            if (min !== null && num < min) return false;
            if (max !== null && num > max) return false;
            return true;
        }

        function applyFromURL() {
            var params = new URLSearchParams(window.location.search);
            if (searchInput) searchInput.value = params.get("q") || "";
            if (sourceSelect) sourceSelect.value = params.get("source") || "";
            if (categorySelect) categorySelect.value = params.get("category") || "";
            if (confidenceSelect) confidenceSelect.value = params.get("confidence") || "";
            if (statusSelect) statusSelect.value = params.get("status") || "";
            if (bidMin) bidMin.value = params.get("bid_min") || "";
            if (bidMax) bidMax.value = params.get("bid_max") || "";
            if (maxBidMin) maxBidMin.value = params.get("maxbid_min") || "";
            if (maxBidMax) maxBidMax.value = params.get("maxbid_max") || "";
            if (headroomMin) headroomMin.value = params.get("headroom_min") || "";
            if (headroomMax) headroomMax.value = params.get("headroom_max") || "";
        }

        function updateURL() {
            var params = new URLSearchParams();
            if (searchInput && searchInput.value.trim()) params.set("q", searchInput.value.trim());
            if (sourceSelect && sourceSelect.value) params.set("source", sourceSelect.value);
            if (categorySelect && categorySelect.value) params.set("category", categorySelect.value);
            if (confidenceSelect && confidenceSelect.value) params.set("confidence", confidenceSelect.value);
            if (statusSelect && statusSelect.value) params.set("status", statusSelect.value);
            if (bidMin && bidMin.value) params.set("bid_min", bidMin.value);
            if (bidMax && bidMax.value) params.set("bid_max", bidMax.value);
            if (maxBidMin && maxBidMin.value) params.set("maxbid_min", maxBidMin.value);
            if (maxBidMax && maxBidMax.value) params.set("maxbid_max", maxBidMax.value);
            if (headroomMin && headroomMin.value) params.set("headroom_min", headroomMin.value);
            if (headroomMax && headroomMax.value) params.set("headroom_max", headroomMax.value);
            var query = params.toString();
            var newUrl = window.location.pathname + (query ? "?" + query : "") + window.location.hash;
            window.history.replaceState(null, "", newUrl);
        }

        // A preset button's own min/max wins when its values aren't already
        // in the headroom fields; clicking it again (same values already
        // set) clears both fields instead - a simple toggle driven purely
        // by the fields' current values, not any separately-tracked state.
        function wirePresets() {
            presetButtons.forEach(function (button) {
                button.addEventListener("click", function () {
                    if (!headroomMin || !headroomMax) return;
                    var min = button.dataset.min || "";
                    var max = button.dataset.max || "";
                    var alreadyActive = headroomMin.value === min && headroomMax.value === max;
                    headroomMin.value = alreadyActive ? "" : min;
                    headroomMax.value = alreadyActive ? "" : max;
                    applyFilters();
                });
            });
        }

        function syncPresetActiveStates() {
            if (!headroomMin || !headroomMax) return;
            presetButtons.forEach(function (button) {
                var min = button.dataset.min || "";
                var max = button.dataset.max || "";
                var isPreset = min !== "" || max !== "";
                button.classList.toggle("is-active", isPreset && headroomMin.value === min && headroomMax.value === max);
            });
        }

        function applyFilters() {
            var q = searchInput ? searchInput.value.trim().toLowerCase() : "";
            var source = sourceSelect ? sourceSelect.value : "";
            var category = categorySelect ? categorySelect.value : "";
            var confidence = confidenceSelect ? confidenceSelect.value : "";
            var status = statusSelect ? statusSelect.value : "";
            var bidMinVal = parsedValue(bidMin);
            var bidMaxVal = parsedValue(bidMax);
            var maxBidMinVal = parsedValue(maxBidMin);
            var maxBidMaxVal = parsedValue(maxBidMax);
            var headroomMinVal = parsedValue(headroomMin);
            var headroomMaxVal = parsedValue(headroomMax);

            tables.forEach(function (table) {
                var tbody = table.querySelector("tbody");
                if (!tbody) return;
                // Dismissed rows (see initDismissals() below) are excluded
                // from both "shown" and the total here - a filter change
                // must never un-hide one, and "N of M shown" should never
                // count them either.
                var rows = Array.prototype.slice.call(tbody.querySelectorAll("tr.scanner-row:not(.is-dismissed)"));
                var shown = 0;

                rows.forEach(function (row) {
                    var matches =
                        (!q || (row.dataset.title || "").toLowerCase().indexOf(q) !== -1) &&
                        (!source || row.dataset.source === source) &&
                        (!category || row.dataset.category === category) &&
                        (!confidence || row.dataset.confidence === confidence) &&
                        (!status || row.dataset.status === status) &&
                        inRange(row.dataset.bid, bidMinVal, bidMaxVal) &&
                        inRange(row.dataset.maxbid, maxBidMinVal, maxBidMaxVal) &&
                        inRange(row.dataset.headroom, headroomMinVal, headroomMaxVal);
                    row.hidden = !matches;

                    // A hidden row's detail always collapses too, rather
                    // than tracking "was this expanded before the filter
                    // changed" - simpler, and avoids a stray open detail
                    // row under a parent that's no longer shown.
                    var detail = detailOf(row);
                    if (detail) {
                        detail.hidden = true;
                        row.classList.remove("is-expanded");
                    }

                    if (matches) shown += 1;
                });

                var countEl = document.getElementById(table.dataset.countTarget);
                if (countEl) countEl.textContent = shown + " of " + rows.length + " shown";
            });

            updateURL();
            updateTabCounts();
            syncPresetActiveStates();
        }

        if (clearButton) {
            clearButton.addEventListener("click", function () {
                elements.forEach(function (el) { el.value = ""; });
                applyFilters();
            });
        }

        applyFromURL();
        wirePresets();
        elements.forEach(function (el) {
            el.addEventListener("input", applyFilters);
            el.addEventListener("change", applyFilters);
        });
        applyFilters(); // initial pass: reflects any filters carried in the URL, sets initial counts
    }

    // ---------- CSRF token (cookie, falling back to the page's own hidden
    // field if cookies are blocked) - shared by any fetch() POST on this
    // page, currently just the Dismiss/Undo controls below. ----------
    function csrfToken() {
        var match = document.cookie.match(/csrftoken=([^;]+)/);
        return match ? match[1] : (document.querySelector("[name=csrfmiddlewaretoken]") || {}).value;
    }

    // ---------- Lot dismissal: the Act Now tables' "Dismiss" (X button or
    // reason menu) and "Undo" controls (_dismiss_control.html's toast-undo,
    // and the "Show dismissed" panel's own persistent Undo button). The
    // dismiss/undo POSTs are fire-and-forget: the row's visibility is what
    // the user sees, so it updates immediately rather than waiting on the
    // network, same spirit as every other best-effort POST in this app
    // (AI review buttons aside, which show their own "Reviewing..." state
    // because that one call matters enough to wait for). ----------
    function postForm(url, body) {
        var headers = { "X-CSRFToken": csrfToken() };
        if (body) headers["Content-Type"] = "application/x-www-form-urlencoded";
        return fetch(url, { method: "POST", headers: headers, body: body || undefined }).catch(function () {});
    }

    function toastContainer() {
        var el = document.getElementById("toast-container");
        if (el) return el;
        el = document.createElement("div");
        el.className = "toast-container";
        el.id = "toast-container";
        el.setAttribute("role", "status");
        el.setAttribute("aria-live", "polite");
        document.body.appendChild(el);
        return el;
    }

    // `viewLink` (optional, {text, href}) adds a plain link between the
    // message and Undo - "Added to Watchlist · View · Undo".
    function showUndoToast(message, onUndo, viewLink) {
        var toast = document.createElement("div");
        toast.className = "toast";
        var text = document.createElement("span");
        text.textContent = message + " ";
        if (viewLink) {
            var view = document.createElement("a");
            view.href = viewLink.href;
            view.textContent = viewLink.text;
            text.appendChild(view);
            text.appendChild(document.createTextNode(" · "));
        }
        var undo = document.createElement("a");
        undo.href = "#";
        undo.textContent = "Undo";
        undo.addEventListener("click", function (event) {
            event.preventDefault();
            onUndo();
            toast.remove();
        });
        toast.appendChild(text);
        toast.appendChild(undo);
        toastContainer().appendChild(toast);
        window.setTimeout(function () { toast.remove(); }, 5000);
    }

    function adjustDismissedCount(delta) {
        var el = document.getElementById("dismissed-count");
        if (!el) return;
        el.textContent = String(Math.max(0, (parseInt(el.textContent, 10) || 0) + delta));
    }

    function initDismissals() {
        function dismiss(control, reason) {
            var row = control.closest("tr.scanner-row");
            if (!row) return;
            row.hidden = true;
            row.classList.add("is-dismissed");
            updateTabCounts();
            adjustDismissedCount(1);
            postForm(control.dataset.dismissUrl, reason ? "reason=" + encodeURIComponent(reason) : "");

            showUndoToast("Dismissed.", function () {
                row.hidden = false;
                row.classList.remove("is-dismissed");
                updateTabCounts();
                adjustDismissedCount(-1);
                postForm(control.dataset.undoUrl);
            });
        }

        document.querySelectorAll(".js-dismiss-btn").forEach(function (btn) {
            btn.addEventListener("click", function () { dismiss(btn, ""); });
        });
        document.querySelectorAll(".js-dismiss-reason").forEach(function (select) {
            select.addEventListener("change", function () {
                var reason = select.value;
                select.value = "";
                if (reason) dismiss(select, reason);
            });
        });
        document.querySelectorAll(".js-undo-dismissed-btn").forEach(function (btn) {
            btn.addEventListener("click", function () {
                postForm(btn.dataset.undoUrl);
                var row = btn.closest("tr");
                if (row) row.remove();
                adjustDismissedCount(-1);
            });
        });

        var toggle = document.getElementById("toggle-dismissed");
        var panel = document.getElementById("dismissed-lots-panel");
        if (toggle && panel) {
            toggle.addEventListener("click", function () { panel.hidden = !panel.hidden; });
        }
    }

    // ---------- Watchlist: the star toggle (every lot row + the AI review
    // page) and the Watchlist page's own "Remove" button.
    //
    // A dashboard table row's star (always "Add to Watchlist", never
    // "Watching" - scanner/dashboard.py excludes every watchlisted lot from
    // these tables up front) removes that row immediately, no page reload,
    // and refreshes the nav's Watchlist badge. An "Added to Watchlist ·
    // View · Undo" toast links to the Watchlist page, or puts the row back
    // exactly where it was and un-watches the lot again.
    //
    // The AI review detail page's star isn't inside a table row at all, so
    // it keeps the simpler plain toggle it always had.
    //
    // On the Watchlist page (watchlist.html), refreshWatchlistSection()
    // re-renders the table (#watchlist-body) after the Calculator dialog
    // changes something on a lot; its "Remove" button reloads the page once
    // the removal is saved - removing a watch is a deliberate, infrequent
    // action, and the reload also refreshes Recently closed and the badge. ----------
    function adjustWatchlistTileCount(delta) {
        var el = document.getElementById("watchlist-tile-count");
        if (!el) return;
        el.textContent = String(Math.max(0, (parseInt(el.textContent, 10) || 0) + delta));
    }

    // The nav's Watchlist badge (templates/index.html): count of watched
    // lots, red when any is over My bid - re-fetched from the server
    // (scanner_views.watchlist_summary) rather than guessed here, since
    // "over My bid" depends on the watch's own max vs. the scanner's.
    function refreshWatchlistNavBadge() {
        var link = document.getElementById("nav-watchlist-link");
        var badge = document.getElementById("nav-watchlist-badge");
        if (!link || !badge || !link.dataset.summaryUrl) return;
        fetch(link.dataset.summaryUrl, { headers: { "X-Requested-With": "XMLHttpRequest" } })
            .then(function (resp) { return resp.ok ? resp.json() : null; })
            .then(function (summary) {
                if (!summary) return;
                badge.textContent = String(summary.count);
                badge.hidden = !summary.count;
                badge.classList.toggle("loss", !!summary.any_over);
                badge.classList.toggle("neutral", !summary.any_over);
                if (summary.any_over) badge.title = "A current bid is above My bid";
                else badge.removeAttribute("title");
            })
            .catch(function () {});
    }
    document.addEventListener("watchlist:changed", refreshWatchlistNavBadge);
    // static/js/watchlist.js after an inline max-bid save: just the badge -
    // re-rendering the table would wipe that row's "Saved" confirmation.
    document.addEventListener("watchlist:badge-stale", refreshWatchlistNavBadge);

    function applyWatchState(group, isWatching) {
        var button = group.querySelector(".js-watch-toggle");
        var link = group.querySelector(".js-watch-set-max-bid");
        if (!button) return;
        button.classList.toggle("is-watching", isWatching);
        button.title = isWatching ? "Remove from watchlist" : "Add to watchlist";
        var star = button.querySelector(".js-watch-star");
        if (star) star.textContent = isWatching ? "★" : "☆";
        var label = button.querySelector(".js-watch-label");
        if (label) label.textContent = isWatching ? "Watching" : "Add to Watchlist";
        if (link) link.hidden = !isWatching;
    }

    function refreshWatchlistSection() {
        var body = document.getElementById("watchlist-body");
        if (!body || !body.dataset.refreshUrl) return Promise.resolve();
        return fetch(body.dataset.refreshUrl, { headers: { "X-Requested-With": "XMLHttpRequest" } })
            .then(function (resp) { return resp.ok ? resp.text() : null; })
            .then(function (html) {
                if (html === null) return;
                body.innerHTML = html;
                var countEl = document.getElementById("watchlist-row-count");
                if (countEl) countEl.textContent = "(" + body.querySelectorAll("tr.scanner-row").length + ")";
                var table = body.querySelector("table.holdings");
                if (table) initSortableTable(table);
                wireWatchlistRemoveButtons();
                // Lets static/js/calculator.js re-wire any "Calc" button
                // in the markup just swapped in - it has no listener of
                // its own yet, since it was never there for calculator.js's
                // own one-time wiring pass at page load to find.
                document.dispatchEvent(new CustomEvent("dashboard-section:refreshed", { detail: { root: body } }));
            })
            .catch(function () {});
    }

    // static/js/calculator.js fires this after saving/clearing a lot's
    // resale estimate or "Use as my max bid" in the Calculator dialog -
    // the Watchlist's My bid/Headroom cells (and its "Use as my max bid"
    // offer) depend on those, so re-render it the same way a newly-watched
    // lot does.
    document.addEventListener("watchlist:changed", refreshWatchlistSection);

    // The Watchlist page's Remove (soft - archived) and Delete (hard -
    // confirm first) buttons: POST, then reload, which also refreshes the
    // Archive tab and the nav badge.
    function wireWatchlistRemoveButtons() {
        document.querySelectorAll(".js-watch-remove").forEach(function (button) {
            if (button.dataset.wired) return;
            button.dataset.wired = "1";
            button.addEventListener("click", function () {
                if (button.dataset.confirm && !window.confirm(button.dataset.confirm)) return;
                button.disabled = true;
                fetch(button.dataset.actionUrl, {
                    method: "POST", headers: {"X-CSRFToken": csrfToken(), "X-Requested-With": "XMLHttpRequest"},
                }).then(function () { window.location.reload(); })
                    .catch(function () { button.disabled = false; });
            });
        });
    }

    // Removes `row` (and its detail row, if any) from its table and moves
    // it to the Watchlist, with an Undo that puts everything back exactly
    // where it was (same parent, same position) and un-watches the lot
    // again - mirrors initDismissals()'s dismiss()/Undo pairing above.
    function moveRowToWatchlist(row, toggleUrl) {
        var parent = row.parentNode;
        var detail = detailOf(row);
        var insertBefore = detail ? detail.nextElementSibling : row.nextElementSibling;

        row.remove();
        if (detail) detail.remove();
        adjustWatchlistTileCount(1);
        updateTabCounts();
        refreshFilterCounts();
        postForm(toggleUrl).then(refreshWatchlistNavBadge);

        var watchlistLink = document.getElementById("nav-watchlist-link");
        var viewLink = watchlistLink ? {text: "View", href: watchlistLink.href} : null;
        showUndoToast("Added to Watchlist ·", function () {
            if (insertBefore && insertBefore.parentNode === parent) {
                parent.insertBefore(row, insertBefore);
                if (detail) parent.insertBefore(detail, insertBefore);
            } else {
                parent.appendChild(row);
                if (detail) parent.appendChild(detail);
            }
            adjustWatchlistTileCount(-1);
            updateTabCounts();
            refreshFilterCounts();
            postForm(toggleUrl).then(refreshWatchlistNavBadge);
        }, viewLink);
    }

    // A star can sit in the main row's own Actions cell, or in its
    // expandable detail row (_scanner_row_detail.html) - either way it
    // belongs to the same lot, so both resolve to that lot's tr.scanner-row
    // (the detail row is always rendered as its immediately preceding
    // sibling, same convention detailOf() above relies on).
    function mainRowOf(button) {
        var tr = button.closest("tr");
        if (!tr) return null;
        if (tr.classList.contains("scanner-row")) return tr;
        if (tr.classList.contains("scanner-detail")) return tr.previousElementSibling;
        return null;
    }

    function initWatchToggles() {
        document.querySelectorAll(".js-watch-toggle").forEach(function (button) {
            var tableRow = mainRowOf(button);

            button.addEventListener("click", function () {
                if (tableRow) {
                    moveRowToWatchlist(tableRow, button.dataset.toggleUrl);
                    return;
                }
                var group = button.closest(".js-watch-group");
                var next = !button.classList.contains("is-watching");
                if (group) applyWatchState(group, next);
                adjustWatchlistTileCount(next ? 1 : -1);
                postForm(button.dataset.toggleUrl).then(refreshWatchlistNavBadge);
            });
        });

        wireWatchlistRemoveButtons();
    }

    document.addEventListener("DOMContentLoaded", function () {
        Array.prototype.slice.call(document.querySelectorAll("table.holdings")).forEach(initSortableTable);
        initExpandableRows();
        initCollapsible();
        initAIReviewForms();
        initTabs();
        initFilters();
        initDismissals();
        initWatchToggles();
        updateTabCounts(); // covers pages where initFilters() has nothing to wire up
    });
})();
