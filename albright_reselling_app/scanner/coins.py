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
    years: tuple | None = None     # (min, max) inclusive if composition depends on year
    numismatic: bool = False       # often worth more than melt: flag for manual review
    minted: tuple | None = None    # ((start, end), ...) years this coin actually exists; others = fake


# Order matters: more specific entries first.
COIN_TYPES = [
    # Gold
    CoinType("double_eagle_20", "gold", 0.9675, (r"double eagle", r"\$20 (liberty|saint|st\.? gaudens)"), numismatic=True),
    CoinType("eagle_10_gold", "gold", 0.48375, (r"\$10 (liberty|indian) gold", r"\$10 gold"), numismatic=True),
    CoinType("half_eagle_5_gold", "gold", 0.24187, (r"\$5 (liberty|indian) gold", r"\$5 gold", r"half eagle"), numismatic=True),
    CoinType("quarter_eagle_gold", "gold", 0.12094, (r"\$2(\.| 1/)?5 gold", r"quarter eagle"), numismatic=True),
    CoinType("krugerrand", "gold", 1.0, (r"krugerrand",)),
    CoinType("gold_buffalo", "gold", 1.0, (r"gold buffalo",)),
    CoinType("gold_eagle_1oz", "gold", 1.0, (r"gold eagle",)),
    # Silver dollars and bullion
    CoinType("morgan_dollar", "silver", 0.7734, (r"\bmorgan\b",), numismatic=True,
             minted=((1878, 1904), (1921, 1921), (2021, 2099))),
    CoinType("peace_dollar", "silver", 0.7734, (r"\bpeace dollar",), numismatic=True,
             minted=((1921, 1928), (1934, 1935), (2021, 2099))),
    CoinType("silver_eagle", "silver", 1.0, (r"silver eagle",)),
    CoinType("silver_maple", "silver", 1.0, (r"silver maple",)),
    CoinType("eisenhower_40", "silver", 0.3161, (r"(eisenhower|\bike\b).{0,30}(40%|silver)",)),
    # 90% / 40% halves
    CoinType("kennedy_90", "silver", 0.3617, (r"kennedy",), years=(1964, 1964)),
    CoinType("kennedy_40", "silver", 0.1479, (r"kennedy",), years=(1965, 1970)),
    CoinType("walking_liberty_half", "silver", 0.3617, (r"walking liberty",)),
    CoinType("franklin_half", "silver", 0.3617, (r"franklin half",)),
    CoinType("barber_half", "silver", 0.3617, (r"barber half",)),
    # Quarters
    CoinType("washington_quarter_90", "silver", 0.1808, (r"washington quarter",), years=(1932, 1964)),
    CoinType("standing_liberty_quarter", "silver", 0.1808, (r"standing liberty",)),
    CoinType("barber_quarter", "silver", 0.1808, (r"barber quarter",)),
    # Dimes and nickels
    CoinType("mercury_dime", "silver", 0.07234, (r"mercury dime",)),
    CoinType("roosevelt_dime_90", "silver", 0.07234, (r"roosevelt dime",), years=(1946, 1964)),
    CoinType("barber_dime", "silver", 0.07234, (r"barber dime",)),
    CoinType("war_nickel", "silver", 0.05626, (r"war ?time nickel", r"war nickel"), years=(1942, 1945)),
]
COIN_TYPES_BY_KEY = {c.key: c for c in COIN_TYPES}

EXCLUDE_PATTERNS = [
    r"\bcopy\b", r"replica", r"reproduction", r"tribute", r"\btoken\b", r"novelty",
    r"fantasy", r"plated", r"layered", r"gold tone", r"silver tone", r"\bclad\b",
    r"colorized", r"\bmedallion\b",
]
NUMISMATIC_FLAGS = {
    "graded": r"\b(pcgs|ngc|anacs|icg)\b",
    "proof": r"\bproof\b",
    "carson_city": r"\bcc\b|carson city",
    "key_date": r"key date",
}
QTY_PATTERNS = [
    r"\b(?:lot|set|group|bag|roll) of (\d{1,3})\b",
    r"\b(\d{1,3})\s+(?:vintage\s+|old\s+|antique\s+)?(?:morgan|peace|silver|walking|franklin|kennedy|mercury|barber|standing)\b", r"\((\d{1,3})\)", r"\b(\d{1,3})\s*(?:x|pcs|pieces|coins)\b", r"\bqty:?\s*(\d{1,3})\b",
]
YEAR_RE = re.compile(r"\b(17[89]\d|18\d\d|19\d\d|20[0-2]\d)\b")


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

    def total_oz(self, metal: str) -> float:
        return round(sum(i.oz_each * i.quantity for i in self.items if i.metal == metal), 4)


def _quantity(text: str) -> int | None:
    for pat in QTY_PATTERNS:
        m = re.search(pat, text)
        if m:
            q = int(m.group(1))
            if 1 <= q <= 500:
                return q
    return None


def _generic_bullion(text: str) -> ParsedItem | None:
    metal = "gold" if "gold" in text else "silver" if "silver" in text else None
    if not metal or not re.search(r"\.999|\b999\b|fine (silver|gold)|bullion|\bbar\b|\bround\b", text):
        return None
    purity = 0.925 if (metal == "silver" and re.search(r"sterling|\b925\b", text)) else 1.0
    frac = re.search(r"\b1/(2|4|10|20)\s*(?:troy\s*)?oz", text)
    oz = re.search(r"\b(\d+(?:\.\d+)?)\s*(?:troy\s*)?(?:oz|ounces?)\b", text)
    grams = re.search(r"\b(\d+(?:\.\d+)?)\s*(?:g|grams?)\b", text)
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


def parse_coin_text(title: str, description: str = "") -> ParseResult:
    text = f"{title} {description}".lower()
    result = ParseResult()

    for pat in EXCLUDE_PATTERNS:
        if re.search(pat, text):
            result.excluded_reason = f"matched exclusion: {pat}"
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

    # 2) Known coin types
    matches = []
    for ct in COIN_TYPES:
        if not any(re.search(p, text) for p in ct.patterns):
            continue
        if ct.years:
            in_range = [y for y in years if ct.years[0] <= y <= ct.years[1]]
            if years and not in_range:
                continue  # a year was stated and it's outside the silver range
            note = "" if in_range else "year not stated - silver content assumed"
        else:
            note = ""
        matches.append((ct, note))
        if ct.numismatic and "numismatic_upside" not in result.flags:
            result.flags.append("numismatic_upside")

    # Keep the first match per family (e.g. kennedy_90 vs kennedy_40 when no year given)
    distinct = {}
    for ct, note in matches:
        family = ct.patterns[0]
        distinct.setdefault(family, (ct, note))

    if len(distinct) == 1:
        ct, note = next(iter(distinct.values()))
        # No US or bullion coin in the table was ever struck in sterling.
        if re.search(r"sterling|\.?\b925\b", text):
            result.excluded_reason = "claims sterling/925 - genuine coin is not sterling (likely fake)"
            return result
        if ct.minted and years and not any(a <= y <= b for y in years for a, b in ct.minted):
            result.excluded_reason = f"year {years[0]} was never minted for {ct.key} (likely fake)"
            return result
        result.items = [ParsedItem(ct.key, ct.metal, ct.oz, qty or 1, note)]
        result.confidence = "high" if (qty and not note) else "medium"
        if note:
            result.confidence = "low"
        return result
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
        expected += item_melt * multipliers.get(item.coin_key, multipliers.get("default", 1.0))
    return round(melt, 2), round(expected, 2)