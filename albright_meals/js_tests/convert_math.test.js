// The Meals converter's math (static/meals/convert_math.js). Run: node --test albright_meals/js_tests/
// (Node 18+). Uses only node:test and node:assert, so the same file also runs in a browser with a tiny shim.
const test = require("node:test");
const assert = require("node:assert/strict");
const C = require("../static/meals/convert_math.js");

const near = (actual, expected, tolerance = 0.01) =>
    assert.ok(Math.abs(actual - expected) <= tolerance, `${actual} is not about ${expected}`);
const text = (rows, key) => rows.find((r) => r.key === key);

test("fractions in: unicode, mixed numbers, decimals", () => {
    assert.equal(C.parseAmount("1/3"), 1 / 3);
    assert.equal(C.parseAmount("1 1/2"), 1.5);
    assert.equal(C.parseAmount("1-1/2"), 1.5);
    assert.equal(C.parseAmount("½"), 0.5);
    assert.equal(C.parseAmount("1½"), 1.5);
    assert.equal(C.parseAmount("2 ¾"), 2.75);
    assert.equal(C.parseAmount(".5"), 0.5);
    assert.equal(C.parseAmount("3"), 3);
    assert.equal(C.parseAmount("1/0"), null);
    assert.equal(C.parseAmount("a cup"), null);
    assert.equal(C.parseAmount(""), null);
});

test("fractions out: nearest 1/8 or 1/3, flagged when rounded", () => {
    assert.deepEqual([C.friendly(16 / 3).text, C.friendly(16 / 3).approx], ["5 1/3", false]);
    assert.deepEqual([C.friendly(0.375).text, C.friendly(0.375).approx], ["3/8", false]);
    assert.deepEqual([C.friendly(2.5).text, C.friendly(2.5).approx], ["2 1/2", false]);
    assert.deepEqual([C.friendly(0.3).text, C.friendly(0.3).approx], ["1/3", true]);
    assert.equal(C.friendly(0.97).text, "1");
    assert.equal(C.friendly(2).text, "2");
    assert.equal(C.friendly(0.02).text, "< 1/8");
});

test("metric decimals", () => {
    assert.equal(C.metric(78.06, "ml"), "78");
    assert.equal(C.metric(4.93, "ml"), "4.9");
    assert.equal(C.metric(1.18, "l"), "1.2");
    assert.equal(C.metric(0.237, "l"), "0.24");
    assert.equal(C.metric(453.6, "g"), "454");
});

test("unit names and conversions", () => {
    assert.equal(C.unitKey("Tbsp."), "tbsp");
    assert.equal(C.unitKey("T"), "tbsp");
    assert.equal(C.unitKey("t"), "tsp");
    assert.equal(C.unitKey("cups"), "cup");
    assert.equal(C.unitKey("fl oz"), "floz");
    assert.equal(C.unitKey("lbs"), "lb");
    assert.equal(C.unitKey("can"), null);
    assert.equal(C.convert(1, "cup", "tbsp"), 16);
    near(C.convert(1, "tbsp", "tsp"), 3);
    near(C.convert(1, "gallon", "cup"), 16);
    near(C.convert(1, "quart", "pint"), 2);
    near(C.convert(1, "cup", "ml"), 236.588);
    near(C.convert(1, "l", "cup"), 4.2268);
    near(C.convert(1, "lb", "oz"), 16);
    near(C.convert(1, "kg", "lb"), 2.2046);
    assert.equal(C.convert(1, "cup", "g"), null);                   // different types
});

test("results: every unit of the type, the practical ones first", () => {
    const rows = C.results(1 / 3, "cup");
    assert.equal(rows.length, 9);
    assert.equal(rows[0].key, "cup");
    assert.equal(text(rows, "tbsp").text, "5 1/3");
    assert.equal(text(rows, "ml").text, "79");
    const small = C.results(2, "tsp");
    assert.ok(small.findIndex((r) => r.key === "tsp") < small.findIndex((r) => r.key === "cup"));
    assert.equal(C.results(500, "g")[0].key, "lb");                 // 1.1 lb reads better than 17.6 oz
    assert.equal(C.results(1, "can").length, 0);
});

test("practical: how a cook would measure it", () => {
    assert.equal(C.practical(0.375, "cup"), "1/4 cup + 2 tbsp");
    assert.equal(C.practical(1 / 3, "cup"), "1/3 cup");
    assert.equal(C.practical(0.5, "cup"), "1/2 cup");
    assert.equal(C.practical(1.25, "cup"), "1 1/4 cups");
    assert.equal(C.practical(5, "tbsp"), "1/4 cup + 1 tbsp");
    assert.equal(C.practical(2, "tsp"), "2 tsp");
    assert.equal(C.practical(4, "tsp"), "1 tbsp + 1 tsp");
    assert.equal(C.practical(20, "oz"), "1 lb + 4 oz");
    assert.equal(C.practical(8, "oz"), "8 oz");
});

test("scaling a line", () => {
    assert.equal(C.scale(1.5, 2), 3);
    assert.equal(C.scale(3, "1/2"), 1.5);
    assert.equal(C.practical(C.scale(0.75, 0.5), "cup"), "1/4 cup + 2 tbsp");
    assert.equal(C.scale(1, "x"), null);
});

test("temperature and oven presets", () => {
    near(C.fToC(350), 176.67);
    near(C.cToF(180), 356);
    assert.equal(Math.round(C.fToC(212)), 100);
    assert.deepEqual(C.OVEN_PRESETS, [325, 350, 375, 400, 425]);
});

test("approximate ingredient weights", () => {
    assert.equal(C.matchIngredient("2 cups all-purpose flour, sifted"), "ap_flour");
    assert.equal(C.matchIngredient("bread flour"), "bread_flour");
    assert.equal(C.matchIngredient("packed light brown sugar"), "brown_sugar");
    assert.equal(C.matchIngredient("unsalted butter, softened"), "butter");
    assert.equal(C.matchIngredient("chicken thighs"), "");
    near(C.crossConvert(1, "cup", "ap_flour").value, 120, 0.5);
    near(C.crossConvert(227, "g", "butter").value, 236.588, 0.5);
    assert.equal(C.crossConvert(1, "cup", "unknown"), null);
});
