"""The Remotive and Remote OK adapters, every HTTP call mocked: field
mapping into income opportunities, the fixed endpoints with the source's
optional parameters, Remote OK's legal-notice first element and
User-Agent, the 6-hour minimum interval per site (however often
scan_crypto runs), the "via Remotive" / "via Remote OK" followed link to
the job on their site, both domains allowlisted automatically, and their
jobs never being sent anywhere (no AI extraction, nothing in Telegram)."""
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.conf import settings
from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from albright_crypto_app import config
from albright_crypto_app.alerts import digest_lines, send_deadline_reminders, send_new_opportunity_alerts
from albright_crypto_app.collectors.job_boards import REMOTEOK_USER_AGENT, RemoteOkCollector, RemotiveCollector
from albright_crypto_app.models import Opportunity, Source
from albright_crypto_app.pipeline import extract_and_score, scan_sources
from albright_crypto_app.tests.test_collectors import GET, NO_SLEEP, _resp
from albright_crypto_app.tests.test_extraction import fake_client

CFG = {**settings.CRYPTO, "allowlist": ["github.com"], "budget_usd": 0}
TELEGRAM = "albright_crypto_app.alerts.send_telegram"

REMOTIVE = {"job-count": 2, "jobs": [
    {"id": 1911, "url": "https://remotive.com/remote-jobs/software-dev/django-developer-1911",
     "title": "Django Developer", "company_name": "Acme", "category": "Software Development",
     "job_type": "contract", "publication_date": "2026-10-20T08:00:00", "candidate_required_location": "USA",
     "salary": "$60 - $80/hour", "tags": ["python", "django", "postgresql"],
     "description": "<p>Build <b>Django</b> APIs.</p>"},
    {"id": 1912, "url": "https://elsewhere.example/job/1912", "title": "Off-site job"},
]}
REMOTEOK = [
    # The first element is Remote OK's legal notice - never a job, even though it has these keys.
    {"id": "legal", "url": "https://remoteok.com/legal", "legal": "API Terms of Service: link back to Remote OK...",
     "last_updated": 1792400000},
    {"id": "120345", "url": "https://remoteok.com/remote-jobs/120345-remote-python-engineer-acme",
     "position": "Python Engineer", "company": "Acme", "tags": ["python", "api", "dev"], "location": "Worldwide",
     "salary_min": 90000, "salary_max": 120000, "date": "2026-10-19T15:00:00+00:00",
     "description": "Work on our <i>data pipelines</i>."},
    {"id": "120346", "url": "https://remoteok.com/remote-jobs/120346-vba", "position": "VBA dev", "company": "B",
     "tags": [], "location": "", "salary_min": 0, "salary_max": 0, "date": "", "description": ""},
]


def source(kind, **options):
    from albright_crypto_app.collectors.job_boards import endpoint_url
    return Source.objects.create(kind=kind, name=kind, url=endpoint_url(kind, options), options=options,
                                 opportunity_type="income", terms_checked=True)


