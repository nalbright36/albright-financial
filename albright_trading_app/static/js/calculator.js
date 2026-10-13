/*
 * The calculator's 3 tabs (Buy cost / eBay profit / Melt value) - shared by
 * the full Calculators page (calculator.html) and the dashboard/Today
 * pages' slide-in panel (_calculator_panel.html), both of which include the
 * same _calculator_tabs.html markup and a #calc-data JSON blob this reads
 * its config from. Vanilla JS, no libraries.
 *
 * Three independent concerns, all guarded so each only activates where its
 * markup actually exists:
 *   1. The tabs themselves (always, wherever #calc-data is present).
 *   2. The slide-in panel's open/close/focus/keyboard-shortcut behavior
 *      (only on pages with #calc-panel - the full Calculators page has no
 *      panel, so this is a no-op there).
 *   3. "Calc" buttons on dashboard lot rows that open the panel prefilled
 *      from that lot (only where .js-calc-from-lot buttons exist).
 */
(function () {
    "use strict";

    var dataEl = document.getElementById("calc-data");
    if (!dataEl) return;
    var CALC_DATA = JSON.parse(dataEl.textContent);

    var SOURCE_PRESETS = CALC_DATA.sourcePresets || {};
    var LEDGER_ITEMS = CALC_DATA.ledgerItems || [];
    var LOT_PREFILL = CALC_DATA.lotPrefill || {};
    var PURITY_CHOICES = CALC_DATA.purityChoices || {};
    var LATEST_SPOT = CALC_DATA.latestSpot || {};
    var CALCULATE_URL = CALC_DATA.calculateUrl;
    // A no-JS POST just rendered real, fresh results for its tab - skip
    // restoring that one tab from localStorage so JS doesn't immediately
    // clobber them with older saved values (the other tab, untouched by
    // this submit, still restores normally). Always false for the panel,
    // which never POSTs a no-JS fallback of its own.
    var JUST_SUBMITTED_BUY = !!CALC_DATA.justSubmittedBuy;
    var JUST_SUBMITTED_EBAY = !!CALC_DATA.justSubmittedEbay;
    var JUST_SUBMITTED_MELT = !!CALC_DATA.justSubmittedMelt;
    var STORAGE_KEY_BUY = "bidcalc_buy_values";
    var STORAGE_KEY_EBAY = "bidcalc_ebay_values";
    var STORAGE_KEY_MELT = "bidcalc_melt_values";
    var STORAGE_KEY_MELT_ITEMS = "bidcalc_melt_items";
    var STORAGE_KEY_TAB = "bidcalc_active_tab";
    var TAB_NAMES = ["buy", "ebay", "melt"];

    var BUY_FIELDS = ["bid", "premium_pct", "tax_pct", "shipping", "other_fees", "budget"];
    var BUY_CHECKBOXES = ["tax_on_premium", "tax_on_shipping"];
    var EBAY_FIELDS = [
        "item_cost", "sale_price", "shipping_charged", "shipping_cost", "packaging", "other_costs",
        "custom_rate_pct", "buyer_tax_pct", "promoted_pct", "target_profit",
    ];

    function byId(id) { return document.getElementById(id); }
    function csrfToken() {
        var match = document.cookie.match(/csrftoken=([^;]+)/);
        return match ? match[1] : (document.querySelector('[name=csrfmiddlewaretoken]') || {}).value;
    }
    function money(value) {
        if (value === null || value === undefined) return "&mdash;";
        var sign = value < 0 ? "-" : "";
        return sign + "$" + Math.abs(value).toFixed(2);
    }
    function pct(value) {
        return (value === null || value === undefined) ? "&mdash;" : Number(value).toFixed(1) + "%";
    }
    function postCalculate(data) {
        return fetch(CALCULATE_URL, {
            method: "POST", headers: {"Content-Type": "application/json", "X-CSRFToken": csrfToken()},
            body: JSON.stringify(data),
        }).then(function (r) { return r.json(); });
    }

    // ---------- Buy cost tab ----------
    function collectBuyData() {
        var data = {tab: "buy"};
        BUY_FIELDS.forEach(function (name) { data[name] = byId("buy-" + name).value; });
        BUY_CHECKBOXES.forEach(function (name) { data[name] = byId("buy-" + name).checked; });
        return data;
    }
    function applyBuyData(data) {
        if (!data) return;
        BUY_FIELDS.forEach(function (name) {
            if (data[name] !== undefined && data[name] !== null) byId("buy-" + name).value = data[name];
        });
        BUY_CHECKBOXES.forEach(function (name) {
            if (data[name] !== undefined) byId("buy-" + name).checked = !!data[name];
        });
        if (data.source) byId("buy-source").value = data.source;
    }
    var lastBuyTotal = 0;
    function renderBuyResults(results) {
        lastBuyTotal = results.total || 0;
        byId("buy-results-body").innerHTML =
            '<div class="grid grid-2" style="margin-bottom: 12px;">' +
            '<div><div class="row-count">Bid</div><span class="num">' + money(results.bid) + '</span></div>' +
            '<div><div class="row-count">Premium</div><span class="num">' + money(results.premium) + '</span></div>' +
            '<div><div class="row-count">Tax</div><span class="num">' + money(results.tax) + '</span></div>' +
            '<div><div class="row-count">Shipping</div><span class="num">' + money(results.shipping) + '</span></div>' +
            '<div><div class="row-count">Other fees</div><span class="num">' + money(results.other_fees) + '</span></div>' +
            '</div>' +
            '<div class="row-count">Out-the-door total</div>' +
            '<span class="ledger-stat__value num" style="font-size: 2rem;">' + money(results.total) + '</span>';
        byId("buy-max-bid-for-budget").innerHTML =
            results.max_bid_for_budget === null || results.max_bid_for_budget === undefined
                ? "&mdash;" : money(results.max_bid_for_budget);
    }

    function applySourcePreset(source) {
        var preset = SOURCE_PRESETS[source];
        if (!preset) return;
        byId("buy-premium_pct").value = preset.premium_pct;
        byId("buy-tax_pct").value = preset.tax_pct;
        byId("buy-shipping").value = preset.shipping;
    }

    var buyTimer = null;
    function scheduleBuyCalculate() { window.clearTimeout(buyTimer); buyTimer = window.setTimeout(doBuyCalculate, 300); }
    function doBuyCalculate() {
        var data = collectBuyData();
        try { localStorage.setItem(STORAGE_KEY_BUY, JSON.stringify(data)); } catch (e) {}
        return postCalculate(data).then(function (results) {
            renderBuyResults(results);
            return results;
        }).catch(function () {});
    }

    byId("buy-form").addEventListener("input", scheduleBuyCalculate);
    byId("buy-form").addEventListener("change", scheduleBuyCalculate);
    byId("buy-form").addEventListener("submit", function (e) {
        if (window.fetch) e.preventDefault();  // let JS own it when available; otherwise real submit
        doBuyCalculate();
    });
    byId("buy-source").addEventListener("change", function (e) {
        applySourcePreset(e.target.value);
        scheduleBuyCalculate();
    });

    // ---------- eBay profit tab ----------
    function collectEbayData() {
        var data = {tab: "ebay", category: byId("ebay-category").value};
        EBAY_FIELDS.forEach(function (name) { data[name] = byId("ebay-" + name).value; });
        return data;
    }
    function applyEbayData(data) {
        if (!data) return;
        EBAY_FIELDS.forEach(function (name) {
            if (data[name] !== undefined && data[name] !== null) byId("ebay-" + name).value = data[name];
        });
        if (data.category) byId("ebay-category").value = data.category;
        syncCustomRateVisibility();
    }
    function renderEbayResults(results) {
        var profitColor = results.profit >= 0 ? "var(--gain)" : "var(--loss)";
        byId("ebay-results-body").innerHTML =
            '<div class="grid grid-2" style="margin-bottom: 12px;">' +
            '<div><div class="row-count">Final value fee</div><span class="num">' + money(results.final_value_fee) + '</span></div>' +
            '<div><div class="row-count">Per-order fee</div><span class="num">' + money(results.per_order_fee) + '</span></div>' +
            '<div><div class="row-count">Promoted fee</div><span class="num">' + money(results.promoted_fee) + '</span></div>' +
            '<div><div class="row-count">Payout from eBay</div><span class="num">' + money(results.payout) + '</span></div>' +
            '</div>' +
            '<div class="row-count">Profit</div>' +
            '<span class="ledger-stat__value num" style="font-size: 2rem; color: ' + profitColor + ';">' + money(results.profit) + '</span>' +
            '<div class="grid grid-2" style="margin-top: 12px;">' +
            '<div><div class="row-count">Margin %</div><span class="num">' + pct(results.margin_pct) + '</span></div>' +
            '<div><div class="row-count">ROI %</div><span class="num">' + pct(results.roi_pct) + '</span></div>' +
            '</div>' +
            '<p style="margin: 12px 0 0;">Break-even listing price: <span class="num" style="font-weight: 600;">' + money(results.break_even_price) + '</span></p>';
        byId("ebay-price-for-profit").innerHTML = money(results.price_for_profit);
    }

    function syncCustomRateVisibility() {
        byId("ebay-custom-rate-field").style.display = byId("ebay-category").value === "custom" ? "" : "none";
    }

    var ebayTimer = null;
    function scheduleEbayCalculate() { window.clearTimeout(ebayTimer); ebayTimer = window.setTimeout(doEbayCalculate, 300); }
    function doEbayCalculate() {
        var data = collectEbayData();
        try { localStorage.setItem(STORAGE_KEY_EBAY, JSON.stringify(data)); } catch (e) {}
        return postCalculate(data).then(function (results) {
            renderEbayResults(results);
            return results;
        }).catch(function () {});
    }

    byId("ebay-form").addEventListener("input", scheduleEbayCalculate);
    byId("ebay-form").addEventListener("change", scheduleEbayCalculate);
    byId("ebay-form").addEventListener("submit", function (e) {
        if (window.fetch) e.preventDefault();
        doEbayCalculate();
    });
    byId("ebay-category").addEventListener("change", function () { syncCustomRateVisibility(); scheduleEbayCalculate(); });

    LEDGER_ITEMS.forEach(function (item) {
        var option = document.createElement("option");
        option.value = String(item.id);
        option.setAttribute("data-cost", item.cost);
        option.textContent = item.label + " ($" + item.cost.toFixed(2) + ")";
        byId("ebay-ledger-pick").appendChild(option);
    });
    byId("ebay-ledger-pick").addEventListener("change", function (e) {
        var option = e.target.selectedOptions[0];
        if (!option || !option.dataset.cost) return;
        byId("ebay-item_cost").value = option.dataset.cost;
        scheduleEbayCalculate();
    });

    // ---------- Melt value tab ----------
    var MELT_FIELDS = ["metal", "purity", "custom_purity", "weight", "unit", "face_value", "spot", "payout_pct"];
    var lastMeltValue = 0;
    var lastPayoutValue = 0;

    function meltMode() {
        var checked = document.querySelector('input[name="mode"]:checked');
        return checked ? checked.value : "weight";
    }
    function collectMeltData() {
        var data = {tab: "melt", mode: meltMode()};
        MELT_FIELDS.forEach(function (name) { data[name] = byId("melt-" + name).value; });
        return data;
    }
    function applyMeltData(data) {
        if (!data) return;
        MELT_FIELDS.forEach(function (name) {
            if (data[name] !== undefined && data[name] !== null) byId("melt-" + name).value = data[name];
        });
        if (data.mode) byId("melt-mode-" + data.mode).checked = true;
        if (data.metal) populatePurityOptions(data.metal, data.purity);
        syncMeltModeVisibility();
        syncPurityVisibility();
    }

    function populatePurityOptions(metal, keepValue) {
        var select = byId("melt-purity");
        var currentValue = keepValue !== undefined ? keepValue : select.value;
        select.innerHTML = "";
        (PURITY_CHOICES[metal] || []).forEach(function (opt) {
            var option = document.createElement("option");
            option.value = opt.value;
            option.textContent = opt.label;
            select.appendChild(option);
        });
        var customOption = document.createElement("option");
        customOption.value = "custom";
        customOption.textContent = "Custom";
        select.appendChild(customOption);
        var hasValue = Array.prototype.some.call(select.options, function (o) { return o.value === currentValue; });
        select.value = hasValue ? currentValue : select.options[0].value;
    }

    function refillSpotForMetal(metal) {
        var info = LATEST_SPOT[metal];
        var dateEl = byId("melt-spot-date");
        if (!info) { dateEl.textContent = ""; return; }
        if (info.price !== null && info.price !== undefined) byId("melt-spot").value = info.price;
        dateEl.textContent = info.date ? ("as of " + info.date) : "no spot price on file yet - enter one manually";
    }

    function syncMeltModeVisibility() {
        var mode = meltMode();
        byId("melt-weight-group").style.display = mode === "weight" ? "" : "none";
        byId("melt-junk-group").style.display = mode === "junk" ? "" : "none";
    }
    function syncPurityVisibility() {
        byId("melt-custom-purity-field").style.display = byId("melt-purity").value === "custom" ? "" : "none";
    }

    function renderMeltResults(results) {
        lastMeltValue = results.melt_value || 0;
        lastPayoutValue = results.payout_value || 0;
        byId("melt-results-body").innerHTML =
            '<div class="grid grid-2" style="margin-bottom: 12px;">' +
            '<div><div class="row-count">Pure metal weight</div><span class="num">' +
            (results.pure_ozt != null ? results.pure_ozt.toFixed(4) : "0.0000") + ' ozt</span></div>' +
            '<div><div class="row-count">Value per gram</div><span class="num">' + money(results.melt_per_gram) + '</span></div>' +
            '</div>' +
            '<div class="row-count">Melt value</div>' +
            '<span class="ledger-stat__value num" style="font-size: 2rem;">' + money(results.melt_value) + '</span>' +
            '<p style="margin: 8px 0 0;">Payout at ' + (results.payout_pct != null ? results.payout_pct : 100) +
            '%: <span class="num" style="font-weight: 600;">' + money(results.payout_value) + '</span></p>';
    }

    var meltTimer = null;
    function scheduleMeltCalculate() { window.clearTimeout(meltTimer); meltTimer = window.setTimeout(doMeltCalculate, 300); }
    function doMeltCalculate() {
        var data = collectMeltData();
        try { localStorage.setItem(STORAGE_KEY_MELT, JSON.stringify(data)); } catch (e) {}
        return postCalculate(data).then(function (results) {
            renderMeltResults(results);
            return results;
        }).catch(function () {});
    }

    byId("melt-form").addEventListener("input", scheduleMeltCalculate);
    byId("melt-form").addEventListener("change", scheduleMeltCalculate);
    byId("melt-form").addEventListener("submit", function (e) {
        if (window.fetch) e.preventDefault();
        doMeltCalculate();
    });
    document.querySelectorAll('input[name="mode"]').forEach(function (radio) {
        radio.addEventListener("change", function () { syncMeltModeVisibility(); scheduleMeltCalculate(); });
    });
    byId("melt-metal").addEventListener("change", function (e) {
        populatePurityOptions(e.target.value);
        refillSpotForMetal(e.target.value);
        syncPurityVisibility();
        scheduleMeltCalculate();
    });
    byId("melt-purity").addEventListener("change", function () { syncPurityVisibility(); scheduleMeltCalculate(); });

    // ---------- Melt item list (localStorage only - "a whole jewelry lot") ----------
    function escapeHtml(value) {
        return String(value == null ? "" : value)
            .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
    }
    var meltItems = [];
    try { meltItems = JSON.parse(localStorage.getItem(STORAGE_KEY_MELT_ITEMS) || "[]"); } catch (e) { meltItems = []; }

    function saveMeltItems() {
        try { localStorage.setItem(STORAGE_KEY_MELT_ITEMS, JSON.stringify(meltItems)); } catch (e) {}
    }
    function renderMeltItemList() {
        var tbody = byId("melt-items-body");
        tbody.innerHTML = "";
        var total = 0;
        meltItems.forEach(function (item, index) {
            total += item.payout || 0;
            var row = document.createElement("tr");
            row.innerHTML =
                "<td>" + escapeHtml(item.label || "(unlabeled)") + "</td>" +
                "<td>" + escapeHtml(item.metal) + "</td>" +
                "<td>" + escapeHtml(item.purity) + "</td>" +
                '<td class="num">' + escapeHtml(item.weight) + "</td>" +
                '<td class="num">' + money(item.melt) + "</td>" +
                '<td class="num">' + money(item.payout) + "</td>" +
                '<td><button type="button" class="btn btn-secondary btn-sm melt-remove-item" data-index="' + index + '">Remove</button></td>';
            tbody.appendChild(row);
        });
        byId("melt-items-total").textContent = money(total);
        tbody.querySelectorAll(".melt-remove-item").forEach(function (button) {
            button.addEventListener("click", function () {
                meltItems.splice(parseInt(button.dataset.index, 10), 1);
                saveMeltItems();
                renderMeltItemList();
            });
        });
    }
    byId("melt-add-to-list").addEventListener("click", function () {
        var mode = meltMode();
        var metal = byId("melt-metal").value;
        var purityText;
        if (mode === "junk") {
            purityText = "90% (junk silver)";
        } else {
            var puritySelect = byId("melt-purity");
            var selected = puritySelect.options[puritySelect.selectedIndex];
            purityText = selected ? selected.textContent : "";
        }
        var weightText = mode === "junk"
            ? ("$" + (byId("melt-face_value").value || "0") + " face")
            : ((byId("melt-weight").value || "0") + " " + byId("melt-unit").value);
        meltItems.push({
            label: byId("melt-label").value, metal: metal, purity: purityText, weight: weightText,
            melt: lastMeltValue, payout: lastPayoutValue,
        });
        saveMeltItems();
        renderMeltItemList();
        byId("melt-label").value = "";
    });

    // ---------- "Use melt/payout value on the eBay tab" ----------
    byId("melt-use-as-item-cost").addEventListener("click", function () {
        byId("ebay-item_cost").value = lastMeltValue.toFixed(2);
        activateTab("ebay");
        scheduleEbayCalculate();
    });
    byId("melt-use-as-listing-price").addEventListener("click", function () {
        byId("ebay-sale_price").value = lastPayoutValue.toFixed(2);
        activateTab("ebay");
        scheduleEbayCalculate();
    });

    // ---------- Tabs (remembers the last-used tab in localStorage) ----------
    function activateTab(name) {
        TAB_NAMES.forEach(function (tab) {
            byId("tab-" + tab).hidden = tab !== name;
        });
        document.querySelectorAll("#calc-tabs .scanner-tab").forEach(function (link) {
            link.classList.toggle("is-active", link.dataset.tab === name);
        });
        try { localStorage.setItem(STORAGE_KEY_TAB, name); } catch (e) {}
    }
    document.querySelectorAll("#calc-tabs .scanner-tab").forEach(function (link) {
        link.addEventListener("click", function (e) { e.preventDefault(); activateTab(link.dataset.tab); });
    });

    // ---------- "Use this total on the eBay tab" ----------
    byId("use-total-on-ebay").addEventListener("click", function () {
        byId("ebay-item_cost").value = lastBuyTotal.toFixed(2);
        activateTab("ebay");
        scheduleEbayCalculate();
    });

    // ---------- Initial load: localStorage first, then an explicit lot
    // prefill (Open in Calculator) always wins for the Buy cost tab. ----------
    var restoredMelt = false;
    try {
        if (!JUST_SUBMITTED_BUY) {
            var savedBuy = localStorage.getItem(STORAGE_KEY_BUY);
            if (savedBuy) applyBuyData(JSON.parse(savedBuy));
        }
        if (!JUST_SUBMITTED_EBAY) {
            var savedEbay = localStorage.getItem(STORAGE_KEY_EBAY);
            if (savedEbay) applyEbayData(JSON.parse(savedEbay));
        }
        if (!JUST_SUBMITTED_MELT) {
            var savedMelt = localStorage.getItem(STORAGE_KEY_MELT);
            if (savedMelt) { applyMeltData(JSON.parse(savedMelt)); restoredMelt = true; }
        }
    } catch (e) {}

    if (LOT_PREFILL && LOT_PREFILL.bid !== undefined) {
        applyBuyData(LOT_PREFILL);
        activateTab("buy");
    } else {
        var savedTab = null;
        try { savedTab = localStorage.getItem(STORAGE_KEY_TAB); } catch (e) {}
        var justSubmittedAny = JUST_SUBMITTED_BUY || JUST_SUBMITTED_EBAY || JUST_SUBMITTED_MELT;
        if (!justSubmittedAny && savedTab && TAB_NAMES.indexOf(savedTab) !== -1) {
            activateTab(savedTab);
        }
    }

    // A restored melt calculation already has its own metal/purity -
    // otherwise, populate purity options and prefill spot for the
    // server-rendered default metal (gold) so both are ready to use
    // immediately, with or without JS having run yet.
    if (!restoredMelt) {
        populatePurityOptions(byId("melt-metal").value);
        refillSpotForMetal(byId("melt-metal").value);
    }
    syncMeltModeVisibility();
    syncPurityVisibility();
    renderMeltItemList();

    syncCustomRateVisibility();
    scheduleBuyCalculate();
    scheduleEbayCalculate();
    scheduleMeltCalculate();

    // =====================================================================
    // The slide-in Calculator panel (dashboard.html/today.html only - a
    // no-op everywhere else, since #calc-panel doesn't exist there).
    // =====================================================================
    var panel = byId("calc-panel");
    var toggleButton = byId("calc-panel-toggle");
    var closeButton = byId("calc-panel-close");
    var lotTitleEl = byId("calc-panel-lot-title");

    if (panel && toggleButton) {
        function panelIsOpen() { return panel.classList.contains("is-open"); }

        function openPanel() {
            if (panelIsOpen()) return;
            panel.classList.add("is-open");
            panel.removeAttribute("inert");
            panel.setAttribute("aria-hidden", "false");
            panel.focus();
        }
        function closePanel() {
            if (!panelIsOpen()) return;
            panel.classList.remove("is-open");
            panel.setAttribute("inert", "");
            panel.setAttribute("aria-hidden", "true");
            toggleButton.focus();
        }
        function togglePanel() {
            if (panelIsOpen()) closePanel(); else openPanel();
        }

        // Closed by default - inert from the start so a hidden-but-off-
        // screen panel can't be Tabbed into.
        panel.setAttribute("inert", "");
        panel.setAttribute("aria-hidden", "true");

        toggleButton.addEventListener("click", togglePanel);
        if (closeButton) closeButton.addEventListener("click", closePanel);

        document.addEventListener("keydown", function (event) {
            if (event.key === "Escape") {
                if (panelIsOpen()) closePanel();
                return;
            }
            if (event.key !== "c" && event.key !== "C") return;
            if (event.metaKey || event.ctrlKey || event.altKey) return;
            var active = document.activeElement;
            var tag = active ? active.tagName : "";
            var typing = ["INPUT", "TEXTAREA", "SELECT"].indexOf(tag) !== -1 || (active && active.isContentEditable);
            if (typing) return;
            event.preventDefault();
            togglePanel();
        });

        // ---------- "Calc" buttons on dashboard/Today lot rows (Act Now,
        // Watch, My Bids) - opens the panel on the Buy cost tab, prefilled
        // from that row's own data-* attributes (current bid + source,
        // same defaults the Source dropdown would apply by hand), with the
        // lot's title shown at the top of the panel. Once the Buy cost
        // total comes back, the eBay tab is prefilled too (that total as
        // item cost, the scanner's expected sale as listing price) -
        // without switching to it, since the panel opens on Buy cost. ----------
        document.querySelectorAll(".js-calc-from-lot").forEach(function (button) {
            button.addEventListener("click", function () {
                var title = button.dataset.title || "";
                var bid = button.dataset.bid || "";
                var source = button.dataset.source || "custom";
                var expectedSale = button.dataset.expectedSale || "";

                if (lotTitleEl) {
                    lotTitleEl.textContent = title;
                    lotTitleEl.hidden = !title;
                }
                openPanel();
                activateTab("buy");
                applyBuyData({bid: bid, source: source});
                applySourcePreset(source);
                doBuyCalculate().then(function (buyResults) {
                    if (!buyResults) return;
                    applyEbayData({item_cost: buyResults.total.toFixed(2), sale_price: expectedSale});
                    scheduleEbayCalculate();
                });
            });
        });
    }
})();
