"""Coin and bullion identification from lot text. Pure Python (no Django).

Strategy: the reference table below decides metal content. Text parsing (and
the optional LLM) only decides WHICH coin and HOW MANY. We never let a model
invent a weight for a known coin type.
"""
import re
from dataclasses import dataclass, field

GRAMS_PER_TROY_OZ = 31.1035
JUNK_SILVER_OZ_PER_DOLLAR_FACE = 0.715  # circulated 90% silver, industry convention


@dataclass(frozen=True)
class CoinType:
    key: str
    metal: str
    oz: float                      # troy oz of pure metal per coin
    patterns: tuple                # regexes, matched against lowercased text
    years: tuple | None = None     # (min, max): this entry applies only to these years
    numismatic: bool = False       # often worth more than melt: flag for manual review
    minted: tuple | None = None    # ((start, end), ...) years the coin exists; others = fake
    gross_g: float | None = None   # total coin weight: cross-checks quantity and authenticity
    note: str = ""                 # caveat attached to every match
    fractional: bool = False       # also made in fractional sizes - size must be stated


P5 = (r"\$5 gold", r"half eagle", r"\$5 (liberty|indian) gold")
P10 = (r"\$10 gold", r"\$10 (liberty|indian) gold")

# Order matters: more specific entries first. Entries sharing patterns[0] are one "family"
# (e.g. classic vs modern $5 gold); the year picks which one applies.
COIN_TYPES = [
    # Gold
    CoinType("double_eagle_20", "gold", 0.9675, (r"double eagle", r"\$20 (liberty|saint|st\.? gaudens)"),
             numismatic=True, gross_g=33.44),
    CoinType("eagle_10_gold", "gold", 0.48375, P10, years=(1795, 1933), numismatic=True, gross_g=16.72),
    CoinType("modern_10_gold", "gold", 0.25, P10, years=(1984, 2099),
             note="modern $10 gold: 1/4 oz Eagle or 0.48 oz commemorative - valued at 1/4 oz"),
    CoinType("half_eagle_5_gold", "gold", 0.24187, P5, years=(1795, 1933), numismatic=True, gross_g=8.36),
    CoinType("modern_5_gold", "gold", 0.1, P5, years=(1984, 2099),
             note="modern $5 gold: 1/10 oz Eagle or 0.24 oz commemorative - valued at 1/10 oz"),
    CoinType("quarter_eagle_gold", "gold", 0.12094, (r"\$2(\.| 1/)?5 gold", r"quarter eagle"),
             numismatic=True, gross_g=4.18),
    CoinType("krugerrand", "gold", 1.0, (r"krugerrand",), fractional=True),
    CoinType("gold_buffalo", "gold", 1.0, (r"gold buffalo",), gross_g=31.1),
    CoinType("gold_eagle_1oz", "gold", 1.0, (r"gold eagle",), fractional=True),
    # Silver dollars and bullion
    CoinType("morgan_dollar", "silver", 0.7734, (r"\bmorgan\b",), numismatic=True, gross_g=26.73,
             minted=((1878, 1904), (1921, 1921), (2021, 2099))),
    CoinType("peace_dollar", "silver", 0.7734, (r"\bpeace dollar",), numismatic=True, gross_g=26.73,
             minted=((1921, 1928), (1934, 1935), (2021, 2099))),
    CoinType("silver_eagle", "silver", 1.0, (r"silver eagle",), gross_g=31.1),
    CoinType("silver_maple", "silver", 1.0, (r"silver maple",), gross_g=31.1),
    CoinType("eisenhower_40", "silver", 0.3161, (r"(eisenhower|\bike\b).{0,30}(40%|silver)",), gross_g=24.59),
    # Halves
    CoinType("kennedy_90", "silver", 0.3617, (r"kennedy",), years=(1964, 1964), gross_g=12.5),
    CoinType("kennedy_40", "silver", 0.1479, (r"kennedy",), years=(1965, 1970), gross_g=11.5),
    CoinType("walking_liberty_half", "silver", 0.3617, (r"walking liberty",), gross_g=12.5),
    CoinType("franklin_half", "silver", 0.3617, (r"franklin half",), gross_g=12.5),
    CoinType("barber_half", "silver", 0.3617, (r"barber half",), gross_g=12.5),
    # Quarters
    CoinType("washington_quarter_90", "silver", 0.1808, (r"washington quarter",), years=(1932, 1964), gross_g=6.25),
    CoinType("standing_liberty_quarter", "silver", 0.1808, (r"standing liberty",), gross_g=6.25),
    CoinType("barber_quarter", "silver", 0.1808, (r"barber quarter",), gross_g=6.25),
    # Dimes and nickels
    CoinType("mercury_dime", "silver", 0.07234, (r"mercury dime",), gross_g=2.5),
    CoinType("roosevelt_dime_90", "silver", 0.07234, (r"roosevelt dime",), years=(1946, 1964), gross_g=2.5),
    CoinType("barber_dime", "silver", 0.07234, (r"barber dime",), gross_g=2.5),
    CoinType("war_nickel", "silver", 0.05626, (r"war ?time nickel", r"war nickel"), years=(1942, 1945), gross_g=5.0),
]
COIN_TYPES_BY_KEY = {c.key: c for c in COIN_TYPES}

