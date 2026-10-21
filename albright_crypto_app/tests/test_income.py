"""The Income opportunity type alongside Crypto: type separation in lists,
tiles, the All overview and the earnings ledger/CSV; skill_match and the
default hide of low-match freelance work; refundable deposits (warned,
never excluded); income AI extraction (mocked OpenAI); income sources not
running until their terms are checked; the JSON API collector; and alerts."""
import csv
import io
from decimal import Decimal
from unittest import mock

from django.conf import settings
from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from albright_crypto_app.alerts import digest_lines, send_new_opportunity_alerts
from albright_crypto_app.collectors.json_api import JsonApiCollector
from albright_crypto_app.extraction import INCOME_SYSTEM_PROMPT, SYSTEM_PROMPT, extract, validate
from albright_crypto_app.models import Earning, Opportunity, Source, TimeLog
from albright_crypto_app.pipeline import (REFUNDABLE_DEPOSIT_REASON, add_manual, extract_and_score, manual_source,
                                          rescore, scan_sources)
from albright_crypto_app.skills import skill_match
from albright_crypto_app.tests.test_collectors import GET, NO_SLEEP, _resp
from albright_crypto_app.tests.test_extraction import fake_client
from albright_crypto_app.tests.test_pipeline import make

CFG = {**settings.CRYPTO, "allowlist": ["github.com", "jobs.example.com"], "budget_usd": 0,
       "alert_min_ev_per_hour": 15, "alert_min_confidence": "medium"}
TELEGRAM = "albright_crypto_app.alerts.send_telegram"
MY_SKILLS = settings.CRYPTO.get("skills") or ["Python", "Django", "PostgreSQL", "data pipelines", "API integrations",
                                              "web scraping", "Excel", "VBA", "Power BI", "automation",
                                              "financial applications"]

JOB_TEXT = ("Remote (US only). We need a Python/Django developer to build API integrations for our billing "
            "system. Pay: $60/hour, about 10 hours per week. Skills: Python, Django, PostgreSQL, React. "
            "Apply by 2026-11-15.")
FIXED_TEXT = "Fixed price: $800 to automate our Excel reports with VBA. Remote, worldwide."
BANK_TEXT = ("Open a checking account and make direct deposits of $500 to receive a $300 bonus. "
             "The deposits stay in your account.")


def make_income(title="Django contract", url="https://jobs.example.com/1", text=JOB_TEXT, **facts):
    source = manual_source("income")
    opp = Opportunity.objects.create(source=source, opportunity_type="income", external_id=f"{title}:{url}",
                                     title=title, url=url, raw_text=text, extraction_status="done", **facts)
    return rescore(opp)


def freelance(title, skills, **facts):
    defaults = dict(reward_usd=Decimal("600"), reward_type="freelance", pay_type="hourly",
                    pay_rate_usd=Decimal("60"), hours_estimate=Decimal("10"), evidence="explicit",
                    evidence_quote="Pay: $60/hour", skills_required=skills)
    defaults.update(facts)
    return make_income(title=title, url=f"https://jobs.example.com/{title.replace(' ', '-')}", **defaults)


class SkillMatchTests(TestCase):
    def test_share_of_required_skills_i_have(self):
        self.assertEqual(skill_match(["Python", "Django", "PostgreSQL", "React"], MY_SKILLS), 75)
        self.assertEqual(skill_match(["React", "Vue"], MY_SKILLS), 0)
        self.assertEqual(skill_match(["python", "Postgres", "power bi", "API integration"], MY_SKILLS), 100)
        self.assertEqual(skill_match(["Python", "python", "Rust"], MY_SKILLS), 50)  # duplicates count once
        self.assertIsNone(skill_match([], MY_SKILLS))
        self.assertIsNone(skill_match(None, MY_SKILLS))

    @override_settings(CRYPTO=CFG)
    def test_pipeline_sets_skill_match_for_income_only(self):
        self.assertEqual(freelance("Good fit", ["Python", "Django", "PostgreSQL", "React"]).skill_match, 75)
        self.assertIsNone(make(title="Crypto one").skill_match)

    @override_settings(CRYPTO=CFG)
    def test_changing_my_skills_updates_matches(self):
        opp = freelance("React job", ["React", "TypeScript"])
        self.assertEqual(opp.skill_match, 0)
        self.client.force_login(User.objects.create_user(username="tester", password="pw-not-real-12345"))
        response = self.client.post(reverse("albright_crypto_app:settings"), {
            "action": "settings", "allowlist": "github.com\njobs.example.com", "rss_feeds": "",
            "skills": "Python\nReact"})
        self.assertEqual(response.status_code, 302)
        opp.refresh_from_db()
        self.assertEqual(opp.skill_match, 50)


