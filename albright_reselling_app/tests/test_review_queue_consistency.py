"""Regression tests for the review queue showing "1 of 128" in the header
while the card said "All caught up" and the "Reviewing:" selector was
blank. Cause: the page template was rendered without the scope data (a
server still running the pre-scopes view), and the header, the card, and
the "All caught up" choice read different context values. Now one build
(review_views.build_review_page) drives all three, positions are checked
server-side, and missing data says "refresh" instead of "All caught up"."""
import json
import re
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.contrib.auth.models import User
from django.template.loader import render_to_string
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from albright_reselling_app import calculator_views
from albright_reselling_app.review_views import POSITIONS_SESSION_KEY, resolve_scope
from albright_reselling_app.scanner_models import BidWatch, LotEvaluation, SourcedLot

D = Decimal
CFG = {**settings.RESELLING_SCANNER, "CANDIDATE_WINDOW_HOURS": 24,
       "REVIEW": {"window_hours": 24, "min_headroom": 30, "hide_categories": [], "hide_sources": []}}


def _lot(ext, hours, **ev):
    lot = SourcedLot.objects.create(source="shopgoodwill", external_id=ext, url=f"https://example.com/{ext}",
                                    title=f"Consistency Lot {ext}", current_price=D("10.00"),
                                    end_time=timezone.now() + timedelta(hours=hours), raw={})
    LotEvaluation.objects.create(lot=lot, category=ev.pop("category", "coins"), **ev)
    return lot


def candidate(ext, hours):
    return _lot(ext, hours, is_candidate=True, confidence="high", max_bid=D("60"), headroom=D("50"))


def check(ext, hours):
    return _lot(ext, hours, confidence="low", max_bid=D("60"), headroom=D("50"))


def lead(ext, hours):
    return _lot(ext, hours, category="games", is_lead=True, lead_reason="n64: sealed", flags=["sealed"])


