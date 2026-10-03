"""Jewelry valuation by metal content. Pure Python (no Django).

Gold/silver jewelry is valued as scrap: pure metal weight x spot x a payout rate
(set in settings). Listings without a stated weight can't be valued - they become
review leads instead. Designer pieces are flagged, since they can sell above scrap.
"""
import re

from .coins import FRACTION_GRAMS_RE, GRAMS_PER_TROY_OZ, GRAMS_RE, OZ_RE, ParsedItem, ParseResult

GRAMS_PER_DWT = 1.555174

GOLD_KARATS = {8: 0.333, 9: 0.375, 10: 0.417, 14: 0.583, 18: 0.750, 22: 0.916, 24: 0.999}
GOLD_STAMPS = {"417": 10, "585": 14, "750": 18, "916": 22}

# Look-alikes: little or no precious metal.
LOOKALIKE_PATTERNS = [
    r"gold[- ]?filled", r"\bgf\b", r"gold[- ]?plated", r"\bgp\b", r"\bge\b", r"\bhge\b", r"\brgp\b",
    r"vermeil", r"gold over", r"gold[- ]?tone", r"silver[- ]?tone", r"silver[- ]?plated?\b", r"silverplate",
    r"\bepns\b", r"nickel silver", r"german silver", r"alpaca silver", r"tibetan silver", r"costume",
    r"\bfaux\b", r"fashion jewelry", r"\bplated\b", r"\bfilled\b", r"\bcopy\b", r"replica",
]
# Gold that is only a surface layer: "14K Rose Gold Plate", "18k gold over sterling"...
GOLD_LAYER = r"gold\s+(?:plate|plated|plating|over|wash|electroplate|fill|filled|vermeil)\b|gold[- ]?plate\b"
# Base metals: any gold is just an accent, so metal value is negligible.
BASE_METALS = r"stainless|\bsteel\b|titanium|tungsten|\bbrass\b|bronze|\bcopper\b|base metal|\bpewter\b"
# Only part of the lot is precious metal - the stated weight overstates it.
PARTIAL = r"\bsome\b|partial|not all|\bmostly\b|mixed metals?|assorted metals?"
# Heavier than these is almost always a misread ("Size 73.4g" = size 7, 3.4g) or not solid.
MAX_PLAUSIBLE_G = {r"\brings?\b": 25, r"earrings?|\bstuds?\b": 15, r"pendant|\bcharms?\b": 30,
                   r"bracelet|bangle|\bcuff\b": 150, r"necklace|\bchain\b|choker": 200}
JEWELRY_WORDS = (r"jewel(le)?ry|\brings?\b|necklace|pendant|bracelet|earrings?|brooch|\bcharms?\b"
                 r"|\bchains?\b|anklet|cuff ?links?|\bstuds?\b|\bband\b|locket|\bscrap\b|\bpins?\b|tie (?:bar|clip|tack)")
DESIGNERS = [
    "tiffany", "cartier", "david yurman", "james avery", "john hardy", "van cleef", "bulgari", "bvlgari",
    "pandora", "kendra scott", "chanel", "hermes", "georg jensen", "tacori", "judith ripka", "lagos",
]
# Pieces whose weight is mostly pearls/beads/stones, with gold only in the clasp or findings.
STONE_HEAVY = r"pearl (?:strand|necklace|bracelet)|\bstrands?\b|\bbeads?\b|\bbeaded\b|tennis bracelet|riviera"
STONE_WORDS = (r"diamond|sapphire|ruby|emerald|opal|pearl|garnet|amethyst|topaz|turquoise|\bstones?\b|\bgems?\b"
               r"|\bcz\b|kyanite|peridot|tourmaline|aquamarine|citrine|morganite|tanzanite|onyx|lapis|jade|coral"
               r"|larimar|agate|quartz|cabochon|moissanite|zircon|spinel|iolite|labradorite|malachite|carnelian"
               r"|moonstone|jasper|amber|cameo|enamel|station|porcelain|ceramic|glass|crystal|shell"
               r"|mother of pearl|cloisonne|mosaic|intaglio|resin|\bwood\b|\bbone\b|\bhorn\b")


def _karat(text: str) -> tuple[int | None, bool]:
    """Return (lowest gold karat found, whether several different karats were listed)."""
    found = {int(k) for k in re.findall(r"\b(8|9|10|14|18|22|24)\s?(?:k|kt|karat|carat)\b", text)}
    found |= {GOLD_STAMPS[s] for s in re.findall(r"\b(417|585|750|916)\b", text)}
    if not found:
        return None, False
    return min(found), len(found) > 1


