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