EXCLUDE_PATTERNS = [
    r"\bcopy\b", r"replica", r"reproduction", r"tribute", r"\btoken\b", r"novelty",
    r"fantasy", r"plated", r"layered", r"gold tone", r"silver tone", r"\bclad\b",
    r"colorized", r"\bmedallion\b",
]
# Jewelry is valued differently (karat, resale as jewelry) - a future module, not melt-as-coin.
# Also common non-coin goods that share coin words ("Silver Eagle" shirts, microphones...).
NOT_COIN_PATTERNS = [
    r"earrings?", r"\brings?\b", r"necklace", r"pendant", r"bracelet", r"brooch", r"\bcharms?\b",
    r"\bstuds?\b", r"jewel(le)?ry", r"cuff ?links?", r"anklet", r"\bchain\b",
    r"shirt", r"hoodie", r"jacket", r"\bhat\b", r"microphone", r"camera", r"figurine", r"statue",
    r"belt buckle", r"\bpatch\b", r"poster", r"\bmug\b", r"knife", r"lighter", r"\bwatch\b",
]
# Real coin/bullion listings almost always use at least one of these words.
COIN_CONTEXT = (r"\bcoins?\b|dollars?\b|\bhalf\b|halves|quarters?\b|\bdimes?\b|nickels?\b|\bcents?\b"
                r"|\boz\b|ounces?|troy|bullion|\bbu\b|\bunc\b|uncirculated|\bproof\b|\bmint\b"
                r"|pcgs|ngc|anacs|\d+%|\bbars?\b|\brounds?\b|\bgrams?\b|\d\s?g\b|\bface\b|\$\d"
                r"|krugerrand|silver maple|(silver|gold) eagles\b|american (silver|gold) eagle")
NUMISMATIC_FLAGS = {
    "graded": r"\b(pcgs|ngc|anacs|icg)\b",
    "proof": r"\bproof\b",
    "carson_city": r"\bcc\b|carson city",
    "key_date": r"key date",
}
# Famous key dates. Cheap listings of these are usually fakes or misreads - verify by hand.
KEY_DATES = {
    "standing_liberty_quarter": {1916}, "mercury_dime": {1916}, "morgan_dollar": {1893, 1895},
    "peace_dollar": {1928}, "walking_liberty_half": {1921}, "barber_dime": {1894},
    "barber_quarter": {1896, 1901, 1913}, "washington_quarter_90": {1932},
}
FINENESS_NUMBERS = {400, 500, 800, 900, 925, 999}  # "900 Silver" is purity, not a quantity
WORD_QTY = {r"\bpair\b": 2, r"\btrio\b": 3}
QTY_PATTERNS = [
    r"\b(?:lot|set|group|bag|roll|bundle) of (\d{1,3})\b",
    r"\((\d{1,3})\)",
    r"\bqty:?\s*(\d{1,3})\b",
    r"\b(\d{1,3})\s*(?:x|pcs|pieces|coins)\b",
    r"\b(\d{1,3})\s+(?:x\s+)?(?:vintage\s+|old\s+|antique\s+|circulated\s+)?(?:silver\s+)?"
    r"(?:morgans?|peace|walking|franklins?|kennedys?|mercury|barber|standing|eagles|dollars|halves"
    r"|half dollars|quarters|dimes|nickels|rounds|bars)\b",
]
YEAR_RE = re.compile(r"\b(17[89]\d|18\d\d|19\d\d|20[0-2]\d)\b")
GRAMS_RE = re.compile(r"(?<![\d.])(\d+(?:\.\d+)?|\.\d+)\s*(?:g|grams?)\b")
FRACTION_RE = re.compile(r"\b1/(2|4|10|20)\s*(?:troy\s*)?oz")
ONE_OZ_RE = re.compile(r"\b1\s*(?:troy\s*)?oz\b|one (?:troy )?ounce")


