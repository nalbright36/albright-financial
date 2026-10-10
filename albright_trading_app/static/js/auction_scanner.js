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
    function initCollapsible() {
        var headers = document.querySelectorAll(".js-collapsible-header");
        headers.forEach(function (header) {
            var body = header.nextElementSibling;
            if (!body) return;
            header.setAttribute("tabindex", "0");
            header.setAttribute("role", "button");
            header.setAttribute("aria-expanded", "true");

            function toggle() {
                var isHidden = body.hidden;
                body.hidden = !isHidden;
                header.classList.toggle("is-collapsed", !isHidden);
                header.setAttribute("aria-expanded", String(isHidden));
            }

            header.addEventListener("click", toggle);
            header.addEventListener("keydown", function (event) {
                if (event.key !== "Enter" && event.key !== " ") return;
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
                var rows = Array.prototype.slice.call(tbody.querySelectorAll("tr.scanner-row"));
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

    document.addEventListener("DOMContentLoaded", function () {
        Array.prototype.slice.call(document.querySelectorAll("table.holdings")).forEach(initSortableTable);
        initExpandableRows();
        initCollapsible();
        initAIReviewForms();
        initTabs();
        initFilters();
        updateTabCounts(); // covers pages where initFilters() has nothing to wire up
    });
})();
