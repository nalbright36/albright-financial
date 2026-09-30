"""On-demand AI resale review for ONE lot, run only when you click the button.

Grounding rules (the point of this module):
  - Claude searches the web for comparable sales (web search tool, capped per review).
  - Every comp must have a URL that actually appeared in the search results or in
    the eBay listings we supplied. Anything else is treated as invented and dropped.
  - With fewer than 2 verified comps, the resale estimate is withheld.
No Django imports here, so it can be unit tested with a mocked client.
"""
import json
import logging
import re
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are a resale pricing analyst for a small reseller who buys at auction and resells, mostly on eBay.
You will be given ONE auction lot. Your job:
1. Identify exactly what is in the lot: items, quantity, brand/model/edition, and condition clues. Say what is uncertain.
2. Use web search to find recent SOLD prices for comparable items. Prefer eBay sold listings, PriceCharting, 130point and PSA auction prices. Active listings are asking prices, not sales - label them "active".
3. Estimate a realistic resale range for the WHOLE lot.

Rules:
- Every comp MUST include the exact URL where you found it. Never invent prices, URLs or dates.
- If you cannot find at least two comparable sales, set resale_low and resale_high to null and explain why.
- Prefer sales from the last 90 days. Price the condition described, not mint condition.
- List red flags: likely fakes or reproductions, missing parts, condition problems, slow-selling items.

Return ONLY a JSON object, no markdown fences, with exactly this shape:
{"identified_items": [{"name": str, "quantity": int, "notes": str}],
 "comps": [{"title": str, "price": number, "type": "sold" or "active", "date": str or null, "url": str, "source": str}],
 "resale_low": number or null, "resale_high": number or null,
 "confidence": "high" or "medium" or "low",
 "red_flags": [str],
 "summary": str}"""


@dataclass
class ReviewResult:
    identified_items: list = field(default_factory=list)
    comps: list = field(default_factory=list)            # verified comps only
    dropped_comps: int = 0                               # comps removed as unverifiable
    resale_low: float | None = None
    resale_high: float | None = None
    confidence: str = "low"
    red_flags: list = field(default_factory=list)
    summary: str = ""
    notes: list = field(default_factory=list)            # our own validation notes
    input_tokens: int = 0
    output_tokens: int = 0
    web_searches: int = 0
    cost_usd: float = 0.0
    raw_text: str = ""

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if k != "raw_text"}


def _normalize_url(url: str) -> str:
    url = (url or "").strip().lower()
    url = re.sub(r"^https?://(www\.)?", "", url)
    return url.split("#")[0].split("?")[0].rstrip("/")


def _extract_json(text: str) -> dict:
    text = re.sub(r"```(?:json)?", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("No JSON object in model response")
    return json.loads(text[start:end + 1])


def _get(obj, name, default=None):
    return obj.get(name, default) if isinstance(obj, dict) else getattr(obj, name, default)


def _search_result_urls(content_blocks) -> set:
    """URLs the web search tool actually returned during this request."""
    urls = set()
    for block in content_blocks:
        if _get(block, "type") == "web_search_tool_result":
            results = _get(block, "content") or []
            if isinstance(results, list):
                for r in results:
                    if _get(r, "url"):
                        urls.add(_normalize_url(_get(r, "url")))
        if _get(block, "type") == "text":  # citations on text blocks also carry real URLs
            for c in _get(block, "citations") or []:
                if _get(c, "url"):
                    urls.add(_normalize_url(_get(c, "url")))
    return urls


def build_user_message(lot: dict, ebay_listings: list) -> str:
    lines = [
        f"Title: {lot.get('title', '')}",
        f"Description: {(lot.get('description') or '')[:3000]}",
        f"Category (scanner's guess): {lot.get('category', 'unknown')}",
        f"Source: {lot.get('source', '')} | Current bid: ${lot.get('current_bid', 0):.2f} | Ends: {lot.get('ends', 'unknown')}",
    ]
    if lot.get("melt_value"):
        lines.append(f"Scanner melt value: ${lot['melt_value']:.2f} (metal content only)")
    if ebay_listings:
        lines.append("\nCurrent eBay ACTIVE listings (asking prices, not sales) you may cite:")
        for item in ebay_listings[:10]:
            lines.append(f"- {item['title']} | ${item['price']:.2f} | {item.get('condition', '')} | {item['url']}")
    return "\n".join(lines)


def run_review(client, lot: dict, cfg: dict, ebay_listings: list | None = None) -> ReviewResult:
    """client: an anthropic.Anthropic() instance (or a test double).
    cfg: settings RESELLING_SCANNER["AI_REVIEW"]."""
    ebay_listings = ebay_listings or []
    tool = {"type": "web_search_20250305", "name": "web_search", "max_uses": cfg.get("max_searches", 3)}
    if cfg.get("allowed_domains"):
        tool["allowed_domains"] = cfg["allowed_domains"]

    resp = client.messages.create(
        model=cfg["model"],
        max_tokens=cfg.get("max_tokens", 2000),
        system=SYSTEM_PROMPT,
        tools=[tool],
        messages=[{"role": "user", "content": build_user_message(lot, ebay_listings)}],
    )

    result = ReviewResult()
    usage = _get(resp, "usage")
    result.input_tokens = _get(usage, "input_tokens", 0) or 0
    result.output_tokens = _get(usage, "output_tokens", 0) or 0
    result.web_searches = _get(_get(usage, "server_tool_use") or {}, "web_search_requests", 0) or 0
    result.cost_usd = round(
        result.input_tokens / 1e6 * cfg.get("input_usd_per_mtok", 0)
        + result.output_tokens / 1e6 * cfg.get("output_usd_per_mtok", 0)
        + result.web_searches * cfg.get("usd_per_search", 0.01), 4)

    blocks = _get(resp, "content") or []
    result.raw_text = "\n".join(_get(b, "text", "") for b in blocks if _get(b, "type") == "text")
    try:
        data = _extract_json(result.raw_text)
    except (ValueError, json.JSONDecodeError) as exc:
        result.notes.append(f"Could not read the AI response as JSON: {exc}")
        result.summary = result.raw_text[:1000]
        return result

    # Only keep comps whose URL we can prove came from search results or our eBay data.
    known = _search_result_urls(blocks) | {_normalize_url(i["url"]) for i in ebay_listings}
    for comp in data.get("comps") or []:
        url, price = _normalize_url(comp.get("url")), comp.get("price")
        if url and url in known and isinstance(price, (int, float)) and price > 0:
            result.comps.append(comp)
        else:
            result.dropped_comps += 1
    if result.dropped_comps:
        result.notes.append(f"{result.dropped_comps} comp(s) dropped: link not found in search results")

    result.identified_items = data.get("identified_items") or []
    result.red_flags = data.get("red_flags") or []
    result.summary = data.get("summary") or ""
    result.confidence = data.get("confidence") if data.get("confidence") in ("high", "medium", "low") else "low"

    low, high = data.get("resale_low"), data.get("resale_high")
    sold = [c for c in result.comps if c.get("type") == "sold"]
    if isinstance(low, (int, float)) and isinstance(high, (int, float)) and len(result.comps) >= 2:
        result.resale_low, result.resale_high = sorted((float(low), float(high)))
        if len(sold) < 2:
            result.confidence = "low"
            result.notes.append("Fewer than 2 verified SOLD comps - estimate leans on asking prices")
    elif low is not None or high is not None:
        result.confidence = "low"
        result.notes.append("Estimate withheld: fewer than 2 verified comps")
    return result