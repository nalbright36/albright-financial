"""AI review grounding rules and the eBay client. All network calls are mocked."""
import json
import os
from types import SimpleNamespace as NS
from unittest import TestCase
from unittest.mock import MagicMock, patch

from albright_reselling_app.scanner import ebay
from albright_reselling_app.scanner.ai_review import run_review

CFG = {"provider": "anthropic", "model": "test-model", "max_searches": 3, "input_usd_per_mtok": 1.0,
       "output_usd_per_mtok": 5.0, "usd_per_search": 0.01}
LOT = {"title": "Lot of 8 Nintendo 64 Games", "description": "", "category": "games",
       "source": "shopgoodwill", "current_bid": 25.0, "ends": "in 5h"}


def fake_response(payload, search_urls, searches=2, text_override=None):
    blocks = [NS(type="web_search_tool_result", content=[NS(url=u) for u in search_urls]),
              NS(type="text", text=text_override if text_override is not None else json.dumps(payload),
                 citations=[])]
    usage = NS(input_tokens=20000, output_tokens=1000, server_tool_use=NS(web_search_requests=searches))
    client = MagicMock()
    client.messages.create.return_value = NS(content=blocks, usage=usage)
    return client


def payload(comps, low=100, high=140):
    return {"identified_items": [{"name": "N64 games", "quantity": 8, "notes": ""}], "comps": comps,
            "resale_low": low, "resale_high": high, "confidence": "high", "red_flags": [], "summary": "ok"}


SOLD_A = {"title": "N64 lot", "price": 120, "type": "sold", "date": "2026-09-01",
          "url": "https://www.ebay.com/itm/111?hash=x", "source": "eBay"}
SOLD_B = {"title": "N64 lot 2", "price": 130, "type": "sold", "date": "2026-09-10",
          "url": "https://www.pricecharting.com/game/nintendo-64/x", "source": "PriceCharting"}
INVENTED = {"title": "made up", "price": 500, "type": "sold", "date": None,
            "url": "https://www.ebay.com/itm/999", "source": "eBay"}


class AIReviewTests(TestCase):
    def test_verified_comps_kept_estimate_given(self):
        client = fake_response(payload([SOLD_A, SOLD_B]),
                               ["https://ebay.com/itm/111", "https://pricecharting.com/game/nintendo-64/x"])
        r = run_review(client, LOT, CFG)
        self.assertEqual(len(r.comps), 2)
        self.assertEqual((r.resale_low, r.resale_high, r.confidence), (100.0, 140.0, "high"))

    def test_invented_url_dropped_and_estimate_withheld(self):
        client = fake_response(payload([SOLD_A, INVENTED]), ["https://ebay.com/itm/111"])
        r = run_review(client, LOT, CFG)
        self.assertEqual((len(r.comps), r.dropped_comps), (1, 1))
        self.assertIsNone(r.resale_low)
        self.assertEqual(r.confidence, "low")

    def test_active_only_comps_lower_confidence(self):
        a1 = dict(SOLD_A, type="active"); a2 = dict(SOLD_B, type="active")
        client = fake_response(payload([a1, a2]),
                               ["https://ebay.com/itm/111", "https://pricecharting.com/game/nintendo-64/x"])
        self.assertEqual(run_review(client, LOT, CFG).confidence, "low")

    def test_ebay_listing_urls_count_as_verified(self):
        ebay_items = [{"title": "N64 lot", "price": 125.0, "url": "https://www.ebay.com/itm/222"}]
        comp = dict(SOLD_A, url="https://www.ebay.com/itm/222", type="active")
        client = fake_response(payload([SOLD_A, comp]), ["https://ebay.com/itm/111"])
        self.assertEqual(len(run_review(client, LOT, CFG, ebay_listings=ebay_items).comps), 2)

    def test_cost_calculation(self):
        client = fake_response(payload([]), [], searches=3)
        r = run_review(client, LOT, CFG)
        self.assertAlmostEqual(r.cost_usd, 20000 / 1e6 * 1 + 1000 / 1e6 * 5 + 0.03)

    def test_markdown_fenced_json_parsed(self):
        text = "Here you go:\n```json\n" + json.dumps(payload([])) + "\n```"
        r = run_review(fake_response(None, [], text_override=text), LOT, CFG)
        self.assertEqual(r.summary, "ok")

    def test_unparseable_response_reported(self):
        r = run_review(fake_response(None, [], text_override="sorry, no idea"), LOT, CFG)
        self.assertTrue(any("JSON" in n for n in r.notes))

    def test_web_search_tool_capped(self):
        client = fake_response(payload([]), [])
        run_review(client, LOT, dict(CFG, allowed_domains=["ebay.com"]))
        tool = client.messages.create.call_args.kwargs["tools"][0]
        self.assertEqual((tool["max_uses"], tool["allowed_domains"]), (3, ["ebay.com"]))


