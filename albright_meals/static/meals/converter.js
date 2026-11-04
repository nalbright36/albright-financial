/*
 * Meals > Convert (albright_meals/_converter.html). Vanilla JS; the math is meals/convert_math.js (MealsConvert).
 *  - Opens from the floating button, any [data-convert-open] (an ingredient's icon passes its amount, unit and
 *    name), or "C" on a desktop. A <dialog> on desktop, a bottom sheet on phones (CSS); closes with x, Esc, a tap
 *    outside, or a swipe down on the sheet - and the recipe stays where it was scrolled.
 *  - Volume / Weight / Temperature; results update as you type, in every unit of the type (practical ones first),
 *    with a "Practical" line, a multiplier, and approximate volume <-> weight for common ingredients.
 *  - Remembers the last tab, units and ingredient (localStorage - wrapped; private browsing just goes without).
 */
(function () {
    "use strict";
    var C = window.MealsConvert, dialog = document.getElementById("meals-converter");
    if (!C || !dialog || typeof dialog.showModal !== "function") return;
    var STORE = "meals_convert";
    var $ = function (id) { return document.getElementById(id); };
    var amount = $("convert-amount"), unit = $("convert-unit"), scaleBox = $("convert-scale"), results = $("convert-results");
    var practicalBox = $("convert-practical"), ingredient = $("convert-ingredient"), cross = $("convert-cross");
    var temp = $("convert-temp"), tempUnit = $("convert-temp-unit"), tempResult = $("convert-temp-result"), from = $("convert-from");
    var state = load(), factor = 1, opener = null, scrollY = 0;

    function load() {
        try { return Object.assign({tab: "volume", volume: "cup", weight: "oz", temp: "F", ingredient: ""}, JSON.parse(localStorage.getItem(STORE)) || {}); }
        catch (e) { return {tab: "volume", volume: "cup", weight: "oz", temp: "F", ingredient: ""}; }
    }
    function save() {
        try { localStorage.setItem(STORE, JSON.stringify(state)); } catch (e) { /* storage off */ }
    }

    C.INGREDIENTS.forEach(function (i) {
        var option = document.createElement("option");
        option.value = i.key;
        option.textContent = i.label;
        ingredient.appendChild(option);
    });

    function fillUnits(type) {
        unit.textContent = "";
        C.ORDER[type].forEach(function (key) {
            var option = document.createElement("option");
            option.value = key;
            option.textContent = C.UNITS[key].label;
            unit.appendChild(option);
        });
        unit.value = state[type] && C.UNITS[state[type]] && C.UNITS[state[type]].type === type ? state[type] : C.ORDER[type][0];
    }

    function setTab(tab) {
        state.tab = tab;
        dialog.querySelectorAll("[data-convert-tab]").forEach(function (b) {
            b.setAttribute("aria-selected", b.getAttribute("data-convert-tab") === tab ? "true" : "false");
        });
        $("convert-amounts").hidden = tab === "temperature";
        $("convert-temperature").hidden = tab !== "temperature";
        if (tab !== "temperature") fillUnits(tab);
        save();
        render();
    }

    function row(list, label, value, approx) {
        var li = document.createElement("li");
        li.className = "meals-converter__row";
        var v = document.createElement("span");
        v.className = "meals-converter__value";
        v.textContent = (approx ? "≈ " : "") + value;
        var l = document.createElement("span");
        l.className = "meals-converter__unit";
        l.textContent = label;
        li.appendChild(v);
        li.appendChild(l);
        list.appendChild(li);
    }

    function render() {
        if (state.tab === "temperature") return renderTemp();
        var base = C.parseAmount(amount.value), key = unit.value, value = base == null ? null : C.scale(base, factor);
        amount.setAttribute("aria-invalid", amount.value.trim() && base == null ? "true" : "false");
        results.textContent = "";
        practicalBox.textContent = "";
        cross.textContent = "";
        if (value == null) return;
        C.results(value, key).forEach(function (r) { row(results, r.label, r.text, r.approx); });
        var practical = C.practical(value, key);
        if (practical) {
            var strong = document.createElement("strong");
            strong.textContent = "Practical: ";
            practicalBox.appendChild(strong);
            practicalBox.appendChild(document.createTextNode(practical));
        }
        var other = C.crossConvert(value, key, ingredient.value);
        if (other) {
            var title = document.createElement("p");
            title.className = "meals-converter__label";
            title.textContent = "≈ " + (other.type === "weight" ? "Weight" : "Volume") + " of " +
                ingredient.options[ingredient.selectedIndex].textContent.toLowerCase();
            cross.appendChild(title);
            var list = document.createElement("ul");
            list.className = "meals-converter__results meals-converter__results--cross";
            C.results(other.value, other.unit).slice(0, 4).forEach(function (r) { row(list, r.label, r.text, true); });
            cross.appendChild(list);
        }
    }

    function renderTemp() {
        var value = C.parseAmount(String(temp.value).replace(/^-/, "")), negative = /^\s*-/.test(temp.value);
        tempResult.textContent = "";
        if (value == null) return;
        if (negative) value = -value;
        var isF = tempUnit.value === "F", out = isF ? C.fToC(value) : C.cToF(value);
        tempResult.textContent = Math.round(value) + "°" + tempUnit.value + " = " + Math.round(out) + "°" + (isF ? "C" : "F");
    }

    function setFactor(value) {
        factor = value;
        dialog.querySelectorAll("[data-convert-scale]").forEach(function (b) {
            b.setAttribute("aria-pressed", parseFloat(b.getAttribute("data-convert-scale")) === value && !scaleBox.value ? "true" : "false");
        });
        render();
    }

    function open(source) {
        opener = source || document.activeElement;
        scrollY = window.scrollY;
        from.hidden = true;
        factor = 1;
        scaleBox.value = "";
        var qty = source && source.getAttribute("data-convert-qty"), u = source && source.getAttribute("data-convert-unit");
        var name = source && source.getAttribute("data-convert-name");
        if (source && source.hasAttribute("data-convert-name")) {
            var key = C.unitKey(u), type = C.unitType(key);
            if (type) { state[type] = key; state.tab = type; }
            if (qty && C.parseAmount(qty) != null) {                 // "2.25" (the parser's) shows as 2 1/4
                var nice = C.friendly(C.parseAmount(qty));
                amount.value = nice && !nice.approx ? nice.text : qty;
            }
            var match = C.matchIngredient(name);
            if (match) state.ingredient = match;
            from.textContent = "From the recipe: " + [qty ? amount.value : "", u, name].filter(Boolean).join(" ") +
                (u && !type ? " - “" + u + "” isn't a unit this converts" : "");
            from.hidden = false;
        }
        ingredient.value = state.ingredient || "";
        tempUnit.value = state.temp === "C" ? "C" : "F";
        setTab(state.tab === "weight" || state.tab === "temperature" ? state.tab : "volume");
        setFactor(1);
        dialog.showModal();
        var first = state.tab === "temperature" ? temp : amount;
        first.focus({preventScroll: true});
        window.scrollTo(0, scrollY);
    }

    function close() {
        dialog.style.transform = "";
        if (dialog.open) dialog.close();
    }
    dialog.addEventListener("close", function () {
        window.scrollTo(0, scrollY);                              // where the recipe was
        if (opener && opener.focus) opener.focus({preventScroll: true});
        window.scrollTo(0, scrollY);
    });

    document.addEventListener("click", function (event) {
        var trigger = event.target.closest && event.target.closest("[data-convert-open]");
        if (trigger) { event.preventDefault(); open(trigger); }
    });
    dialog.addEventListener("click", function (event) {
        if (event.target === dialog) close();                    // the backdrop - a tap outside
        if (event.target.closest("[data-convert-close]")) close();
        var tab = event.target.closest("[data-convert-tab]");
        if (tab) setTab(tab.getAttribute("data-convert-tab"));
        var chip = event.target.closest("[data-convert-scale]");
        if (chip) { scaleBox.value = ""; setFactor(parseFloat(chip.getAttribute("data-convert-scale"))); }
        var oven = event.target.closest("[data-convert-oven]");
        if (oven) { temp.value = oven.getAttribute("data-convert-oven"); tempUnit.value = "F"; state.temp = "F"; save(); renderTemp(); }
    });
    // Arrow keys between the tabs (the tablist pattern).
    dialog.querySelector('[role="tablist"]').addEventListener("keydown", function (event) {
        if (event.key !== "ArrowRight" && event.key !== "ArrowLeft") return;
        var tabs = Array.prototype.slice.call(dialog.querySelectorAll("[data-convert-tab]")), i = tabs.indexOf(document.activeElement);
        var next = tabs[(i + (event.key === "ArrowRight" ? 1 : tabs.length - 1)) % tabs.length];
        event.preventDefault();
        next.focus();
        next.click();
    });

    amount.addEventListener("input", render);
    unit.addEventListener("change", function () { state[state.tab] = unit.value; save(); render(); });
    ingredient.addEventListener("change", function () { state.ingredient = ingredient.value; save(); render(); });
    scaleBox.addEventListener("input", function () {
        var f = C.parseAmount(scaleBox.value);
        setFactor(f != null && f > 0 ? f : 1);
    });
    temp.addEventListener("input", renderTemp);
    tempUnit.addEventListener("change", function () { state.temp = tempUnit.value; save(); renderTemp(); });

    // "C" opens it on a desktop - not while typing, and not with a modifier key.
    document.addEventListener("keydown", function (event) {
        if (event.key !== "c" && event.key !== "C") return;
        if (event.metaKey || event.ctrlKey || event.altKey || dialog.open) return;
        var t = event.target;
        if (t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName))) return;
        if (!window.matchMedia("(hover: hover) and (pointer: fine)").matches) return;
        event.preventDefault();
        open($("meals-convert-open"));
    });

    // Phones: swipe the sheet down (from its grip or header) to close it.
    var grip = dialog.querySelector("[data-convert-grip]"), head = dialog.querySelector(".meals-converter__head");
    var startY = null;
    function down(event) {
        if (!window.matchMedia("(max-width: 639.98px)").matches || event.target.closest("button")) return;
        startY = event.clientY;
        dialog.style.transition = "none";
    }
    function move(event) {
        if (startY == null) return;
        var dy = Math.max(0, event.clientY - startY);
        dialog.style.transform = "translateY(" + dy + "px)";
    }
    function up(event) {
        if (startY == null) return;
        var dy = event.clientY - startY;
        startY = null;
        dialog.style.transition = "";
        if (dy > 80) close(); else dialog.style.transform = "";
    }
    [grip, head].forEach(function (el) {
        if (!el) return;
        el.addEventListener("pointerdown", down);
    });
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
    window.addEventListener("pointercancel", up);
})();
