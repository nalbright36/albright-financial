"""Email alerts (Gmail, read-only), HN "Who is hiring", and the Himalayas,
Jobicy and Arbeitnow adapters. Gmail, HN, the job APIs and OpenAI are all
mocked - nothing reaches a real service."""
import base64
import json
import time
from decimal import Decimal
from unittest import mock
from urllib.parse import parse_qs, urlparse

from django.conf import settings
from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse

from albright_crypto_app import gmail
from albright_crypto_app.collectors.hn_hiring import HnHiringCollector, plain_text
from albright_crypto_app.collectors.job_boards import ArbeitnowCollector, HimalayasCollector, JobicyCollector
from albright_crypto_app.models import AIExtraction, GmailConnection, Opportunity, ProcessedItem, Source
from albright_crypto_app.pipeline import scan_sources
from albright_crypto_app.tests.test_collectors import GET, NO_SLEEP, _resp
from albright_crypto_app.tests.test_extraction import fake_client

CFG = {**settings.CRYPTO, "allowlist": ["github.com"], "budget_usd": 0}
GMAIL_GET = "albright_crypto_app.gmail.requests.get"
GMAIL_POST = "albright_crypto_app.gmail.requests.post"
GOOGLE = {"GOOGLE_OAUTH_CLIENT_ID": "client-123.apps.googleusercontent.com", "GOOGLE_OAUTH_CLIENT_SECRET": "shh"}


def b64(text):
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


EMAIL_HTML = """<html><body>
<p>3 new jobs match your saved search "django"</p>
<div><a href="https://www.upwork.com/jobs/~01abc">Django API developer for billing system</a>
<p>Hourly: $55-$70. Est. 20 hours. Skills: Python, Django, PostgreSQL. Remote, US only.</p></div>
<div><a href="https://www.upwork.com/jobs/~02def">Excel VBA report automation</a>
<p>Fixed price: $400. Skills: Excel, VBA.</p></div>
<div><a href="https://jobs.example-scam.biz/apply">Data entry - work from home</a>
<p>Earn $900/week! Pay the $49 training fee to start.</p></div>
<p><a href="https://www.upwork.com/ab/notification-settings">Manage notifications</a></p>
</body></html>"""

SPLIT = {"postings": [
    {"title": "Django API developer for billing system", "link": 1,
     "posting_text": "Hourly: $55-$70. Est. 20 hours. Skills: Python, Django, PostgreSQL. Remote, US only.",
     "reward_type": "freelance", "pay_type": "hourly", "pay_rate_usd": 55, "evidence": "explicit",
     "evidence_quote": "Hourly: $55-$70", "hours_estimate": 20, "skills_required": ["Python", "Django", "PostgreSQL"],
     "location_requirement": "Remote, US only"},
    {"title": "Excel VBA report automation", "link": 2, "posting_text": "Fixed price: $400. Skills: Excel, VBA.",
     "reward_type": "freelance", "pay_type": "fixed", "reward_usd": 400, "evidence": "explicit",
     "evidence_quote": "Fixed price: $400", "skills_required": ["Excel", "VBA"]},
    {"title": "Data entry - work from home", "link": 3,
     "posting_text": "Earn $900/week! Pay the $49 training fee to start.", "reward_type": "freelance"},
    {"title": "Invented posting", "link": 99, "posting_text": "not in the email at all",
     "evidence": "explicit", "evidence_quote": "$5,000 guaranteed"},
]}


def gmail_message(message_id, subject="New jobs for you"):
    return {"id": message_id, "payload": {
        "mimeType": "multipart/alternative",
        "headers": [{"name": "Subject", "value": subject}, {"name": "From", "value": "Upwork <donotreply@upwork.com>"}],
        "parts": [{"mimeType": "text/html", "body": {"data": b64(EMAIL_HTML)}}]}}