@dataclass
class ParsedItem:
    coin_key: str            # a COIN_TYPES key, or generic_silver / generic_gold / junk_silver_face
    metal: str
    oz_each: float
    quantity: int
    note: str = ""


@dataclass
class ParseResult:
    items: list = field(default_factory=list)
    confidence: str = "low"          # high / medium / low
    flags: list = field(default_factory=list)
    excluded_reason: str = ""
    needs_llm: bool = False
    method: str = "regex"
    needs_review: bool = False       # worth a human look even without a max bid
    review_reason: str = ""

    def total_oz(self, metal: str) -> float:
        return round(sum(i.oz_each * i.quantity for i in self.items if i.metal == metal), 4)


def _add(note: str, extra: str) -> str:
    return f"{note}; {extra}" if note else extra


def _quantity(text: str) -> int | None:
    for pat, n in WORD_QTY.items():
        if re.search(pat, text):
            return n
    for pat in QTY_PATTERNS:
        for m in re.finditer(pat, text):
            q = int(m.group(1))
            if 1 <= q <= 200 and q not in FINENESS_NUMBERS:
                return q
    return None


def _generic_bullion(text: str) -> ParsedItem | None:
    metal = "gold" if "gold" in text else "silver" if "silver" in text else None
    if not metal or not re.search(r"\.999|\b999\b|fine (silver|gold)|bullion|\bbar\b|\bround\b", text):
        return None
    purity = 0.925 if (metal == "silver" and re.search(r"sterling|\b925\b", text)) else 1.0
    frac = FRACTION_RE.search(text)
    oz = re.search(r"\b(\d+(?:\.\d+)?)\s*(?:troy\s*)?(?:oz|ounces?)\b", text)
    grams = GRAMS_RE.search(text)
    if frac:
        weight = 1 / int(frac.group(1))
    elif oz:
        weight = float(oz.group(1))
    elif grams:
        weight = float(grams.group(1)) / GRAMS_PER_TROY_OZ
    else:
        return None
    note = "weight stated in listing" + (", sterling 92.5%" if purity < 1 else "")
    return ParsedItem(f"generic_{metal}", metal, round(weight * purity, 4), 1, note)


def _evaluate_type(ct: CoinType, note: str, text: str, years: list, qty, result: ParseResult) -> ParseResult:
    # No coin in the table was ever struck in sterling.
    if re.search(r"sterling|\.?\b925\b", text):
        result.excluded_reason = "claims sterling/925 - genuine coin is not sterling (likely fake)"
        return result
    if ct.minted and years and not any(a <= y <= b for y in years for a, b in ct.minted):
        result.excluded_reason = f"year {years[0]} was never minted for {ct.key} (likely fake)"
        return result

    oz_each = ct.oz
    confidence = "high" if qty else "medium"
    if note:
        confidence = "low" if note.startswith("year not stated") else "medium"

    if ct.fractional:
        frac = FRACTION_RE.search(text)
        if frac:
            oz_each = 1 / int(frac.group(1))
            note = _add(note, f"size 1/{frac.group(1)} oz")
        elif not ONE_OZ_RE.search(text):
            note = _add(note, "size not stated (fractional sizes exist)")
            confidence = "low"

    grams = GRAMS_RE.search(text)
    if grams and ct.gross_g:
        implied = float(grams.group(1)) / ct.gross_g
        n = round(implied)
        if n >= 1 and abs(implied - n) / n <= 0.04:
            if qty and qty != n:
                note = _add(note, f"text says {qty}, weight implies {n}")
                confidence = "medium" if confidence == "high" else confidence
            elif not qty:
                note = _add(note, f"quantity {n} from stated weight")
            qty = n
        else:
            result.flags.append("weight_mismatch")
            note = _add(note, "stated weight doesn't fit this coin (possible fake)")
            confidence = "low"

    if len(set(years)) == 1 and years[0] in KEY_DATES.get(ct.key, set()):
        result.flags.append("key_date_verify")
        note = _add(note, "famous key date - cheap ones are usually fakes")
        confidence = "low"

    result.items = [ParsedItem(ct.key, ct.metal, oz_each, qty or 1, note)]
    result.confidence = confidence
    return result


