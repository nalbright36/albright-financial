"""LLM fallback for lots the regex parser can't pin down (mixed lots, vague titles).

The model only IDENTIFIES coins and counts them. Metal content still comes from
the COIN_TYPES table, and LLM results are capped at "medium" confidence.
"""
import json
import logging

from django.conf import settings
from openai import OpenAI

from .coins import COIN_TYPES_BY_KEY, GRAMS_PER_TROY_OZ, ParsedItem, ParseResult

log = logging.getLogger(__name__)

SYSTEM_PROMPT = f"""You identify coins and precious-metal bullion in auction listings.
Only report facts written in the listing. Never guess a year, weight, purity, or quantity that is not stated.
If the listing is a copy, replica, token, plated, clad, or not a precious-metal item, set not_precious_metal true.

Allowed coin_type values: {", ".join(COIN_TYPES_BY_KEY)}, generic_silver, generic_gold, unknown.

Return JSON only, no other text:
{{"items": [{{"coin_type": str, "quantity": int or null, "stated_weight_grams": number or null,
  "stated_weight_troy_oz": number or null}}],
 "not_precious_metal": bool, "graded": bool, "notes": str}}"""


def llm_parse(title: str, description: str = "") -> ParseResult:
    cfg = settings.RESELLING_SCANNER["LLM"]
    client = OpenAI()  # reads OPENAI_API_KEY from the environment
    resp = client.chat.completions.create(
        model=cfg["model"],
        response_format={"type": "json_object"},
        temperature=0,
        max_tokens=400,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"Title: {title}\nDescription: {description[:2000]}"},
        ],
    )
    data = json.loads(resp.choices[0].message.content)
    result = ParseResult(method="llm")

    if data.get("not_precious_metal"):
        result.excluded_reason = f"LLM: not precious metal ({data.get('notes', '')[:120]})"
        return result
    if data.get("graded"):
        result.flags.append("graded")

    all_qty_stated = True
    for raw in data.get("items", []):
        key = raw.get("coin_type", "unknown")
        qty = raw.get("quantity")
        if qty is None:
            all_qty_stated = False
            qty = 1
        if key in COIN_TYPES_BY_KEY:
            ct = COIN_TYPES_BY_KEY[key]
            result.items.append(ParsedItem(key, ct.metal, ct.oz, int(qty), "identified by LLM"))
            if ct.numismatic and "numismatic_upside" not in result.flags:
                result.flags.append("numismatic_upside")
        elif key in ("generic_silver", "generic_gold"):
            oz = raw.get("stated_weight_troy_oz")
            if oz is None and raw.get("stated_weight_grams"):
                oz = raw["stated_weight_grams"] / GRAMS_PER_TROY_OZ
            if oz:  # no stated weight -> can't value it
                result.items.append(ParsedItem(key, key.split("_")[1], round(float(oz), 4), int(qty), "LLM, stated weight"))

    if result.items:
        result.confidence = "medium" if all_qty_stated else "low"
    return result