class FakeGmail:
    """Routes the mocked requests.get by Gmail API path; records every call."""

    def __init__(self, message_ids=("m1",)):
        self.message_ids = list(message_ids)
        self.calls = []

    def __call__(self, url, params=None, headers=None, timeout=None):
        self.calls.append(("GET", url, params))
        path = url.split("/users/me/", 1)[1]
        if path == "labels":
            data = {"labels": [{"id": "INBOX", "name": "INBOX"}, {"id": "Label_7", "name": "Opportunities"}]}
        elif path == "messages":
            assert params["labelIds"] == "Label_7"
            data = {"messages": [{"id": m} for m in self.message_ids]}
        elif path.startswith("messages/"):
            data = gmail_message(path.split("/")[1])
        elif path == "profile":
            data = {"emailAddress": "me@example.com"}
        else:
            raise AssertionError(f"unexpected Gmail call {url}")
        resp = mock.Mock(status_code=200)
        resp.json.return_value = data
        resp.raise_for_status.return_value = None
        return resp


def connect_gmail(**token):
    connection = GmailConnection(pk=GmailConnection.SINGLETON_PK, email="me@example.com", scope=gmail.SCOPE)
    connection.set_token({"access_token": "ya29.test", "refresh_token": "1//refresh", "scope": gmail.SCOPE,
                          "expires_at": time.time() + 3600, **token})
    connection.save()
    return connection


@override_settings(CRYPTO=CFG, **GOOGLE)
class EmailAlertTests(TestCase):
    def setUp(self):
        connect_gmail()
        self.source = Source.objects.create(kind="email", name="Gmail label: opportunities", url="gmail:opportunities",
                                            opportunity_type="income", options={"label": "opportunities"})
        self.gmail = FakeGmail()

    def scan(self, client):
        with mock.patch(GMAIL_GET, side_effect=self.gmail), mock.patch(GMAIL_POST) as post:
            new, errors = scan_sources(sources=[self.source], client=client)
        post.assert_not_called()   # token still valid: no refresh needed
        return new, errors

    def test_one_email_becomes_several_safety_checked_opportunities(self):
        client = fake_client(SPLIT)
        new, errors = self.scan(client)

        self.assertEqual(errors, [])
        self.assertEqual(client.responses.create.call_count, 1)      # one AI call for the whole email
        by_title = {o.title: o for o in new}
        self.assertEqual(set(by_title), {"Django API developer for billing system", "Excel VBA report automation",
                                         "Data entry - work from home", "Invented posting"})
        django_job = by_title["Django API developer for billing system"]
        self.assertEqual(django_job.url, "https://www.upwork.com/jobs/~01abc")     # the link from the email
        self.assertEqual((django_job.opportunity_type, django_job.extraction_status), ("income", "done"))
        self.assertEqual((django_job.pay_type, django_job.pay_rate_usd, django_job.reward_usd),
                         ("hourly", Decimal("55.00"), Decimal("1100.00")))
        self.assertEqual(django_job.skills_required, ["Python", "Django", "PostgreSQL"])
        self.assertEqual(django_job.skill_match, 100)
        self.assertEqual(by_title["Excel VBA report automation"].url, "https://www.upwork.com/jobs/~02def")
        # Safety checks ran on each posting.
        scam = by_title["Data entry - work from home"]
        self.assertEqual(scam.safety_verdict, "block")
        self.assertIn("pay to start, train, or buy equipment", scam.safety_reasons)
        # A posting whose link/quote isn't in the email gets neither.
        invented = by_title["Invented posting"]
        self.assertTrue(invented.url.startswith("https://mail.google.com/"))
        self.assertEqual((invented.evidence, invented.evidence_quote), ("none", ""))
        self.assertEqual(AIExtraction.objects.get().purpose, "email_split")

    def test_no_duplicates_and_no_second_ai_call(self):
        self.scan(fake_client(SPLIT))
        client = fake_client(SPLIT)
        new, _errors = self.scan(client)
        self.assertEqual(new, [])
        client.responses.create.assert_not_called()
        self.assertEqual(Opportunity.objects.filter(source=self.source).count(), 4)
        self.assertEqual(list(ProcessedItem.objects.values_list("item_id", "opportunities_created")), [("m1", 4)])

    def test_new_message_is_processed_old_one_is_not(self):
        self.scan(fake_client(SPLIT))
        self.gmail.message_ids = ["m2", "m1"]
        client = fake_client({"postings": []})
        self.scan(client)
        self.assertEqual(client.responses.create.call_count, 1)
        self.assertEqual(set(ProcessedItem.objects.values_list("item_id", flat=True)), {"m1", "m2"})

    def test_only_ever_gets_from_gmail(self):
        self.scan(fake_client(SPLIT))
        self.assertTrue(self.gmail.calls)
        for method, url, _params in self.gmail.calls:
            self.assertEqual(method, "GET")
            self.assertTrue(url.startswith("https://gmail.googleapis.com/gmail/v1/users/me/"), url)
            self.assertNotRegex(url, r"send|trash|delete|modify|batchModify|drafts|import|insert")

    def test_ai_limit_leaves_the_message_for_next_time(self):
        with override_settings(CRYPTO={**CFG, "ai_daily_limit": 0}):
            client = fake_client(SPLIT)
            new, errors = self.scan(client)
        self.assertEqual((new, errors), ([], []))
        client.responses.create.assert_not_called()
        self.assertFalse(ProcessedItem.objects.exists())

    def test_not_connected_means_not_runnable(self):
        GmailConnection.objects.all().delete()
        self.assertFalse(Source.objects.get(pk=self.source.pk).runnable)

    def test_expired_token_is_refreshed_and_saved_encrypted(self):
        GmailConnection.objects.all().delete()
        connect_gmail(expires_at=time.time() - 10)
        refreshed = mock.Mock(status_code=200)
        refreshed.json.return_value = {"access_token": "ya29.fresh", "expires_in": 3600, "scope": gmail.SCOPE}
        with mock.patch(GMAIL_GET, side_effect=self.gmail), mock.patch(GMAIL_POST, return_value=refreshed) as post:
            scan_sources(sources=[self.source], client=fake_client({"postings": []}))
        self.assertEqual(post.call_args.args[0], gmail.TOKEN_URL)
        self.assertEqual(post.call_args.kwargs["data"]["grant_type"], "refresh_token")
        stored = GmailConnection.objects.get()
        self.assertEqual(stored.get_token()["access_token"], "ya29.fresh")
        self.assertNotIn("ya29", stored.encrypted_token)
        self.assertNotIn("1//refresh", stored.encrypted_token)