@override_settings(CRYPTO=CFG)
class TypeSeparationTests(TestCase):
    def setUp(self):
        self.client.force_login(User.objects.create_user(username="tester", password="pw-not-real-12345"))
        self.crypto = make(title="Crypto bounty", reward_usd=Decimal("200"), reward_type="bounty",
                           hours_estimate=Decimal("4"), evidence="explicit", evidence_quote="Bounty: $200 in USDC")
        self.income = freelance("Django contract", ["Python", "Django"])
        Earning.objects.create(opportunity=self.crypto, amount=Decimal("200"), currency="USDC",
                               usd_value_at_receipt=Decimal("200"))
        Earning.objects.create(opportunity=self.income, amount=Decimal("300"), currency="USD",
                               usd_value_at_receipt=Decimal("300"))
        TimeLog.objects.create(opportunity=self.crypto, minutes=120)
        TimeLog.objects.create(opportunity=self.income, minutes=600)

    def titles(self, name, **params):
        content = self.client.get(reverse(f"albright_crypto_app:{name}"), params).content.decode()
        table = content.split('id="crypto-table"')[1].split("</table>")[0] if 'id="crypto-table"' in content else ""
        return [o.title for o in Opportunity.objects.all() if f">{o.title}</a>" in table]

    def test_each_dashboard_lists_only_its_type(self):
        self.assertEqual(self.titles("crypto"), ["Crypto bounty"])
        self.assertEqual(self.titles("income"), ["Django contract"])

    def test_tab_counts_and_reward_type_filters_are_per_type(self):
        crypto = self.client.get(reverse("albright_crypto_app:crypto")).context
        income = self.client.get(reverse("albright_crypto_app:income")).context
        self.assertEqual(dict((k, c) for k, _l, c in crypto["tabs"])["ranked"], 1)
        self.assertEqual(dict((k, c) for k, _l, c in income["tabs"])["ranked"], 1)
        self.assertIn("testnet", dict(crypto["reward_types"]))
        self.assertNotIn("freelance", dict(crypto["reward_types"]))
        self.assertEqual([k for k, _l in income["reward_types"]],
                         ["freelance", "bounty", "research_study", "competition", "bonus", "settlement", "unknown"])

    def test_tiles_are_per_type(self):
        crypto = self.client.get(reverse("albright_crypto_app:crypto")).context["tiles"]
        income = self.client.get(reverse("albright_crypto_app:income")).context["tiles"]
        self.assertEqual((crypto["earned_all_time"], crypto["hours_logged"], crypto["realized_per_hour"]),
                         (Decimal("200"), Decimal("2"), Decimal("100.00")))
        self.assertEqual((income["earned_all_time"], income["hours_logged"], income["realized_per_hour"]),
                         (Decimal("300"), Decimal("10"), Decimal("30.00")))

    def test_all_overview_combines_and_splits_by_type(self):
        context = self.client.get(reverse("albright_crypto_app:dashboard")).context
        self.assertEqual((context["tiles"]["earned_all_time"], context["tiles"]["hours_logged"],
                          context["tiles"]["realized_per_hour"]), (Decimal("500"), Decimal("12"), Decimal("41.67")))
        by_type = {t["key"]: t["tiles"] for t in context["by_type"]}
        self.assertEqual(by_type["crypto"]["earned_all_time"], Decimal("200"))
        self.assertEqual(by_type["income"]["realized_per_hour"], Decimal("30.00"))

    def test_earning_takes_its_opportunitys_type(self):
        self.assertEqual(list(Earning.objects.order_by("pk").values_list("opportunity_type", flat=True)),
                         ["crypto", "income"])

    def test_earnings_ledger_type_column_and_filter(self):
        content = self.client.get(reverse("albright_crypto_app:earnings")).content.decode()
        self.assertIn("<th>Type</th>", content)
        self.assertIn("$500.00", content)
        income_only = self.client.get(reverse("albright_crypto_app:earnings"), {"type": "income"})
        self.assertEqual([e.opportunity_type for e in income_only.context["earnings"]], ["income"])
        self.assertEqual(income_only.context["total_usd"], Decimal("300"))
        self.assertContains(income_only, 'href="?export=csv&amp;type=income"')

    def test_csv_has_a_type_column_and_follows_the_filter(self):
        rows = list(csv.reader(io.StringIO(self.client.get(
            reverse("albright_crypto_app:earnings"), {"export": "csv"}).content.decode())))
        self.assertEqual(rows[0][-1], "type")
        self.assertEqual(sorted(r[-1] for r in rows[1:]), ["crypto", "income"])
        rows = list(csv.reader(io.StringIO(self.client.get(
            reverse("albright_crypto_app:earnings"), {"export": "csv", "type": "crypto"}).content.decode())))
        self.assertEqual([r[-1] for r in rows[1:]], ["crypto"])

    def test_blocked_income_goes_to_its_own_blocked_tab(self):
        scam = make_income(title="Easy job", text="Great job! You must pay for training before you start.")
        self.assertEqual(scam.safety_verdict, "block")
        self.assertNotIn("Easy job", self.titles("income"))
        self.assertEqual(self.titles("income", tab="blocked"), ["Easy job"])
        self.assertEqual(self.titles("crypto", tab="blocked"), [])

    def test_manual_add_for_each_type(self):
        response = self.client.post(reverse("albright_crypto_app:add"), {
            "opportunity_type": "income", "url": "https://jobs.example.com/new", "title": "Survey study",
            "text": "Paid research study: $50 for a 30 minute interview."})
        opp = Opportunity.objects.get(title="Survey study")
        self.assertRedirects(response, reverse("albright_crypto_app:opportunity", args=[opp.pk]))
        self.assertEqual((opp.opportunity_type, opp.source.kind, opp.source.opportunity_type),
                         ("income", "manual", "income"))
        self.assertContains(self.client.get(reverse("albright_crypto_app:add"), {"type": "income"}),
                            '<option value="income" selected>')


