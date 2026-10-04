"""fetch -> upsert lots -> classify (coins/jewelry/games/cards/none) -> value or lead -> save."""
import hashlib
import logging
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.conf import settings
from django.utils import timezone

from ..scanner_models import LotEvaluation, SourcedLot
from .adapters.base import SourceBlocked, SourceUnavailable
from .adapters.hibid import HiBidAdapter
from .adapters.maxsold import MaxSoldAdapter
from .adapters.shopgoodwill import ShopGoodwillAdapter
from .coins import estimate_resale
from .features import extract_features
from .insights import calibrated_category_fees, calibrated_multipliers, get_calibrated
from .max_bid import BuyCosts, SellFees, max_bid
from .spot import get_all_spot
from .valuers import classify

log = logging.getLogger(__name__)
ADAPTERS = {"shopgoodwill": ShopGoodwillAdapter, "maxsold": MaxSoldAdapter, "hibid": HiBidAdapter}


def _d(x, places="0.01"):
    return Decimal(str(x)).quantize(Decimal(places))


def _cfg():
    return settings.RESELLING_SCANNER


def _categories_for(cfg, source):
    """Every category in the source's own keyword dict, if it has one;
    otherwise every category in the global KEYWORDS dict."""
    keywords_dict = cfg["SOURCES"][source].get("keywords") or cfg["KEYWORDS"]
    return list(keywords_dict.keys())


def _keywords_for(cfg, source, category):
    """Source-specific keywords (RESELLING_SCANNER["SOURCES"][source]["keywords"])
    win when present; otherwise fall back to the global per-category list. This
    keeps ShopGoodwill (no "keywords" key in its SOURCES entry) unchanged."""
    return cfg["SOURCES"][source].get("keywords", {}).get(category) or cfg["KEYWORDS"][category]


def _buyer_premium_pct(raw, src_cfg, source):
    """Per-lot premium (HiBid: raw["_costs"]["buyer_premium_pct"], parsed by
    the adapter from each lot's own auction - HiBid's premium varies by
    auction house, unlike ShopGoodwill/MaxSold's one flat source-level rate)
    overrides the source's own default when present. Shared by the max bid
    calc here, the ledger's "I won this" cost prefill, and the AI review
    service's suggested max bid, so all three price a HiBid lot the same
    way the scanner itself did. When there's no per-lot premium, falls
    back to a calibration override for this source before the flat
    settings.py default - see scanner/insights.py."""
    per_lot = (raw or {}).get("_costs", {}).get("buyer_premium_pct")
    if per_lot is not None:
        return per_lot
    # HiBid has no flat "buyer_premium_pct" in its SOURCES config (its own
    # default lives under "default_buyer_premium_pct" and is already baked
    # into every parsed lot's raw["_costs"]) - this fallback only matters
    # for sources that always set a flat rate (ShopGoodwill/MaxSold).
    default = src_cfg.get("buyer_premium_pct", 0.0)
    return get_calibrated(f"SOURCES.{source}.buyer_premium_pct", default)


def _sales_tax_pct(src_cfg, source):
    """Same override-then-settings fallback as _buyer_premium_pct, for the
    other buy-side rate shared by the pipeline, the ledger, and the AI
    review service."""
    return get_calibrated(f"SOURCES.{source}.sales_tax_pct", src_cfg["sales_tax_pct"])


def _inbound_shipping(raw, src_cfg):
    """Per-lot inbound cost. For a pickup lot with a known drive distance
    (raw["_pickup"]["distance_miles"]) and a mileage rate configured for the
    source, price the round trip (there and back) plus a flat per-trip cost.
    Otherwise fall back to the source's flat default_inbound_shipping."""
    pickup = (raw or {}).get("_pickup") or {}
    distance = pickup.get("distance_miles")
    mileage_rate = src_cfg.get("mileage_rate")
    if distance is not None and mileage_rate:
        return (2 * distance * mileage_rate) + src_cfg.get("pickup_fixed_cost", 0)
    return src_cfg["default_inbound_shipping"]


def _upsert(raw, keyword, tz_name):
    end = raw.end_time
    if end and timezone.is_naive(end):
        end = timezone.make_aware(end, ZoneInfo(tz_name))
    features = extract_features(raw.raw, raw.title, raw.description, raw.source)
    lot, created = SourcedLot.objects.update_or_create(
        source=raw.source, external_id=raw.external_id,
        defaults=dict(url=raw.url, title=raw.title[:500], description=raw.description,
                      image_url=raw.image_url[:500], current_price=_d(raw.current_price),
                      bid_count=raw.bid_count, end_time=end, matched_keyword=keyword, raw=raw.raw,
                      features=features),
    )
    if created:
        # Set once, on the lot's very first scan - re-scans update current_price
        # (and features) every time, but this is the listing's starting point.
        lot.first_seen_price = lot.current_price
        lot.save(update_fields=["first_seen_price"])
    return lot


