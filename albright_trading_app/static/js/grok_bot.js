/*
 * Grok Bot pages (grok_bot.html, grok_bot_strategy.html). Vanilla JS; read-only - nothing here
 * talks to Alpaca or changes anything.
 *  - Tabs: one panel at a time; the hash keeps the tab on reload.
 *  - Strategy filter: shows only the chosen strategy's rows in Open positions, Recent orders and
 *    Closed trades (rows carry data-strategies="<keys>"); the tab counts follow; ?strategy= keeps it.
 *  - The comparison table sorts by a column (click, or Enter/Space on the header); Unattributed and
 *    Total stay at the bottom.
 *  - Chart: each strategy's cumulative P&L in its own color (#grok-series), or the account's equity
 *    (#grok-equity) - Lightweight Charts, like the stock page.
 */
(function () {
    "use strict";

    function readJson(id) {
        var node = document.getElementById(id);
        if (!node) return null;
        try { return JSON.parse(node.textContent); } catch (e) { return null; }
    }

    // ---------- Tabs ----------
    var tabs = Array.prototype.slice.call(document.querySelectorAll("#grok-tabs .scanner-tab"));
    var panels = Array.prototype.slice.call(document.querySelectorAll(".grok-panel"));
    function showTab(name) {
        tabs.forEach(function (t) {
            var active = t.dataset.tab === name;
            t.classList.toggle("is-active", active);
            t.setAttribute("aria-selected", active ? "true" : "false");
        });
        panels.forEach(function (p) { p.hidden = p.dataset.panel !== name; });
    }
    tabs.forEach(function (t) {
        t.addEventListener("click", function (e) {
            e.preventDefault();
            showTab(t.dataset.tab);
            history.replaceState(null, "", location.search + "#" + t.dataset.tab);
        });
    });
    var initialTab = (location.hash || "").slice(1);
    if (["positions", "orders", "trades"].indexOf(initialTab) !== -1) showTab(initialTab);

    // ---------- Strategy filter ----------
    var filter = document.getElementById("strategy-filter");
    function applyFilter() {
        var key = filter.value;
        panels.forEach(function (panel) {
            var rows = Array.prototype.slice.call(panel.querySelectorAll("tbody tr[data-strategies]"));
            var shown = 0;
            rows.forEach(function (row) {
                var match = !key || row.dataset.strategies.split(" ").indexOf(key) !== -1;
                row.hidden = !match;
                if (match) shown += 1;
            });
            var count = document.querySelector('[data-count="' + panel.dataset.panel + '"]');
            if (count && rows.length) count.textContent = shown;
            var empty = panel.querySelector(".grok-empty");
            if (empty) empty.hidden = shown > 0 || !rows.length;
            var table = panel.querySelector("table");
            if (table && rows.length) table.hidden = shown === 0;
        });
        var params = new URLSearchParams(location.search);
        if (key) params.set("strategy", key); else params.delete("strategy");
        var query = params.toString();
        history.replaceState(null, "", location.pathname + (query ? "?" + query : "") + location.hash);
    }
    if (filter) {
        filter.addEventListener("change", applyFilter);
        if (filter.value) applyFilter();
    }

    // ---------- Sorting the comparison table ----------
    var table = document.getElementById("strategy-table");
    if (table) {
        var body = table.querySelector("tbody");
        var headers = Array.prototype.slice.call(table.querySelectorAll("th[data-sort-key]"));
        var sortKey = null, ascending = true;
        function value(row, key) {
            var raw = row.dataset[key];
            if (raw === undefined || raw === "") return null;
            var number = parseFloat(raw);
            return isNaN(number) ? raw : number;
        }
        function sortBy(key) {
            ascending = sortKey === key ? !ascending : (key === "name" || key === "status");
            sortKey = key;
            var rows = Array.prototype.slice.call(body.querySelectorAll("tr.strategy-row"));
            rows.sort(function (a, b) {
                var x = value(a, key), y = value(b, key);
                if (x === null && y === null) return 0;
                if (x === null) return 1;            // blanks last either way
                if (y === null) return -1;
                var cmp = typeof x === "number" && typeof y === "number" ? x - y : String(x).localeCompare(String(y));
                return ascending ? cmp : -cmp;
            });
            rows.forEach(function (row) { body.appendChild(row); });
            headers.forEach(function (h) {
                var active = h.dataset.sortKey === key;
                h.classList.toggle("sorted-asc", active && ascending);
                h.classList.toggle("sorted-desc", active && !ascending);
                if (active) h.setAttribute("aria-sort", ascending ? "ascending" : "descending");
                else h.removeAttribute("aria-sort");
            });
        }
        headers.forEach(function (h) {
            h.tabIndex = 0;
            h.addEventListener("click", function () { sortBy(h.dataset.sortKey); });
            h.addEventListener("keydown", function (e) {
                if (e.key === "Enter" || e.key === " ") { e.preventDefault(); sortBy(h.dataset.sortKey); }
            });
        });
    }

    // ---------- Chart ----------
    var container = document.getElementById("grok-chart");
    if (!container) return;
    var series = readJson("grok-series") || [];
    var equity = readJson("grok-equity") || [];
    var legend = document.getElementById("chart-legend");
    var toggles = Array.prototype.slice.call(document.querySelectorAll("#chart-toggle [data-chart]"));
    if (typeof LightweightCharts === "undefined") {
        container.innerHTML = '<p style="color: var(--text-secondary); padding: 16px 0;">The chart couldn\'t load.</p>';
        return;
    }
    function cssVar(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }
    var chart = LightweightCharts.createChart(container, {
        layout: { background: { color: "transparent" }, textColor: cssVar("--chart-text") },
        grid: { vertLines: { color: cssVar("--chart-grid") }, horzLines: { color: cssVar("--chart-grid") } },
        rightPriceScale: { borderColor: cssVar("--chart-grid") },
        timeScale: { borderColor: cssVar("--chart-grid"), timeVisible: true },
        width: container.clientWidth,
        height: 320,
    });
    var lines = [], view = "strategies";
    var empty = document.createElement("p");
    empty.style.cssText = "color: var(--text-secondary); padding: 16px 0; margin: 0;";
    container.appendChild(empty);

    function money(v) { return (v < 0 ? "-$" : "+$") + Math.abs(v).toFixed(2).replace(/\B(?=(\d{3})+(?!\d))/g, ","); }

    function draw() {
        lines.forEach(function (line) { chart.removeSeries(line); });
        lines = [];
        if (legend) legend.innerHTML = "";
        var hasData = false;
        if (view === "equity") {
            if (equity.length) {
                var up = equity[equity.length - 1].value >= equity[0].value;
                var line = chart.addLineSeries({ color: cssVar(up ? "--gain" : "--loss"), lineWidth: 2 });
                line.setData(equity);
                lines.push(line);
                hasData = true;
            }
            empty.textContent = "No equity history for this period.";
        } else {
            series.forEach(function (s) {
                if (!s.points.length) return;
                var line = chart.addLineSeries({ color: s.color || cssVar("--text-tertiary"), lineWidth: 2,
                                                 lineStyle: s.key === "unattributed" ? 2 : 0, title: "" });
                line.setData(s.points);
                lines.push(line);
                hasData = true;
                if (legend) {
                    var item = document.createElement("li");
                    var dot = document.createElement("span");
                    dot.className = "strategy-chip__dot";
                    dot.style.setProperty("--strategy-color", s.color || cssVar("--text-tertiary"));
                    item.appendChild(dot);
                    item.appendChild(document.createTextNode(" " + s.name + " " + money(s.points[s.points.length - 1].value)));
                    legend.appendChild(item);
                }
            });
            empty.textContent = "No P&L for any strategy in this period yet.";
        }
        empty.hidden = hasData;
        chart.timeScale().fitContent();
    }
    toggles.forEach(function (button) {
        button.addEventListener("click", function () {
            view = button.dataset.chart;
            toggles.forEach(function (b) {
                var on = b === button;
                b.classList.toggle("is-active", on);
                b.setAttribute("aria-pressed", on ? "true" : "false");
            });
            draw();
        });
    });
    draw();
    window.addEventListener("resize", function () { chart.applyOptions({ width: container.clientWidth }); });
    document.addEventListener("themechange", function () {
        chart.applyOptions({
            layout: { textColor: cssVar("--chart-text") },
            grid: { vertLines: { color: cssVar("--chart-grid") }, horzLines: { color: cssVar("--chart-grid") } },
            rightPriceScale: { borderColor: cssVar("--chart-grid") },
            timeScale: { borderColor: cssVar("--chart-grid") },
        });
        draw();
    });
})();
