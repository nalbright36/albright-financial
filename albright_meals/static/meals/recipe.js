/*
 * A recipe's ingredients (albright_meals/recipe.html): Select all / none, and the "Add N items" button's count.
 * Vanilla JS; without it the form still posts whatever is ticked.
 */
(function () {
    "use strict";
    var form = document.getElementById("ingredient-form");
    if (!form) return;
    var button = document.getElementById("ingredient-add");
    var count = form.querySelector("[data-pick-count]");
    var plural = form.querySelector("[data-pick-plural]");
    var boxes = Array.prototype.slice.call(form.querySelectorAll("[data-pick-box]:not([disabled])"));

    function refresh() {
        var n = boxes.filter(function (b) { return b.checked; }).length;
        if (count) count.textContent = n;
        if (plural) plural.textContent = n === 1 ? "" : "s";
        if (button) button.disabled = n === 0;
    }

    form.addEventListener("click", function (e) {
        var pick = e.target.closest("[data-pick]");
        if (!pick) return;
        e.preventDefault();
        boxes.forEach(function (b) { b.checked = pick.dataset.pick === "all"; });
        refresh();
    });
    form.addEventListener("change", refresh);
    refresh();
})();

/*
 * The recipe's Rating drop-down (recipe.html, views.recipe_rate): this person's own rating saves as soon as it
 * changes - blank removes it - with its meaning underneath; "Needs tweaks" also asks "What would you change?"
 * (saved when that box changes). Without JS the Save button posts the form.
 */
(function () {
    "use strict";
    var form = document.getElementById("recipe-rating");
    if (!form || !window.fetch) return;
    var select = form.querySelector("select[name=level]"), note = form.querySelector("input[name=note]");
    var noteField = document.getElementById("recipe-rating-note-field");
    var status = document.getElementById("recipe-rating-status"), meaning = document.getElementById("recipe-rating-meaning");
    var tweaks = form.getAttribute("data-needs-tweaks"), saved = select.value;
    form.classList.add("is-live");

    function showNote() { noteField.hidden = select.value !== tweaks; }
    function save() {
        status.textContent = "Saving…";
        fetch(form.action, {method: "POST", credentials: "same-origin", body: new FormData(form),
                            headers: {"X-Requested-With": "XMLHttpRequest"}})
            .then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); })
            .then(function (data) {
                saved = select.value;
                select.setAttribute("data-level", data.level || "");
                meaning.textContent = data.meaning || "Only you see your rating.";
                status.textContent = data.level ? "Saved" : "Cleared";
                setTimeout(function () { status.textContent = ""; }, 2500);
            })
            .catch(function () {
                select.value = saved;
                select.setAttribute("data-level", saved);
                showNote();
                status.textContent = "Not saved - try again.";
            });
    }
    select.addEventListener("change", function () {
        showNote();
        save();
        if (select.value === tweaks) note.focus();
    });
    note.addEventListener("change", save);
    form.addEventListener("submit", function (e) { e.preventDefault(); save(); });
})();
