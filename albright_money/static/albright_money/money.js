/*
 * Albright Money - on every Money page via the app registry (apps_registry: Money's script). Vanilla JS;
 * every page works without it (each form posts normally).
 *
 *   category   a transaction row's category menu saves as soon as it changes (views.set_category) and then
 *              offers "Always categorize <payee> as <category>?" - Yes makes a rule (views.always)
 *   filters    the Transactions filters apply on change
 *   splits     the running total of a split against the amount; "Add a part" adds a row
 *   import     the mapping step shows the amount column or the debit / credit columns, whichever is picked
 */
(function () {
    "use strict";

    function csrf() {
        var m = document.cookie.match(/csrftoken=([^;]+)/);
        if (m) return m[1];
        var input = document.querySelector("input[name=csrfmiddlewaretoken]");
        return input ? input.value : "";
    }
    function post(url, body) {
        return fetch(url, {method: "POST", credentials: "same-origin", body: body,
                           headers: {"X-CSRFToken": csrf(), "X-Requested-With": "XMLHttpRequest"}})
            .then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); });
    }

    // ---------- Category: save on change, then offer a rule ----------
    var offer = document.getElementById("mn-offer");
    function showOffer(data) {
        if (!offer) return;
        offer.textContent = "";
        if (!data) { offer.hidden = true; return; }
        var text = document.createElement("span");
        text.append("Always categorize ");
        var payee = document.createElement("strong"); payee.textContent = data.payee;
        var cat = document.createElement("strong"); cat.textContent = data.category;
        text.append(payee, " as ", cat, "?");
        var yes = document.createElement("button");
        yes.type = "button"; yes.className = "btn btn-primary btn-sm"; yes.textContent = "Yes";
        var no = document.createElement("button");
        no.type = "button"; no.className = "btn btn-secondary btn-sm"; no.textContent = "No";
        var row = document.createElement("div");
        row.className = "mn-offer__form";
        row.append(text, yes, no);
        offer.append(row);
        offer.hidden = false;
        no.addEventListener("click", function () { showOffer(null); });
        yes.addEventListener("click", function () {
            yes.disabled = no.disabled = true;
            post(data.url, new FormData()).then(function (res) {
                offer.textContent = res.message;
                setTimeout(function () { offer.hidden = true; }, 5000);
                if (res.changed) window.location.reload();          // other rows took the category too
            }).catch(function () { offer.textContent = "Couldn't save the rule - try again."; });
        });
    }
    document.addEventListener("change", function (event) {
        var select = event.target;
        if (!select.matches || !select.matches("select.mn-cat") || !window.fetch) return;
        var form = select.closest("form"), row = select.closest(".mn-txn");
        var was = select.getAttribute("data-was");
        if (was === null) {
            was = Array.prototype.find.call(select.options, function (o) { return o.defaultSelected; });
            was = was ? was.value : "";
        }
        select.classList.add("is-saving");
        post(form.action, new FormData(form)).then(function (res) {
            select.setAttribute("data-was", select.value);
            if (row) {
                row.classList.toggle("is-review", res.needs_review);
                var flag = row.querySelector(".mn-flag");
                if (flag && !res.needs_review) flag.parentNode.removeChild(flag);
            }
            showOffer(res.offer);
        }).catch(function () {
            select.value = was;
            showOffer(null);
            if (offer) { offer.textContent = "Not saved - try again."; offer.hidden = false; }
        }).then(function () { select.classList.remove("is-saving"); });
    });
    document.addEventListener("click", function (event) {
        var no = event.target.closest && event.target.closest("[data-offer-no]");
        if (no && offer) { event.preventDefault(); offer.hidden = true; }
    });

    // ---------- Filters apply on change ----------
    document.addEventListener("change", function (event) {
        if (event.target.matches && event.target.matches("[data-autosubmit]")) event.target.form.submit();
    });

    // ---------- Splits ----------
    var rows = document.getElementById("mn-split-rows"), add = document.getElementById("mn-split-add");
    var sum = document.getElementById("mn-split-sum"), txnForm = document.getElementById("mn-txn-form");
    function money(text) {
        var v = parseFloat(String(text || "").replace(/[$,\s]/g, ""));
        return isNaN(v) ? null : Math.round(v * 100);
    }
    function showSum() {
        var parts = Array.prototype.map.call(rows.querySelectorAll("input[name=split_amount]"), function (i) { return money(i.value); })
            .filter(function (v) { return v !== null; });
        var amount = money(txnForm.elements.amount.value);
        if (!parts.length || amount === null) { sum.textContent = ""; return; }
        var total = parts.reduce(function (a, b) { return a + b; }, 0), left = amount - total;
        sum.textContent = "Parts: " + (total / 100).toFixed(2) + " of " + (amount / 100).toFixed(2) +
            (left ? " - " + (Math.abs(left) / 100).toFixed(2) + (left > 0 ? " still to assign" : " too much") : " - adds up");
    }
    if (rows && add && txnForm) {
        add.hidden = false;
        add.addEventListener("click", function () {
            var copy = rows.lastElementChild.cloneNode(true);
            copy.querySelectorAll("input").forEach(function (i) { i.value = ""; });
            copy.querySelector("select").value = "";
            rows.appendChild(copy);
            copy.querySelector("select").focus();
        });
        txnForm.addEventListener("input", showSum);
        showSum();
    }

    // ---------- Import: which amount columns ----------
    var map = document.getElementById("mn-map");
    if (map) {
        var showStyle = function () {
            var picked = map.querySelector("input[name=style]:checked");
            map.querySelectorAll("[data-style]").forEach(function (el) {
                el.hidden = !!picked && el.getAttribute("data-style") !== picked.value;
            });
        };
        map.addEventListener("change", showStyle);
        showStyle();
    }
})();
