/*
 * Ratings on the recipe cards, household stamps, and sharing a recipe (albright_meals/recipes.html, recipe.html).
 * Vanilla JS; without it every form posts normally.
 *
 *   card rating  my rating drop-down on each card saves as soon as it changes (views.recipe_rate) and shows a small
 *                "Saved" check - the card's link is underneath, so using the drop-down never opens the recipe
 *   stamps       tapping a household member's stamp shows their label and note under the card
 *   share        the Share button opens the dialog for its recipe: add someone by their exact username or email,
 *                Remove, Copy link (views.recipe_share answers with who it's shared with now)
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

    // ---------- Household stamps: tap for the label and note ----------
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

    // ---------- Sharing ----------
    var dialog = document.getElementById("share-dialog");
    if (!dialog || !dialog.showModal) return;
    var form = document.getElementById("share-form"), list = document.getElementById("share-list");
    var status = document.getElementById("share-status"), name = dialog.querySelector("[data-share-name]");
    var current = null;

    function render(shares) {
        list.textContent = "";
        if (!shares.length) {
            var none = document.createElement("li");
            none.className = "meals-hint";
            none.textContent = "Nobody outside your household yet.";
            list.appendChild(none);
        }
        shares.forEach(function (s) {
            var li = document.createElement("li"), label = document.createElement("span"), remove = document.createElement("button");
            label.textContent = s.name;
            remove.type = "button";
            remove.className = "btn btn-secondary btn-sm";
            remove.textContent = "Remove";
            remove.setAttribute("aria-label", "Stop sharing with " + s.name);
            remove.addEventListener("click", function () {
                var body = new FormData();
                body.append("action", "remove");
                body.append("user", s.user);
                post(current.getAttribute("data-share-open"), body).then(done);
            });
            li.append(label, remove);
            list.appendChild(li);
        });
        if (current) current.setAttribute("data-share-list", JSON.stringify(shares));
    }
    function done(data) {
        status.textContent = data.ok ? (data.message || "") : (data.error || "Not saved - try again.");
        status.classList.toggle("is-error", !data.ok);
        if (data.shares) render(data.shares);
    }
    document.addEventListener("click", function (event) {
        var open = event.target.closest && event.target.closest("[data-share-open]");
        if (!open) return;
        current = open;
        name.textContent = open.getAttribute("data-share-title");
        status.textContent = "";
        form.reset();
        var shares = [];
        try { shares = JSON.parse(open.getAttribute("data-share-list") || "[]"); } catch (e) { shares = []; }
        render(shares);
        dialog.showModal();
        form.elements.who.focus();
    });
    form.addEventListener("submit", function (event) {
        event.preventDefault();
        var body = new FormData(form);
        body.append("action", "add");
        post(current.getAttribute("data-share-open"), body).then(function (data) {
            done(data);
            if (data.ok) form.elements.who.value = "";
        }).catch(function () { done({ok: false}); });
    });
    dialog.addEventListener("click", function (event) {
        if (event.target === dialog || event.target.closest("[data-share-close]")) dialog.close();
        var copy = event.target.closest("[data-share-copy]");
        if (copy && current) {
            var url = new URL(current.getAttribute("data-share-link"), window.location.origin).href;
            (navigator.clipboard ? navigator.clipboard.writeText(url) : Promise.reject()).then(function () {
                status.textContent = "Link copied - it opens only for people it's shared with.";
            }).catch(function () { status.textContent = url; });
        }
    });
})();
