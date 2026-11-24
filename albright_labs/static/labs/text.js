// Counts in words, one way everywhere (templates/index.html loads it before every app's script):
// plural(17, "item") -> "17 items", plural(1, "item") -> "1 item", plural(2, "box", "boxes") -> "2 boxes".
// The same as albright_labs.text.plural and the {{ n|plural:"item" }} template filter.
(function () {
    window.plural = function (n, word, many) {
        return n + " " + (Number(n) === 1 ? word : (many || word + "s"));
    };
})();
