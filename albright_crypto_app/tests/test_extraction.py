"""AI extraction with a mocked OpenAI client: valid facts saved, an
evidence quote that isn't in the text drops evidence to "none", unknown
categories become "unknown"/"none", JSON-only parsing, cost recorded, and
the daily limit / shared monthly AI budget enforced before any call."""
import json
from decimal import Decimal
from unittest import mock

from django.conf import settings
from django.test import TestCase, override_settings

from albright_crypto_app.extraction import ExtractionLimitReached, extract, quote_in_text, validate
from albright_crypto_app.models import AIExtraction, Opportunity, Source
from albright_crypto_app.pipeline import extract_and_score
from albright_reselling_app.scanner_models import AIReview, SourcedLot

TEXT = ("Fix the CSV parser. Bounty: $200 in USDC, paid on merge by the maintainers. "
        "Deadline 2026-11-30. Should take about 4 hours. Steps: fork, fix, open a PR.")


def fake_client(facts, input_tokens=1000, output_tokens=200):
    client = mock.Mock()
    client.responses.create.return_value = {
        "output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps(facts)}]}],
        "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
    }
    return client


GOOD = {"reward_usd": 200, "reward_currency": "USDC", "reward_type": "bounty", "evidence": "explicit",
        "evidence_quote": "Bounty: $200 in USDC, paid on merge", "deadline": "2026-11-30", "hours_estimate": 4,
        "costs_usd": 0, "requires_spending": False,
        "steps": [{"text": "Fork the repo", "automatable": True}, {"text": "Fix the parser", "automatable": False},
                  {"text": "Open a PR", "automatable": False}],
        "eligibility_notes": ""}


class ExtractionTests(TestCase):
    def setUp(self):
        self.source = Source.objects.create(kind="github", name="q", url="label:bounty")
        self.opp = Opportunity.objects.create(source=self.source, external_id="1", title="CSV parser bounty",
                                              url="https://github.com/acme/widget/issues/1", raw_text=TEXT)

    def test_valid_facts_extracted_and_cost_recorded(self):
        facts = extract(self.opp, client=fake_client(GOOD))

        self.assertEqual(facts["reward_usd"], Decimal("200.00"))
        self.assertEqual((facts["reward_type"], facts["evidence"]), ("bounty", "explicit"))
        self.assertEqual(facts["evidence_quote"], "Bounty: $200 in USDC, paid on merge")
        self.assertEqual(facts["hours_estimate"], Decimal("4.00"))
        self.assertEqual(facts["deadline"].date().isoformat(), "2026-11-30")
        self.assertEqual(facts["automatable_steps"], 1)
        record = AIExtraction.objects.get()
        cfg = settings.RESELLING_SCANNER["AI_REVIEW"]
        expected = round(1000 / 1e6 * cfg["input_usd_per_mtok"] + 200 / 1e6 * cfg["output_usd_per_mtok"], 4)
        self.assertEqual(record.cost_usd, Decimal(str(expected)))

    def test_quote_not_in_text_sets_evidence_to_none(self):
        invented = {**GOOD, "evidence_quote": "Guaranteed $500 bonus for every PR"}

        facts = extract(self.opp, client=fake_client(invented))

        self.assertEqual(facts["evidence"], "none")
        self.assertEqual(facts["evidence_quote"], "")

    def test_quote_matching_ignores_case_and_whitespace(self):
        self.assertTrue(quote_in_text("bounty:   $200 in usdc", TEXT))
        self.assertTrue(quote_in_text('"Bounty: $200 in USDC"', TEXT))
        self.assertFalse(quote_in_text("$300 in USDC", TEXT))
        self.assertFalse(quote_in_text("", TEXT))

    def test_unknown_values_fall_back(self):
        facts = validate({"reward_usd": "lots", "reward_type": "jackpot", "evidence": "trust me",
                          "hours_estimate": -2, "deadline": "soon", "steps": "not a list", "costs_usd": None}, TEXT)

        self.assertIsNone(facts["reward_usd"])
        self.assertEqual((facts["reward_type"], facts["evidence"]), ("unknown", "none"))
        self.assertIsNone(facts["hours_estimate"])
        self.assertIsNone(facts["deadline"])
        self.assertEqual((facts["steps"], facts["costs_usd"]), ([], Decimal("0")))
        self.assertFalse(facts["requires_spending"])

    def test_json_wrapped_in_text_or_fences_still_parses(self):
        client = mock.Mock()
        client.responses.create.return_value = {"output_text": "Here you go:\n```json\n" + json.dumps(GOOD) + "\n```",
                                                "usage": {}}

        facts = extract(self.opp, client=client)

        self.assertEqual(facts["reward_type"], "bounty")

    @override_settings(CRYPTO={**settings.CRYPTO, "ai_daily_limit": 1})
    def test_daily_limit_stops_before_calling(self):
        extract(self.opp, client=fake_client(GOOD))
        client = fake_client(GOOD)

        with self.assertRaises(ExtractionLimitReached):
            extract(self.opp, client=client)

        client.responses.create.assert_not_called()

    def test_shared_monthly_budget_counts_reseller_reviews(self):
        budget = Decimal(str(settings.RESELLING_SCANNER["AI_REVIEW"]["monthly_budget_usd"]))
        lot = SourcedLot.objects.create(source="shopgoodwill", external_id="b1", url="https://e.com/1", title="lot")
        AIReview.objects.create(lot=lot, status="done", cost_usd=budget)  # reseller spent it all
        client = fake_client(GOOD)

        with self.assertRaises(ExtractionLimitReached):
            extract(self.opp, client=client)

        client.responses.create.assert_not_called()

    def test_over_limit_still_scores_with_unknown_facts(self):
        with override_settings(CRYPTO={**settings.CRYPTO, "ai_daily_limit": 0}):
            opp = extract_and_score(self.opp, client=fake_client(GOOD))

        self.assertEqual(opp.extraction_status, "skipped")
        self.assertIsNone(opp.reward_usd)
        self.assertEqual(opp.confidence, "low")
        self.assertIn(opp.safety_verdict, ("ok", "warn"))

    def test_api_error_recorded_and_opportunity_still_scored(self):
        client = mock.Mock()
        client.responses.create.side_effect = RuntimeError("provider down")

        opp = extract_and_score(self.opp, client=client)

        self.assertEqual(opp.extraction_status, "error")
        self.assertEqual(AIExtraction.objects.get().status, "error")