@override_settings(CRYPTO=CFG)
class JobBoardTests(TestCase):
    def setUp(self):
        NO_SLEEP.start()
        self.addCleanup(NO_SLEEP.stop)

    def test_remotive_request_and_mapping(self):
        src = source("remotive", category="software-dev", search="django")
        with mock.patch(GET, return_value=_resp(json_data=REMOTIVE)) as get:
            new, errors = scan_sources(use_ai=False)

        self.assertEqual(errors, [])
        self.assertEqual(get.call_args.args[0], "https://remotive.com/api/remote-jobs")
        self.assertEqual(get.call_args.kwargs["params"], {"category": "software-dev", "search": "django"})
        self.assertEqual(len(new), 1)  # the off-site job is skipped
        opp = new[0]
        self.assertEqual((opp.opportunity_type, opp.external_id, opp.url, opp.title),
                         ("income", "remotive:1911", REMOTIVE["jobs"][0]["url"], "Django Developer"))
        self.assertEqual((opp.reward_type, opp.location_requirement), ("freelance", "USA"))
        self.assertEqual(opp.skills_required, ["python", "django", "postgresql"])
        self.assertEqual(opp.skill_match, 100)
        for line in ("Build Django APIs.", "Company: Acme", "Category: Software Development", "Job type: contract",
                     "Location: USA", "Salary: $60 - $80/hour"):
            self.assertIn(line, opp.raw_text)
        self.assertEqual(opp.metadata["via"], "Remotive")
        self.assertEqual(opp.domain_status, "allowlisted")
        self.assertEqual(src.opportunities.count(), 1)

    def test_remotive_without_options_sends_no_params(self):
        source("remotive")
        with mock.patch(GET, return_value=_resp(json_data={"jobs": []})) as get:
            scan_sources(use_ai=False)
        self.assertIsNone(get.call_args.kwargs["params"])

    def test_remoteok_skips_the_legal_notice_and_maps_jobs(self):
        source("remoteok", tag="python")
        with mock.patch(GET, return_value=_resp(json_data=REMOTEOK)) as get:
            new, errors = scan_sources(use_ai=False)

        self.assertEqual(errors, [])
        self.assertEqual(get.call_args.args[0], "https://remoteok.com/api")
        self.assertEqual(get.call_args.kwargs["params"], {"tag": "python"})
        self.assertEqual(get.call_args.kwargs["headers"]["User-Agent"], REMOTEOK_USER_AGENT)
        self.assertIn("remoteok.com/api", REMOTEOK_USER_AGENT)
        self.assertEqual([o.external_id for o in new], ["remoteok:120345", "remoteok:120346"])
        self.assertFalse(Opportunity.objects.filter(external_id="remoteok:legal").exists())
        opp = new[0]
        self.assertEqual((opp.title, opp.location_requirement, opp.skills_required),
                         ("Python Engineer", "Worldwide", ["python", "api", "dev"]))
        self.assertIn("Work on our data pipelines.", opp.raw_text)
        self.assertIn("Salary: $90,000–$120,000 per year", opp.raw_text)
        self.assertIn("Tags: python, api, dev", opp.raw_text)
        self.assertEqual(opp.metadata["via"], "Remote OK")
        self.assertEqual(timezone.localtime(opp.metadata and new[0].discovered_at).year, timezone.now().year)
        self.assertNotIn("Salary:", new[1].raw_text)  # zero salary = not stated

    def test_collectors_read_only_their_own_jobs(self):
        with mock.patch(GET, return_value=_resp(json_data=[REMOTEOK[0]])):
            self.assertEqual(list(RemoteOkCollector().collect(Source(kind="remoteok"))), [])  # only the notice
        with mock.patch(GET, return_value=_resp(json_data={"jobs": [REMOTIVE["jobs"][1]]})):
            self.assertEqual(list(RemotiveCollector().collect(Source(kind="remotive"))), [])

    def test_six_hour_minimum_interval(self):
        src = source("remotive")
        with mock.patch(GET, return_value=_resp(json_data=REMOTIVE)) as get:
            scan_sources(use_ai=False)
            scan_sources(use_ai=False)          # scan_crypto runs again soon after: no fetch
            Source.objects.filter(pk=src.pk).update(last_checked=timezone.now() - timedelta(hours=5, minutes=59))
            scan_sources(use_ai=False)
            self.assertEqual(get.call_count, 1)
            Source.objects.filter(pk=src.pk).update(last_checked=timezone.now() - timedelta(hours=6, minutes=1))
            scan_sources(use_ai=False)
            self.assertEqual(get.call_count, 2)

    def test_interval_is_per_site_so_extra_sources_cannot_add_fetches(self):
        source("remotive", category="software-dev")
        source("remotive", category="data")
        source("remoteok")
        with mock.patch(GET, side_effect=lambda url, **kw: _resp(
                json_data=REMOTEOK if "remoteok" in url else {"jobs": []})) as get:
            scan_sources(use_ai=False)
            scan_sources(use_ai=False)
        self.assertEqual(sorted(c.args[0] for c in get.call_args_list),
                         ["https://remoteok.com/api", "https://remotive.com/api/remote-jobs"])

    def test_skipped_for_the_interval_is_not_an_error(self):
        source("remoteok")
        with mock.patch(GET, return_value=_resp(json_data=REMOTEOK)):
            scan_sources(use_ai=False)
            new, errors = scan_sources(use_ai=False)
        self.assertEqual((new, errors), ([], []))

    def test_a_failed_fetch_is_not_retried(self):
        source("remotive")
        with mock.patch(GET, return_value=_resp(status=503)) as get:
            new, errors = scan_sources(use_ai=False)
        self.assertEqual(get.call_count, 1)
        self.assertEqual(len(errors), 1)

    def test_terms_must_be_checked_first(self):
        src = source("remoteok")
        Source.objects.filter(pk=src.pk).update(terms_checked=False)
        with mock.patch(GET) as get:
            scan_sources(use_ai=False)
        get.assert_not_called()

    def test_both_domains_are_allowlisted_automatically(self):
        self.assertIn("remotive.com", config.allowlist())
        self.assertIn("remoteok.com", config.allowlist())
        with override_settings(CRYPTO={**CFG, "allowlist": []}):
            self.assertEqual(config.allowlist(), list(config.BUILT_IN_ALLOWLIST))
            self.assertEqual(config.allowlist()[:2], ["remotive.com", "remoteok.com"])
        config.set_value("allowlist", ["github.com", "remoteok.com"])
        self.assertEqual(config.allowlist()[:3], ["github.com", "remoteok.com", "remotive.com"])


