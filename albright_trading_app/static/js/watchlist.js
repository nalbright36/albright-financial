/*
 * The Watchlist page (watchlist.html): its two tabs (Watching / Archive,
 * remembered in the URL hash) and each tab's filters - client-side only,
 * hiding table rows by their data-* attributes, never changing what's
 * watched or archived. Vanilla JS, no libraries.
 *
 *   Watching: source, category, "Only show over my bid" (#table-watchlist,
 *     from _watchlist_section.html). Re-applied after static/js/
 *     auction_scanner.js swaps in a freshly rendered table
 *     ("dashboard-section:refreshed"), so filters survive a Calculator-
 *     dialog save without a reload.
 *   Archive: outcome, source, category, archived-date range (#table-archive).
 */
(function () {
    "use strict";

    function byId(id) { return document.getElementById(id); }

    // ---------- Tabs ----------
    var TABS = ["watching", "archive"];
    var nav = byId("watchlist-tabs");
    if (nav) {
        var links = Array.prototype.slice.call(nav.querySelectorAll(".scanner-tab"));
        var activate = function (name, updateHash) {
            if (TABS.indexOf(name) === -1) name = TABS[0];
            TABS.forEach(function (tab) {
                var panel = byId(tab);
                if (panel) panel.hidden = tab !== name;
            });
            links.forEach(function (link) { link.classList.toggle("is-active", link.dataset.tab === name); });
            if (updateHash) window.history.replaceState(null, "", "#" + name);
        };
        links.forEach(function (link) {
            link.addEventListener("click", function (event) {
                event.preventDefault();
                activate(link.dataset.tab, true);
            });
        });
        activate((window.location.hash || "").replace("#", ""), false);
    }

    // Shows only rows matching every active test; returns [shown, total].
    function filterRows(selector, tests) {
        var rows = document.querySelectorAll(selector);
        var shown = 0;
        rows.forEach(function (row) {
            var visible = tests.every(function (test) { return test(row); });
            row.hidden = !visible;
            if (visible) shown += 1;
        });
        return [shown, rows.length];
    }

    function showCount(countEl, emptyEl, filtering, shown, total) {
        if (countEl) countEl.textContent = filtering ? ("Showing " + shown + " of " + total) : "";
        if (emptyEl) emptyEl.hidden = !(filtering && total && shown === 0);
    }

    // ---------- Watching filters ----------
    var sourceEl = byId("wl-source");
    var categoryEl = byId("wl-category");
    var overOnlyEl = byId("wl-over-only");
    if (sourceEl && categoryEl && overOnlyEl) {
        var applyWatchingFilters = function () {
            var source = sourceEl.value;
            var category = categoryEl.value;
            var overOnly = overOnlyEl.checked;
            var result = filterRows("#table-watchlist tbody tr.scanner-row", [
                function (row) { return !source || row.dataset.source === source; },
                function (row) { return !category || row.dataset.category === category; },
                function (row) { return !overOnly || row.dataset.over === "1"; },
            ]);
            showCount(byId("wl-filter-count"), byId("wl-filter-empty"), !!(source || category || overOnly),
                result[0], result[1]);
        };
        [sourceEl, categoryEl, overOnlyEl].forEach(function (el) { el.addEventListener("change", applyWatchingFilters); });
        document.addEventListener("dashboard-section:refreshed", applyWatchingFilters);
        applyWatchingFilters();
    }

    // ---------- Archive filters ----------
    var outcomeEl = byId("ar-outcome");
    if (outcomeEl) {
        var arSourceEl = byId("ar-source");
        var arCategoryEl = byId("ar-category");
        var fromEl = byId("ar-date-from");
        var toEl = byId("ar-date-to");
        var applyArchiveFilters = function () {
            var outcome = outcomeEl.value;
            var source = arSourceEl.value;
            var category = arCategoryEl.value;
            var from = fromEl.value;  // "YYYY-MM-DD" - compares correctly as a string
            var to = toEl.value;
            var result = filterRows("#table-archive tbody tr.scanner-row", [
                function (row) { return !outcome || row.dataset.outcome === outcome; },
                function (row) { return !source || row.dataset.source === source; },
                function (row) { return !category || row.dataset.category === category; },
                function (row) { return !from || row.dataset.archivedDate >= from; },
                function (row) { return !to || row.dataset.archivedDate <= to; },
            ]);
            showCount(byId("ar-filter-count"), byId("ar-filter-empty"), !!(outcome || source || category || from || to),
                result[0], result[1]);
        };
        [outcomeEl, arSourceEl, arCategoryEl, fromEl, toEl].forEach(function (el) {
            el.addEventListener("change", applyArchiveFilters);
        });
        applyArchiveFilters();
    }
})();
