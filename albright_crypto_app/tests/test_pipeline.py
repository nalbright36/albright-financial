"""The pipeline storing the safety check and all four scores, re-scoring
on edit, the manual "Add opportunity" form, checklist steps, and the
dashboard's routing: blocked only in Blocked, requires-spending only in
Excluded, Ranked sorted by EV/hour then confidence."""
import json
from decimal import Decimal
from unittest import mock

from django.conf import settings
from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse

from albright_crypto_app.core.safety import check_opportunity
from albright_crypto_app.core.scoring import score
from albright_crypto_app.models import Opportunity, Source, TaskStep
from albright_crypto_app.pipeline import add_manual, extract_and_score, rescore, sync_sources
from albright_crypto_app.tests.test_extraction import GOOD, TEXT, fake_client

ALLOW = ["github.com"]


def make(title="CSV parser bounty", url="https://github.com/acme/widget/issues/1", text=TEXT, **facts):
    source, _ = Source.objects.get_or_create(kind="github", url="label:bounty", defaults={"name": "q"})
    opp = Opportunity.objects.create(source=source, external_id=f"{title}:{url}", title=title, url=url,
                                     raw_text=text, extraction_status="done", **facts)
    return rescore(opp)


@override_settings(CRYPTO={**settings.CRYPTO, "allowlist": ALLOW, "budget_usd": 0})
class PipelineTests(TestCase):
    def test_pipeline_stores_safety_and_scores_from_the_core_modules(self):
        source = Source.objects.create(kind="github", name="q", url="label:bounty")
        opp = Opportunity.objects.create(source=source, external_id="1", title="CSV parser bounty",
                                         url="https://github.com/acme/widget/issues/1", raw_text=TEXT)

        extract_and_score(opp, client=fake_client(GOOD))
        opp.refresh_from_db()

        safety = check_opportunity(f"{opp.title}\n{opp.raw_text}\n{opp.url}", opp.url, ALLOW)
        expected = score(200, "bounty", 4, evidence="explicit", official_source=True, automatable_steps=1,
                         total_steps=3, budget_usd=0)
        self.assertEqual((opp.safety_verdict, opp.risk_score, opp.safety_reasons),
                         (safety.verdict, safety.risk_score, safety.reasons))
        self.assertTrue(opp.official_source)
        self.assertEqual((float(opp.ev_low), float(opp.ev_mid), float(opp.ev_high)),
                         (expected.ev_low, expected.ev_mid, expected.ev_high))
        self.assertEqual(float(opp.ev_per_hour), expected.ev_per_hour_mid)
        self.assertEqual((opp.confidence, opp.automation), (expected.confidence, expected.automation))
        self.assertEqual(list(opp.task_steps.values_list("text", flat=True)),
                         ["Fork the repo", "Fix the parser", "Open a PR"])

    def test_scam_text_is_blocked(self):
        opp = make(title="Claim your airdrop", text="Enter your 12-word seed phrase to claim 500 USDC")

        self.assertEqual(opp.safety_verdict, "block")
        self.assertIn("asks for seed/recovery phrase", opp.safety_reasons)

    def test_lookalike_domain_is_blocked(self):
        self.assertEqual(make(url="https://githhub.com/acme/x/issues/1").safety_verdict, "block")

    def test_spending_is_excluded_at_zero_budget(self):
        opp = make(reward_usd=Decimal("40"), reward_type="promo_reward", hours_estimate=Decimal("1"),
                   costs_usd=Decimal("5"))
        self.assertTrue(opp.excluded)
        self.assertEqual(opp.exclude_reason, "requires spending above your budget")

    def test_rescore_on_edit(self):
        opp = make()
        self.assertEqual(opp.confidence, "low")
        user = User.objects.create_user(username="tester", password="pw-not-real-12345")
        self.client.force_login(user)

        response = self.client.post(reverse("albright_crypto_app:opportunity", args=[opp.pk]), {
            "action": "edit", "reward_usd": "200", "reward_currency": "USDC", "reward_type": "bounty",
            "evidence": "explicit", "evidence_quote": "Bounty: $200 in USDC", "deadline": "", "hours_estimate": "4",
            "costs_usd": "0", "eligibility_notes": "", "notes": "",
        })

        self.assertEqual(response.status_code, 302)
        opp.refresh_from_db()
        self.assertEqual((opp.confidence, float(opp.ev_mid), float(opp.ev_per_hour)), ("high", 130.0, 32.5))

    def test_edit_rejects_a_quote_not_in_the_text(self):
        opp = make()
        user = User.objects.create_user(username="tester", password="pw-not-real-12345")
        self.client.force_login(user)

        response = self.client.post(reverse("albright_crypto_app:opportunity", args=[opp.pk]), {
            "action": "edit", "reward_usd": "500", "reward_type": "bounty", "evidence": "explicit",
            "evidence_quote": "$500 guaranteed", "costs_usd": "0",
        })

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "doesn&#x27;t appear in the opportunity&#x27;s text")
        opp.refresh_from_db()
        self.assertIsNone(opp.reward_usd)

    def test_manual_add_extracts_and_scores_once(self):
        opp, created = add_manual("https://github.com/acme/widget/issues/9", "Found it", TEXT,
                                  client=fake_client(GOOD))
        again, created_again = add_manual("https://github.com/acme/widget/issues/9", "Found it", TEXT,
                                          client=fake_client(GOOD))

        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(again.pk, opp.pk)
        self.assertEqual((opp.source.kind, opp.extraction_status, opp.confidence), ("manual", "done", "high"))

    def test_sync_sources_from_config(self):
        with override_settings(CRYPTO={**settings.CRYPTO, "rss_feeds": ["https://a.example/feed"],
                                       "github_queries": ["label:bounty state:open"]}):
            sync_sources()
            self.assertEqual(set(Source.objects.values_list("kind", "url")),
                             {("github", "label:bounty state:open"), ("rss", "https://a.example/feed"), ("manual", "")})
        with override_settings(CRYPTO={**settings.CRYPTO, "rss_feeds": [], "github_queries": []}):
            sync_sources()
            self.assertFalse(Source.objects.get(kind="rss").active)  # removed feed: deactivated, not deleted