@override_settings(CRYPTO=CFG, **GOOGLE)
class GmailOAuthTests(TestCase):
    def setUp(self):
        self.client.force_login(User.objects.create_user(username="tester", password="pw-not-real-12345"))

    def test_connect_requests_gmail_readonly_scope_only(self):
        response = self.client.post(reverse("albright_crypto_app:gmail_connect"))
        query = parse_qs(urlparse(response["Location"]).query)
        self.assertTrue(response["Location"].startswith("https://accounts.google.com/o/oauth2/v2/auth?"))
        self.assertEqual(query["scope"], ["https://www.googleapis.com/auth/gmail.readonly"])
        self.assertEqual(query["include_granted_scopes"], ["false"])
        self.assertEqual(gmail.SCOPE, "https://www.googleapis.com/auth/gmail.readonly")
        self.assertTrue(query["state"][0])

    def token_response(self, scope):
        resp = mock.Mock(status_code=200)
        resp.json.return_value = {"access_token": "ya29.new", "refresh_token": "1//new", "expires_in": 3599,
                                  "scope": scope, "token_type": "Bearer"}
        return resp

    def callback(self, scope):
        self.client.post(reverse("albright_crypto_app:gmail_connect"))
        state = self.client.session["opportunities_gmail_oauth_state"]
        with mock.patch(GMAIL_POST, return_value=self.token_response(scope)), \
                mock.patch(GMAIL_GET, side_effect=FakeGmail()):
            return self.client.get(reverse("albright_crypto_app:gmail_callback"), {"code": "4/abc", "state": state})

    def test_callback_stores_the_token_encrypted(self):
        self.callback(gmail.SCOPE)
        connection = GmailConnection.objects.get()
        self.assertEqual((connection.email, connection.scope), ("me@example.com", gmail.SCOPE))
        self.assertEqual(connection.get_token()["refresh_token"], "1//new")
        self.assertNotIn("1//new", connection.encrypted_token)
        self.assertTrue(Source.objects.filter(kind="email", options={"label": "opportunities"}).exists())

    def test_callback_refuses_any_broader_scope(self):
        response = self.callback(f"{gmail.SCOPE} https://www.googleapis.com/auth/gmail.modify")
        self.assertFalse(GmailConnection.objects.exists())
        self.assertContains(self.client.get(response["Location"]), "only gmail.readonly is accepted")

    def test_callback_refuses_a_mismatched_state(self):
        self.client.post(reverse("albright_crypto_app:gmail_connect"))
        with mock.patch(GMAIL_POST) as post:
            self.client.get(reverse("albright_crypto_app:gmail_callback"), {"code": "4/abc", "state": "forged"})
        post.assert_not_called()
        self.assertFalse(GmailConnection.objects.exists())

    def test_setup_page_and_disconnect(self):
        connect_gmail()
        page = self.client.get(reverse("albright_crypto_app:gmail"))
        self.assertContains(page, "me@example.com")
        self.assertContains(page, "read-only")
        self.client.post(reverse("albright_crypto_app:gmail"), {"label": "Job alerts"})
        self.assertEqual(Source.objects.get(kind="email").options, {"label": "Job alerts"})
        with mock.patch(GMAIL_POST) as post:
            self.client.post(reverse("albright_crypto_app:gmail_disconnect"))
        self.assertEqual(post.call_args.args[0], gmail.REVOKE_URL)
        self.assertFalse(GmailConnection.objects.exists())

    def test_unconfigured_server_explains_setup(self):
        with override_settings(GOOGLE_OAUTH_CLIENT_ID="", GOOGLE_OAUTH_CLIENT_SECRET=""):
            page = self.client.get(reverse("albright_crypto_app:gmail"))
        self.assertContains(page, "GOOGLE_OAUTH_CLIENT_ID")
        self.assertNotContains(page, 'id="connect-gmail"')