def _apply_valuation(evaluation, parse, lot, spot, src, cfg):
    """Coins & jewelry: metal valuation (estimate_resale + max_bid), plus
    is_lead/lead_reason from the parser's own needs_review flag (e.g.
    jewelry with no stated weight, or from a flagged designer). Fees are the
    source's FEES merged with CATEGORY_FEES.get(category, {}) - a no-op
    merge for coins (no "coins" key in CATEGORY_FEES), but drops eBay/
    shipping fees for jewelry, which sells to scrap buyers instead."""
    evaluation.method = parse.method
    evaluation.flags = parse.flags
    evaluation.confidence = parse.confidence
    evaluation.excluded_reason = parse.excluded_reason
    evaluation.coin_keys = ",".join(i.coin_key for i in parse.items)[:200]
    evaluation.silver_oz = _d(parse.total_oz("silver"), "0.0001")
    evaluation.gold_oz = _d(parse.total_oz("gold"), "0.0001")

    if parse.items and not parse.excluded_reason:
        multipliers = calibrated_multipliers(cfg["RESALE_MULTIPLIERS"])
        melt, expected = estimate_resale(parse, spot, multipliers)
        base_fees = {**cfg["FEES"], **cfg["CATEGORY_FEES"].get(evaluation.category, {})}
        fees = calibrated_category_fees(evaluation.category, base_fees)
        buy = BuyCosts(
            _buyer_premium_pct(lot.raw, src, lot.source), _sales_tax_pct(src, lot.source),
            _inbound_shipping(lot.raw, src),
        )
        bid = max_bid(expected, SellFees(**fees), buy)
        evaluation.melt_value, evaluation.expected_sale, evaluation.max_bid = _d(melt), _d(expected), _d(bid)
        evaluation.headroom = _d(bid - float(lot.current_price))
        evaluation.is_candidate = (
            bid > 0 and float(lot.current_price) <= bid and parse.confidence in cfg["CANDIDATE_CONFIDENCE"]
        )
    else:
        evaluation.melt_value = evaluation.expected_sale = evaluation.max_bid = evaluation.headroom = _d(0)
        evaluation.is_candidate = False

    evaluation.is_lead = bool(parse.needs_review and not parse.excluded_reason)
    evaluation.lead_reason = parse.review_reason

    # LEAD_RULES["jewelry_no_weight"]: a jewelry lot flagged "no_weight" and
    # nothing else isn't worth a manual lead review when this is False - a
    # designer piece (flagged "designer:...") stays a lead either way, with
    # or without a stated weight.
    if (evaluation.category == "jewelry" and "no_weight" in evaluation.flags
            and not any(flag.startswith("designer:") for flag in evaluation.flags)
            and not cfg["LEAD_RULES"]["jewelry_no_weight"]):
        evaluation.is_lead = False


def _apply_lead(evaluation, lead):
    """Games & cards: no free price source, so no valuation - just a lead
    flag for lots worth pricing by hand."""
    evaluation.method = ""
    evaluation.confidence = ""
    evaluation.coin_keys = ""
    evaluation.silver_oz = evaluation.gold_oz = _d(0, "0.0001")
    evaluation.excluded_reason = ""
    evaluation.melt_value = evaluation.expected_sale = evaluation.max_bid = evaluation.headroom = _d(0)
    evaluation.is_candidate = False
    evaluation.is_lead = lead.is_lead
    evaluation.lead_reason = lead.reason
    evaluation.flags = lead.signals


def _apply_none(evaluation, reason):
    """Doesn't fit any tracked category - nothing to value or lead on."""
    evaluation.method = ""
    evaluation.confidence = ""
    evaluation.coin_keys = ""
    evaluation.silver_oz = evaluation.gold_oz = _d(0, "0.0001")
    evaluation.flags = []
    evaluation.excluded_reason = reason
    evaluation.melt_value = evaluation.expected_sale = evaluation.max_bid = evaluation.headroom = _d(0)
    evaluation.is_candidate = False
    evaluation.is_lead = False
    evaluation.lead_reason = ""


