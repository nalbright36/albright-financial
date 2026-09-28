"""fetch -> upsert lots -> parse (regex, then LLM if needed) -> value -> max bid -> save."""
import hashlib
import logging
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.conf import settings
from django.utils import timezone

from ..scanner_models import LotEvaluation, SourcedLot
from .adapters.base import SourceBlocked, SourceUnavailable
from .adapters.shopgoodwill import ShopGoodwillAdapter
from .coins import estimate_resale, parse_coin_text
from .max_bid import BuyCosts, SellFees, max_bid
from .spot import get_all_spot

log = logging.getLogger(__name__)
ADAPTERS = {"shopgoodwill": ShopGoodwillAdapter}


def _d(x, places="0.01"):
    return Decimal(str(x)).quantize(Decimal(places))


def _cfg():
    return settings.RESELLING_SCANNER


def _upsert(raw, keyword, tz_name):
    end = raw.end_time
    if end and timezone.is_naive(end):
        end = timezone.make_aware(end, ZoneInfo(tz_name))
    lot, _ = SourcedLot.objects.update_or_create(
        source=raw.source, external_id=raw.external_id,
        defaults=dict(url=raw.url, title=raw.title[:500], description=raw.description,
                      image_url=raw.image_url[:500], current_price=_d(raw.current_price),
                      bid_count=raw.bid_count, end_time=end, matched_keyword=keyword, raw=raw.raw),
    )
    return lot


def evaluate(lot, spot, llm_budget, use_llm=True):
    cfg = _cfg()
    src = cfg["SOURCES"][lot.source]
    evaluation, _ = LotEvaluation.objects.get_or_create(lot=lot)

    text_hash = hashlib.sha256(f"{lot.title}|{lot.description}".encode()).hexdigest()
    parse = parse_coin_text(lot.title, lot.description)

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

    evaluation.method = parse.method
    evaluation.flags = parse.flags
    evaluation.confidence = parse.confidence
    evaluation.excluded_reason = parse.excluded_reason
    evaluation.coin_keys = ",".join(i.coin_key for i in parse.items)[:200]
    evaluation.silver_oz = _d(parse.total_oz("silver"), "0.0001")
    evaluation.gold_oz = _d(parse.total_oz("gold"), "0.0001")

    if parse.items and not parse.excluded_reason:
        melt, expected = estimate_resale(parse, spot, cfg["RESALE_MULTIPLIERS"])
        buy = BuyCosts(src["buyer_premium_pct"], src["sales_tax_pct"], src["default_inbound_shipping"])
        bid = max_bid(expected, SellFees(**cfg["FEES"]), buy)
        evaluation.melt_value, evaluation.expected_sale, evaluation.max_bid = _d(melt), _d(expected), _d(bid)
        evaluation.headroom = _d(bid - float(lot.current_price))
        evaluation.is_candidate = (
            bid > 0 and float(lot.current_price) <= bid and parse.confidence in cfg["CANDIDATE_CONFIDENCE"]
        )
    else:
        evaluation.melt_value = evaluation.expected_sale = evaluation.max_bid = evaluation.headroom = _d(0)
        evaluation.is_candidate = False

    evaluation.save()
    return evaluation, llm_budget


def run_scan(source: str, category: str = "coins", use_llm=True, keywords=None):
    cfg = _cfg()
    adapter = ADAPTERS[source]()
    spot = get_all_spot()
    llm_budget = cfg["LLM"]["max_calls_per_run"]
    tz_name = cfg["SOURCES"][source].get("timezone", "UTC")
    seen, candidates, failed_keywords = 0, [], []
    consecutive_failures = 0

    for keyword in keywords or cfg["KEYWORDS"][category]:
        try:
            for raw in adapter.search(keyword):
                lot = _upsert(raw, keyword, tz_name)
                ev, llm_budget = evaluate(lot, spot, llm_budget, use_llm)
                seen += 1
                if ev.is_candidate:
                    candidates.append(ev)
        except SourceBlocked as exc:
            log.error("Stopping scan: %s", exc)
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

    return {"seen": seen, "candidates": candidates, "spot": spot, "failed_keywords": failed_keywords,
            "llm_calls": cfg["LLM"]["max_calls_per_run"] - llm_budget}