# ---------------------------------------------------------------------------
# HN "Who is hiring"
# ---------------------------------------------------------------------------

HN_ITEMS = {
    "user/whoishiring": {"id": "whoishiring", "submitted": [9003, 9002, 9001, 8000]},
    "item/9003": {"id": 9003, "type": "story", "title": "Ask HN: Who wants to be hired? (October 2026)"},
    "item/9002": {"id": 9002, "type": "story", "title": "Ask HN: Freelancer? Seeking freelancer? (October 2026)"},
    "item/9001": {"id": 9001, "type": "story", "title": "Ask HN: Who is hiring? (October 2026)", "kids": [1, 2, 3, 4, 5]},
    "item/8000": {"id": 8000, "type": "story", "title": "Ask HN: Who is hiring? (September 2026)", "kids": [99]},
    "item/1": {"id": 1, "by": "acme", "time": 1790000000,
               "text": "Acme | Contract Django developer | REMOTE (US) | $90/hr<p>Python, Django, PostgreSQL. "
                       "Apply: <a href=\"https://acme.example/jobs\">https://acme.example/jobs</a>"},
    "item/2": {"id": 2, "by": "bigco", "time": 1790000100,
               "text": "BigCo | Senior Rust engineer | Onsite NYC | Full-time<p>Rust, Kubernetes."},
    "item/3": {"id": 3, "by": "dataco", "time": 1790000200,
               "text": "DataCo | Data engineer | REMOTE | Full-time<p>Python, data pipelines, PostgreSQL, API integrations."},
    "item/4": {"id": 4, "deleted": True},
    "item/5": {"id": 5, "by": "frontco", "time": 1790000300,
               "text": "FrontCo | Frontend | REMOTE | Full-time<p>React, TypeScript, GraphQL."},
}

