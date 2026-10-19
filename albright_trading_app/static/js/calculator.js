/*
 * The calculator's 3 tabs (Melt value / Buy cost / eBay profit) - shared by
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
 *   4. The dialog's "My resale estimate" panel (#calc-estimate) for that
 *      lot - save/clear/reset, prefilled from the lot's saved estimate.
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
    // Display order; the first one (Melt value) is the default whenever no
    // valid tab is remembered - a first visit, or cleared/blocked storage.
    var TAB_NAMES = ["melt", "buy", "ebay"];
    var DEFAULT_TAB = TAB_NAMES[0];

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
            // data-label: the dialog stacks this table's cells as label/value
            // pairs instead of scrolling sideways (see style.css .calc-dialog).
            row.innerHTML =
                '<td data-label="Label">' + escapeHtml(item.label || "(unlabeled)") + "</td>" +
                '<td data-label="Metal">' + escapeHtml(item.metal) + "</td>" +
                '<td data-label="Purity">' + escapeHtml(item.purity) + "</td>" +
                '<td class="num" data-label="Weight">' + escapeHtml(item.weight) + "</td>" +
                '<td class="num" data-label="Melt">' + money(item.melt) + "</td>" +
                '<td class="num" data-label="Payout">' + money(item.payout) + "</td>" +
                '<td class="melt-item-actions"><button type="button" class="btn btn-secondary btn-sm melt-remove-item" data-index="' + index + '">Remove</button></td>';
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
        if (!justSubmittedAny) {
            // The most recently used tab, else Melt value.
            activateTab(savedTab && TAB_NAMES.indexOf(savedTab) !== -1 ? savedTab : DEFAULT_TAB);
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
    // The Calculator dialog (dashboard.html only - a no-op everywhere else,
    // since #calc-dialog doesn't exist there). A native <dialog> via
    // showModal(): the browser handles the dimmed backdrop, centering,
    // moving focus in on open, and Esc-to-close - all we add is backdrop-
    // click-to-close and restoring scroll position/focus on close.
    // =====================================================================
    var dialog = byId("calc-dialog");
    var dialogToggleButton = byId("calc-dialog-toggle");
    var closeButton = byId("calc-dialog-close");
    var lotTitleEl = byId("calc-dialog-lot-title");

    if (dialog && typeof dialog.showModal === "function") {
        var savedScrollY = 0;
        var openerElement = null;

        function openDialog(opener) {
            savedScrollY = window.scrollY;
            openerElement = opener || null;
            if (!dialog.open) dialog.showModal();
        }

        // One place for "closed, regardless of how" (✕ button, Esc, or a
        // backdrop click - see below) - dialog's native "close" event
        // fires for all three, so there's nothing close-button-specific
        // to wire beyond calling .close().
        dialog.addEventListener("close", function () {
            window.scrollTo(0, savedScrollY);
            if (openerElement) openerElement.focus();
            openerElement = null;
        });

        if (closeButton) closeButton.addEventListener("click", function () { dialog.close(); });

        // Backdrop click: a click lands directly on <dialog> itself (not a
        // descendant) only when it hits the backdrop-adjacent padding
        // outside the ::backdrop-covered content box - clicking anything
        // inside .calc-dialog__body/__header hits that element instead.
        dialog.addEventListener("click", function (event) {
            if (event.target === dialog) dialog.close();
        });

        if (dialogToggleButton) {
            dialogToggleButton.addEventListener("click", function () {
                if (lotTitleEl) { lotTitleEl.textContent = ""; lotTitleEl.hidden = true; }
                hideEstimatePanel();
                openDialog(dialogToggleButton);
            });
        }

        // ---------- "My resale estimate" (only when opened from a lot's
        // Calc button). Saved per lot server-side (calculator_views.
        // lot_estimate), which also computes My max bid from the estimate
        // plus this dialog's Buy cost/eBay tab values - this file never
        // does that math itself, it only asks calculate_api for a preview
        // (tab "estimate", the same server function). A saved estimate's
        // own calculator values win over the scanner's defaults when the
        // lot is reopened; "Reset to scanner values" goes back to those. ----------
        var estimatePanel = byId("calc-estimate");
        var estimateValueEl = byId("calc-estimate-value");
        var estimateNotesEl = byId("calc-estimate-notes");
        var estimateMaxEl = byId("calc-estimate-max");
        var estimateStatusEl = byId("calc-estimate-status");
        var estimateSavedLabelEl = byId("calc-estimate-saved-label");
        var estimateSavedDateEl = byId("calc-estimate-saved-date");
        var estimateClearButton = byId("calc-estimate-clear");
        var estimateUseMaxButton = byId("calc-estimate-use-max");
        // The lot the dialog is currently open for: {id, bid, estimateUrl,
        // clearUrl, scanner, watch} - null when opened from the toolbar.
        var currentLot = null;

        function hideEstimatePanel() {
            currentLot = null;
            if (estimatePanel) estimatePanel.hidden = true;
        }

        function setEstimateStatus(text) {
            if (estimateStatusEl) estimateStatusEl.textContent = text || "";
        }

        function showSavedState(saved) {
            estimateSavedLabelEl.hidden = !saved;
            estimateClearButton.hidden = !saved;
            estimateSavedDateEl.textContent = saved ? "Your saved estimate from " + saved.updated_date : "";
            syncUseMaxButton(saved);
        }

        function syncUseMaxButton(saved) {
            var watch = currentLot && currentLot.watch;
            estimateUseMaxButton.hidden = !(saved && watch && watch.watching && watch.watch_max_bid === null);
            if (!estimateUseMaxButton.hidden) {
                estimateUseMaxButton.textContent = "Use as my max bid (" + money(saved.my_max_bid) + ")";
            }
        }

        // The "My est. $X · My max $Y" notes under this lot's title in
        // every dashboard table/detail row (_my_estimate_note.html).
        function updateEstimateNotes(lotId, saved) {
            document.querySelectorAll('.js-my-est[data-lot-id="' + lotId + '"]').forEach(function (el) {
                var textEl = el.querySelector(".js-my-est-text");
                if (textEl) {
                    textEl.textContent = saved
                        ? "My est. " + money(saved.resale_estimate) + " · My max " + money(saved.my_max_bid) : "";
                }
                el.hidden = !saved;
            });
        }

        function collectEstimateInputs() {
            return {buy: collectBuyData(), ebay: collectEbayData()};
        }

        var estimatePreviewTimer = null;
        function scheduleEstimatePreview() {
            if (!currentLot) return;
            window.clearTimeout(estimatePreviewTimer);
            estimatePreviewTimer = window.setTimeout(doEstimatePreview, 300);
        }
        function doEstimatePreview() {
            if (!currentLot) return;
            var data = collectEstimateInputs();
            data.tab = "estimate";
            data.resale_estimate = estimateValueEl.value;
            postCalculate(data).then(function (results) {
                estimateMaxEl.innerHTML = money(results.my_max_bid);
            }).catch(function () {});
        }

        // Bid + source defaults, then once the Buy cost total is back, the
        // eBay tab's item cost (that total) and listing price. prefillGen
        // makes sure only the latest prefill's eBay step lands - the
        // scanner prefill and the saved-estimate prefill that follows it
        // race each other's calculate responses on open.
        var prefillGen = 0;
        function prefillThenEbay(buyData, salePrice) {
            var gen = ++prefillGen;
            applyBuyData(buyData);
            return doBuyCalculate().then(function (buyResults) {
                if (!buyResults || gen !== prefillGen) return;
                var ebayData = {item_cost: buyResults.total.toFixed(2)};
                if (salePrice !== null && salePrice !== undefined && salePrice !== "") ebayData.sale_price = salePrice;
                applyEbayData(ebayData);
                scheduleEbayCalculate();
                scheduleEstimatePreview();
            });
        }

        function applyScannerValues(scanner) {
            estimateValueEl.value = "";
            estimateNotesEl.value = "";
            estimateMaxEl.innerHTML = "&mdash;";
            applyBuyData({bid: scanner.bid, source: scanner.source});
            applySourcePreset(scanner.source);
            return prefillThenEbay({}, scanner.expected_sale);
        }

        function applySavedValues(saved) {
            var inputs = saved.calc_inputs || {};
            // The saved calculator values win over the scanner's source
            // defaults - except the bid itself, which is always the lot's
            // live current bid (My max bid doesn't depend on it anyway).
            var buy = Object.assign({}, inputs.buy || {}, {bid: currentLot.bid});
            applyEbayData(inputs.ebay || {});
            estimateValueEl.value = saved.resale_estimate;
            estimateNotesEl.value = saved.notes || "";
            estimateMaxEl.innerHTML = money(saved.my_max_bid);
            return prefillThenEbay(buy, saved.resale_estimate);
        }

        function openEstimatePanel(button) {
            currentLot = {id: button.dataset.lotId, bid: button.dataset.bid || "", estimateUrl: button.dataset.estimateUrl};
            var lot = currentLot;
            if (!estimatePanel || !lot.estimateUrl) return Promise.resolve(null);
            estimatePanel.hidden = false;
            estimateValueEl.value = "";
            estimateNotesEl.value = "";
            estimateMaxEl.innerHTML = "&mdash;";
            setEstimateStatus("");
            estimateSavedLabelEl.hidden = true;
            estimateClearButton.hidden = true;
            estimateUseMaxButton.hidden = true;
            return fetch(lot.estimateUrl, {headers: {"X-Requested-With": "XMLHttpRequest"}})
                .then(function (r) { return r.ok ? r.json() : null; })
                .then(function (data) {
                    if (!data || currentLot !== lot) return null;  // reopened on another lot meanwhile
                    lot.scanner = data.scanner;
                    lot.watch = data.watch;
                    lot.clearUrl = data.clear_url;
                    showSavedState(data.saved);
                    return data;
                })
                .catch(function () { return null; });
        }

        function estimatePost(url, body) {
            return fetch(url, {
                method: "POST",
                headers: {"Content-Type": "application/json", "X-CSRFToken": csrfToken(), "X-Requested-With": "XMLHttpRequest"},
                body: JSON.stringify(body || {}),
            }).then(function (r) {
                return r.json().then(function (data) { return {ok: r.ok, data: data}; });
            });
        }

        if (estimatePanel) {
            estimateValueEl.addEventListener("input", function () {
                // My estimate is the eBay tab's listing price too, so its
                // profit/fees always reflect the number being saved.
                if (estimateValueEl.value !== "") {
                    byId("ebay-sale_price").value = estimateValueEl.value;
                    scheduleEbayCalculate();
                }
                scheduleEstimatePreview();
            });
            ["buy-form", "ebay-form"].forEach(function (id) {
                byId(id).addEventListener("input", scheduleEstimatePreview);
                byId(id).addEventListener("change", scheduleEstimatePreview);
            });

            byId("calc-estimate-save").addEventListener("click", function () {
                if (!currentLot) return;
                var lot = currentLot;
                if (!(parseFloat(estimateValueEl.value) > 0)) {
                    setEstimateStatus("Enter your resale estimate first.");
                    estimateValueEl.focus();
                    return;
                }
                var body = collectEstimateInputs();
                body.resale_estimate = estimateValueEl.value;
                body.notes = estimateNotesEl.value;
                setEstimateStatus("Saving...");
                estimatePost(lot.estimateUrl, body).then(function (res) {
                    if (currentLot !== lot) return;
                    if (!res.ok) { setEstimateStatus(res.data.error || "Couldn't save."); return; }
                    lot.watch = res.data.watch;
                    lot.clearUrl = res.data.clear_url;
                    var saved = res.data.saved;
                    estimateMaxEl.innerHTML = money(saved.my_max_bid);
                    showSavedState(saved);
                    setEstimateStatus("Saved ✓ " + saved.updated_time);
                    updateEstimateNotes(lot.id, saved);
                    document.dispatchEvent(new CustomEvent("watchlist:changed"));
                }).catch(function () { setEstimateStatus("Couldn't save."); });
            });

            estimateClearButton.addEventListener("click", function () {
                if (!currentLot || !currentLot.clearUrl) return;
                var lot = currentLot;
                estimatePost(lot.clearUrl).then(function (res) {
                    if (currentLot !== lot || !res.ok) return;
                    estimateValueEl.value = "";
                    estimateNotesEl.value = "";
                    estimateMaxEl.innerHTML = "&mdash;";
                    showSavedState(null);
                    setEstimateStatus("Estimate cleared");
                    updateEstimateNotes(lot.id, null);
                    document.dispatchEvent(new CustomEvent("watchlist:changed"));
                }).catch(function () {});
            });

            estimateUseMaxButton.addEventListener("click", function () {
                if (!currentLot || !currentLot.watch) return;
                var lot = currentLot;
                estimatePost(lot.watch.use_my_max_url).then(function (res) {
                    if (currentLot !== lot) return;
                    if (!res.ok) { setEstimateStatus(res.data.error || "Couldn't set max bid."); return; }
                    lot.watch.watch_max_bid = res.data.watch_max_bid;
                    estimateUseMaxButton.hidden = true;
                    setEstimateStatus("Watchlist max bid set to " + money(res.data.watch_max_bid));
                    document.dispatchEvent(new CustomEvent("watchlist:changed"));
                }).catch(function () {});
            });

            byId("calc-estimate-reset").addEventListener("click", function (e) {
                e.preventDefault();
                if (!currentLot || !currentLot.scanner) return;
                // Only refills the dialog - the saved estimate stays until
                // "Save to this lot" overwrites it or "Clear estimate".
                applyScannerValues(currentLot.scanner);
                setEstimateStatus("Showing scanner values - not saved");
            });
        }

        // ---------- "Calc" buttons on dashboard lot rows (Act Now, Watch,
        // Watchlist) - opens the dialog on the Buy cost tab, prefilled from
        // that row's own data-* attributes (current bid + source, same
        // defaults the Source dropdown would apply by hand), with the
        // lot's title shown in the dialog's title bar. Once the Buy cost
        // total comes back, the eBay tab is prefilled too (that total as
        // item cost, the scanner's expected sale as listing price) -
        // without switching to it, since the dialog opens on Buy cost.
        // Plain type="button" elements, never inside a <form> of their
        // own, so clicking one can never submit/navigate/scroll.
        //
        // wireCalcButtons(root) is re-run (root scoped to just the
        // replaced markup) whenever static/js/auction_scanner.js swaps in
        // fresh HTML without a full page reload - currently just the
        // Watchlist section after a lot is added there - since a button
        // cloned in via innerHTML has no listener of its own yet. ----------
        function wireCalcButtons(root) {
            root.querySelectorAll(".js-calc-from-lot").forEach(function (button) {
                button.addEventListener("click", function () {
                    var title = button.dataset.title || "";
                    var bid = button.dataset.bid || "";
                    var source = button.dataset.source || "custom";
                    var expectedSale = button.dataset.expectedSale || "";

                    if (lotTitleEl) {
                        lotTitleEl.textContent = title;
                        lotTitleEl.hidden = !title;
                    }
                    activateTab("buy");
                    openDialog(button);
                    // Scanner values right away (so the dialog is never
                    // blank), then - once the lot's saved estimate (if
                    // any) is back - those saved values on top.
                    applyScannerValues({bid: bid, source: source, expected_sale: expectedSale});
                    var lotEstimate = openEstimatePanel(button);
                    lotEstimate.then(function (data) {
                        if (data && data.saved) applySavedValues(data.saved);
                    });
                });
            });
        }

        wireCalcButtons(document);
        document.addEventListener("dashboard-section:refreshed", function (event) {
            wireCalcButtons(event.detail.root);
        });
    }
})();