@override_settings(CRYPTO=CFG)
class LowMatchHideTests(TestCase):
    def setUp(self):
        self.client.force_login(User.objects.create_user(username="tester", password="pw-not-real-12345"))
        self.good = freelance("Good fit", ["Python", "Django"])               # 100
        self.low = freelance("Poor fit", ["React", "Vue", "Python"])          # 33
        self.unknown = freelance("No skills listed", [])                      # None - stays visible
        self.study = make_income(title="Research study", url="https://jobs.example.com/s", reward_type="research_study",
                                 skills_required=["React"], reward_usd=Decimal("50"))

    def ranked(self, **params):
        response = self.client.get(reverse("albright_crypto_app:income"), params)
        return [o.title for o in response.context["opportunities"]], response

    def test_freelance_below_50_hidden_by_default(self):
        titles, response = self.ranked()
        self.assertNotIn("Poor fit", titles)
        self.assertEqual(set(titles), {"Good fit", "No skills listed", "Research study"})  # only freelance is hidden
        self.assertContains(response, "1 freelance opportunity below 50% skill match hidden.")
        self.assertEqual(dict((k, c) for k, _l, c in response.context["tabs"])["ranked"], 3)

    def test_toggle_shows_them(self):
        titles, response = self.ranked(show_low_match="1")
        self.assertIn("Poor fit", titles)
        self.assertContains(response, 'id="low-match-toggle">Hide low matches</a>')
        self.assertContains(self.ranked()[1], 'href="?tab=ranked&amp;show_low_match=1" id="low-match-toggle"')

    def test_exactly_50_is_shown(self):
        freelance("Half fit", ["Python", "React"])
        self.assertIn("Half fit", self.ranked()[0])

    def test_hide_is_ranked_only(self):
        Opportunity.objects.filter(pk=self.low.pk).update(status="doing")
        self.assertIn("Poor fit", self.ranked(tab="doing")[0])