@override_settings(CRYPTO=CFG)
class AttributionAndKeepInAppTests(TestCase):
    def setUp(self):
        NO_SLEEP.start()
        self.addCleanup(NO_SLEEP.stop)
        self.client.force_login(User.objects.create_user(username="tester", password="pw-not-real-12345"))
        source("remotive")
        source("remoteok")
        responses = lambda url, **kw: _resp(json_data=REMOTEOK if "remoteok" in url else REMOTIVE)  # noqa: E731
        with mock.patch(GET, side_effect=responses):
            self.new, _errors = scan_sources(use_ai=False)
        self.remotive = Opportunity.objects.get(external_id="remotive:1911")
        self.remoteok = Opportunity.objects.get(external_id="remoteok:120345")

    def test_via_link_in_the_inbox_hidden_and_detail(self):
        Opportunity.objects.filter(pk=self.remoteok.pk).update(hidden_reason="low fit (10/100)")
        cards = {c["id"]: c for c in self.client.get(reverse("albright_crypto_app:inbox")).context["queue"]}
        self.assertEqual((cards[self.remotive.pk]["source"], cards[self.remotive.pk]["via"]), ("Remotive", True))
        self.assertEqual(cards[self.remotive.pk]["open_url"], self.remotive.url)    # the inbox's "via" link: their page
        hidden = self.client.get(reverse("albright_crypto_app:hidden")).content.decode()
        for opp, name, page in ((self.remotive, "Remotive", None), (self.remoteok, "Remote OK", hidden)):
            link = (f'<a href="{opp.url}" target="_blank" rel="noopener" class="crypto-link crypto-via">'
                    f'via {name}</a>')
            with self.subTest(name=name):
                if page:
                    self.assertIn(link, page)
                detail = self.client.get(reverse("albright_crypto_app:opportunity", args=[opp.pk])).content.decode()
                self.assertIn(link, detail)
                self.assertNotIn("nofollow", detail)
        self.assertNotIn("nofollow", hidden)

    def test_never_sent_to_the_ai_provider(self):
        client = fake_client({"reward_type": "freelance"})
        extract_and_score(self.remotive, use_ai=True, client=client)
        client.responses.create.assert_not_called()
        self.assertEqual(self.remotive.extraction_status, "skipped")

    @mock.patch(TELEGRAM)
    def test_never_sent_to_telegram(self, send):
        Opportunity.objects.filter(pk=self.remotive.pk).update(confidence="high", ev_per_hour=Decimal("99"))
        self.assertEqual(send_new_opportunity_alerts(Opportunity.objects.filter(pk=self.remotive.pk)), [])
        send.assert_not_called()

        Opportunity.objects.filter(pk=self.remoteok.pk).update(status="working",
                                                               deadline=timezone.now() + timedelta(hours=5))
        send_deadline_reminders()
        message = send.call_args.args[0]
        self.assertIn("A job via Remote OK (details in the app)", message)
        self.assertNotIn("Python Engineer", message)
        self.assertNotIn("remoteok.com", message)

        text = "\n".join(digest_lines())
        self.assertIn("Income: 3 new (24h)", text)
        for title in ("Django Developer", "Python Engineer", "VBA dev"):
            self.assertNotIn(title, text)

    def test_add_job_board_sources_on_settings(self):
        response = self.client.post(reverse("albright_crypto_app:settings"), {
            "action": "add_source", "source-kind": "remotive", "source-category": "data", "source-search": "python"})
        self.assertEqual(response.status_code, 302)
        added = Source.objects.get(kind="remotive", options={"category": "data", "search": "python"})
        self.assertEqual((added.name, added.url, added.terms_checked),
                         ("Remotive: data, python", "https://remotive.com/api/remote-jobs?category=data&search=python",
                          False))
        response = self.client.post(reverse("albright_crypto_app:settings"), {
            "action": "add_source", "source-kind": "remoteok", "source-tag": "django"})
        self.assertEqual(Source.objects.get(kind="remoteok", options={"tag": "django"}).name, "Remote OK: django")

        page = self.client.get(reverse("albright_crypto_app:settings"))
        self.assertContains(page, "Fetched at most once every 6 hours")
        self.assertContains(page, '<option value="remotive">Remotive</option>')

    def test_other_kinds_still_need_a_url_and_name(self):
        response = self.client.post(reverse("albright_crypto_app:settings"), {
            "action": "add_source", "source-kind": "rss_income"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Enter the feed or API URL.")