@override_settings(RESELLING_SCANNER=CFG)
class QueueConsistencyTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="tester", password="pw-not-real-12345")
        self.client.login(username="tester", password="pw-not-real-12345")
        self.soon = [candidate(f"soon{i}", 2 + i) for i in range(3)]
        self.later = [candidate(f"later{i}", 30 + i) for i in range(2)]
        self.checks = [check(f"check{i}", 4 + i) for i in range(2)]
        self.leads = [lead(f"lead{i}", 8 + i) for i in range(2)]

    # ---------------- helpers ----------------
    def open(self, url):
        content = self.client.get(url).content.decode()
        progress = re.search(r'id="rq-progress"[^>]*>(\d+) of (\d+)<', content)
        selector = content.split('id="rq-scope"')[1].split("</select>")[0] if 'id="rq-scope"' in content else ""
        selected = re.findall(r'<option value="(\w+)" selected>', selector)  # the "Reviewing:" selector only
        queues = starts = None
        m = re.search(r'<script id="review-queue-data" type="application/json">(.*?)</script>', content, re.S)
        if m:
            queues = json.loads(m.group(1))
            starts = json.loads(re.search(r'<script id="review-start-indexes" type="application/json">(.*?)</script>',
                                          content, re.S).group(1))
        scope = re.search(r'data-scope="(\w+)"', content)
        return {"content": content, "progress": progress and (int(progress.group(1)), int(progress.group(2))),
                "selected": selected, "queues": queues, "starts": starts, "scope": scope and scope.group(1)}

    def assert_header_matches_card(self, page):
        """The header's "N of M" is exactly the current scope's start lot and length."""
        queue = page["queues"][page["scope"]]
        start = page["starts"][page["scope"]]
        self.assertEqual(page["progress"], (start + 1 if queue else 0, len(queue)))
        if queue:
            lot_id = queue[start]["lot_id"]
            self.assertTrue(SourcedLot.objects.filter(pk=lot_id).exists())
        self.assertEqual(page["selected"], [page["scope"]])  # never blank, never two

    def save(self, scope, lot):
        self.client.post(reverse("albright_reselling_app:review_position"),
                         data=json.dumps({"scope": scope, "lot_id": lot.pk}), content_type="application/json")

    def set_session_positions(self, value):
        session = self.client.session
        session[POSITIONS_SESSION_KEY] = value
        session.save()

    # ---------------- 1. each button's scope ----------------
    def test_every_start_review_button_opens_a_real_first_lot(self):
        dashboard = self.client.get(reverse("albright_reselling_app:dashboard")).content.decode()
        hrefs = sorted(set(re.findall(r'href="([^"]*/review/\?scope=\w+)" class="btn btn-primary btn-sm">Start review',
                                      dashboard)))
        expected_first = {"candidates": self.soon[0], "later": self.later[0], "review": self.checks[0]}

        self.assertEqual(len(hrefs), 3)  # candidates, later, review (Check by hand and Leads share it)
        for href in hrefs:
            with self.subTest(href=href):
                page = self.open(href)
                scope = href.rsplit("=", 1)[1]
                self.assertEqual(page["scope"], scope)
                self.assertGreater(page["progress"][1], 0)
                self.assertEqual(page["progress"][0], 1)
                self.assertEqual(page["queues"][scope][0]["lot_id"], expected_first[scope].pk)
                self.assert_header_matches_card(page)
                self.assertNotIn("<h2>All caught up</h2>", page["content"])

    # ---------------- 2. unknown / missing / alias scopes ----------------
    def test_unknown_or_missing_scope_falls_back_and_is_selected(self):
        base = reverse("albright_reselling_app:review_queue")
        for url in (base, base + "?scope=", base + "?scope=nonsense", base + "?scope=%20"):
            with self.subTest(url=url):
                page = self.open(url)
                self.assertEqual(page["scope"], "review")
                self.assert_header_matches_card(page)

    def test_aliases_resolve_to_real_scopes(self):
        self.assertEqual(resolve_scope("check_by_hand"), "review")
        self.assertEqual(resolve_scope("Leads"), "review")
        self.assertEqual(resolve_scope("ending_soon"), "candidates")
        self.assertEqual(resolve_scope("ending_later"), "later")
        self.assertEqual(resolve_scope(None), "review")
        page = self.open(reverse("albright_reselling_app:review_queue") + "?scope=ending_later")
        self.assertEqual(page["scope"], "later")
        self.assert_header_matches_card(page)

    # ---------------- 3. stale / out-of-range / old-format positions ----------------
    def test_saved_position_resumes_and_header_agrees(self):
        self.save("candidates", self.soon[2])

        page = self.open(reverse("albright_reselling_app:review_queue") + "?scope=candidates")

        self.assertEqual(page["starts"]["candidates"], 2)
        self.assertEqual(page["progress"], (3, 3))
        self.assert_header_matches_card(page)

    def test_position_on_a_lot_no_longer_in_the_queue_resets_to_top(self):
        self.save("candidates", self.soon[2])
        BidWatch.objects.create(lot=self.soon[2], status="watching")  # now excluded from the queue

        page = self.open(reverse("albright_reselling_app:review_queue") + "?scope=candidates")

        self.assertEqual(page["starts"]["candidates"], 0)
        self.assertEqual(page["progress"], (1, 2))
        self.assert_header_matches_card(page)

    def test_position_from_another_scope_or_unknown_lot_resets(self):
        self.save("candidates", self.later[0])  # a real lot, but not in this scope
        self.set_session_positions({"date": timezone.localdate().isoformat(),
                                    "positions": {"candidates": self.later[0].pk, "later": 999_999_999}})

        page = self.open(reverse("albright_reselling_app:review_queue") + "?scope=later")

        self.assertEqual(page["starts"], {"review": 0, "candidates": 0, "later": 0})
        self.assert_header_matches_card(page)

    def test_malformed_saved_positions_never_break_the_page(self):
        today = timezone.localdate().isoformat()
        for value in ("junk", ["x"], {"date": today, "positions": "nope"},
                      {"date": today, "positions": {"candidates": "abc", "bogus": 1}}, {"positions": {}}):
            with self.subTest(saved=value):
                self.set_session_positions(value)
                page = self.open(reverse("albright_reselling_app:review_queue") + "?scope=candidates")
                self.assertEqual(page["starts"]["candidates"], 0)
                self.assert_header_matches_card(page)

    def test_old_single_scope_format_maps_to_the_original_queue(self):
        """An older {"date", "lot_id"} record could only have meant Check by
        hand + Leads - it resumes there and leaves the new scopes alone."""
        self.set_session_positions({"date": timezone.localdate().isoformat(), "lot_id": self.checks[1].pk})

        page = self.open(reverse("albright_reselling_app:review_queue") + "?scope=review")

        self.assertEqual(page["starts"], {"review": 1, "candidates": 0, "later": 0})
        self.assertEqual(page["progress"], (2, 4))
        self.save("candidates", self.soon[1])  # saving a new-format position on top works
        self.assertEqual(self.open(reverse("albright_reselling_app:review_queue"))["starts"]["candidates"], 1)

    # ---------------- 4. count and card always agree ----------------
    def test_count_and_card_agree_for_every_scope_with_exclusions_and_settings(self):
        BidWatch.objects.create(lot=self.soon[0], status="watching")
        LotEvaluation.objects.filter(lot=self.later[1]).update(headroom=D("5"))  # under min_headroom
        for scope, first, total in (("candidates", self.soon[1], 2), ("later", self.later[0], 1),
                                    ("review", self.checks[0], 4)):
            with self.subTest(scope=scope):
                page = self.open(reverse("albright_reselling_app:review_queue") + f"?scope={scope}")
                self.assertEqual(page["progress"], (1, total))
                self.assertEqual(page["queues"][scope][0]["lot_id"], first.pk)
                selector = page["content"].split('id="rq-scope"')[1].split("</select>")[0]
                options = dict(re.findall(r'<option value="(\w+)"[^>]*>[^<]*\((\d+)\)</option>', selector))
                self.assertEqual(options, {"review": "4", "candidates": "2", "later": "1"})
                self.assert_header_matches_card(page)

    # ---------------- 5. "All caught up" only at zero ----------------
    def test_all_caught_up_only_when_nothing_is_queued(self):
        page = self.open(reverse("albright_reselling_app:review_queue"))
        self.assertNotIn("<h2>All caught up</h2>", page["content"])

        LotEvaluation.objects.all().delete()
        page = self.open(reverse("albright_reselling_app:review_queue") + "?scope=candidates")

        self.assertIn("<h2>All caught up</h2>", page["content"])
        self.assertEqual(page["progress"], (0, 0))
        self.assertEqual(page["selected"], ["candidates"])

    def test_empty_scope_shows_zero_not_one(self):
        LotEvaluation.objects.filter(lot__in=self.later).delete()

        page = self.open(reverse("albright_reselling_app:review_queue") + "?scope=later")

        self.assertEqual(page["progress"], (0, 0))
        self.assertNotIn("<h2>All caught up</h2>", page["content"])  # other scopes still have lots - JS says so
        self.assert_header_matches_card(page)

    def test_template_without_review_data_says_refresh_not_all_caught_up(self):
        """The original bug: the page rendered without its scope data showed
        "All caught up" next to a real count. Now it asks for a refresh."""
        request = RequestFactory().get("/")
        request.user = self.user
        old_context = {"queue": [{"lot_id": 1}] * 128, **calculator_views.panel_context(self.user)}

        html = render_to_string("review_queue.html", old_context, request=request)

        self.assertIn("The review queue didn't load", html)
        self.assertNotIn("All caught up", html)
        self.assertNotIn("1 of 128", html)
        self.assertNotIn('id="rq-scope"', html)