def _apply_reserve_not_met(evaluation, lot):
    """HiBid-only: raw["_hibid"]["reserve_not_met"] means the auction's
    reserve hasn't been hit yet, so the current bid isn't a real signal of
    what this lot will actually sell for - never a candidate off that bid,
    regardless of what the valuation math said, until a later scan finds
    the reserve met (or the lot closes). A no-op for every other source,
    since their raw dicts never carry "_hibid" at all."""
    if not (lot.raw or {}).get("_hibid", {}).get("reserve_not_met"):
        return
    evaluation.is_candidate = False
    if "reserve_not_met" not in evaluation.flags:
        evaluation.flags = [*evaluation.flags, "reserve_not_met"]


def evaluate(lot, spot, llm_budget, use_llm=True):
    cfg = _cfg()
    src = cfg["SOURCES"][lot.source]
    evaluation, _ = LotEvaluation.objects.get_or_create(lot=lot)

    hours_left = (lot.end_time - timezone.now()).total_seconds() / 3600 if lot.end_time else None
    classification = classify(lot.title, lot.description, float(lot.current_price), cfg["LEAD_LIMITS"],
                              hours_left=hours_left, window_hours=cfg["LEAD_WINDOW_HOURS"])
    evaluation.category = classification.category

    if classification.category == "coins":
        text_hash = hashlib.sha256(f"{lot.title}|{lot.description}".encode()).hexdigest()
        parse = classification.parse

        if parse.needs_llm and use_llm and cfg["LLM"]["enabled"]:
            if evaluation.llm_title_hash == text_hash and evaluation.method == "llm":
                return evaluation, llm_budget  # already paid for this exact text; keep prior result
            if llm_budget > 0:
                from .llm import llm_parse  # imported lazily so the scanner runs without openai installed
                try:
                    parse = llm_parse(lot.title, lot.description)
                    evaluation.llm_title_hash = text_hash
                    llm_budget -= 1
                except Exception as exc:  # noqa: BLE001
                    log.warning("LLM failed for %s: %s", lot, exc)

        _apply_valuation(evaluation, parse, lot, spot, src, cfg)

    elif classification.category == "jewelry":
        _apply_valuation(evaluation, classification.parse, lot, spot, src, cfg)

    elif classification.category in ("games", "cards"):
        _apply_lead(evaluation, classification.lead)

    else:  # "none"
        _apply_none(evaluation, classification.reason)

    _apply_reserve_not_met(evaluation, lot)

    evaluation.save()
    return evaluation, llm_budget


def run_scan(source: str, category: str | None = None, use_llm=True, keywords=None):
    cfg = _cfg()
    adapter = ADAPTERS[source]()
    spot = get_all_spot()
    llm_budget = cfg["LLM"]["max_calls_per_run"]
    tz_name = cfg["SOURCES"][source].get("timezone", "UTC")
    seen, failed_keywords = 0, []
    blocked = False
    latest_by_lot = {}  # lot_id -> most recent evaluation, so a lot matched by
                        # several keywords is only counted/listed once
    consecutive_failures = 0

    if keywords is not None:
        keyword_list = list(keywords)
    else:
        categories = [category] if category else _categories_for(cfg, source)
        keyword_list = [kw for cat in categories for kw in _keywords_for(cfg, source, cat)]

    for keyword in keyword_list:
        try:
            for raw in adapter.search(keyword):
                lot = _upsert(raw, keyword, tz_name)
                ev, llm_budget = evaluate(lot, spot, llm_budget, use_llm)
                seen += 1
                latest_by_lot[ev.lot_id] = ev
        except SourceBlocked as exc:
            log.error("Stopping scan: %s", exc)
            blocked = True
            break
        except SourceUnavailable as exc:
            log.warning("Keyword %r failed, keeping lots found so far: %s", keyword, exc)
            failed_keywords.append(keyword)
            consecutive_failures += 1
            if consecutive_failures >= 2:
                log.error("Stopping scan: %s consecutive keywords failed with SourceUnavailable - "
                          "the site is probably down or throttling us", consecutive_failures)
                break
            adapter.pause()
            continue
        else:
            consecutive_failures = 0
        adapter.pause()

    candidates = [ev for ev in latest_by_lot.values() if ev.is_candidate]
    leads = [ev for ev in latest_by_lot.values() if ev.is_lead]

    return {"seen": seen, "candidates": candidates, "leads": leads, "spot": spot,
            "failed_keywords": failed_keywords, "keywords_scanned": keyword_list, "blocked": blocked,
            "llm_calls": cfg["LLM"]["max_calls_per_run"] - llm_budget}
