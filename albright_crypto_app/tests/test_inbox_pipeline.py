"""The Inbox / Pipeline workflow: My profile's prefill from the resume; each
Settings filter (work type, location, pay floors, seniority, negative
keywords) sending items to Hidden with the reason; the AI fit score (mocked
OpenAI) and its 60 threshold, keyword fit for job boards that keep their jobs
out of the AI; Inbox order and every keyboard action's endpoint; Pipeline
moves, the applied date and the 7-day follow-up; the status-mapping
migration; duplicate merging and auto-expiry; earnings by type; and the
default job sources."""
import importlib
import json
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.apps import apps as django_apps
from django.conf import settings
from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from albright_crypto_app import config, relevance, workflow
from albright_crypto_app.alerts import send_followup_reminders, send_new_opportunity_alerts
from albright_crypto_app.models import (AIExtraction, ApplicationDraft, Earning, MyProfile, Opportunity, Source,
                                        TimeLog, Walkthrough)
from albright_crypto_app.my_profile import RESUME_PREFILL, check_text, prefill_profile, profile_text
from albright_crypto_app.pipeline import (add_manual, extract_and_score, rescore, store, sync_default_income_sources)
from albright_crypto_app.collectors.base import RawOpportunity
from albright_crypto_app.tests.test_extraction import fake_client

CFG = {**settings.CRYPTO, "allowlist": ["github.com", "jobs.example.com"], "budget_usd": 0, "ai_daily_limit": 30}
TELEGRAM = "albright_crypto_app.alerts.send_telegram"
JOB_TEXT = "We need SQL, Python and Power BI for executive dashboards. Remote within the US."


def board(kind="rss_income", url="https://jobs.example.com/feed", name="Job feed", terms=True):
    source, _ = Source.objects.get_or_create(kind=kind, url=url, opportunity_type="income",
                                             defaults={"name": name, "terms_checked": terms})
    return source


_counter = iter(range(1, 10 ** 6))


def job(title="Data Analyst", source=None, location="Remote (US)", text=JOB_TEXT, score=True, **fields):
    """An income job posting, facts already extracted, safety-checked and prefiltered (no fit yet)."""
    source = source or board()
    n = next(_counter)
    fields.setdefault("extraction_status", "done")
    opp = Opportunity.objects.create(
        source=source, opportunity_type="income", external_id=f"job-{n}", url=f"https://jobs.example.com/{n}",
        title=title, raw_text=text, location_requirement=location, **fields)
    return rescore(opp) if score else opp


def fit_reply(score, **extra):
    return {"fit_score": score, "matched_skills": ["SQL", "Python", "Power BI", "Rust"],
            "missing_requirements": ["Snowflake"], "role_match": "Data Analyst", "seniority": "mid",
            "company": "Acme", "why_it_fits": "A data analyst role built on SQL and Power BI.", **extra}


class ProfileMixin:
    def setUp(self):
        super().setUp()
        self.profile = MyProfile.load()
        prefill_profile(self.profile)


# ---------------------------------------------------------------------------
# Profile prefill
# ---------------------------------------------------------------------------

class ProfilePrefillTests(TestCase):
    def test_prefill_fills_every_resume_field(self):
        profile = MyProfile.load()
        changed = prefill_profile(profile)
        profile.refresh_from_db()
        self.assertEqual(set(changed), set(RESUME_PREFILL))
        self.assertEqual(len(profile.target_roles), 13)
        self.assertIn("Power BI Developer", profile.target_roles)
        self.assertIn("Python/Django Developer (freelance/contract)", profile.target_roles)
        self.assertEqual(profile.skills[:3], ["SQL", "Python", "Django"])
        self.assertIn("Salesforce (Certified Administrator)", profile.skills)
        self.assertEqual(len(profile.skills), 21)
        self.assertEqual(profile.years_experience, "8+")
        self.assertEqual(profile.industries, ["insurance/warranty", "healthcare/specialty pharma", "fintech (own project)"])
        self.assertEqual(profile.education, [{"school": "Illinois State University", "degree": "B.S. Finance", "year": "2018"}])
        self.assertEqual(profile.location, "Tampa, FL")

    def test_prefill_never_overwrites_and_keeps_my_own_skills(self):
        profile = MyProfile.load()
        profile.location, profile.skills = "Orlando, FL", ["Excel", "python"]
        profile.save()
        changed = prefill_profile(profile)
        profile.refresh_from_db()
        self.assertNotIn("location", changed)
        self.assertEqual(profile.location, "Orlando, FL")
        self.assertEqual(profile.skills[:2], ["Excel", "python"])
        self.assertNotIn("Python", profile.skills)          # already there as "python"
        self.assertIn("Tableau", profile.skills)
        self.assertEqual(prefill_profile(profile), [])        # running again changes nothing

    def test_profile_page_prefill_button_and_new_fields_are_editable(self):
        self.client.force_login(User.objects.create_user(username="tester", password="pw-not-real-12345"))
        self.client.post(reverse("albright_crypto_app:profile"), {"action": "prefill"})
        page = self.client.get(reverse("albright_crypto_app:profile")).content.decode()
        self.assertIn("Business Systems Analyst", page)
        self.assertIn('value="8+"', page)
        response = self.client.post(reverse("albright_crypto_app:profile"), {
            "target_roles": "Data Analyst\nAnalytics Engineer", "skills": "SQL, dbt", "years_experience": "9",
            "industries": "insurance", "location": "Tampa, FL", "work_history": "", "projects": "", "education": "",
            "certifications": "", "links": "", "resume_template": "{summary}", "proposal_template": "{proposal_body}"})
        self.assertEqual(response.status_code, 302)
        profile = MyProfile.load()
        self.assertEqual(profile.target_roles, ["Data Analyst", "Analytics Engineer"])
        self.assertEqual((profile.skills, profile.years_experience, profile.industries), (["SQL", "dbt"], "9", ["insurance"]))

    def test_drafts_may_use_profile_facts_but_never_target_roles_as_experience(self):
        profile = MyProfile.load()
        prefill_profile(profile)
        facts = profile_text(profile)
        self.assertIn("8+ years of experience", facts)
        self.assertIn("insurance/warranty", facts)
        self.assertNotIn("Data Product Manager", facts)
        kept, removed = check_text("I worked as a Data Product Manager at Globex.", profile, "We need an analyst.")
        self.assertEqual(kept, "")
        self.assertTrue(removed)

    def test_skill_match_follows_the_profile(self):
        profile = MyProfile.load()
        profile.skills = ["Python", "Salesforce (Certified Administrator)"]
        profile.save()
        self.assertEqual(config.skills(), ["Python", "Salesforce (Certified Administrator)"])
        opp = job(skills_required=["Python", "Salesforce", "Java"])
        self.assertEqual(opp.skill_match, 67)


