/*
 * The Watchlist page's filters (watchlist.html): source, category, and
 * "Only show over my bid" - client-side only, hiding rows of the live
 * table (#table-watchlist, from _watchlist_section.html) by its rows'
 * data-source/data-category/data-over attributes. Vanilla JS, no
 * libraries. Re-applied after static/js/auction_scanner.js swaps in a
 * freshly rendered table ("dashboard-section:refreshed"), so filters
 * survive a Calculator-dialog save without a reload.
 */
(function () {
    "use strict";

    var sourceEl = document.getElementById("wl-source");
    var categoryEl = document.getElementById("wl-category");
    var overOnlyEl = document.getElementById("wl-over-only");
    var countEl = document.getElementById("wl-filter-count");
    var emptyEl = document.getElementById("wl-filter-empty");
    if (!sourceEl || !categoryEl || !overOnlyEl) return;

    function applyFilters() {
        var source = sourceEl.value;
        var category = categoryEl.value;
        var overOnly = overOnlyEl.checked;
        var rows = document.querySelectorAll("#table-watchlist tbody tr.scanner-row");
        var shown = 0;
        rows.forEach(function (row) {
            var visible = (!source || row.dataset.source === source) &&
                (!category || row.dataset.category === category) &&
                (!overOnly || row.dataset.over === "1");
            row.hidden = !visible;
            if (visible) shown += 1;
        });
        var filtering = !!(source || category || overOnly);
        if (countEl) countEl.textContent = filtering ? ("Showing " + shown + " of " + rows.length) : "";
        if (emptyEl) emptyEl.hidden = !(filtering && rows.length && shown === 0);
    }

    [sourceEl, categoryEl, overOnlyEl].forEach(function (el) {
        el.addEventListener("change", applyFilters);
    });
    document.addEventListener("dashboard-section:refreshed", applyFilters);
    applyFilters();
})();
