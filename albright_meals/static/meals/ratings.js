/*
 * Ratings on the recipe cards and the space's stamps (albright_meals/recipes.html, recipe.html).
 * Vanilla JS; without it every form posts normally.
 *
 *   card rating  my rating chip on each card (recipes.html): tapping it opens a small menu of the levels (js/menu.js);
 *                choosing one paints the chip at once and saves it (views.recipe_rate) - back as it was if that fails.
 *                The chip sits above the card's link, so rating never opens the recipe. Without JS the menu's buttons
 *                post the form and come back to the list.
 *   delete       a card's Delete asks first (form[data-confirm])
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

    // ---------- Card ratings: the chip and its menu ----------
    // The menu's panel is moved to the end of <body> while open (menu.js), so everything is found by the recipe's id.
    function paint(id, level, label) {
        var chip = document.querySelector('[data-rate-chip="' + id + '"]');
        if (!chip) return;
        chip.setAttribute("data-level", level || "");
        chip.querySelector(".rl-rate__label").textContent = level ? label : "Rate";
        var title = chip.closest("[data-recipe]").querySelector(".meals-recipe-card__title");
        chip.setAttribute("aria-label", "My rating for " + (title ? title.textContent : "this recipe") + ": " + (level ? label : "not rated"));
        document.querySelectorAll('[data-rate-form="' + id + '"] .rl-rate__opt').forEach(function (opt) {
            if (opt.value === String(level || "")) opt.setAttribute("aria-current", "true");
            else opt.removeAttribute("aria-current");
        });
    }
    document.addEventListener("click", function (event) {
        var opt = event.target.closest && event.target.closest("[data-rate-form] button[name=level]");
        if (opt) opt.form._level = opt;                     // which level was chosen (e.submitter isn't everywhere)
    });
    document.addEventListener("submit", function (event) {
        var form = event.target;
        if (form.matches("form[data-confirm]")) {
            if (!window.confirm(form.getAttribute("data-confirm"))) event.preventDefault();
            return;
        }
        if (!form.matches("[data-rate-form]")) return;
        event.preventDefault();
        var id = form.getAttribute("data-rate-form"), button = event.submitter || form._level;
        var level = button ? button.value : "", label = button ? button.textContent.trim() : "";
        var chip = document.querySelector('[data-rate-chip="' + id + '"]');
        var before = {level: chip.getAttribute("data-level"), label: chip.querySelector(".rl-rate__label").textContent};
        var status = document.getElementById("rate-status");
        var details = chip.closest("details");
        if (details) details.open = false;
        paint(id, level, label);
        var body = new FormData(form);
        body.set("level", level);
        post(form.action, body).then(function (data) {
            if (!data.ok) throw new Error("not saved");
            paint(id, data.level ? String(data.level) : "", data.label);
            if (status) status.textContent = data.level ? "Rated " + data.label : "Rating cleared";
        }).catch(function () {
            paint(id, before.level, before.label);
            chip.classList.add("is-failed");
            setTimeout(function () { chip.classList.remove("is-failed"); }, 1600);
            if (status) status.textContent = "Couldn't save the rating - try again.";
        });
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