# ---------------------------------------------------------------------------
# Filters (stage 1 - free)
# ---------------------------------------------------------------------------

@override_settings(CRYPTO=CFG)
class FilterTests(TestCase):
    def set_filters(self, **changes):
        config.set_value("filters", {**config.FILTER_DEFAULTS, **changes})

    def test_defaults(self):
        flt = config.filters()
        self.assertEqual(flt["work_types"], ["full_time", "contract", "freelance", "part_time", "bounty", "paid_task"])
        self.assertEqual((flt["home"], flt["radius_miles"]), ("Tampa, FL", 30))
        self.assertEqual((flt["min_salary_year"], flt["min_rate_hour"], flt["min_fixed_project"]), (70000, 40, 500))
        self.assertTrue(flt["exclude_entry"] and flt["exclude_executive"] and flt["remote_us"] and flt["local"])
        self.assertIn("clearance required", flt["negative_keywords"])
        self.assertEqual(config.fit_min_score(), 60)

    def test_work_type_switched_off(self):
        self.set_filters(work_types=["full_time", "contract", "freelance", "bounty", "paid_task"])
        self.assertEqual(job("Part-time Data Analyst").hidden_reason, "work type: Part-time (switched off in Filters)")
        self.assertEqual(job("Data Analyst").hidden_reason, "")
        self.assertEqual(job("Data Analyst").work_type, "full_time")
        self.assertEqual(job("Contract Power BI Developer").work_type, "contract")

    def test_location(self):
        cases = {
            "UK only": "location: UK only",
            "Remote - Europe": "location: Europe only",
            "Remote (Canada)": "location: Canada only",
            "Remote (US)": "",
            "Remote - US or Canada": "",
            "Worldwide": "",
            "Hybrid - St. Petersburg, FL": "",               # ~20 miles from Tampa
            "On-site, Brandon, FL": "",
            "On-site, Orlando, FL": "location: on-site in On-site, Orlando, FL (not within 30 miles of Tampa, FL)",
            "Austin, TX": "location: on-site in Austin, TX (not within 30 miles of Tampa, FL)",
            "Onsite NYC": "location: on-site in Onsite NYC (not within 30 miles of Tampa, FL)",
            "Remote (US) or hybrid in NYC": "",
            "": "",                                             # not stated: left for me to judge
        }
        for location, reason in cases.items():
            with self.subTest(location=location):
                self.assertEqual(job(f"Data Analyst {location}", location=location).hidden_reason, reason)

    def test_location_switches_and_radius(self):
        self.set_filters(remote_us=False)
        self.assertEqual(job(location="Remote (US)").hidden_reason, "location: remote (switched off in Filters)")
        self.set_filters(local=False)
        self.assertEqual(job("BI Analyst", location="Tampa, FL").hidden_reason,
                         "location: on-site / hybrid (switched off in Filters)")
        self.assertAlmostEqual(relevance.miles_between("tampa", "orlando"), 78, delta=5)
        self.set_filters(radius_miles=60)
        self.assertIn("not within 60 miles", job("Reporting Analyst", location="Orlando, FL").hidden_reason)
        self.set_filters(radius_miles=90)
        self.assertEqual(job("Reporting Analyst", location="Orlando, FL").hidden_reason, "")

    def test_pay_floors_only_when_stated(self):
        cases = [
            ("Data Analyst", {"metadata": {"salary": "$55,000 - $65,000 per year"}},
             "pay below floor: $65,000/year (floor $70,000/year)"),
            ("Data Analyst II", {"metadata": {"salary": "$60k–$90k"}}, ""),           # top of the range clears it
            ("Data Analyst III", {"metadata": {"salary": "$30/hr"}},
             "pay below floor: $30/hour (floor $70,000/year)"),                        # full-time: $62,400/yr
            ("Contract Data Engineer", {"pay_type": "hourly", "pay_rate_usd": Decimal("35")},
             "pay below floor: $35/hour (floor $40/hour)"),
            ("Contract Analytics Engineer", {"pay_type": "hourly", "pay_rate_usd": Decimal("40")}, ""),
            ("Freelance Power BI report", {"pay_type": "fixed", "pay_rate_usd": Decimal("300")},
             "pay below floor: $300 fixed (floor $500)"),
            ("Freelance Tableau dashboard", {"pay_type": "fixed", "pay_rate_usd": Decimal("750")}, ""),
            ("Reporting Analyst", {}, ""),                                              # pay not stated
        ]
        for title, fields, reason in cases:
            with self.subTest(title=title):
                self.assertEqual(job(title, **fields).hidden_reason, reason)

    def test_seniority(self):
        self.assertEqual(job("Data Analyst Intern").hidden_reason, "seniority: intern / entry-level")
        self.assertEqual(job("Entry-Level Reporting Analyst").hidden_reason, "seniority: intern / entry-level")
        self.assertEqual(job("VP of Data").hidden_reason, "seniority: VP / C-level")
        self.assertEqual(job("Chief Data Officer").hidden_reason, "seniority: VP / C-level")
        self.assertEqual(job("Senior Data Analyst").hidden_reason, "")
        self.set_filters(exclude_entry=False, exclude_executive=False)
        self.assertEqual(job("Data Analyst Intern 2").hidden_reason, "")

    def test_negative_keywords(self):
        self.assertEqual(job("Registered Nurse").hidden_reason, "negative keyword: nurse")
        self.assertEqual(job("Senior Java Developer").hidden_reason, "negative keyword: Java developer")
        self.assertEqual(job("iOS Engineer").hidden_reason, "negative keyword: iOS")
        self.assertEqual(job("Data Analyst", text="Security clearance required. SQL.").hidden_reason,
                         "negative keyword: clearance required")
        # A single word only counts in the title: "a key driver of growth" isn't a driving job.
        self.assertEqual(job("BI Developer", text="Dashboards are a key driver of growth.").hidden_reason, "")
        self.set_filters(negative_keywords=["tableau"])
        self.assertEqual(job("Tableau Developer").hidden_reason, "negative keyword: tableau")

    def test_bounties_and_crypto_skip_the_job_filters(self):
        bounty = job("Fix the parser", location="UK only", reward_type="bounty", reward_usd=Decimal("50"))
        self.assertEqual((bounty.work_type, bounty.hidden_reason), ("bounty", ""))
        task = job("Paid survey for nurses", reward_type="paid_task")
        self.assertEqual((task.work_type, task.hidden_reason), ("paid_task", ""))   # "nurses" isn't "nurse"
        self.set_filters(work_types=["full_time"])
        self.assertEqual(rescore(bounty).hidden_reason, "work type: Bounty (switched off in Filters)")

    def test_hidden_page_shows_reasons_and_blocked_items(self):
        self.client.force_login(User.objects.create_user(username="tester", password="pw-not-real-12345"))
        nurse = job("Registered Nurse")
        uk = job("Data Analyst UK", location="UK only")
        scam = job("Easy money", text="Send us your seed phrase to get paid. Telegram only.")
        visible = job("Data Analyst")
        self.assertTrue(scam.is_blocked)
        page = self.client.get(reverse("albright_crypto_app:hidden")).content.decode()
        for opp in (nurse, uk, scam):
            self.assertIn(f'data-opportunity-id="{opp.pk}"', page)
        self.assertNotIn(f'data-opportunity-id="{visible.pk}"', page)
        self.assertIn("negative keyword: nurse", page)
        self.assertIn("location: UK only", page)
        self.assertIn("blocked: ", page)
        inbox = self.client.get(reverse("albright_crypto_app:inbox")).content.decode()
        self.assertIn(f'Hidden (3)</a>', inbox)

    def test_show_in_inbox_overrides_filters_but_never_the_safety_block(self):
        self.client.force_login(User.objects.create_user(username="tester", password="pw-not-real-12345"))
        nurse = job("Registered Nurse")
        scam = job("Easy money", text="Send us your seed phrase to get paid.")
        self.client.post(reverse("albright_crypto_app:show_in_inbox", args=[nurse.pk]))
        self.client.post(reverse("albright_crypto_app:show_in_inbox", args=[scam.pk]))
        nurse.refresh_from_db()
        scam.refresh_from_db()
        self.assertEqual((nurse.show_anyway, nurse.hidden_reason), (True, ""))
        self.assertFalse(scam.show_anyway)
        self.assertEqual(rescore(nurse).hidden_reason, "")

    def test_saving_filters_resorts_the_inbox(self):
        self.client.force_login(User.objects.create_user(username="tester", password="pw-not-real-12345"))
        analyst = job("Tableau Developer")
        self.assertEqual(analyst.hidden_reason, "")
        form = {**config.FILTER_DEFAULTS, "negative_keywords": "nurse\ntableau", "work_types": ["full_time", "contract"]}
        form = {k: v for k, v in form.items() if v is not False}
        response = self.client.post(reverse("albright_crypto_app:filters"), form)
        self.assertEqual(response.status_code, 302)
        analyst.refresh_from_db()
        self.assertEqual(analyst.hidden_reason, "negative keyword: tableau")
        self.assertEqual(config.filters()["negative_keywords"], ["nurse", "tableau"])

    def test_manual_adds_are_never_filtered(self):
        opp, _ = add_manual("https://jobs.example.com/rn", "Registered Nurse", "A nursing job.", use_ai=False,
                            opportunity_type="income")
        self.assertEqual(opp.hidden_reason, "")
        self.assertTrue(opp.show_anyway)