HN_AI = {"postings": [
    {"id": 1, "title": "Acme - Contract Django developer", "work_types": ["contract"], "remote": True,
     "reward_type": "freelance", "pay_type": "hourly", "pay_rate_usd": 90, "evidence": "explicit",
     "evidence_quote": "$90/hr", "skills_required": ["Python", "Django", "PostgreSQL"],
     "location_requirement": "REMOTE (US)"},
    {"id": 2, "title": "BigCo - Senior Rust engineer", "work_types": ["full_time"], "remote": False,
     "skills_required": ["Rust", "Kubernetes"], "location_requirement": "Onsite NYC"},
    {"id": 3, "title": "DataCo - Data engineer", "work_types": ["full_time"], "remote": True,
     "skills_required": ["Python", "data pipelines", "PostgreSQL", "API integrations"]},
    {"id": 5, "title": "FrontCo - Frontend", "work_types": ["full_time"], "remote": True,
     "skills_required": ["React", "TypeScript", "GraphQL"]},
    {"id": 777, "title": "Not a comment we sent", "work_types": ["contract"]},
]}


def hn_get(url, params=None, headers=None, timeout=None):
    key = url.split("/v0/", 1)[1].removesuffix(".json")
    return _resp(json_data=HN_ITEMS.get(key, {}))


@override_settings(CRYPTO=CFG)
class HnHiringTests(TestCase):
    def setUp(self):
        NO_SLEEP.start()
        self.addCleanup(NO_SLEEP.stop)
        self.source = Source.objects.create(kind="hn_hiring", name="HN: Who is hiring",
                                            url="https://hacker-news.firebaseio.com/v0", opportunity_type="income",
                                            terms_checked=True)

    def test_finds_the_latest_who_is_hiring_thread(self):
        with mock.patch(GET, side_effect=hn_get):
            thread = HnHiringCollector().latest_thread()
        self.assertEqual(thread["id"], 9001)   # skips "Who wants to be hired?" and "Freelancer?", not September's

    def test_every_posting_is_stored_and_the_filters_decide(self):
        client = fake_client(HN_AI)
        with mock.patch(GET, side_effect=hn_get) as get:
            new, errors = scan_sources(sources=[self.source], client=client)
        self.assertEqual(errors, [])
        self.assertEqual(sorted(o.external_id for o in new), ["hn:1", "hn:2", "hn:3", "hn:5"])
        self.assertEqual(Opportunity.objects.get(external_id="hn:2").hidden_reason,
                         "location: on-site in Onsite NYC (not within 30 miles of Tampa, FL)")
        self.assertEqual(Opportunity.objects.get(external_id="hn:1").company, "Acme")
        contract = Opportunity.objects.get(external_id="hn:1")
        self.assertEqual(contract.url, "https://news.ycombinator.com/item?id=1")
        self.assertEqual((contract.reward_type, contract.pay_rate_usd, contract.extraction_status),
                         ("freelance", Decimal("90.00"), "done"))
        self.assertIn("https://acme.example/jobs", contract.raw_text)
        self.assertEqual(Opportunity.objects.get(external_id="hn:3").skill_match, 100)
        self.assertEqual(client.responses.create.call_count, 1)    # one batch (fit included - no extra calls)
        sent = client.responses.create.call_args.kwargs["input"]
        self.assertIn("id=1", sent)
        self.assertNotIn("id=4", sent)                              # deleted comment never sent
        self.assertNotIn("id=99", sent)                             # last month's thread untouched
        # Every comment is recorded, kept or not - none is classified twice.
        self.assertEqual(set(ProcessedItem.objects.values_list("item_id", flat=True)), {"1", "2", "3", "4", "5"})
        for call in get.call_args_list:
            self.assertTrue(call.args[0].startswith("https://hacker-news.firebaseio.com/v0/"))

    def test_second_run_sends_nothing_to_the_ai(self):
        with mock.patch(GET, side_effect=hn_get):
            scan_sources(sources=[self.source], client=fake_client(HN_AI))
            Source.objects.filter(pk=self.source.pk).update(last_checked=None)
            client = fake_client(HN_AI)
            new, _errors = scan_sources(sources=[Source.objects.get(pk=self.source.pk)], client=client)
        self.assertEqual(new, [])
        client.responses.create.assert_not_called()

    def test_off_until_terms_checked_and_polite_interval(self):
        Source.objects.filter(pk=self.source.pk).update(terms_checked=False)
        with mock.patch(GET, side_effect=hn_get) as get:
            scan_sources(client=fake_client(HN_AI))
        get.assert_not_called()
        self.assertEqual(HnHiringCollector.min_interval.total_seconds(), 6 * 3600)

    def test_plain_text_keeps_links(self):
        self.assertEqual(plain_text('Apply <a href="https://x.example/a">here</a><p>Remote &amp; part-time'),
                         "Apply https://x.example/a\n\nRemote & part-time")


