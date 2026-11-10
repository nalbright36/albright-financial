"""POST opportunities (scope opportunities:write): a bot adds a job or freelance opening to its owner's Opps
inbox - owner set, its own "My bot" source, the same url again is the same opening, bad input refused - and
GET opportunities/new shows each owner only their own bot items plus the site-collected ones."""
from datetime import datetime, timezone as dt_timezone

from django.contrib.auth.models import User

from albright_crypto_app.models import Opportunity

from .models import BotToken
from .tests import BotApiTestCase, url

CREATE = url("opportunities_create")
NEW = url("opportunities_new")
JOB = {"title": "Django contractor", "url": "https://jobs.example.com/django-contractor", "company": "Acme",
       "work_type": "contract", "pay_type": "hourly", "pay_rate_usd": "85", "location_requirement": "Remote (US)",
       "skills_required": ["Python", "Django"], "posted_at": "2026-11-08T09:00:00",
       "application_deadline": "2026-11-30T17:00:00-05:00", "text": "Build and maintain Django apps."}


class OpportunitiesWriteTests(BotApiTestCase):
    def setUp(self):
        super().setUp()
        self.token, self.raw = BotToken.issue(self.user, "Grok bot", ["opportunities:read", "opportunities:write"])

    def test_scope_required(self):
        _token, raw = BotToken.issue(self.user, "Reader only", ["opportunities:read"])
        response = self.call("POST", CREATE, JOB, raw=raw)
        self.assertEqual(response.status_code, 403)
        self.assertIn("opportunities:write", response.json()["error"])
        self.assertFalse(Opportunity.objects.exists())

    def test_adds_to_the_owners_inbox(self):
        response = self.call("POST", CREATE, JOB)
        self.assertEqual(response.status_code, 201, response.content)
        body = response.json()
        self.assertTrue(body["created"])
        opp = Opportunity.objects.get(pk=body["id"])
        self.assertEqual(body["safety_verdict"], opp.safety_verdict)
        self.assertEqual((opp.owner, opp.status, opp.source.kind, opp.opportunity_type, opp.source.name),
                         (self.user, "inbox", "bot", "income", "Grok bot"))
        self.assertEqual((opp.title, opp.company, opp.work_type, opp.pay_type, str(opp.pay_rate_usd)),
                         ("Django contractor", "Acme", "contract", "hourly", "85.00"))
        self.assertEqual((opp.skills_required, opp.location_requirement, opp.raw_text),
                         (["Python", "Django"], "Remote (US)", "Build and maintain Django apps."))
        self.assertIsNotNone(opp.posted_at.tzinfo)
        self.assertEqual(opp.application_deadline, datetime(2026, 11, 30, 22, 0, tzinfo=dt_timezone.utc))
        self.assertTrue(opp.show_anyway)

    def test_same_url_again_is_the_same_opening(self):
        first = self.call("POST", CREATE, JOB).json()
        again = self.call("POST", CREATE, {**JOB, "url": JOB["url"] + "/", "title": "Renamed"})
        self.assertEqual(again.status_code, 200)
        self.assertEqual(again.json(), {"id": first["id"], "created": False})
        self.assertEqual(Opportunity.objects.count(), 1)

    def test_bad_input_refused(self):
        for change, field in (({"work_type": "crypto"}, "work_type"), ({"url": "ftp://jobs.example.com/x"}, "url"),
                              ({"url": "not a link"}, "url"), ({"pay_rate_usd": "-5"}, "pay_rate_usd"),
                              ({"pay_rate_usd": "lots"}, "pay_rate_usd"), ({"pay_type": "salary"}, "pay_type"),
                              ({"posted_at": "last week"}, "posted_at"), ({"title": ""}, "title")):
            with self.subTest(field=field, change=change):
                response = self.call("POST", CREATE, {**JOB, **change})
                self.assertEqual(response.status_code, 400)
                self.assertIn(f'"{field}"', response.json()["error"])
        self.assertFalse(Opportunity.objects.exists())

    def test_reading_shows_only_my_bot_items_and_site_collected_ones(self):
        mine = self.call("POST", CREATE, JOB).json()["id"]
        from albright_crypto_app.tests.test_pipeline import make
        site = make(title="Site-collected bounty")
        other = User.objects.create_superuser(username="other-admin", password="pw-not-real-12345", email="")
        _token, raw = BotToken.issue(other, "Their bot", ["opportunities:read", "opportunities:write",
                                                          "opportunities:suggest"])
        ids = [row["id"] for row in self.call("GET", NEW, raw=raw).json()["opportunities"]]
        self.assertIn(site.pk, ids)
        self.assertNotIn(mine, ids)
        own = [row["id"] for row in self.call("GET", NEW).json()["opportunities"]]
        self.assertIn(mine, own)
        self.assertIn(site.pk, own)
        # Their own bot keeps a separate source and inbox entry for the same url.
        theirs = self.call("POST", CREATE, JOB, raw=raw)
        self.assertEqual(theirs.status_code, 201)
        self.assertEqual(Opportunity.objects.get(pk=theirs.json()["id"]).owner, other)
        # ...and can't leave notes on mine.
        self.assertEqual(self.call("POST", url("opportunity_note", mine), {"text": "hi"}, raw=raw).status_code, 404)