def parse_coin_text(title: str, description: str = "") -> ParseResult:
    text = f"{title} {description}".lower()
    result = ParseResult()

    for pat in EXCLUDE_PATTERNS:
        if re.search(pat, text):
            result.excluded_reason = f"matched exclusion: {pat}"
            return result
    for pat in NOT_COIN_PATTERNS:
        if re.search(pat, text):
            result.excluded_reason = f"not a coin/bullion listing: {pat}"
            return result
    if not re.search(COIN_CONTEXT, text):
        result.excluded_reason = "no coin/bullion words in listing"
        return result

    result.flags = [name for name, pat in NUMISMATIC_FLAGS.items() if re.search(pat, text)]
    years = [int(y) for y in YEAR_RE.findall(text)]
    qty = _quantity(text)

    # 1) "$X face 90%" junk silver
    face = re.search(r"\$\s?(\d+(?:\.\d{1,2})?)\s*(?:face|fv)\b", text)
    if face and "90%" in text:
        oz = float(face.group(1)) * JUNK_SILVER_OZ_PER_DOLLAR_FACE
        result.items = [ParsedItem("junk_silver_face", "silver", round(oz, 4), 1, f"${face.group(1)} face 90%")]
        result.confidence = "high"
        return result

    # 2) Known coin types (first matching entry per family; the year decides within a family)
    distinct = {}
    for ct in COIN_TYPES:
        if ct.patterns[0] in distinct or not any(re.search(p, text) for p in ct.patterns):
            continue
        if ct.years:
            in_range = [y for y in years if ct.years[0] <= y <= ct.years[1]]
            if years and not in_range:
                continue  # a year was stated and it's outside this entry's range
            stated_pct = re.search(r"\b(40|90)\s?%", text)
            if not in_range and stated_pct:
                # "40% silver" / "90% silver" decides between e.g. kennedy_40 and kennedy_90
                pct = 0.40 if stated_pct.group(1) == "40" else 0.90
                is_40 = ct.key.endswith("_40")
                if (pct == 0.40) != is_40:
                    continue
                note = ct.note
            else:
                note = ct.note if in_range else "year not stated - composition assumed"
        else:
            note = ct.note
        distinct[ct.patterns[0]] = (ct, note)
        if ct.numismatic and "numismatic_upside" not in result.flags:
            result.flags.append("numismatic_upside")

    if len(distinct) == 1:
        ct, note = next(iter(distinct.values()))
        return _evaluate_type(ct, note, text, years, qty, result)
    if len(distinct) > 1:
        result.needs_llm = True  # mixed lot - let the LLM split it
        return result

    # 3) Generic bars / rounds
    generic = _generic_bullion(text)
    if generic:
        generic.quantity = qty or 1
        result.items = [generic]
        result.confidence = "medium"
        return result

    # Mentions silver/gold coins but nothing we can pin down
    if re.search(r"\b(silver|gold)\b", text) and re.search(r"\bcoins?\b|dollar|half|quarter|dime", text):
        result.needs_llm = True
    return result


def estimate_resale(parse: ParseResult, spot: dict, multipliers: dict) -> tuple[float, float]:
    """Return (melt_value, expected_sale). expected_sale = melt x per-type multiplier."""
    melt = expected = 0.0
    for item in parse.items:
        price = spot.get(item.metal)
        if price is None:
            raise ValueError(f"No spot price for {item.metal}")
        item_melt = item.oz_each * item.quantity * price
        melt += item_melt
        # exact key first (e.g. "jewelry_gold_14k"), then its family ("jewelry_gold"), then default
        family = item.coin_key.rsplit("_", 1)[0]
        rate = multipliers.get(item.coin_key, multipliers.get(family, multipliers.get("default", 1.0)))
        expected += item_melt * rate
    return round(melt, 2), round(expected, 2)