"""External links (allowlisted open directly; everything else goes through
the warning page showing the full domain), earnings totals and the CSV
export, and Telegram alerts (new opportunity, deadline reminder, digest)."""
import csv
import io
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.conf import settings
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.template import Context, Template
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from albright_crypto_app.alerts import digest_lines, send_deadline_reminders, send_new_opportunity_alerts
from albright_crypto_app.models import Earning, Opportunity, TimeLog
from albright_crypto_app.tests.test_pipeline import make

CFG = {**settings.CRYPTO, "allowlist": ["github.com", "etherscan.io"], "budget_usd": 0,
       "alert_min_ev_per_hour": 15, "alert_min_confidence": "medium"}
TELEGRAM = "albright_crypto_app.alerts.send_telegram"


def render_link(url, label=""):
    return Template('{% load crypto_links %}{% external_link url label %}').render(Context({"url": url, "label": label}))


@override_settings(CRYPTO=CFG)
class LinkTests(TestCase):
    def setUp(self):
        self.client.force_login(User.objects.create_user(username="tester", password="pw-not-real-12345"))

    def test_allowlisted_links_open_directly(self):
        html = render_link("https://github.com/acme/x/issues/1")
        self.assertIn('href="https://github.com/acme/x/issues/1"', html)
        self.assertIn('target="_blank"', html)
        self.assertIn("noopener", html)

    def test_other_links_go_through_the_warning_page(self):
        html = render_link("https://someproject.dev/bounty?x=1")
        self.assertIn(reverse("albright_crypto_app:leave") + "?to=https%3A%2F%2Fsomeproject.dev%2Fbounty%3Fx%3D1", html)
        self.assertNotIn('href="https://someproject.dev', html)
        self.assertIn("unverified", html)

    def test_non_web_links_are_never_linked(self):
        for url in ("javascript:alert(1)", "data:text/html,hi", "not a url", ""):
            with self.subTest(url=url):
                self.assertNotIn("<a ", render_link(url, "x"))

    def test_warning_page_shows_full_domain(self):
        response = self.client.get(reverse("albright_crypto_app:leave"),
                                   {"to": "https://claims.someproject.dev/airdrop?ref=1"})
        self.assertContains(response, "This site isn't on your verified list")
        self.assertContains(response, '<p class="crypto-domain" id="leave-domain">claims.someproject.dev</p>', html=False)
        self.assertContains(response, 'href="https://claims.someproject.dev/airdrop?ref=1"')

    def test_warning_page_flags_lookalikes(self):
        response = self.client.get(reverse("albright_crypto_app:leave"), {"to": "https://githhub.com/x"})
        self.assertContains(response, "looks like an impersonation")

    def test_warning_page_redirects_allowlisted_and_refuses_non_web(self):
        allowed = self.client.get(reverse("albright_crypto_app:leave"), {"to": "https://github.com/a"})
        self.assertRedirects(allowed, "https://github.com/a", fetch_redirect_response=False)
        bad = self.client.get(reverse("albright_crypto_app:leave"), {"to": "javascript:alert(1)"})
        self.assertContains(bad, "That isn't a web link")
        self.assertNotContains(bad, "javascript:alert")

    def test_every_external_link_on_pages_goes_through_the_check(self):
        opp = make(title="Unverified", url="https://someproject.dev/bounty")
        for name, args in (("inbox", []), ("opportunity", [opp.pk])):
            content = self.client.get(reverse(f"albright_crypto_app:{name}", args=args)).content.decode()
            self.assertNotIn('href="https://someproject.dev', content, name)
            self.assertIn("leave/?to=https%3A%2F%2Fsomeproject.dev", content, name)


