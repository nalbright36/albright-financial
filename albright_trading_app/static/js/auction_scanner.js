/*
 * Auction Scanner dashboard: column sorting, a shared filter bar, and
 * collapsible section headers. Vanilla JS, no libraries.
 *
 * Every table renders complete on the server first - this file only
 * reorders/hides rows that are already in the DOM, so the page works
 * (just without these controls) if JavaScript is disabled.
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

            var rows = Array.prototype.slice.call(tbody.querySelectorAll("tr"));
            rows.sort(function (a, b) {
                var cmp = compareRaw(key, a.dataset[key], b.dataset[key]);
                return sortAsc ? cmp : -cmp;
            });
            rows.forEach(function (row) { tbody.appendChild(row); });

            headers.forEach(function (h) { h.classList.remove("sorted-asc", "sorted-desc"); });
            var active = headers.filter(function (h) { return h.dataset.sortKey === key; })[0];
            if (active) active.classList.add(sortAsc ? "sorted-asc" : "sorted-desc");
        }

        headers.forEach(function (h) {
            h.addEventListener("click", function () { sortBy(h.dataset.sortKey); });
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

    // ---------- Shared filter bar (source / category / confidence / search) ----------
    function initFilters() {
        var searchInput = document.getElementById("af-search");
        var sourceSelect = document.getElementById("af-source");
        var categorySelect = document.getElementById("af-category");
        var confidenceSelect = document.getElementById("af-confidence");
        if (!searchInput || !sourceSelect || !categorySelect || !confidenceSelect) return;

        var tables = Array.prototype.slice.call(document.querySelectorAll("table[data-filterable]"));
        if (!tables.length) return;

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
                var rows = Array.prototype.slice.call(tbody.querySelectorAll("tr"));
                var shown = 0;

                rows.forEach(function (row) {
                    var matches =
                        (!q || (row.dataset.title || "").toLowerCase().indexOf(q) !== -1) &&
                        (!source || row.dataset.source === source) &&
                        (!category || row.dataset.category === category) &&
                        (!confidence || row.dataset.confidence === confidence);
                    row.hidden = !matches;
                    if (matches) shown += 1;
                });

                var countEl = document.getElementById(table.dataset.countTarget);
                if (countEl) countEl.textContent = shown + " of " + rows.length + " shown";
            });

            updateURL();
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
        initCollapsible();
        initFilters();
    });
})();
