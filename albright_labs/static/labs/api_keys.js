// My account > API keys (albright_labs/api_keys.html) and the connection approval page:
//  - Copy buttons: data-copy-target is the id of the element to copy (a textarea's or input's value, else its text).
//  - The Connect your AI tabs (role="tablist"): remembers the last tab picked (localStorage - wrapped, since it can
//    be blocked), arrow keys move between tabs. Without JS every panel shows.
//  - Full access / Read only / Custom: the Custom checkboxes show only with Custom picked.
//  - Forms with data-confirm ask first (revoke, rotate).
(function () {
    "use strict";
    var TAB_KEY = "albright.apiKeys.tab";

    document.addEventListener("click", function (event) {
        var button = event.target.closest(".js-copy");
        if (!button) return;
        var source = document.getElementById(button.dataset.copyTarget);
        if (!source) return;
        var text = "value" in source && source.tagName !== "BUTTON" ? source.value : source.textContent;
        var done = function () {
            var label = button.textContent;
            button.textContent = "Copied";
            setTimeout(function () { button.textContent = label; }, 1500);
        };
        if (navigator.clipboard && navigator.clipboard.writeText) {
            navigator.clipboard.writeText(text.trim()).then(done, function () { select(source); });
        } else {
            select(source);
        }
    });

    function select(el) {
        if (el.select) { el.select(); return; }
        var range = document.createRange();
        range.selectNodeContents(el);
        var selection = window.getSelection();
        selection.removeAllRanges();
        selection.addRange(range);
    }

    function readTab() { try { return window.localStorage.getItem(TAB_KEY); } catch (e) { return null; } }
    function saveTab(value) { try { window.localStorage.setItem(TAB_KEY, value); } catch (e) { /* blocked: fine */ } }

    var tablist = document.querySelector('[role="tablist"].ak-tabs');
    if (tablist) {
        var tabs = Array.prototype.slice.call(tablist.querySelectorAll('[role="tab"]'));
        var show = function (tab, remember) {
            tabs.forEach(function (t) {
                var on = t === tab;
                t.setAttribute("aria-selected", on ? "true" : "false");
                t.tabIndex = on ? 0 : -1;
                var panel = document.getElementById(t.getAttribute("aria-controls"));
                if (panel) panel.hidden = !on;
            });
            if (remember) saveTab(tab.dataset.tab);
        };
        tablist.hidden = false;
        var saved = readTab();
        show(tabs.filter(function (t) { return t.dataset.tab === saved; })[0] || tabs[0], false);
        tabs.forEach(function (tab, i) {
            tab.addEventListener("click", function () { show(tab, true); });
            tab.addEventListener("keydown", function (event) {
                var next = null;
                if (event.key === "ArrowRight") next = tabs[(i + 1) % tabs.length];
                if (event.key === "ArrowLeft") next = tabs[(i - 1 + tabs.length) % tabs.length];
                if (event.key === "Home") next = tabs[0];
                if (event.key === "End") next = tabs[tabs.length - 1];
                if (next) { event.preventDefault(); show(next, true); next.focus(); }
            });
        });
    }

    function syncAccess(form) {
        var custom = form.querySelector(".ak-custom");
        var picked = form.querySelector('input[name="access"]:checked');
        if (custom && picked) custom.hidden = picked.value !== "custom";
    }
    document.querySelectorAll("form .ak-choices").forEach(function (choices) { syncAccess(choices.closest("form")); });
    document.addEventListener("change", function (event) {
        if (event.target.name === "access" && event.target.form) syncAccess(event.target.form);
    });

    document.addEventListener("submit", function (event) {
        var form = event.target;
        if (form.matches && form.matches("form[data-confirm]") && !window.confirm(form.dataset.confirm)) event.preventDefault();
    });
})();