@override_settings(CRYPTO=CFG)
class RefundableDepositTests(TestCase):
    def test_refundable_deposit_is_warned_not_excluded(self):
        opp = make_income(title="Bank bonus", url="https://jobs.example.com/bank", text=BANK_TEXT,
                          reward_type="bonus", reward_usd=Decimal("300"), evidence="explicit",
                          evidence_quote="to receive a $300 bonus", refundable_deposit_usd=Decimal("500"),
                          hours_estimate=Decimal("1"))
        self.assertFalse(opp.excluded)
        self.assertEqual(opp.safety_verdict, "warn")
        self.assertIn(REFUNDABLE_DEPOSIT_REASON, opp.safety_reasons)
        self.assertEqual(opp.safety_reasons.count(REFUNDABLE_DEPOSIT_REASON), 1)
        self.assertIn(opp, list(Opportunity.objects.filter(excluded=False, opportunity_type="income")))

        user = User.objects.create_user(username="tester", password="pw-not-real-12345")
        self.client.force_login(user)
        detail = self.client.get(reverse("albright_crypto_app:opportunity", args=[opp.pk]))
        self.assertContains(detail, "requires a refundable deposit")
        self.assertContains(detail, "$500.00 (you get it back - not spending)")

    def test_rescore_does_not_duplicate_the_warning(self):
        opp = make_income(text=BANK_TEXT, refundable_deposit_usd=Decimal("500"))
        rescore(opp)
        self.assertEqual(opp.safety_reasons.count(REFUNDABLE_DEPOSIT_REASON), 1)

    def test_non_refundable_costs_are_still_excluded(self):
        opp = make_income(title="Starter kit", text="Buy the $50 starter kit to begin earning $200.",
                          reward_usd=Decimal("200"), costs_usd=Decimal("50"), reward_type="bonus")
        self.assertTrue(opp.excluded)

    def test_extraction_does_not_count_a_refundable_deposit_as_spending(self):
        facts = validate({"reward_type": "bonus", "reward_usd": 300, "refundable_deposit_usd": 500,
                          "requires_spending": True, "costs_usd": 0}, BANK_TEXT, "income")
        self.assertEqual((facts["refundable_deposit_usd"], facts["requires_spending"]), (Decimal("500.00"), False))


INCOME_FACTS = {
    "reward_type": "freelance", "pay_type": "hourly", "pay_rate_usd": 60, "reward_usd": None,
    "evidence": "explicit", "evidence_quote": "Pay: $60/hour, about 10 hours per week",
    "deadline": None, "application_deadline": "2026-11-15", "hours_estimate": 10, "hours_is_estimate": False,
    "skills_required": ["Python", "Django", "PostgreSQL", "React", "python"],
    "location_requirement": "Remote (US only)", "costs_usd": 0, "refundable_deposit_usd": None,
    "requires_spending": False, "steps": [{"text": "Apply", "automatable": False}], "eligibility_notes": "US only",
}


