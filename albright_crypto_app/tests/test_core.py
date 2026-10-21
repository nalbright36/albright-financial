"""Run from the crypto app with: python manage.py test <app>.tests --keepdb (no database needed)."""
from unittest import TestCase

from ..core.safety import check_domain, check_opportunity
from ..core.scoring import score

ALLOW = ["github.com", "galxe.com", "coinbase.com", "layer3.xyz"]


class SafetyTests(TestCase):
    def test_blocks_seed_phrase_and_keys(self):
        for text in ["Enter your 12-word seed phrase to claim", "Import your private key to verify eligibility"]:
            self.assertEqual(check_opportunity(text, "https://galxe.com/x", ALLOW).verdict, "block", text)

    def test_blocks_deposit_to_unlock_and_guarantees(self):
        self.assertEqual(check_opportunity("Send 0.1 ETH to unlock your reward", "https://github.com/a", ALLOW).verdict, "block")
        self.assertEqual(check_opportunity("Guaranteed returns, double your crypto", "https://github.com/a", ALLOW).verdict, "block")
        self.assertEqual(check_opportunity("Please validate your wallet to receive", "https://github.com/a", ALLOW).verdict, "block")

    def test_lookalike_domains_blocked(self):
        for url in ["https://githhub.com/x", "https://galxe-claim.io/x", "https://coinbasse.com", "https://xn--gthub-cta.com"]:
            self.assertEqual(check_domain(url, ALLOW)[1], "lookalike", url)
            self.assertEqual(check_opportunity("Complete the quest", url, ALLOW).verdict, "block", url)

    def test_allowlisted_and_subdomains(self):
        self.assertEqual(check_domain("https://docs.github.com/a", ALLOW)[1], "allowlisted")
        self.assertEqual(check_domain("https://www.galxe.com/quest", ALLOW)[1], "allowlisted")

    def test_warnings_raise_risk_without_blocking(self):
        r = check_opportunity("Connect your wallet and pay gas fee to claim. Ends in 2 hours", "https://galxe.com/q", ALLOW)
        self.assertEqual(r.verdict, "warn")
        self.assertGreaterEqual(r.risk_score, 60)

    def test_unknown_domain_warns(self):
        r = check_opportunity("Fix the parser bug, $200 bounty", "https://someproject.dev/bounty", ALLOW)
        self.assertEqual((r.verdict, r.domain_status), ("warn", "unknown"))

    def test_clean_allowlisted_is_ok(self):
        self.assertEqual(check_opportunity("Fix the parser bug, $200 bounty", "https://github.com/o/r/issues/1", ALLOW).verdict, "ok")


class ScoringTests(TestCase):
    def test_explicit_official_bounty(self):
        s = score(200, "bounty", hours=4, evidence="explicit", official_source=True)
        self.assertEqual((s.confidence, s.ev_mid, s.ev_per_hour_mid, s.reward_counts_as_confirmed), ("high", 130.0, 32.5, True))

    def test_speculative_airdrop_low_and_unconfirmed(self):
        s = score(200, "speculative_airdrop", hours=3, evidence="stated")
        self.assertEqual((s.ev_mid, s.confidence, s.reward_counts_as_confirmed), (10.0, "medium", False))

    def test_zero_budget_excludes_spending(self):
        self.assertTrue(score(40, "promo_reward", 0.5, costs_usd=5).excluded)
        self.assertTrue(score(40, "promo_reward", 0.5, requires_spending=True).excluded)
        self.assertFalse(score(40, "promo_reward", 0.5, costs_usd=5, budget_usd=25).excluded)

    def test_no_evidence_halves_probability(self):
        self.assertEqual(score(100, "promo_reward", 1, evidence="none").ev_mid, 40.0)

    def test_automation_levels(self):
        self.assertEqual(score(10, "microtask", 1, automatable_steps=3, total_steps=4).automation, "high")
        self.assertEqual(score(10, "microtask", 1, automatable_steps=0, total_steps=4).automation, "none")

    def test_unknown_hours_has_no_hourly(self):
        self.assertIsNone(score(50, "bounty", None, evidence="explicit", official_source=True).ev_per_hour_mid)


class IncomeTests(TestCase):
    def test_job_scams_blocked(self):
        for text in ["Pay the $49 training fee to start your new job today",
                     "Deposit the check we mail you and send the remainder via wire transfer",
                     "Work from home as a package reshipper",
                     "Payment for the starter kit must be made in Apple gift cards",
                     "Your interview will be held on Telegram with our HR manager"]:
            self.assertEqual(check_opportunity(text, "https://github.com/x", ALLOW).verdict,
                             "block" if "interview" not in text else "warn", text)

    def test_unrealistic_pay_warns(self):
        r = check_opportunity("Earn $500 per day from home, no experience needed", "https://github.com/x", ALLOW)
        self.assertIn("unrealistic pay for little work", r.reasons)

    def test_income_types_scored(self):
        f = score(1200, "freelance", hours=20, evidence="explicit", official_source=True)
        self.assertEqual((f.confidence, f.reward_counts_as_confirmed), ("high", True))
        c = score(10000, "competition", hours=40, evidence="explicit", official_source=True)
        self.assertFalse(c.reward_counts_as_confirmed)
        self.assertLess(c.ev_mid, 1000)