# ---------------------------------------------------------------------------
# Fit score (stage 2)
# ---------------------------------------------------------------------------

@override_settings(CRYPTO=CFG)
class FitTests(ProfileMixin, TestCase):
    def test_ai_fit_is_stored_and_shown(self):
        opp = job(score=False)
        client = fake_client(fit_reply(85))
        extract_and_score(opp, client=client)
        opp.refresh_from_db()
        self.assertEqual((opp.fit_score, opp.fit_method, opp.role_match, opp.seniority), (85, "ai", "Data Analyst", "mid"))
        self.assertEqual(opp.matched_skills, ["SQL", "Python", "Power BI"])     # "Rust" isn't in my profile - dropped
        self.assertEqual(opp.missing_requirements, ["Snowflake"])
        self.assertEqual(opp.fit_reason, "A data analyst role built on SQL and Power BI.")
        self.assertEqual(opp.company, "Acme")
        self.assertEqual(opp.hidden_reason, "")
        self.assertEqual(AIExtraction.objects.get().purpose, "fit")
        sent = client.responses.create.call_args.kwargs
        self.assertIn("TARGET ROLES: Data Analyst; Business Intelligence Analyst/Developer", sent["input"])
        self.assertIn("Remote (US)", sent["input"])
        self.client.force_login(User.objects.create_user(username="tester", password="pw-not-real-12345"))
        page = self.client.get(reverse("albright_crypto_app:inbox")).content.decode()
        data = json.loads(page.split('<script id="inbox-data" type="application/json">')[1].split("</script>")[0])
        self.assertEqual(data[0]["matched_skills"], ["SQL", "Python", "Power BI"])
        self.assertEqual(data[0]["fit_reason"], "A data analyst role built on SQL and Power BI.")

    def test_threshold_is_60(self):
        for score, reason in ((60, ""), (59, "low fit (59/100)"), (20, "low fit (20/100)")):
            with self.subTest(score=score):
                opp = job(f"Data Analyst {score}", score=False)
                extract_and_score(opp, client=fake_client(fit_reply(score)))
                opp.refresh_from_db()
                self.assertEqual(opp.hidden_reason, reason)

    def test_filtered_out_postings_never_reach_the_ai(self):
        opp = job("Registered Nurse", score=False, extraction_status="pending")
        client = fake_client(fit_reply(90))
        extract_and_score(opp, client=client)
        client.responses.create.assert_not_called()
        opp.refresh_from_db()
        self.assertEqual((opp.extraction_status, opp.fit_score, opp.hidden_reason),
                         ("filtered", None, "negative keyword: nurse"))

    def test_bounties_keep_ev_per_hour_and_get_no_fit(self):
        opp = job("CSV bounty", score=False, reward_type="bounty", reward_usd=Decimal("200"),
                  hours_estimate=Decimal("4"), evidence="explicit", evidence_quote="We need SQL")
        client = fake_client(fit_reply(90))
        extract_and_score(opp, client=client)
        client.responses.create.assert_not_called()
        opp.refresh_from_db()
        self.assertIsNone(opp.fit_score)
        self.assertIsNotNone(opp.ev_per_hour)

    def test_job_boards_that_keep_jobs_in_app_get_keyword_fit_without_ai(self):
        remotive = board(kind="remotive", url="https://remotive.com/api/remote-jobs?category=data", name="Remotive")
        good = job("Senior Power BI Developer", source=remotive, score=False, skills_required=["Power BI", "SQL", "DAX"])
        poor = job("Marketing Coordinator", source=remotive, score=False, text="Run our social media.")
        client = fake_client(fit_reply(99))
        extract_and_score(good, client=client)
        extract_and_score(poor, client=client)
        client.responses.create.assert_not_called()
        good.refresh_from_db()
        poor.refresh_from_db()
        self.assertEqual((good.fit_method, good.role_match), ("keywords", "Power BI Developer"))
        self.assertGreaterEqual(good.fit_score, 60)
        self.assertIn("SQL", good.matched_skills)
        self.assertEqual(good.missing_requirements, ["DAX"])
        self.assertEqual(good.seniority, "senior")
        self.assertEqual((poor.fit_score, poor.hidden_reason), (0, "low fit (0/100)"))

    def test_ai_limit_leaves_fit_pending_then_a_later_scan_scores_it(self):
        opp = job(score=False)
        with override_settings(CRYPTO={**CFG, "ai_daily_limit": 0}):
            extract_and_score(opp, client=fake_client(fit_reply(80)))
        opp.refresh_from_db()
        self.assertEqual((opp.fit_method, opp.hidden_reason), ("", ""))   # still in the Inbox, "pending"
        self.assertEqual(relevance.score_pending_fits(client=fake_client(fit_reply(80))), 1)
        opp.refresh_from_db()
        self.assertEqual(opp.fit_score, 80)

    def test_validate_fit_rejects_what_it_cant_check(self):
        result = relevance.validate_fit({"fit_score": "140", "matched_skills": ["sql", "Cobol", 7],
                                         "role_match": "Astronaut", "seniority": "galactic",
                                         "why_it_fits": "x" * 400}, self.profile)
        self.assertEqual(result["fit_score"], 100)
        self.assertEqual(result["matched_skills"], ["SQL"])
        self.assertEqual((result["role_match"], result["seniority"]), ("", "unknown"))
        self.assertEqual(len(result["fit_reason"]), 300)
        self.assertIsNone(relevance.validate_fit({"fit_score": "high"}, self.profile)["fit_score"])

    def test_ai_reported_seniority_is_filtered_too(self):
        opp = job("Head of Analytics", score=False)
        extract_and_score(opp, client=fake_client(fit_reply(90, seniority="executive")))
        opp.refresh_from_db()
        self.assertEqual(opp.hidden_reason, "seniority: VP / C-level")

    def test_hidden_postings_are_never_alerted(self):
        opp = job("Contract Data Analyst", score=False, pay_type="hourly", pay_rate_usd=Decimal("90"),
                  reward_usd=Decimal("900"), reward_type="freelance", hours_estimate=Decimal("10"),
                  evidence="explicit", evidence_quote="SQL")
        extract_and_score(opp, client=fake_client(fit_reply(30)))
        opp.refresh_from_db()
        with mock.patch(TELEGRAM) as telegram:
            self.assertEqual(send_new_opportunity_alerts([opp]), [])
        telegram.assert_not_called()


