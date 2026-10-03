"""Leads for categories without a free price source (video games, trading cards).

No max bid here - without price data we can't compute one honestly. Instead a lot
becomes a "lead" when it's clearly in a target category, shows at least one value
signal (bulk lot, sealed, graded, complete-in-box, vintage platform...), and the
current bid is still under a threshold you set. You price leads by hand.
"""
import re
from dataclasses import dataclass, field

EXCLUDE = [
    r"\brepro(duction)?s?\b", r"\breplica\b", r"\bfake\b", r"\bproxy\b", r"\bproxies\b", r"\bcustom\b",
    r"\breprint\b", r"digital code", r"\bcode only\b", r"\bempty box\b", r"\bbox only\b", r"\bcase only\b",
    r"board game", r"jigsaw|puzzle", r"\bplush\b", r"\bfigure\b", r"\bposter\b", r"\bcommemorative coins?\b",
    r"\bt-?shirt\b|hoodie|\bhat\b", r"gift card", r"greeting card", r"playing cards", r"tarot",
]

GAME_PLATFORMS = {
    # vintage/retro platforms carry most of the value in game lots
    "n64": r"\bn64\b|nintendo 64", "gamecube": r"game ?cube|\bngc\b", "snes": r"\bsnes\b|super nintendo",
    "nes": r"\bnes\b|nintendo entertainment system", "game boy": r"game ?boy|\bgba\b|\bgbc\b",
    "genesis": r"sega genesis|\bgenesis\b", "saturn": r"sega saturn", "dreamcast": r"dreamcast",
    "ps1": r"\bps1\b|playstation 1\b|psx", "ps2": r"\bps2\b|playstation 2", "psp": r"\bpsp\b",
    "ds": r"nintendo ds|\b3ds\b|\bds lite\b", "atari": r"\batari\b", "xbox": r"original xbox",
}
GAME_CONTEXT = r"\bgames?\b|\bconsole\b|\bcartridges?\b|\bcarts?\b|\bcib\b|\bsystem\b|\bhandheld\b"
GAME_SIGNALS = {
    "bulk lot": r"\blot\b|bundle|\bbox of\b|collection|\b\d{1,3}\s*(?:games|carts|cartridges)\b",
    "complete in box": r"\bcib\b|complete in box|with (?:box|manual)|box and manual",
    "sealed": r"\bsealed\b|\bnew in box\b|\bnib\b|\bwata\b|\bvga\b",
    "console": r"\bconsole\b|\bsystem\b|\bhandheld\b",
}

CARD_TYPES = {
    "pokemon": r"pok[eé]mon", "magic": r"magic the gathering|\bmtg\b", "yugioh": r"yu-?gi-?oh",
    "sports": r"baseball|basketball|football|hockey|soccer|\bnba\b|\bnfl\b|\bmlb\b|topps|panini|upper deck|fleer|donruss|bowman",
}
CARD_CONTEXT = r"\bcards?\b|\btcg\b|booster|\betb\b|elite trainer|\bholos?\b|graded|\bpsa\b|\bbgs\b|\bcgc\b|\bsgc\b|rookie|\brc\b"
CARD_SIGNALS = {
    "bulk lot": r"\blot\b|bundle|binder|\bbox of\b|collection|\b\d{2,5}\+?\s*cards\b|shoebox|album",
    "graded": r"\bpsa\s?\d|\bbgs\b|\bcgc\b|\bsgc\b|graded|\bslab",
    "sealed": r"\bsealed\b|booster (?:box|pack)|\betb\b|elite trainer|\bfactory set\b|\bhobby box\b|\bwax\b",
    "vintage": r"\bvintage\b|\bwotc\b|1st edition|first edition|shadowless|\bbase set\b|19[5-9]\d",
    "key cards": r"\bholos?\b|rookie|\brc\b|autograph|\bauto\b|refractor|numbered|/\d{2,3}\b",
}


@dataclass
class LeadResult:
    category: str = ""           # "games" / "cards" / ""
    is_lead: bool = False
    subtype: str = ""            # platform or card type
    signals: list = field(default_factory=list)
    excluded_reason: str = ""

    @property
    def reason(self) -> str:
        return f"{self.subtype}: {', '.join(self.signals)}" if self.is_lead else self.excluded_reason


# Games: one strong signal, or two weaker ones. Cards: a strong signal is required, because
# almost every card lot is a "bulk lot" with "holos".
STRONG_SIGNALS = {"games": {"sealed", "complete in box", "console"},
                  "cards": {"graded", "sealed", "vintage"}}
REQUIRE_STRONG = {"cards"}


def evaluate_lead(title: str, description: str, current_price: float, max_bids: dict,
                  hours_left: float | None = None, window_hours: float | None = None) -> LeadResult:
    """max_bids: {"games": 40, "cards": 40} - leads only while the current bid is at or under this.
    hours_left/window_hours: if both given, only lots ending within the window can be leads."""
    text = f"{title} {description}".lower()
    for pat in EXCLUDE:
        if re.search(pat, text):
            return LeadResult(excluded_reason=f"excluded: {pat}")

    card = next((k for k, p in CARD_TYPES.items() if re.search(p, text)), None)
    platform = next((k for k, p in GAME_PLATFORMS.items() if re.search(p, text)), None)

    if card and re.search(CARD_CONTEXT, text):
        category, subtype, signals = "cards", card, CARD_SIGNALS
    elif platform and re.search(GAME_CONTEXT, text):
        category, subtype, signals = "games", platform, GAME_SIGNALS
    else:
        return LeadResult(excluded_reason="not a game or card listing")

    found = [name for name, pat in signals.items() if re.search(pat, text)]
    result = LeadResult(category=category, subtype=subtype, signals=found)
    if not found:
        result.excluded_reason = f"{subtype}: no value signals (single common item?)"
    elif not STRONG_SIGNALS[category] & set(found) and (category in REQUIRE_STRONG or len(found) < 2):
        result.excluded_reason = f"{subtype}: no strong signal ({', '.join(found)})"
    elif hours_left is not None and window_hours is not None and not (0 <= hours_left <= window_hours):
        result.excluded_reason = f"{subtype}: ends in {hours_left:.0f}h, outside the {window_hours:.0f}h lead window"
    elif current_price > max_bids.get(category, 0):
        result.excluded_reason = f"{subtype}: bid ${current_price:.2f} over lead limit ${max_bids.get(category, 0)}"
    else:
        result.is_lead = True
    return result