@override_settings(CRYPTO=CFG)
class IncomeExtractionTests(TestCase):
    def setUp(self):
        self.opp = Opportunity.objects.create(source=manual_source("income"), opportunity_type="income",
                                              external_id="1", title="Django contract",
                                              url="https://jobs.example.com/1", raw_text=JOB_TEXT)

    def test_income_prompt_and_fields(self):
        client = fake_client(INCOME_FACTS)
        facts = extract(self.opp, client=client)

        self.assertEqual(client.responses.create.call_args.kwargs["instructions"], INCOME_SYSTEM_PROMPT)
        self.assertEqual((facts["pay_type"], facts["pay_rate_usd"]), ("hourly", Decimal("60.00")))
        self.assertEqual(facts["reward_usd"], Decimal("600.00"))  # rate x hours
        self.assertEqual((facts["hours_estimate"], facts["hours_is_estimate"]), (Decimal("10.00"), False))
        self.assertEqual(facts["skills_required"], ["Python", "Django", "PostgreSQL", "React"])
        self.assertEqual(facts["location_requirement"], "Remote (US only)")
        self.assertEqual(timezone.localtime(facts["application_deadline"]).date().isoformat(), "2026-11-15")
        self.assertEqual(facts["evidence"], "explicit")

    def test_pipeline_saves_income_facts_and_skill_match(self):
        extract_and_score(self.opp, client=fake_client(INCOME_FACTS))
        self.opp.refresh_from_db()
        self.assertEqual((self.opp.reward_usd, self.opp.skill_match, self.opp.pay_type),
                         (Decimal("600.00"), 75, "hourly"))
        self.assertIsNotNone(self.opp.ev_per_hour)

    def test_crypto_still_uses_its_own_prompt(self):
        source = Source.objects.create(kind="github", name="q", url="label:bounty")
        crypto = Opportunity.objects.create(source=source, external_id="c", title="Bounty",
                                            url="https://github.com/a/b/issues/1", raw_text="Bounty: $50")
        client = fake_client({"reward_type": "freelance", "pay_type": "hourly"})
        facts = extract(crypto, client=client)
        self.assertEqual(client.responses.create.call_args.kwargs["instructions"], SYSTEM_PROMPT)
        self.assertEqual(facts["reward_type"], "unknown")  # not a crypto reward type
        self.assertNotIn("pay_type", facts)

    def test_fixed_price_reward_and_hours_marked_as_estimate(self):
        facts = validate({"reward_type": "freelance", "pay_type": "fixed", "reward_usd": None, "pay_rate_usd": 800,
                          "hours_estimate": 12, "hours_is_estimate": False, "evidence": "explicit",
                          "evidence_quote": "Fixed price: $800"}, FIXED_TEXT, "income")
        self.assertEqual(facts["reward_usd"], Decimal("800.00"))
        self.assertEqual((facts["hours_estimate"], facts["hours_is_estimate"]), (Decimal("12.00"), True))

    def test_estimated_hours_only_allowed_for_fixed_price(self):
        facts = validate({"reward_type": "freelance", "pay_type": "hourly", "pay_rate_usd": 60, "hours_estimate": 20,
                          "hours_is_estimate": True}, JOB_TEXT, "income")
        self.assertEqual((facts["hours_estimate"], facts["hours_is_estimate"], facts["reward_usd"]),
                         (None, False, None))  # hourly with no stated hours -> reward unknown

    def test_same_rules_unknowns_and_quote_check(self):
        facts = validate({"reward_type": "testnet", "pay_type": "salary", "evidence": "explicit",
                          "evidence_quote": "$5,000 per week guaranteed", "skills_required": "Python"},
                         JOB_TEXT, "income")
        self.assertEqual((facts["reward_type"], facts["pay_type"], facts["evidence"], facts["evidence_quote"]),
                         ("unknown", None, "none", ""))
        self.assertEqual(facts["skills_required"], [])
        self.assertIsNone(facts["location_requirement"])


