/*
 * Ratings on the recipe cards and the space's stamps (albright_meals/recipes.html, recipe.html).
 * Vanilla JS; without it every form posts normally.
 *
 *   card rating  my rating drop-down on each card saves as soon as it changes (views.recipe_rate) and shows a small
 *                "Saved" check - the card's link is underneath, so using the drop-down never opens the recipe
 *   stamps       tapping someone's stamp shows their label and note under the card
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
            .then(function (r) {
                return r.json().catch(function () { return {}; }).then(function (data) {
                    if (!r.ok && !data.error) throw new Error(r.status);
                    return data;
                });
            });
    }

    // ---------- Card ratings ----------
    document.querySelectorAll("[data-card-rate]").forEach(function (form) {
        form.classList.add("is-live");
        var select = form.querySelector("select"), status = form.querySelector(".meals-card-rate__status");
        var saved = select.value;
        function save() {
            status.textContent = "";
            post(form.action, new FormData(form)).then(function (data) {
                if (!data.ok) throw new Error("not saved");
                saved = select.value;
                select.setAttribute("data-level", data.level || "");
                status.textContent = "✓ Saved";
                setTimeout(function () { status.textContent = ""; }, 2000);
            }).catch(function () {
                select.value = saved;
                select.setAttribute("data-level", saved);
                status.textContent = "Not saved";
            });
        }
        select.addEventListener("change", save);
        form.addEventListener("submit", function (e) { e.preventDefault(); save(); });
    });

    // ---------- The space's stamps: tap for the label and note ----------
    document.addEventListener("click", function (event) {
        var stamp = event.target.closest && event.target.closest("[data-stamp]");
        if (!stamp) return;
        var holder = stamp.closest("[data-recipe], .meals-recipe"), note = holder && holder.querySelector("[data-stamp-note]");
        if (!note) {
            note = document.createElement("p");
            note.className = "meals-stamp-note";
            note.setAttribute("data-stamp-note", "");
            stamp.parentNode.insertAdjacentElement("afterend", note);
        }
        var text = stamp.getAttribute("data-stamp");
        note.hidden = !note.hidden && note.textContent === text;
        note.textContent = text;
    });

})();