# ---------------------------------------------------------------------------
# Himalayas, Jobicy, Arbeitnow
# ---------------------------------------------------------------------------

HIMALAYAS = {"jobs": [
    {"title": "Python Contractor", "companyName": "Acme", "employmentType": "Contractor",
     "guid": "https://himalayas.app/companies/acme/jobs/python-contractor", "applicationLink": "https://acme.example/apply",
     "locationRestrictions": ["United States"], "minSalary": 90000, "maxSalary": 120000, "currency": "USD",
     "description": "<p>Build <b>APIs</b>.</p>", "pubDate": 1790000000, "categories": ["Software"]},
    {"title": "Off-site", "guid": "https://elsewhere.example/job"},
]}
JOBICY = {"jobs": [
    {"id": 123, "url": "https://jobicy.com/jobs/123-django-dev", "jobTitle": "Django Dev", "companyName": "B",
     "jobType": ["Part-Time"], "jobGeo": "USA", "jobDescription": "Django work", "pubDate": "2026-10-20 10:00:00",
     "annualSalaryMin": "50000", "annualSalaryMax": "70000", "salaryCurrency": "USD"},
]}
ARBEITNOW = {"data": [
    {"slug": "data-engineer-berlin-1", "company_name": "C", "title": "Data Engineer", "description": "<p>ETL</p>",
     "remote": True, "url": "https://www.arbeitnow.com/jobs/companies/c/data-engineer-berlin-1",
     "tags": ["python", "sql"], "job_types": ["full time"], "location": "Berlin", "created_at": 1790000000},
]}


