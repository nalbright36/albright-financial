// A public recipe share link (albright_meals/shared_recipe.html): the servings scaler and Print. A file, not inline -
// the public pages' CSP allows no inline script. Without JS the page shows the recipe as written.
(function () {
    "use strict";
    var FRACTIONS = [[1 / 8, "1/8"], [1 / 4, "1/4"], [1 / 3, "1/3"], [1 / 2, "1/2"], [2 / 3, "2/3"], [3 / 4, "3/4"]];

    // 1.5 -> "1 1/2"; 0.333 -> "1/3"; 2.4 -> "2.4" (as meal_view._quantity_text writes them).
    function amount(value) {
        if (!(value > 0)) return "";
        var whole = Math.floor(value + 1e-9), rest = value - whole;
        if (rest < 0.02) return String(whole);
        if (rest > 0.98) return String(whole + 1);
        for (var i = 0; i < FRACTIONS.length; i++) {
            if (Math.abs(rest - FRACTIONS[i][0]) < 0.02) return (whole ? whole + " " : "") + FRACTIONS[i][1];
        }
        return String(Math.round(value * 100) / 100);
    }

    var scale = document.querySelector("[data-scale]");
    if (scale) {
        var base = parseInt(scale.getAttribute("data-base"), 10) || 1, now = base;
        var out = scale.querySelector("[data-servings]");
        var rows = Array.prototype.slice.call(document.querySelectorAll(".sr-ing__amt[data-qty]"));
        rows.forEach(function (el) { el.setAttribute("data-written", el.textContent); });
        var draw = function () {
            out.textContent = now;
            var factor = now / base;
            rows.forEach(function (el) {
                if (now === base) { el.textContent = el.getAttribute("data-written"); return; }
                var qty = parseFloat(el.getAttribute("data-qty")) * factor;
                var unit = qty > 1 ? el.getAttribute("data-many") : el.getAttribute("data-one");
                el.textContent = amount(qty) + (unit ? " " + unit : "");
            });
            scale.querySelector('[data-step="-1"]').disabled = now <= 1;
            scale.querySelector('[data-step="1"]').disabled = now >= 99;
        };
        Array.prototype.forEach.call(scale.querySelectorAll("[data-step]"), function (btn) {
            btn.hidden = false;
            btn.addEventListener("click", function () {
                now = Math.max(1, Math.min(99, now + parseInt(btn.getAttribute("data-step"), 10)));
                draw();
            });
        });
        draw();
    }

    var print = document.querySelector("[data-print]");
    if (print && window.print) {
        print.hidden = false;
        print.addEventListener("click", function () { window.print(); });
    }
})();