# ---------------------------------------------------------------------------
# Inbox
# ---------------------------------------------------------------------------

@override_settings(CRYPTO=CFG)
class InboxTests(ProfileMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(User.objects.create_user(username="tester", password="pw-not-real-12345"))

    def queue(self, **params):
        page = self.client.get(reverse("albright_crypto_app:inbox"), params).content.decode()
        if 'id="inbox-data"' not in page:
            return [], page
        return json.loads(page.split('<script id="inbox-data" type="application/json">')[1].split("</script>")[0]), page

    def post(self, name, opp, body=None):
        return self.client.post(reverse(f"albright_crypto_app:inbox_{name}", args=[opp.pk]),
                                json.dumps(body or {}), content_type="application/json")

    def test_order_is_highest_fit_then_soonest_deadline(self):
        now = timezone.now()
        low = job("Analyst A", fit_score=70, fit_method="ai")
        high = job("Analyst B", fit_score=90, fit_method="ai")
        tie_later = job("Analyst C", fit_score=80, fit_method="ai", application_deadline=now + timedelta(days=9))
        tie_soon = job("Analyst D", fit_score=80, fit_method="ai", deadline=now + timedelta(days=2))
        unscored = job("Analyst E")
        data, page = self.queue()
        self.assertEqual([d["id"] for d in data], [high.pk, tie_soon.pk, tie_later.pk, low.pk, unscored.pk])
        self.assertIn(">1 of 5</span>", page)

    def test_card_has_everything_and_respects_link_checks(self):
        remotive = board(kind="remotive", url="https://remotive.com/api/remote-jobs", name="Remotive")
        opp = job("Power BI Developer", source=remotive, company="Acme", fit_score=88, fit_method="keywords",
                  matched_skills=["Power BI"], fit_reason="Title matches", missing_requirements=["DAX"],
                  metadata={"salary": "$90,000 - $110,000 per year"}, deadline=timezone.now() + timedelta(days=3))
        Opportunity.objects.filter(pk=opp.pk).update(url="https://remotive.com/remote-jobs/data/1")
        other = job("Data Engineer", fit_score=75, fit_method="ai")
        Opportunity.objects.filter(pk=other.pk).update(url="https://unknown-board.example/job/2")
        data, _page = self.queue()
        card = next(d for d in data if d["id"] == opp.pk)
        for key in ("title", "company", "type", "pay", "location", "fit_score", "matched_skills", "fit_reason",
                    "missing_requirements", "safety_warnings", "deadline", "source", "posted"):
            self.assertIn(key, card)
        self.assertEqual((card["company"], card["type"], card["pay"]), ("Acme", "Full-time", "$90,000 - $110,000 per year"))
        self.assertEqual((card["source"], card["via"]), ("Remotive", True))
        self.assertEqual(card["open_url"], "https://remotive.com/remote-jobs/data/1")   # their link, as their terms ask
        unknown = next(d for d in data if d["id"] == other.pk)
        self.assertTrue(unknown["open_url"].startswith(reverse("albright_crypto_app:leave")))  # O: warning page first

    def test_type_filter(self):
        analyst = job("Data Analyst")
        gig = job("Freelance Django build", pay_type="fixed", pay_rate_usd=Decimal("900"))
        bounty = job("Docs bounty", reward_type="bounty")
        task = job("Rate AI answers", reward_type="paid_task")
        expected = {"jobs": [analyst.pk], "freelance": [gig.pk], "bounties": [bounty.pk], "paid_tasks": [task.pk],
                    "crypto": []}
        for key, ids in expected.items():
            with self.subTest(type=key):
                self.assertEqual(sorted(d["id"] for d in self.queue(type=key)[0]), sorted(ids))
        self.assertEqual(len(self.queue(type="all")[0]), 4)

    def test_keyboard_actions_through_their_endpoints(self):
        opp = job("Data Analyst", fit_score=80, fit_method="ai")
        # K - keep -> Pipeline "Saved"
        self.assertEqual(self.post("keep", opp).json()["status"], "saved")
        opp.refresh_from_db()
        self.assertEqual(opp.status, "saved")
        self.assertIsNotNone(opp.stage_changed_at)
        # U - undo
        self.assertEqual(self.post("undo", opp).json()["status"], "inbox")
        # S - skip, no reason, then 1-4 picks one
        self.assertEqual(self.post("skip", opp).json()["reason"], "")
        for key, reason in (("1", "not_relevant"), ("2", "pay"), ("3", "location"), ("4", "seniority")):
            self.assertEqual(self.post("skip", opp, {"reason": reason}).json()["reason"], reason)
        self.assertEqual(self.post("skip", opp, {"reason": "bogus"}).status_code, 400)
        self.post("undo", opp)
        opp.refresh_from_db()
        self.assertEqual((opp.status, opp.skip_reason), ("inbox", ""))
        self.assertEqual(self.post("undo", opp).status_code, 400)     # nothing to undo now

    def test_w_walkthrough_reuses_a_saved_one(self):
        opp = job("Data Analyst")

        def fake_generate(o):
            return Walkthrough.objects.create(opportunity=o, kind="income", content={
                "sections": [{"title": "How to apply", "items": ["Send the form"]}], "warnings": []})

        with mock.patch("albright_crypto_app.inbox_views.walkthroughs.generate", side_effect=fake_generate) as gen:
            first = self.post("walkthrough", opp).json()
            second = self.post("walkthrough", opp).json()
        self.assertEqual(gen.call_count, 1)
        self.assertEqual(first["walkthrough"]["sections"][0]["items"], ["Send the form"])
        self.assertTrue(second["reused"])

    def test_p_drafts_a_proposal_then_a_resume(self):
        opp = job("Data Analyst")

        def fake_draft(kind):
            return lambda o, profile: ApplicationDraft.objects.create(opportunity=o, kind=kind, text=f"{kind} text")

        with mock.patch("albright_crypto_app.inbox_views.my_profile.generate_proposal", side_effect=fake_draft("proposal")), \
                mock.patch("albright_crypto_app.inbox_views.my_profile.tailor_resume", side_effect=fake_draft("resume")):
            proposal = self.post("draft", opp).json()
            resume = self.post("draft", opp, {"kind": "resume"}).json()
            again = self.post("draft", opp).json()
        self.assertEqual(proposal["draft"]["text"], "proposal text")
        self.assertEqual(resume["draft"]["kind"], "resume")
        self.assertTrue(again["reused"])
        self.assertEqual(ApplicationDraft.objects.count(), 2)

    def test_p_needs_a_profile_and_an_income_opportunity(self):
        MyProfile.objects.all().delete()
        opp = job("Data Analyst")
        self.assertIn("Profile", self.post("draft", opp).json()["error"])

    def test_shortcut_legend_and_phone_buttons(self):
        job("Data Analyst")
        _data, page = self.queue()
        for key, action in (("K", "keep"), ("S", "skip"), ("W", "walkthrough"), ("P", "draft"), ("O", "open"),
                            ("→", "next"), ("←", "back"), ("U", "undo"), ("?", "help")):
            self.assertIn(f'<li data-legend="{action}">', page)
            self.assertIn(f'<kbd class="kbd">{key}</kbd>', page)
        self.assertIn('data-action="keep"', page)
        self.assertNotIn('data-action="help"', page)
        for label in ("Not relevant", "Pay too low", "Location", "Seniority"):
            self.assertIn(label, page)

    def test_skip_reasons_this_week(self):
        for i, reason in enumerate(["location"] * 3 + ["pay", ""]):
            opp = job(f"Data Analyst {i}")
            workflow.move(opp, "skipped", reason)
        old = job("Old skip")
        workflow.move(old, "skipped", "location", now=timezone.now() - timedelta(days=9))
        _data, page = self.queue()
        self.assertIn("3 skipped for location", page)
        self.assertIn("1 skipped for pay too low", page)
        self.assertIn("1 skipped for no reason", page)

    def test_hidden_and_merged_items_stay_out(self):
        visible = job("Data Analyst")
        job("Registered Nurse")
        merged = job("Data Analyst copy")
        Opportunity.objects.filter(pk=merged.pk).update(duplicate_of=visible)
        self.assertEqual([d["id"] for d in self.queue()[0]], [visible.pk])

    def test_empty_inbox(self):
        _data, page = self.queue()
        self.assertIn("All caught up", page)


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

@override_settings(CRYPTO=CFG)
class PipelineBoardTests(TestCase):
    def setUp(self):
        self.client.force_login(User.objects.create_user(username="tester", password="pw-not-real-12345"))

    def move(self, opp, stage, reason=""):
        return self.client.post(reverse("albright_crypto_app:pipeline_move", args=[opp.pk]),
                                {"stage": stage, "reason": reason})

    def test_columns_and_closed_collapsed(self):
        cards = {stage: job(f"Job {stage}") for stage in ("saved", "applied", "talking", "working", "paid", "closed")}
        for stage, opp in cards.items():
            workflow.move(opp, stage)
        page = self.client.get(reverse("albright_crypto_app:pipeline")).content.decode()
        board, closed = page.split('id="pipeline-board"')[1].split('id="pipeline-closed"')
        labels = [label for label in ("Saved", "Applied", "Talking / interview", "Working", "Paid") if label in board]
        self.assertEqual(len(labels), 5)
        self.assertLess(board.index('data-stage="saved"'), board.index('data-stage="paid"'))
        self.assertNotIn(f'data-opportunity-id="{cards["closed"].pk}"', board)
        self.assertIn(f'data-opportunity-id="{cards["closed"].pk}"', closed)
        self.assertIn('<details class="card pipeline-closed"', page)     # collapsed by default (no "open")

    def test_moving_to_applied_records_the_date_and_days_in_stage(self):
        opp = job("Data Analyst", company="Acme", metadata={"salary": "$95,000 per year"})
        workflow.move(opp, "saved", now=timezone.now() - timedelta(days=3))
        page = self.client.get(reverse("albright_crypto_app:pipeline")).content.decode()
        self.assertIn('data-days-in-stage="3"', page)
        self.assertIn("Acme", page)
        self.assertIn("$95,000 per year", page)
        self.move(opp, "applied")
        opp.refresh_from_db()
        self.assertEqual(opp.status, "applied")
        self.assertAlmostEqual(opp.applied_at.timestamp(), timezone.now().timestamp(), delta=10)
        first_applied = opp.applied_at
        self.move(opp, "talking")
        self.move(opp, "applied")
        opp.refresh_from_db()
        self.assertEqual(opp.applied_at, first_applied)    # the first application date sticks
        self.move(opp, "closed", "declined")
        opp.refresh_from_db()
        self.assertEqual((opp.status, opp.close_reason), ("closed", "declined"))

    def test_follow_up_after_seven_quiet_days_shown_and_sent_once(self):
        opp = job("Data Analyst", company="Acme")
        workflow.move(opp, "applied", now=timezone.now() - timedelta(days=8))
        recent = job("BI Analyst")
        workflow.move(recent, "applied", now=timezone.now() - timedelta(days=3))
        moved_on = job("Data Engineer")
        workflow.move(moved_on, "applied", now=timezone.now() - timedelta(days=10))
        workflow.move(moved_on, "talking", now=timezone.now() - timedelta(days=1))
        opp.refresh_from_db()
        self.assertTrue(opp.followup_due)
        page = self.client.get(reverse("albright_crypto_app:pipeline")).content.decode()
        self.assertIn("Follow up - applied", page)
        self.assertIn("1 to follow up.", page)
        with mock.patch(TELEGRAM) as telegram:
            self.assertEqual(send_followup_reminders(), [opp.pk])
            self.assertEqual(send_followup_reminders(), [])     # once only
        self.assertEqual(telegram.call_count, 1)
        message = telegram.call_args.args[0]
        self.assertIn("Follow up on an application", message)
        self.assertIn("Data Analyst at Acme", message)

    def test_follow_up_for_a_job_board_posting_hides_its_details(self):
        remotive = board(kind="remotive", url="https://remotive.com/api/remote-jobs", name="Remotive")
        opp = job("Data Analyst", source=remotive, company="Acme")
        workflow.move(opp, "applied", now=timezone.now() - timedelta(days=8))
        with mock.patch(TELEGRAM) as telegram:
            send_followup_reminders()
        message = telegram.call_args.args[0]
        self.assertNotIn("Data Analyst", message)
        self.assertNotIn("Acme", message)
        self.assertIn("via Remotive", message)

    def test_detail_page_stage_buttons_and_draft_sent(self):
        opp = job("Data Analyst")
        self.client.post(reverse("albright_crypto_app:opportunity", args=[opp.pk]),
                         {"action": "status", "status": "working"})
        opp.refresh_from_db()
        self.assertEqual(opp.status, "working")
        draft = ApplicationDraft.objects.create(opportunity=opp, kind="proposal", text="Hi")
        self.client.post(reverse("albright_crypto_app:opportunity", args=[opp.pk]),
                         {"action": "draft_sent", "draft": draft.pk})
        opp.refresh_from_db()
        self.assertEqual(opp.status, "applied")
        self.assertIsNotNone(opp.applied_at)
        page = self.client.get(reverse("albright_crypto_app:opportunity", args=[opp.pk])).content.decode()
        self.assertIn(f'href="{reverse("albright_crypto_app:pipeline")}"', page)


# ---------------------------------------------------------------------------
# The status-mapping migration
# ---------------------------------------------------------------------------

class StatusMigrationTests(TestCase):
    def test_old_statuses_map_onto_the_workflow(self):
        migration = importlib.import_module("albright_crypto_app.migrations.0006_map_statuses_to_workflow")
        source = board()
        old = {}
        for status in ("new", "reviewing", "doing", "applied", "submitted", "paid", "rejected", "skipped", "expired"):
            old[status] = Opportunity.objects.create(source=source, external_id=status, url="https://jobs.example.com/x",
                                                     title=status, status=status)
        sent_at = timezone.now() - timedelta(days=20)
        ApplicationDraft.objects.create(opportunity=old["submitted"], kind="proposal", text="Hi", sent_at=sent_at)
        migration.forward(django_apps, None)
        expected = {"new": ("inbox", ""), "reviewing": ("inbox", ""), "doing": ("working", ""),
                    "applied": ("applied", ""), "submitted": ("applied", ""), "paid": ("paid", ""),
                    "rejected": ("closed", "lost"), "skipped": ("skipped", ""), "expired": ("closed", "expired")}
        for status, (new, reason) in expected.items():
            with self.subTest(status=status):
                opp = Opportunity.objects.get(pk=old[status].pk)
                self.assertEqual((opp.status, opp.close_reason), (new, reason))
        self.assertEqual(Opportunity.objects.get(pk=old["submitted"].pk).applied_at, sent_at)
        applied = Opportunity.objects.get(pk=old["applied"].pk)
        self.assertAlmostEqual(applied.applied_at.timestamp(), timezone.now().timestamp(), delta=10)
        self.assertFalse(applied.followup_due)    # follow-ups count from the migration, not from long ago
        migration.backward(django_apps, None)
        self.assertEqual(Opportunity.objects.get(pk=old["doing"].pk).status, "doing")
        self.assertEqual(Opportunity.objects.get(pk=old["expired"].pk).status, "expired")
        self.assertEqual(Opportunity.objects.get(pk=old["rejected"].pk).status, "rejected")


# ---------------------------------------------------------------------------
# Duplicates and expiry
# ---------------------------------------------------------------------------

@override_settings(CRYPTO=CFG)
class CleanupTests(TestCase):
    def raw(self, external_id, title, company, url, published=None):
        return RawOpportunity(external_id=external_id, url=url, title=title, text=JOB_TEXT,
                              metadata={"company": company}, published=published,
                              facts={"location_requirement": "Remote (US)"})

    def test_same_company_similar_title_from_another_source_is_merged(self):
        remotive = board(kind="remotive", url="https://remotive.com/api/remote-jobs", name="Remotive")
        feed = board()
        first = store(remotive, self.raw("r1", "Senior Data Analyst", "Acme Inc.", "https://remotive.com/jobs/1"))
        extract_and_score(first, use_ai=False)
        second = store(feed, self.raw("w1", "Sr. Data Analyst (Remote)", "ACME", "https://jobs.example.com/acme"))
        client = fake_client(fit_reply(90))
        extract_and_score(second, client=client)
        client.responses.create.assert_not_called()       # a duplicate costs nothing
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(second.duplicate_of, first)
        self.assertEqual(first.other_links, [{"source": "Job feed", "url": "https://jobs.example.com/acme",
                                              "opportunity": second.pk}])
        self.client.force_login(User.objects.create_user(username="tester", password="pw-not-real-12345"))
        page = self.client.get(reverse("albright_crypto_app:opportunity", args=[first.pk])).content.decode()
        self.assertIn("Also posted on", page)

    def test_not_merged_different_title_company_or_over_30_days(self):
        remotive = board(kind="remotive", url="https://remotive.com/api/remote-jobs", name="Remotive")
        feed = board()
        base = store(remotive, self.raw("r1", "Data Analyst", "Acme", "https://remotive.com/jobs/1"))
        extract_and_score(base, use_ai=False)
        others = [
            store(feed, self.raw("w1", "Salesforce Administrator", "Acme", "https://jobs.example.com/1")),
            store(feed, self.raw("w2", "Data Analyst", "Globex", "https://jobs.example.com/2")),
            store(feed, self.raw("w3", "Data Analyst", "Acme", "https://jobs.example.com/3",
                                 published=timezone.now() - timedelta(days=45))),
        ]
        for opp in others:
            extract_and_score(opp, use_ai=False)
            opp.refresh_from_db()
            self.assertIsNone(opp.duplicate_of_id, opp.title)

    def test_sweep_merges_existing_duplicates(self):
        a = job("Power BI Developer", company="Initech")
        b = job("Power BI Developer", company="Initech, LLC",
                source=board(kind="rss_income", url="https://other.example/feed", name="Other feed"))
        self.assertEqual(relevance.merge_duplicates(), 1)
        b.refresh_from_db()
        self.assertEqual(b.duplicate_of, a)

    def test_auto_expiry(self):
        now = timezone.now()
        past_deadline = job("Analyst 1", deadline=now - timedelta(hours=1))
        old_contract = job("Contract Analyst", posted_at=now - timedelta(days=15))
        fresh_contract = job("Contract BI Analyst", posted_at=now - timedelta(days=10))
        full_time_15 = job("Data Analyst FT", posted_at=now - timedelta(days=15))
        full_time_31 = job("Data Engineer FT", posted_at=now - timedelta(days=31))
        saved_old = job("Saved Analyst", posted_at=now - timedelta(days=60))
        workflow.move(saved_old, "saved")
        self.assertEqual(relevance.expire_stale(now), 3)
        for opp, status in ((past_deadline, "closed"), (old_contract, "closed"), (fresh_contract, "inbox"),
                            (full_time_15, "inbox"), (full_time_31, "closed"), (saved_old, "saved")):
            opp.refresh_from_db()
            self.assertEqual(opp.status, status, opp.title)
        past_deadline.refresh_from_db()
        self.assertEqual(past_deadline.close_reason, "expired")

    def test_store_keeps_the_posted_date(self):
        published = timezone.now() - timedelta(days=5)
        opp = store(board(), self.raw("p1", "Data Analyst", "Acme", "https://jobs.example.com/p1", published=published))
        self.assertEqual(opp.posted_at, published)


# ---------------------------------------------------------------------------
# Earnings by type, default sources
# ---------------------------------------------------------------------------

@override_settings(CRYPTO=CFG)
class EarningsByTypeTests(TestCase):
    def test_hours_and_rate_by_type(self):
        self.client.force_login(User.objects.create_user(username="tester", password="pw-not-real-12345"))
        contract = job("Contract Django build")
        bounty = job("Docs bounty", reward_type="bounty")
        Earning.objects.create(opportunity=contract, amount=Decimal("600"), currency="USD",
                               usd_value_at_receipt=Decimal("600"))
        Earning.objects.create(opportunity=bounty, amount=Decimal("50"), currency="USD", usd_value_at_receipt=Decimal("50"))
        TimeLog.objects.create(opportunity=contract, minutes=600)
        TimeLog.objects.create(opportunity=bounty, minutes=30)
        rows = {r["key"]: r for r in self.client.get(reverse("albright_crypto_app:earnings")).context["by_type"]}
        self.assertEqual((rows["freelance"]["earned"], rows["freelance"]["hours"], rows["freelance"]["per_hour"]),
                         (Decimal("600"), Decimal("10"), Decimal("60.00")))
        self.assertEqual(rows["bounties"]["per_hour"], Decimal("100.00"))
        self.assertNotIn("jobs", rows)
        page = self.client.get(reverse("albright_crypto_app:earnings")).content.decode()
        self.assertIn('id="earnings-by-type"', page)


class DefaultSourceTests(TestCase):
    def test_default_job_sources_respect_terms_per_site(self):
        Source.objects.create(kind="remotive", name="My Remotive", url="https://remotive.com/api/remote-jobs",
                              opportunity_type="income", terms_checked=True)
        sync_default_income_sources()
        remotive = Source.objects.filter(kind="remotive").exclude(name="My Remotive")
        self.assertEqual(sorted(s.options.get("category") or s.options.get("search") for s in remotive),
                         ["analytics engineer", "data", "power bi", "salesforce administrator", "software-dev", "tableau"])
        self.assertTrue(all(s.terms_checked for s in remotive))                 # Remotive's terms already checked
        remoteok = Source.objects.filter(kind="remoteok")
        self.assertEqual(sorted(s.options["tag"] for s in remoteok), ["analytics", "data", "python", "salesforce"])
        self.assertFalse(any(s.terms_checked for s in remoteok))                # never checked - won't run
        self.assertFalse(any(s.runnable for s in remoteok))
        wwr = Source.objects.filter(kind="rss_income", url__contains="weworkremotely.com")
        self.assertEqual(wwr.count(), 2)
        self.assertTrue(wwr.get(url__contains="programming").active)
        self.assertFalse(wwr.get(url__contains="data").active)                  # unconfirmed URL starts off
        self.assertTrue(Source.objects.filter(kind="hn_hiring").exists())
        before = Source.objects.count()
        Source.objects.filter(kind="remoteok").update(active=False)
        sync_default_income_sources()
        self.assertEqual(Source.objects.count(), before)                         # never re-added or switched back on
        self.assertFalse(Source.objects.filter(kind="remoteok", active=True).exists())
