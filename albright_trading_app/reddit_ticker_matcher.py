import re

# Tickers that double as common English words/abbreviations and are
# typed in all-caps by real people often enough that a bare uppercase
# match alone isn't safe. These only count when written as a cashtag
# ($ALL), never as a bare word. Extend this list as you spot noise in
# your results — it's the main lever for precision vs. recall here.
AMBIGUOUS_TICKER_BLOCKLIST = {
    "A", "ALL", "ON", "SO", "IT", "KEY", "NOW", "REAL", "ARE", "FOR",
    "ONE", "DAY", "GO", "LOW", "NEW", "OK", "CAT", "GOOD", "BEN",
    "MAN", "PLAY", "SEE", "WELL", "FAST", "TOO", "OR", "AI", "ANY",
}


def build_ticker_patterns(symbols):
    """
    Given a list of S&P 500 symbols, returns compiled regex patterns:
    - cashtag_pattern: matches $SYMBOL for ANY symbol (high precision,
      always trusted regardless of ambiguity).
    - bare_pattern: matches a standalone uppercase SYMBOL as its own
      word, excluding symbols on the ambiguous blocklist.
    """
    # Longest symbols first so overlapping prefixes don't shadow each
    # other (e.g. "GOOG" vs "GOOGL").
    sorted_symbols = sorted(set(symbols), key=len, reverse=True)
    escaped = [re.escape(s) for s in sorted_symbols]

    cashtag_pattern = re.compile(
        r"\$(" + "|".join(escaped) + r")\b"
    )

    bare_symbols = [s for s in sorted_symbols if s not in AMBIGUOUS_TICKER_BLOCKLIST]
    bare_escaped = [re.escape(s) for s in bare_symbols]
    # No re.IGNORECASE here on purpose — requiring exact uppercase
    # is itself a strong filter against casual lowercase word usage.
    bare_pattern = re.compile(
        r"(?<![A-Za-z0-9\$])(" + "|".join(bare_escaped) + r")(?![A-Za-z0-9])"
    )

    return cashtag_pattern, bare_pattern


def find_tickers(text, cashtag_pattern, bare_pattern):
    """
    Returns the set of unique symbols mentioned in a piece of text.
    Cashtag matches always count; bare matches only count for
    non-ambiguous symbols (handled by bare_pattern already excluding
    the blocklist).
    """
    if not text:
        return set()

    found = set(cashtag_pattern.findall(text))
    found.update(bare_pattern.findall(text))
    return found