"""Counts in words, one way everywhere: plural(17, "item") -> "17 items", plural(1, "item") -> "1 item",
plural(2, "box", "boxes") -> "2 boxes". Templates: {{ n|plural:"item" }} (or "box,boxes") - albright_labs.templatetags.counts,
loaded on every template (TEMPLATES builtins). JavaScript: window.plural(n, "item") - labs/text.js, on every page."""


def plural(n, word, many=None):
    try:
        one = int(n) == 1 and float(n) == 1
    except (TypeError, ValueError):
        one = False
    return f"{n} {word if one else (many or word + 's')}"
