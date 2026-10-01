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
    // on the row that isn't a link/button/form) ----------
    function initExpandableRows() {
        document.querySelectorAll("table.holdings tbody").forEach(function (tbody) {
            tbody.addEventListener("click", function (event) {
                if (event.target.closest("a, button, form")) return;
                var row = event.target.closest("tr.scanner-row");
                if (!row) return;
                var detail = detailOf(row);
                if (!detail) return;
                detail.hidden = !detail.hidden;
                row.classList.toggle("is-expanded", !detail.hidden);
            });
        });
    }

    // ---------- Collapsible card headers ----------
    function initCollapsible() {
        var headers = document.querySelectorAll(".js-collapsible-header");
        headers.forEach(function (header) {
            var body = header.nextElementSibling;
            if (!body) return;
            header.addEventListener("click", function () {
                var isHidden = body.hidden;
                body.hidden = !isHidden;
                header.classList.toggle("is-collapsed", !isHidden);
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

    // ---------- Shared filter bar (source / category / confidence / search) ----------
    function initFilters() {
        var searchInput = document.getElementById("af-search");
        var sourceSelect = document.getElementById("af-source");
        var categorySelect = document.getElementById("af-category");
        var confidenceSelect = document.getElementById("af-confidence");
        if (!searchInput || !sourceSelect || !categorySelect || !confidenceSelect) return;

        var tables = Array.prototype.slice.call(document.querySelectorAll("table[data-filterable]"));

        function applyFromURL() {
            var params = new URLSearchParams(window.location.search);
            searchInput.value = params.get("q") || "";
            sourceSelect.value = params.get("source") || "";
            categorySelect.value = params.get("category") || "";
            confidenceSelect.value = params.get("confidence") || "";
        }

        function updateURL() {
            var params = new URLSearchParams();
            if (searchInput.value.trim()) params.set("q", searchInput.value.trim());
            if (sourceSelect.value) params.set("source", sourceSelect.value);
            if (categorySelect.value) params.set("category", categorySelect.value);
            if (confidenceSelect.value) params.set("confidence", confidenceSelect.value);
            var query = params.toString();
            var newUrl = window.location.pathname + (query ? "?" + query : "") + window.location.hash;
            window.history.replaceState(null, "", newUrl);
        }

        function applyFilters() {
            var q = searchInput.value.trim().toLowerCase();
            var source = sourceSelect.value;
            var category = categorySelect.value;
            var confidence = confidenceSelect.value;

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
                        (!confidence || row.dataset.confidence === confidence);
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
        }

        applyFromURL();
        [searchInput, sourceSelect, categorySelect, confidenceSelect].forEach(function (el) {
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