@override_settings(CRYPTO=CFG)
class EarningsTests(TestCase):
    def setUp(self):
        self.client.force_login(User.objects.create_user(username="tester", password="pw-not-real-12345"))
        self.opp = make(title="Paid bounty")
        today = timezone.localdate()
        Earning.objects.create(opportunity=self.opp, date=today, amount=Decimal("200"), currency="USDC",
                               usd_value_at_receipt=Decimal("199.80"))
        Earning.objects.create(date=today.replace(day=1) - timedelta(days=40), amount=Decimal("0.01"),
                               currency="ETH", usd_value_at_receipt=Decimal("31.25"))
        TimeLog.objects.create(opportunity=self.opp, minutes=150)

    def test_totals_and_tiles(self):
        response = self.client.get(reverse("albright_crypto_app:earnings"))
        self.assertContains(response, '<span class="ledger-stat__value num" id="earnings-total"', html=False)
        self.assertContains(response, "$231.05")
        tiles = self.client.get(reverse("albright_crypto_app:earnings"), {"type": "crypto"}).context["tiles"]
        self.assertEqual(tiles["earned_all_time"], Decimal("231.05"))
        self.assertEqual(tiles["earned_this_month"], Decimal("199.80"))
        self.assertEqual(tiles["hours_logged"], Decimal("2.5"))
        self.assertEqual(tiles["realized_per_hour"], Decimal("92.42"))

    def test_csv_export_for_taxes(self):
        response = self.client.get(reverse("albright_crypto_app:earnings"), {"export": "csv"})

        self.assertEqual(response["Content-Type"], "text/csv")
        rows = list(csv.reader(io.StringIO(response.content.decode())))
        self.assertEqual(rows[0][:4], ["date", "amount", "currency", "usd_value_at_receipt"])
        self.assertEqual([r[1:4] for r in rows[1:]], [["0.01", "ETH", "31.25"], ["200", "USDC", "199.80"]])

    def test_tx_link_must_be_allowlisted(self):
        earning = Earning(amount=Decimal("1"), currency="ETH", usd_value_at_receipt=Decimal("3000"),
                          tx_link="https://etherscan-claim.io/tx/0xabc")
        with self.assertRaises(ValidationError):
            earning.full_clean()
        earning.tx_link = "https://etherscan.io/tx/0xabc"
        earning.full_clean()

    def test_record_earning_from_detail_page(self):
        response = self.client.post(reverse("albright_crypto_app:opportunity", args=[self.opp.pk]), {
            "action": "earning", "date": timezone.localdate().isoformat(), "amount": "50", "currency": "USDC",
            "usd_value_at_receipt": "50.00", "tx_link": "", "note": "second payout"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.opp.earnings.count(), 2)


@override_settings(CRYPTO=CFG)
class AlertTests(TestCase):
    def qualifying(self, **overrides):
        facts = dict(reward_usd=Decimal("200"), reward_type="bounty", hours_estimate=Decimal("4"), evidence="explicit",
                     evidence_quote="Bounty: $200 in USDC")
        facts.update(overrides)
        return make(**facts)

    @mock.patch(TELEGRAM)
    def test_alert_once_for_a_qualifying_opportunity(self, send):
        opp = self.qualifying(title="Great bounty")

        self.assertEqual(send_new_opportunity_alerts([opp]), [opp.pk])
        self.assertEqual(send_new_opportunity_alerts([Opportunity.objects.get(pk=opp.pk)]), [])
        send.assert_called_once()
        message = send.call_args.args[0]
        self.assertIn("Great bounty", message)
        self.assertIn("EV/hour $32.50", message)
        self.assertIn(reverse("albright_crypto_app:opportunity", args=[opp.pk]), message)

    @mock.patch(TELEGRAM)
    def test_no_alert_for_blocked_excluded_low_confidence_or_low_ev(self, send):
        blocked = make(title="Scam", text="Send 0.1 ETH to unlock your reward", reward_usd=Decimal("500"),
                       reward_type="bounty", hours_estimate=Decimal("1"), evidence="explicit")
        excluded = self.qualifying(title="Spend", requires_spending=True)
        low_conf = make(title="Vague", reward_type="bounty", hours_estimate=Decimal("1"))
        low_ev = self.qualifying(title="Slow", hours_estimate=Decimal("40"))

        self.assertEqual(send_new_opportunity_alerts([blocked, excluded, low_conf, low_ev]), [])
        send.assert_not_called()

    @mock.patch(TELEGRAM)
    def test_unverified_listing_is_not_linked_directly_in_alerts(self, send):
        opp = self.qualifying(title="Off-list", url="https://someproject.dev/bounty")
        Opportunity.objects.filter(pk=opp.pk).update(confidence="medium", ev_per_hour=Decimal("20"),
                                                     safety_verdict="warn")
        send_new_opportunity_alerts([Opportunity.objects.get(pk=opp.pk)])
        self.assertNotIn("someproject.dev", send.call_args.args[0])

    @mock.patch(TELEGRAM)
    def test_deadline_reminder_24h_before_for_working_only(self, send):
        now = timezone.now()
        doing = make(title="Due soon", deadline=now + timedelta(hours=20))
        Opportunity.objects.filter(pk=doing.pk).update(status="working")
        make(title="Not doing", deadline=now + timedelta(hours=20))
        later = make(title="Due later", deadline=now + timedelta(hours=40))
        Opportunity.objects.filter(pk=later.pk).update(status="working")

        self.assertEqual(send_deadline_reminders(now=now), [doing.pk])
        self.assertEqual(send_deadline_reminders(now=now), [])
        self.assertIn("Due soon", send.call_args.args[0])

    def test_digest_section(self):
        now = timezone.now()
        self.qualifying(title="Fresh bounty")
        make(title="Blocked one", text="Enter your seed phrase to claim")
        due = make(title="Due this week", deadline=now + timedelta(days=3))
        Opportunity.objects.filter(pk=due.pk).update(status="working")
        Earning.objects.create(amount=Decimal("25"), currency="USDC", usd_value_at_receipt=Decimal("25"))

        text = "\n".join(digest_lines(now=now))

        self.assertIn("<b>Opportunities</b>", text)
        self.assertIn("Crypto: 2 new (24h), 1 blocked", text)
        self.assertIn("Income: 0 new (24h), 0 blocked", text)
        self.assertIn("Fresh bounty", text)
        self.assertIn("Due this week", text)
        self.assertIn("Earned this week (confirmed): $25.00", text)

    def test_digest_section_is_in_the_daily_digest(self):
        from albright_reselling_app.scanner.alerts import build_digest_message
        self.assertIn("<b>Opportunities</b>", build_digest_message())