def fake_openai_response(payload, source_urls, cited_urls=(), searches=2, text_override=None):
    output = [NS(type="web_search_call", action=NS(sources=[NS(url=u) for u in source_urls]))
              for _ in range(searches)]
    text = text_override if text_override is not None else json.dumps(payload)
    output.append(NS(type="message", content=[NS(type="output_text", text=text,
                     annotations=[NS(type="url_citation", url=u) for u in cited_urls])]))
    client = MagicMock()
    client.responses.create.return_value = NS(output=output, usage=NS(input_tokens=15000, output_tokens=800))
    return client


OPENAI_CFG = dict(CFG, provider="openai")


class OpenAIReviewTests(TestCase):
    def test_sources_and_citations_verify_comps(self):
        client = fake_openai_response(payload([SOLD_A, SOLD_B]), ["https://ebay.com/itm/111"],
                                      cited_urls=["https://pricecharting.com/game/nintendo-64/x"])
        r = run_review(client, LOT, OPENAI_CFG)
        self.assertEqual((len(r.comps), r.resale_low, r.confidence), (2, 100.0, "high"))

    def test_invented_url_dropped(self):
        client = fake_openai_response(payload([SOLD_A, INVENTED]), ["https://ebay.com/itm/111"])
        r = run_review(client, LOT, OPENAI_CFG)
        self.assertEqual((len(r.comps), r.dropped_comps, r.resale_low), (1, 1, None))

    def test_request_shape_and_cost(self):
        client = fake_openai_response(payload([]), [], searches=3)
        r = run_review(client, LOT, dict(OPENAI_CFG, allowed_domains=["ebay.com"]))
        kwargs = client.responses.create.call_args.kwargs
        self.assertEqual(kwargs["tools"], [{"type": "web_search", "filters": {"allowed_domains": ["ebay.com"]}}])
        self.assertEqual((kwargs["max_tool_calls"], kwargs["include"]), (3, ["web_search_call.action.sources"]))
        self.assertEqual(r.web_searches, 3)
        self.assertAlmostEqual(r.cost_usd, 15000 / 1e6 * 1 + 800 / 1e6 * 5 + 0.03)

    def test_default_provider_is_openai(self):
        client = fake_openai_response(payload([]), [])
        run_review(client, LOT, {k: v for k, v in CFG.items() if k != "provider"})
        client.responses.create.assert_called_once()


class EbayTests(TestCase):
    def test_query_from_title(self):
        self.assertEqual(ebay.query_from_title("Vintage LOT of 8 Nintendo 64 Games!! L@@K"), "8 nintendo 64 games")

    @patch.dict(os.environ, {"EBAY_CLIENT_ID": "id", "EBAY_CLIENT_SECRET": "secret"})
    @patch("albright_reselling_app.scanner.ebay.requests")
    def test_search_active(self, req):
        ebay._token_cache.update(token=None, expires=0)
        req.post.return_value = MagicMock(json=lambda: {"access_token": "tok", "expires_in": 7200})
        req.get.return_value = MagicMock(json=lambda: {"itemSummaries": [
            {"title": "N64 Game Lot", "price": {"value": "99.99"}, "condition": "Used",
             "itemWebUrl": "https://www.ebay.com/itm/1"},
            {"title": "no price", "itemWebUrl": "https://www.ebay.com/itm/2"}]})
        items = ebay.search_active("n64 games")
        self.assertEqual(items, [{"title": "N64 Game Lot", "price": 99.99, "condition": "Used",
                                  "url": "https://www.ebay.com/itm/1"}])
        self.assertEqual(req.get.call_args.kwargs["headers"]["Authorization"], "Bearer tok")