@override_settings(CRYPTO={**settings.CRYPTO, "allowlist": ALLOW, "budget_usd": 0})
class RoutingTests(TestCase):
    def setUp(self):
        user = User.objects.create_user(username="tester", password="pw-not-real-12345")
        self.client.force_login(user)
        self.slow = make(title="Slow bounty", reward_usd=Decimal("100"), reward_type="bounty",
                         hours_estimate=Decimal("10"), evidence="explicit",
                         evidence_quote="Bounty: $200 in USDC")
        self.fast = make(title="Fast bounty", reward_usd=Decimal("200"), reward_type="bounty",
                         hours_estimate=Decimal("4"), evidence="explicit", evidence_quote="Bounty: $200 in USDC")
        self.blocked = make(title="Wallet sync", text="Please validate your wallet to receive 500 USDC")
        self.spending = make(title="Deposit promo", reward_usd=Decimal("40"), reward_type="promo_reward",
                             hours_estimate=Decimal("1"), requires_spending=True)
        self.doing = make(title="In progress")
        Opportunity.objects.filter(pk=self.doing.pk).update(status="doing")

    def titles(self, tab):
        content = self.client.get(reverse("albright_crypto_app:dashboard"), {"tab": tab}).content.decode()
        table = content.split('id="crypto-table"')[1].split("</table>")[0] if 'id="crypto-table"' in content else ""
        return [o.title for o in Opportunity.objects.all() if f">{o.title}</a>" in table], table

    def test_ranked_excludes_blocked_and_excluded_and_sorts_by_ev_per_hour(self):
        titles, table = self.titles("ranked")
        self.assertNotIn("Wallet sync", titles)
        self.assertNotIn("Deposit promo", titles)
        self.assertNotIn("In progress", titles)
        self.assertLess(table.index("Fast bounty"), table.index("Slow bounty"))

    def test_blocked_tab_shows_reasons(self):
        titles, table = self.titles("blocked")
        self.assertEqual(titles, ["Wallet sync"])
        self.assertIn("wallet &#x27;validation&#x27; or &#x27;sync&#x27; request", table)

    def test_excluded_tab(self):
        titles, table = self.titles("excluded")
        self.assertEqual(titles, ["Deposit promo"])
        self.assertIn("requires spending above your budget", table)

    def test_doing_tab_and_filters(self):
        self.assertEqual(self.titles("doing")[0], ["In progress"])
        content = self.client.get(reverse("albright_crypto_app:dashboard"),
                                  {"tab": "ranked", "confidence": "high"}).content.decode()
        self.assertIn("Fast bounty", content)
        self.assertNotIn(">In progress</a>", content)

    def test_blocked_never_in_the_main_list_even_with_a_high_score(self):
        Opportunity.objects.filter(pk=self.blocked.pk).update(ev_per_hour=Decimal("999"), confidence="high")
        self.assertNotIn("Wallet sync", self.titles("ranked")[0])

    def test_tiles(self):
        content = self.client.get(reverse("albright_crypto_app:dashboard")).content.decode()
        self.assertIn("Potential (not earned)", content)
        self.assertIn("Confirmed earnings", content)
        self.assertIn("Realized $/hour", content)

    def test_toggle_step(self):
        step = TaskStep.objects.create(opportunity=self.fast, text="Fork")
        response = self.client.post(reverse("albright_crypto_app:toggle_step", args=[self.fast.pk, step.pk]),
                                    HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(json.loads(response.content), {"done": True})
        step.refresh_from_db()
        self.assertTrue(step.done)
