/*
 * Meals > Convert - the math, as pure functions (no DOM). Used by meals/converter.js in the browser (the MealsConvert global)
 * and by albright_meals/js_tests/convert_math.test.js under `node --test` (module.exports).
 *
 *   parseAmount("1 1/2") -> 1.5          "½", "1/3", "1-1/2", ".5", "2" - null when it isn't a number
 *   unitKey("Tbsp.") -> "tbsp"           spellings -> the keys below; unitType("tbsp") -> "volume"
 *   convert(1, "cup", "tbsp") -> 16      within a type (US measures)
 *   friendly(5.333) -> {text: "5 1/3", approx: false}   kitchen units: nearest 1/8 or 1/3; approx when it rounded
 *   metric(78.06, "ml") -> "78"          sensible decimals for mL, L, g, kg
 *   results(1, "cup") -> every unit of its type, the practical ones first
 *   practical(0.375, "cup") -> "1/4 cup + 2 tbsp"     how a cook would measure it
 *   fToC / cToF, scale, and approximate ingredient weights (INGREDIENTS, matchIngredient, crossConvert).
 */
(function (root) {
    "use strict";

    // US measures. Volume in mL, weight in g. Kitchen units also count in teaspoons / ounces ("in"), so their
    // conversions are exact (1 cup = 16 tbsp, not 15.99995).
    var TSP_ML = 4.92892159375, OZ_G = 28.349523125;
    var UNITS = {
        tsp: {type: "volume", size: TSP_ML, in: 1, label: "tsp", kitchen: true},
        tbsp: {type: "volume", size: 3 * TSP_ML, in: 3, label: "tbsp", kitchen: true},
        cup: {type: "volume", size: 48 * TSP_ML, in: 48, label: "cup", plural: "cups", kitchen: true},
        floz: {type: "volume", size: 6 * TSP_ML, in: 6, label: "fl oz", kitchen: true},
        pint: {type: "volume", size: 96 * TSP_ML, in: 96, label: "pint", plural: "pints", kitchen: true},
        quart: {type: "volume", size: 192 * TSP_ML, in: 192, label: "quart", plural: "quarts", kitchen: true},
        gallon: {type: "volume", size: 768 * TSP_ML, in: 768, label: "gallon", plural: "gallons", kitchen: true},
        ml: {type: "volume", size: 1, label: "mL"},
        l: {type: "volume", size: 1000, label: "L"},
        oz: {type: "weight", size: OZ_G, in: 1, label: "oz", kitchen: true},
        lb: {type: "weight", size: 16 * OZ_G, in: 16, label: "lb", kitchen: true},
        g: {type: "weight", size: 1, label: "g"},
        kg: {type: "weight", size: 1000, label: "kg"}
    };
    var ORDER = {volume: ["cup", "tbsp", "tsp", "floz", "ml", "l", "pint", "quart", "gallon"],
                 weight: ["oz", "lb", "g", "kg"]};
    // The range where each unit reads naturally - those come first in the results.
    var NATURAL = {tsp: [0.125, 3], tbsp: [1, 4.01], cup: [0.25, 8], floz: [1, 16], pint: [1, 4], quart: [1, 4], gallon: [1, 1e9],
                   ml: [5, 1000], l: [1, 1e9], oz: [0.25, 16], lb: [1, 1e9], g: [1, 1000], kg: [1, 1e9]};
    var ALIASES = {
        tsp: ["tsp", "t", "teaspoon", "teaspoons", "tsps"], tbsp: ["tbsp", "tbs", "tbl", "tablespoon", "tablespoons", "tbsps"],
        cup: ["cup", "cups", "c"], floz: ["fl oz", "floz", "fl. oz", "fluid ounce", "fluid ounces"],
        pint: ["pint", "pints", "pt"], quart: ["quart", "quarts", "qt"], gallon: ["gallon", "gallons", "gal"],
        ml: ["ml", "milliliter", "milliliters", "millilitre", "millilitres"], l: ["l", "liter", "liters", "litre", "litres"],
        oz: ["oz", "ounce", "ounces"], lb: ["lb", "lbs", "pound", "pounds"], g: ["g", "gram", "grams", "gr"],
        kg: ["kg", "kgs", "kilogram", "kilograms"]
    };
    var SPELLING = {};
    Object.keys(ALIASES).forEach(function (key) { ALIASES[key].forEach(function (s) { SPELLING[s] = key; }); });
    SPELLING.T = "tbsp";                                     // "1 T" is a tablespoon; "1 t" a teaspoon

    var UNICODE = {"½": "1/2", "⅓": "1/3", "⅔": "2/3", "¼": "1/4", "¾": "3/4", "⅕": "1/5", "⅖": "2/5", "⅗": "3/5", "⅘": "4/5",
                   "⅙": "1/6", "⅚": "5/6", "⅛": "1/8", "⅜": "3/8", "⅝": "5/8", "⅞": "7/8"};
    // Friendly fractions: eighths and thirds.
    var FRACTIONS = [[0, ""], [1 / 8, "1/8"], [1 / 4, "1/4"], [1 / 3, "1/3"], [3 / 8, "3/8"], [1 / 2, "1/2"], [5 / 8, "5/8"],
                     [2 / 3, "2/3"], [3 / 4, "3/4"], [7 / 8, "7/8"], [1, ""]];

    function parseAmount(text) {
        if (typeof text === "number") return isFinite(text) && text >= 0 ? text : null;
        var s = String(text == null ? "" : text).trim();
        Object.keys(UNICODE).forEach(function (u) { s = s.split(u).join(" " + UNICODE[u]); });
        s = s.replace(/(\d)\s*-\s*(\d+\/\d+)/, "$1 $2").replace(/\s+/g, " ").trim();          // "1-1/2" -> "1 1/2"
        var m = s.match(/^(\d+)\s+(\d+)\/(\d+)$/);
        if (m) return +m[3] ? +m[1] + m[2] / m[3] : null;
        m = s.match(/^(\d+)\/(\d+)$/);
        if (m) return +m[2] ? m[1] / m[2] : null;
        m = s.match(/^(\d+(?:\.\d+)?|\.\d+)$/);
        return m ? parseFloat(m[1]) : null;
    }

    function unitKey(text) {
        var raw = String(text == null ? "" : text).trim().replace(/\.$/, "");
        if (raw === "T") return "tbsp";
        var s = raw.toLowerCase().replace(/\./g, "").replace(/\s+/g, " ");
        if (UNITS[s]) return s;
        return SPELLING[s] || null;
    }

    function unitType(key) { return UNITS[key] ? UNITS[key].type : null; }

    function convert(value, from, to) {
        if (!UNITS[from] || !UNITS[to] || UNITS[from].type !== UNITS[to].type) return null;
        if (UNITS[from].in && UNITS[to].in) return value * UNITS[from].in / UNITS[to].in;       // exact
        return value * UNITS[from].size / UNITS[to].size;
    }

    function scale(value, factor) {
        var f = typeof factor === "number" ? factor : parseAmount(factor);
        return value == null || f == null ? null : value * f;
    }

    function friendly(value) {
        if (value == null || !isFinite(value)) return null;
        if (value > 0 && value < 1 / 16) return {text: "< 1/8", approx: true, value: value};
        var whole = Math.floor(value), part = value - whole, best = FRACTIONS[0];
        FRACTIONS.forEach(function (f) { if (Math.abs(part - f[0]) < Math.abs(part - best[0])) best = f; });
        var w = whole + (best[0] === 1 ? 1 : 0), frac = best[0] === 1 ? "" : best[1];
        var rounded = w + (best[0] === 1 ? 0 : best[0]);
        var text = w && frac ? w + " " + frac : (w ? String(w) : (frac || "0"));
        return {text: text, approx: Math.abs(rounded - value) > 0.005 * Math.max(1, value), value: rounded};
    }

    function trim(n, places) {
        return String(parseFloat(n.toFixed(places)));
    }

    function metric(value, key) {
        if (value == null || !isFinite(value)) return null;
        if (key === "ml" || key === "g") return value >= 10 ? String(Math.round(value)) : trim(value, 1);
        return value >= 10 ? trim(value, 1) : trim(value, value >= 1 ? 1 : 2);                        // L, kg
    }

    // One unit's amount, as shown: {key, label, text, approx}.
    function show(value, key) {
        var unit = UNITS[key];
        if (unit.kitchen) {
            var f = friendly(value);
            var label = unit.plural && f.value > 1 ? unit.plural : unit.label;
            return {key: key, label: label, text: f.text, approx: f.approx, value: value};
        }
        var m = metric(value, key);
        return {key: key, label: unit.label, text: m, approx: Math.abs(parseFloat(m) - value) > 0.005 * Math.max(1, value), value: value};
    }

    function natural(value, key) {
        var r = NATURAL[key];
        return value >= r[0] && value < r[1];
    }

    // Every unit of the amount's type, the ones that read naturally first (kitchen units, then metric).
    function results(value, from) {
        var type = unitType(from);
        if (!type || value == null) return [];
        var rows = ORDER[type].map(function (key, i) { return {row: show(convert(value, from, key), key), i: i}; });
        rows.sort(function (a, b) {
            var na = natural(a.row.value, a.row.key) ? 0 : 1, nb = natural(b.row.value, b.row.key) ? 0 : 1;
            return na - nb || a.i - b.i;
        });
        return rows.map(function (r) { return r.row; });
    }

    // How a cook would measure it: cups (whole, then 3/4, 2/3, 1/2, 1/3, 1/4) + whole tbsp + tsp (to 1/8);
    // pounds + ounces for weight.
    var CUP_PARTS = [[3 / 4, "3/4"], [2 / 3, "2/3"], [1 / 2, "1/2"], [1 / 3, "1/3"], [1 / 4, "1/4"]];
    function practical(value, from) {
        var type = unitType(from), eps = 1e-6;
        if (!type || value == null || value <= 0) return "";
        if (type === "weight") {
            var oz = convert(value, from, "oz"), lbs = Math.floor(oz / 16 + eps), rest = friendly(oz - lbs * 16);
            var parts = [];
            if (lbs) parts.push(lbs + " lb");
            if (rest.value > 0) parts.push(rest.text + " oz");
            return parts.join(" + ") || "0 oz";
        }
        var cups = convert(value, from, "cup"), whole = Math.floor(cups + eps), left = cups - whole;
        // Try each cup measure that fits (and none); keep the most accurate, then the fewest pieces - a third of a
        // cup only when nothing is left over (1/4 cup + 2 tbsp reads better than 1/3 cup + 2 tsp).
        var best = null;
        [null].concat(CUP_PARTS).filter(function (p) {                  // what's left must be under 1/4 cup (spoons)
            var used = p ? p[0] : 0;
            return used <= left + eps && left - used < 0.25 - eps;
        }).forEach(function (piece) {
            var rest = spoons(left - (piece ? piece[0] : 0)), pieces = rest.parts.length + (piece ? 1 : 0);
            var score = [rest.error > 0.005 ? 1 : 0, pieces + (piece && piece[0] % 0.25 > eps && rest.parts.length ? 0.5 : 0), rest.error];
            if (!best || score[0] < best.score[0] || (score[0] === best.score[0] && (score[1] < best.score[1] ||
                    (score[1] === best.score[1] && score[2] < best.score[2])))) best = {piece: piece, rest: rest, score: score};
        });
        var out = [], piece = best.piece;
        if (whole || piece) out.push((whole ? whole + (piece ? " " + piece[1] : "") : piece[1]) + (whole > 1 || (whole === 1 && piece) ? " cups" : " cup"));
        out = out.concat(best.rest.parts);
        return out.join(" + ") || "0 tsp";
    }

    // Cups (under 1/4) -> {parts: ["2 tbsp", "1/2 tsp"], error (in tsp)}: whole tablespoons, then teaspoons to 1/8.
    function spoons(cups) {
        var eps = 1e-6, tbsp = Math.max(0, cups) * 16, whole = Math.floor(tbsp + eps), tsp = (tbsp - whole) * 3;
        var f = tsp < 0.02 ? {value: 0, text: "0"} : friendly(tsp), parts = [];        // crumbs of rounding: nothing
        if (f.value >= 3 - eps) { whole += 1; f = {value: 0, text: "0"}; }
        if (whole) parts.push(whole + " tbsp");
        if (f.value > eps || f.text === "< 1/8") parts.push(f.text + " tsp");
        return {parts: parts, error: Math.abs(whole * 3 + (f.value || 0) - tbsp * 3)};
    }

    function fToC(f) { return (f - 32) * 5 / 9; }
    function cToF(c) { return c * 9 / 5 + 32; }
    var OVEN_PRESETS = [325, 350, 375, 400, 425];

    // Approximate grams per US cup. Varies by brand and how it's packed.
    var INGREDIENTS = [
        {key: "ap_flour", label: "All-purpose flour", grams_per_cup: 120, words: ["all purpose flour", "all-purpose flour", "ap flour", "plain flour", "flour"]},
        {key: "bread_flour", label: "Bread flour", grams_per_cup: 127, words: ["bread flour"]},
        {key: "sugar", label: "Granulated sugar", grams_per_cup: 200, words: ["granulated sugar", "white sugar", "caster sugar", "sugar"]},
        {key: "brown_sugar", label: "Brown sugar (packed)", grams_per_cup: 213, words: ["brown sugar", "light brown sugar", "dark brown sugar"]},
        {key: "powdered_sugar", label: "Powdered sugar", grams_per_cup: 120, words: ["powdered sugar", "confectioners sugar", "confectioners' sugar", "icing sugar"]},
        {key: "butter", label: "Butter", grams_per_cup: 227, words: ["butter", "unsalted butter", "salted butter"]},
        {key: "rice", label: "Rice (uncooked)", grams_per_cup: 185, words: ["rice", "white rice", "long grain rice", "jasmine rice", "basmati rice"]},
        {key: "oats", label: "Rolled oats", grams_per_cup: 90, words: ["rolled oats", "old fashioned oats", "oats", "oatmeal"]},
        {key: "honey", label: "Honey", grams_per_cup: 340, words: ["honey"]},
        {key: "milk", label: "Milk", grams_per_cup: 245, words: ["milk", "whole milk", "skim milk", "2% milk"]},
        {key: "water", label: "Water", grams_per_cup: 237, words: ["water", "warm water", "cold water"]},
        {key: "oil", label: "Vegetable oil", grams_per_cup: 218, words: ["vegetable oil", "canola oil", "oil", "olive oil"]},
        {key: "cocoa", label: "Cocoa powder", grams_per_cup: 85, words: ["cocoa powder", "cocoa", "unsweetened cocoa"]},
        {key: "chocolate_chips", label: "Chocolate chips", grams_per_cup: 170, words: ["chocolate chips", "chocolate chip", "chips"]},
        {key: "cheese", label: "Shredded cheese", grams_per_cup: 113, words: ["shredded cheese", "shredded cheddar", "shredded mozzarella", "cheese"]}
    ];
    var BY_KEY = {};
    INGREDIENTS.forEach(function (i) { BY_KEY[i.key] = i; });

    // An ingredient line's name -> the closest ingredient key (the longest phrase found as whole words), or "".
    function matchIngredient(name) {
        var text = " " + String(name || "").toLowerCase().replace(/[^a-z0-9%' -]+/g, " ").replace(/\s+/g, " ") + " ";
        var best = "", length = 0;
        INGREDIENTS.forEach(function (i) {
            i.words.forEach(function (w) {
                if (text.indexOf(" " + w + " ") >= 0 && w.length > length) { best = i.key; length = w.length; }
            });
        });
        return best;
    }

    // Volume <-> weight for an ingredient: {type: the other type, grams | ml} or null.
    function crossConvert(value, from, ingredient) {
        var item = BY_KEY[ingredient], type = unitType(from);
        if (!item || value == null || !type) return null;
        var perMl = item.grams_per_cup / UNITS.cup.size;
        if (type === "volume") return {type: "weight", unit: "g", value: convert(value, from, "ml") * perMl};
        return {type: "volume", unit: "ml", value: convert(value, from, "g") / perMl};
    }

    var api = {UNITS: UNITS, ORDER: ORDER, OVEN_PRESETS: OVEN_PRESETS, INGREDIENTS: INGREDIENTS, parseAmount: parseAmount,
               unitKey: unitKey, unitType: unitType, convert: convert, scale: scale, friendly: friendly, metric: metric,
               show: show, results: results, practical: practical, fToC: fToC, cToF: cToF, matchIngredient: matchIngredient,
               crossConvert: crossConvert};
    if (typeof module !== "undefined" && module.exports) module.exports = api;
    else root.MealsConvert = api;
})(typeof self !== "undefined" ? self : this);