def _weight_grams(text: str) -> float | None:
    dwt = re.search(r"(?<![\d.])(\d+(?:\.\d+)?|\.\d+)\s*(?:dwt|pennyweight)", text)
    if dwt:
        return float(dwt.group(1)) * GRAMS_PER_DWT
    oz = OZ_RE.search(text)
    frac_g = FRACTION_GRAMS_RE.search(text)
    if frac_g and int(frac_g.group(2)):
        return int(frac_g.group(1)) / int(frac_g.group(2))
    grams = GRAMS_RE.search(text)
    if grams:
        return float(grams.group(1))
    if oz:
        return float(oz.group(1)) * GRAMS_PER_TROY_OZ
    return None


def parse_jewelry_text(title: str, description: str = "") -> ParseResult:
    text = f"{title} {description}".lower()
    result = ParseResult()

    if not re.search(JEWELRY_WORDS, text):
        result.excluded_reason = "not a jewelry listing"
        return result
    is_silver = bool(re.search(r"sterling|\b925\b|\.925", text))
    gold_layer = bool(re.search(GOLD_LAYER, text))
    if re.search(BASE_METALS, text):
        result.excluded_reason = "base metal (stainless/brass/etc.) - gold is only an accent"
        return result
    if gold_layer and not is_silver:
        result.excluded_reason = "gold plated/filled over base metal"
        return result
    for pat in LOOKALIKE_PATTERNS:
        # plating over sterling is still sterling ("925 rhodium plated", "gold plate over 925")
        if re.search(pat, text) and not (is_silver and re.search(r"plat|vermeil|over", pat)):
            result.excluded_reason = f"plated/filled/costume: {pat}"
            return result

    karat, mixed = _karat(text)
    notes, confidence = [], "high"
    if is_silver and (karat is not None or gold_layer):
        # sterling piece with gold plating or small gold accents: value the silver only
        karat, mixed = None, False
        notes.append("sterling with gold accents/plating - valued as sterling only")
        confidence = "medium"
    if karat is None and not is_silver:
        result.excluded_reason = "no gold karat or sterling stated"
        return result

    designer = next((d for d in DESIGNERS if d in text), None)
    if designer:
        result.flags.append(f"designer:{designer}")  # may sell far above scrap - price by hand

    grams = _weight_grams(text)
    if grams is None:
        result.flags.append("no_weight")
        result.needs_review = True
        result.review_reason = f"{karat}k gold, no weight stated" if karat else "sterling, no weight stated"
        return result

    for pat, cap in MAX_PLAUSIBLE_G.items():
        if re.search(pat, text) and grams > cap:
            result.flags.append("weight_implausible")
            notes.append(f"{grams:g}g is implausible for this item - check listing")
            confidence = "low"
            break
    if re.search(STONE_HEAVY, text):
        result.flags.append("mostly_stones")
        notes.append("weight is mostly pearls/beads/stones - gold content unknown")
        confidence = "low"
    if re.search(PARTIAL, text):
        notes.append("only part of the lot may be precious metal")
        confidence = "low"
    # Stated weights almost always include the stones. Small diamond/CZ accents are fine;
    # anything else (colored stones, pearls, jade...) can be most of the weight.
    colored = re.sub(r"diamonds?|\bcz\b|cubic zirconia|accents?", "", text)
    if re.search(STONE_WORDS, colored) or (
            re.search(STONE_WORDS, text) and re.search(r"bracelet|bangle|necklace|choker|\bstrand", text)):
        if "mostly_stones" not in result.flags:
            result.flags.append("mostly_stones")
        notes.append("stones may be much of the stated weight - check by hand")
        confidence = "low"
    if re.search(STONE_WORDS, text):
        grams *= 0.9  # stated weight usually includes stones
        notes.append("has stones - weight reduced 10%")
        confidence = "medium" if confidence == "high" else confidence
    if mixed:
        notes.append(f"mixed karats - valued at lowest ({karat}k)")
        confidence = "medium" if confidence == "high" else confidence

    if karat is not None:
        purity, metal, key = GOLD_KARATS[karat], "gold", f"jewelry_gold_{karat}k"
    else:
        purity, metal, key = 0.925, "silver", "jewelry_sterling"
    oz = grams * purity / GRAMS_PER_TROY_OZ
    notes.insert(0, f"{grams:.2f}g at {purity:.1%}")
    result.items = [ParsedItem(key, metal, round(oz, 4), 1, "; ".join(notes))]
    result.confidence = confidence
    if designer:
        result.needs_review = True
        result.review_reason = f"designer ({designer}) - may be worth more than scrap"
    return result