@override_settings(CRYPTO=CFG)
class NewJobBoardTests(TestCase):
    def setUp(self):
        NO_SLEEP.start()
        self.addCleanup(NO_SLEEP.stop)
        self.client.force_login(User.objects.create_user(username="tester", password="pw-not-real-12345"))

    def scan(self, kind, payload, **options):
        from albright_crypto_app.collectors.job_boards import endpoint_url
        source = Source.objects.create(kind=kind, name=kind, url=endpoint_url(kind, options), options=options,
                                       opportunity_type="income", terms_checked=True)
        with mock.patch(GET, return_value=_resp(json_data=payload)) as get:
            new, errors = scan_sources(sources=[source], client=fake_client({}))
        self.assertEqual(errors, [])
        return new, get

    def test_himalayas(self):
        new, get = self.scan("himalayas", HIMALAYAS)
        self.assertEqual(get.call_args.args[0], "https://himalayas.app/jobs/api")
        [opp] = new
        self.assertEqual((opp.url, opp.reward_type, opp.location_requirement),
                         ("https://himalayas.app/companies/acme/jobs/python-contractor", "freelance", "United States"))
        self.assertIn("Salary: $90,000–$120,000 per year", opp.raw_text)
        self.assertEqual(opp.extraction_status, "skipped")   # kept in the app: never sent to the AI

    def test_jobicy(self):
        new, get = self.scan("jobicy", JOBICY, tag="python")
        self.assertEqual(get.call_args.kwargs["params"], {"count": 50, "tag": "python"})
        [opp] = new
        self.assertEqual((opp.external_id, opp.reward_type, opp.location_requirement), ("jobicy:123", "freelance", "USA"))

    def test_arbeitnow(self):
        new, get = self.scan("arbeitnow", ARBEITNOW)
        self.assertEqual(get.call_args.args[0], "https://www.arbeitnow.com/api/job-board-api")
        [opp] = new
        self.assertEqual((opp.reward_type, opp.location_requirement, opp.skills_required),
                         ("unknown", "Remote", ["python", "sql"]))

    def test_attribution_and_followed_link_back(self):
        for kind, payload, name in (("himalayas", HIMALAYAS, "Himalayas"), ("jobicy", JOBICY, "Jobicy"),
                                    ("arbeitnow", ARBEITNOW, "Arbeitnow")):
            with self.subTest(kind=kind):
                [opp] = self.scan(kind, payload)[0]
                detail = self.client.get(reverse("albright_crypto_app:opportunity", args=[opp.pk])).content.decode()
                self.assertIn(f'<a href="{opp.url}" target="_blank" rel="noopener" class="crypto-link crypto-via">'
                              f'via {name}</a>', detail)
                self.assertNotIn("nofollow", detail)
                self.assertTrue(opp.source.keep_in_app)

    def test_off_until_terms_checked(self):
        Source.objects.create(kind="jobicy", name="j", url="https://jobicy.com/api/v2/remote-jobs",
                              opportunity_type="income")
        with mock.patch(GET) as get:
            scan_sources(client=fake_client({}))
        get.assert_not_called()

    def test_hn_source_can_be_added_from_settings(self):
        self.client.post(reverse("albright_crypto_app:settings"), {"action": "add_source", "source-kind": "hn_hiring"})
        source = Source.objects.get(kind="hn_hiring")
        self.assertEqual((source.url, source.terms_checked), ("https://hacker-news.firebaseio.com/v0", False))


@override_settings(CRYPTO=CFG)
class HnFitInTheBatchTests(TestCase):
    """The fit step for HN rides along in the batch call: with a profile, each
    posting's fit comes back in the same response - still one AI call."""

    def setUp(self):
        from albright_crypto_app.models import MyProfile
        from albright_crypto_app.my_profile import prefill_profile
        prefill_profile(MyProfile.load())
        NO_SLEEP.start()
        self.addCleanup(NO_SLEEP.stop)
        self.source = Source.objects.create(kind="hn_hiring", name="HN: Who is hiring",
                                            url="https://hacker-news.firebaseio.com/v0", opportunity_type="income",
                                            terms_checked=True)

    def test_fit_comes_from_the_batch_and_low_fit_is_hidden(self):
        scores = {1: 85, 2: 70, 3: 75, 5: 20}
        reply = {"postings": [{**p, "fit_score": scores.get(p["id"]), "matched_skills": ["Python", "Cobol"],
                               "role_match": "Data Engineer", "why_it_fits": "Python data work."}
                              for p in HN_AI["postings"]]}
        client = fake_client(reply)
        with mock.patch(GET, side_effect=hn_get):
            scan_sources(sources=[self.source], client=client)
        self.assertEqual(client.responses.create.call_count, 1)
        self.assertIn("=== CANDIDATE PROFILE ===", client.responses.create.call_args.kwargs["input"])
        data = Opportunity.objects.get(external_id="hn:3")
        self.assertEqual((data.fit_score, data.fit_method, data.role_match, data.matched_skills),
                         (75, "ai", "Data Engineer", ["Python"]))
        self.assertEqual(data.hidden_reason, "")
        self.assertEqual(Opportunity.objects.get(external_id="hn:5").hidden_reason, "low fit (20/100)")
        self.assertNotIn("fit", data.metadata)
