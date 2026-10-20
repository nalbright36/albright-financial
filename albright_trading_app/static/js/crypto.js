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
})();