@override_settings(CRYPTO=CFG)
class IncomeSourceTests(TestCase):
    def setUp(self):
        NO_SLEEP.start()
        self.addCleanup(NO_SLEEP.stop)

    API = {"jobs": [
        {"id": 7, "url": "https://jobs.example.com/7", "title": "Python data pipeline contractor",
         "company_name": "Acme", "candidate_required_location": "USA only", "salary": "$70/hour",
         "description": "<p>Build <b>data pipelines</b> in Python.</p>", "publication_date": "2026-10-18T09:00:00"},
        {"id": 8, "title": "No link"},
    ]}

    def test_income_source_does_not_run_until_terms_are_checked(self):
        source = Source.objects.create(kind="api_income", name="Jobs API", url="https://jobs.example.com/api",
                                       opportunity_type="income")
        with mock.patch(GET, return_value=_resp(json_data=self.API)) as get:
            new, errors = scan_sources(use_ai=False)
            self.assertEqual((new, errors), ([], []))
            scan_sources(sources=[source], use_ai=False)  # even when named explicitly
            get.assert_not_called()

            source.terms_checked = True
            source.save()
            new, errors = scan_sources(use_ai=False)
        self.assertEqual(errors, [])
        self.assertEqual([o.title for o in new], ["Python data pipeline contractor"])
        self.assertEqual(new[0].opportunity_type, "income")
        get.assert_called_once()

    def test_inactive_income_source_never_runs(self):
        Source.objects.create(kind="rss_income", name="Feed", url="https://jobs.example.com/feed",
                              opportunity_type="income", terms_checked=True, active=False)
        with mock.patch(GET) as get:
            scan_sources(use_ai=False)
        get.assert_not_called()

    def test_json_api_collector_reads_items(self):
        source = Source(kind="api_income", url="https://jobs.example.com/api", opportunity_type="income")
        with mock.patch(GET, return_value=_resp(json_data=self.API)):
            items = list(JsonApiCollector().collect(source))
        self.assertEqual(len(items), 1)  # the item without a link is skipped
        item = items[0]
        self.assertEqual((item.external_id, item.url), ("api:7", "https://jobs.example.com/7"))
        self.assertIn("Build data pipelines in Python.", item.text)
        self.assertIn("Location: USA only", item.text)
        self.assertIn("Salary: $70/hour", item.text)
        self.assertEqual(item.published.year, 2026)

    def test_json_api_list_payload_skips_non_items(self):
        payload = [{"legal": "notice"}, {"id": "a1", "link": "https://jobs.example.com/a1", "position": "VBA dev"}]
        with mock.patch(GET, return_value=_resp(json_data=payload)):
            items = list(JsonApiCollector().collect(Source(kind="api_income", url="https://jobs.example.com/x")))
        self.assertEqual([i.title for i in items], ["VBA dev"])

    def test_add_income_source_on_settings_page(self):
        self.client.force_login(User.objects.create_user(username="tester", password="pw-not-real-12345"))
        response = self.client.post(reverse("albright_crypto_app:settings"), {
            "action": "add_source", "source-name": "Remote jobs feed", "source-kind": "rss_income",
            "source-url": "https://jobs.example.com/remote.rss", "source-notes": "Terms: feed may be polled hourly"})
        self.assertEqual(response.status_code, 302)
        source = Source.objects.get(name="Remote jobs feed")
        self.assertEqual((source.kind, source.opportunity_type, source.terms_checked, source.runnable),
                         ("rss_income", "income", False, False))

        page = self.client.get(reverse("albright_crypto_app:settings"))
        self.assertContains(page, "It won't run until you tick it.")
        self.assertContains(page, '<option value="api_income">API (income)</option>')
        self.assertNotContains(page, '<option value="github">')  # only income kinds can be added here

        duplicate = self.client.post(reverse("albright_crypto_app:settings"), {
            "action": "add_source", "source-name": "Again", "source-kind": "rss_income",
            "source-url": "https://jobs.example.com/remote.rss"})
        self.assertContains(duplicate, "already in your list")

        self.client.post(reverse("albright_crypto_app:settings"), {
            "action": "sources", f"active_{source.pk}": "on", f"terms_{source.pk}": "on"})
        source.refresh_from_db()
        self.assertTrue(source.runnable)

    def test_add_source_rejects_non_web_urls(self):
        self.client.force_login(User.objects.create_user(username="tester", password="pw-not-real-12345"))
        response = self.client.post(reverse("albright_crypto_app:settings"), {
            "action": "add_source", "source-name": "Bad", "source-kind": "api_income", "source-url": "ftp://x.example/a"})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Source.objects.filter(name="Bad").exists())


