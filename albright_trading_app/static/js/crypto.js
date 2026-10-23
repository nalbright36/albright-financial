/*
 * Crypto app (albright_crypto_app): tick a step off without a reload.
 * Without JS the same form posts normally and the page reloads. Vanilla JS.
 *
 * This file - like the whole crypto app - never touches a wallet: no
 * window.ethereum, no signing, no transactions.
 */
(function () {
    "use strict";

    function csrfToken() {
        var match = document.cookie.match(/csrftoken=([^;]+)/);
        return match ? match[1] : "";
    }

    document.addEventListener("submit", function (event) {
        var form = event.target.closest(".js-step-form");
        if (!form || !window.fetch) return;
        event.preventDefault();
        var button = form.querySelector("button");
        var item = form.closest("li");
        button.disabled = true;
        fetch(form.action, {
            method: "POST",
            body: new FormData(form),
            headers: {"X-Requested-With": "XMLHttpRequest", "X-CSRFToken": csrfToken()},
        }).then(function (r) { return r.ok ? r.json() : null; }).then(function (data) {
            button.disabled = false;
            if (!data) return;
            item.classList.toggle("is-done", data.done);
            button.innerHTML = data.done ? "&#10003;" : "&#9744;";
            button.setAttribute("aria-pressed", data.done ? "true" : "false");
        }).catch(function () { button.disabled = false; });
    });

    // Copy buttons (proposal / resume drafts): data-copy-target is the id of
    // the element whose text is copied. Without the clipboard API the text
    // stays selectable on the page.
    document.addEventListener("click", function (event) {
        var button = event.target.closest(".js-copy");
        if (!button) return;
        var source = document.getElementById(button.dataset.copyTarget);
        if (!source || !navigator.clipboard) return;
        navigator.clipboard.writeText(source.textContent).then(function () {
            var label = button.textContent;
            button.textContent = "Copied";
            setTimeout(function () { button.textContent = label; }, 1500);
        });
    });

    // Pipeline / detail stage forms: the "why it closed" choice only shows
    // when Closed is picked (without JS it's shown and ignored for other stages).
    document.addEventListener("change", function (event) {
        var select = event.target.closest(".js-stage-select");
        if (!select) return;
        var reason = select.form.querySelector(".js-close-reason");
        if (reason) reason.hidden = select.value !== "closed";
    });
})();
