"""Listing-feature extraction for the "what predicts a cheap close"
analysis (see management/commands/export_features.py). Pure Python - no
Django imports, no network calls - so extract_features() is trivially
testable against sample raw item dicts and safe to call from both the
pipeline (on every scan) and the track_closed command (at close time).

Every value is JSON-serializable (str/int/float/bool/None), since the
result is stored directly in SourcedLot.features, a JSONField.
"""
import re

VAGUE_WORDS = ("lot", "box", "junk", "misc", "miscellaneous", "assorted", "estate", "grab bag", "untested")

WEIGHT_RE = re.compile(r"\b\d+(\.\d+)?\s*(g|grams?|dwt|ozt|oz|ounces?)\b", re.IGNORECASE)
KARAT_RE = re.compile(r"\b\d{1,2}\s*kt?\b", re.IGNORECASE)
PURITY_RE = re.compile(
    r"\.(925|999|900|958|750|585|417|333)\b|\b(925|999|900|958|750|585|417|333)\b"
    r"|\bsterling\b|\bfine\s+(silver|gold)\b",
    re.IGNORECASE,
)
QUANTITY_RE = re.compile(r"\blot\s+of\s+\d+\b|\(\s*\d+\s*\)|\bset\s+of\s+\d+\b", re.IGNORECASE)

ALL_CAPS_THRESHOLD = 0.8  # share of alphabetic chars that must be uppercase to count as "mostly caps"


def _first(d, *keys):
    for k in keys:
        v = d.get(k)
        if v is not None:
            return v
    return None


def _is_mostly_upper(title):
    letters = [c for c in title if c.isalpha()]
    if not letters:
        return False
    return sum(1 for c in letters if c.isupper()) / len(letters) >= ALL_CAPS_THRESHOLD


def _has_vague_word(title_lower):
    return any(word in title_lower for word in VAGUE_WORDS)


def _photo_count(raw, source):
    if source == "maxsold":
        return len([u for u in (raw.get("imageUrls") or []) if u])
    # ShopGoodwill search results only ever carry one image URL (we'll
    # improve this later with a per-item detail fetch).
    return 1 if _first(raw, "imageURL", "imageUrl") else 0


def _relist_id(raw):
    """ShopGoodwill's "relistId" on a raw item - None if absent or 0 (not
    a relist). MaxSold raw dicts never carry this key, so this is a no-op
    there (always None) without needing a source check."""
    try:
        value = int(raw.get("relistId"))
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def extract_features(raw_lot_dict, title, description, source):
    raw = raw_lot_dict or {}
    title = title or ""
    description = description or ""
    title_lower = title.lower()
    pickup = raw.get("_pickup") or {}

    has_weight = bool(WEIGHT_RE.search(title))
    has_quantity = bool(QUANTITY_RE.search(title_lower))
    relist_id = _relist_id(raw)

    return {
        "title_length": len(title),
        "title_word_count": len(title.split()),
        "title_all_caps": _is_mostly_upper(title),
        "title_has_weight": has_weight,
        "title_has_karat_or_purity": bool(KARAT_RE.search(title)) or bool(PURITY_RE.search(title)),
        "title_has_quantity": has_quantity,
        "title_vague": _has_vague_word(title_lower) and not has_weight and not has_quantity,
        "description_length": len(description),
        "photo_count": _photo_count(raw, source),
        "views": raw.get("views"),
        "starting_price": _first(raw, "startingPrice", "minimumBid"),
        "seller_id": raw.get("sellerId"),
        "seller_category": raw.get("catFullName"),
        "auction_id": raw.get("amAuctionId"),
        "distance_miles": pickup.get("distance_miles"),
        "shipping_price": raw.get("shippingPrice"),
        "has_shipping": pickup.get("has_shipping"),
        "relist_id": relist_id,
        "is_relisted": relist_id is not None,
    }