@override_settings(CRYPTO=CFG)
class IncomeAlertTests(TestCase):
    @mock.patch(TELEGRAM)
    def test_freelance_alert_needs_skill_match_60(self, send):
        good = freelance("Good fit", ["Python", "Django", "PostgreSQL", "React"])   # 75
        weak = freelance("Weak fit", ["Python", "React", "Vue", "Go", "Rust"])     # 20
        border = freelance("Border fit", ["Python", "Django", "React", "Vue", "Excel"])  # 60
        self.assertGreaterEqual(good.ev_per_hour, 15)
        self.assertIn(good.confidence, ("medium", "high"))

        sent = send_new_opportunity_alerts([good, weak, border])

        self.assertEqual(sorted(sent), sorted([good.pk, border.pk]))
        message = send.call_args_list[0].args[0]
        self.assertIn("<b>Income: new opportunity</b>", message)
        self.assertIn("Hourly $60.00/h", message)
        self.assertIn("skill match 75%", message)

    @mock.patch(TELEGRAM)
    def test_non_freelance_income_uses_the_usual_thresholds(self, send):
        study = make_income(title="Paid study", url="https://jobs.example.com/study",
                            text="Paid research study: $100 for a one hour interview.", reward_type="research_study",
                            reward_usd=Decimal("100"), hours_estimate=Decimal("1"), evidence="explicit",
                            evidence_quote="$100 for a one hour interview")
        self.assertEqual(send_new_opportunity_alerts([study]), [study.pk])

    def test_digest_has_crypto_and_income_lines(self):
        make(title="Crypto thing")
        freelance("Great Django gig", ["Python", "Django"])
        freelance("Poor fit gig", ["React", "Vue"])
        text = "\n".join(digest_lines())
        self.assertIn("<b>Opportunities</b>", text)
        self.assertIn("Crypto: 1 new (24h)", text)
        self.assertIn("Income: 2 new (24h)", text)
        self.assertIn("Great Django gig", text)
        self.assertNotIn("Poor fit gig", text)  # below the freelance skill-match cutoff


@override_settings(CRYPTO=CFG)
class IncomeDetailAndEditTests(TestCase):
    def setUp(self):
        self.client.force_login(User.objects.create_user(username="tester", password="pw-not-real-12345"))
        self.opp = freelance("Django contract", ["Python", "React"])

    def test_detail_shows_income_facts(self):
        response = self.client.get(reverse("albright_crypto_app:opportunity", args=[self.opp.pk]))
        self.assertContains(response, 'id="skills-required"')
        self.assertContains(response, "50% match")
        self.assertContains(response, f'href="{reverse("albright_crypto_app:income")}"')
        self.assertContains(response, "Income opportunities")

    def test_edit_limits_reward_types_and_rescores_skill_match(self):
        response = self.client.get(reverse("albright_crypto_app:opportunity", args=[self.opp.pk]))
        choices = [k for k, _l in response.context["edit_form"].fields["reward_type"].choices]
        self.assertIn("freelance", choices)
        self.assertNotIn("testnet", choices)

        response = self.client.post(reverse("albright_crypto_app:opportunity", args=[self.opp.pk]), {
            "action": "edit", "reward_usd": "600", "reward_currency": "USD", "reward_type": "freelance",
            "evidence": "explicit", "evidence_quote": "Pay: $60/hour", "hours_estimate": "10", "costs_usd": "0",
            "pay_type": "hourly", "pay_rate_usd": "60", "skills": "Python, Django", "location_requirement": "Remote",
            "eligibility_notes": "", "notes": ""})
        self.assertEqual(response.status_code, 302)
        self.opp.refresh_from_db()
        self.assertEqual((self.opp.skills_required, self.opp.skill_match), (["Python", "Django"], 100))

    def test_crypto_edit_form_has_no_income_fields(self):
        crypto = make(title="Crypto one")
        form = self.client.get(reverse("albright_crypto_app:opportunity", args=[crypto.pk])).context["edit_form"]
        self.assertNotIn("pay_type", form.fields)
        self.assertNotIn("skills", form.fields)
