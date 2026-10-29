/*
 * Recipes > Import from link (albright_meals/_import_box.html). Vanilla JS.
 *  - Import sends the link (fetch POST) and puts the answer - the preview to
 *    fix and save, "Already saved", or why it couldn't - under the box, with
 *    no page reload. Saving the preview is a normal form post.
 *  - Without JS the form opens the same preview as its own page.
 */
(function () {
    "use strict";
    var form = document.getElementById("recipe-import-form");
    var result = document.getElementById("recipe-import-result");
    if (!form || !result) return;
    var button = document.getElementById("recipe-import-button");
    var busy = false;

    function finish() {
        busy = false;
        button.disabled = false;
        button.textContent = "Import";
    }

    function message(text) {
        result.innerHTML = "";
        var p = document.createElement("p");
        p.className = "meals-import__message meals-import__message--error";
        p.setAttribute("role", "alert");
        p.textContent = text;
        result.appendChild(p);
    }

    form.addEventListener("submit", function (event) {
        event.preventDefault();
        if (busy) return;
        busy = true;
        button.disabled = true;
        button.textContent = "Reading the page...";
        result.innerHTML = "";
        fetch(form.action, {
            method: "POST",
            body: new FormData(form),
            credentials: "same-origin",
            headers: {"X-Requested-With": "XMLHttpRequest", "Accept": "application/json"}
        }).then(function (response) {
            if (!response.ok) throw new Error("HTTP " + response.status);
            return response.json();
        }).then(function (data) {
            result.innerHTML = data.html || "";
            var heading = document.getElementById("recipe-import-heading");
            if (heading) heading.focus();
        }).catch(function () {
            message("Couldn't import that link just now - try again.");
        }).then(finish);
    });
})();
