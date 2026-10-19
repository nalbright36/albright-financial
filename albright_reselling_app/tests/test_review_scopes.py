"""The review queue's scopes (scanner/review_queue.py SCOPES): "candidates"
(Act now's Candidates ending soon), "later" (the Watch tab's Ending later),
and "review" (Check by hand then Leads) - each scope's queue and order,
the candidates' valuation summary, exclusions and review settings, the
page's scope selector, and each scope keeping its own position for the day
(review_views.review_position, session-saved)."""
import json
import re
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from albright_reselling_app.review_views import POSITIONS_SESSION_KEY
from albright_reselling_app.scanner.review_queue import review_queue, valuation_summary
from albright_reselling_app.scanner_models import BidWatch, LotDismissal, LotEvaluation, SourcedLot

D = Decimal


def _cfg(**review):
    base = {"window_hours": 24, "min_headroom": 30, "hide_categories": [], "hide_sources": []}
    return {**settings.RESELLING_SCANNER, "CANDIDATE_WINDOW_HOURS": 24, "REVIEW": {**base, **review}}


def _lot(ext, hours, source="shopgoodwill"):
    return SourcedLot.objects.create(
        source=source, external_id=ext, url=f"https://example.com/{ext}", title=f"Scope Lot {ext}",
        current_price=D("10.00"), end_time=timezone.now() + timedelta(hours=hours), raw={},
    )


def _candidate(ext, hours, headroom="40.00", category="coins", source="shopgoodwill", **kw):
    lot = _lot(ext, hours, source=source)
    LotEvaluation.objects.create(lot=lot, category=category, is_candidate=True, confidence="high",
                                 max_bid=D("10.00") + D(headroom), headroom=D(headroom), **kw)
    return lot


def _check(ext, hours):
    lot = _lot(ext, hours)
    LotEvaluation.objects.create(lot=lot, category="coins", confidence="low", max_bid=D("60"), headroom=D("50"))
    return lot


def _lead(ext, hours):
    lot = _lot(ext, hours)
    LotEvaluation.objects.create(lot=lot, category="games", is_lead=True, lead_reason="n64: sealed", flags=["sealed"])
    return lot


def ids(scope):
    return [card["lot_id"] for card in review_queue(scope=scope)]


@override_settings(RESELLING_SCANNER=_cfg())
class ScopeQueueTests(TestCase):
    def setUp(self):
        self.soon_late = _candidate("soon-late", 20)
        self.soon_early = _candidate("soon-early", 2)
        self.later_far = _candidate("later-far", 72)
        self.later_near = _candidate("later-near", 30)
        self.check = _check("check", 5)
        self.lead = _lead("lead", 6)

    def test_candidates_scope_is_candidates_ending_soon_by_end_time(self):
        self.assertEqual(ids("candidates"), [self.soon_early.pk, self.soon_late.pk])

    def test_later_scope_is_ending_later_candidates_by_end_time(self):
        self.assertEqual(ids("later"), [self.later_near.pk, self.later_far.pk])

    def test_review_scope_unchanged(self):
        self.assertEqual(ids("review"), [self.check.pk, self.lead.pk])
        self.assertEqual(ids("review"), [c["lot_id"] for c in review_queue()])  # still the default

    def test_watchlisted_and_dismissed_excluded_in_every_scope(self):
        BidWatch.objects.create(lot=self.soon_early, status="watching")
        LotDismissal.objects.create(lot=self.later_near, dismissed_at=timezone.now(),
                                    headroom_at_dismissal=D("40.00"))

        self.assertEqual(ids("candidates"), [self.soon_late.pk])
        self.assertEqual(ids("later"), [self.later_far.pk])

    def test_soft_removed_watch_is_not_excluded(self):
        BidWatch.objects.create(lot=self.soon_early, status="watching", archived_at=timezone.now(),
                                archive_reason="soft_removed")

        self.assertIn(self.soon_early.pk, ids("candidates"))

    def test_dismissed_candidate_reopens_when_headroom_grows(self):
        LotDismissal.objects.create(lot=self.soon_early, dismissed_at=timezone.now(), headroom_at_dismissal=D("5"))

        self.assertIn(self.soon_early.pk, ids("candidates"))  # 40 - 5 >= the 25 reopen threshold


class ScopeSettingsTests(TestCase):
    @override_settings(RESELLING_SCANNER=_cfg(min_headroom=30))
    def test_min_headroom_applies_to_both_candidate_scopes(self):
        enough = _candidate("hr-ok", 3, headroom="30.00")
        _candidate("hr-short", 4, headroom="29.99")
        later_ok = _candidate("hr-later-ok", 40, headroom="50.00")
        _candidate("hr-later-short", 41, headroom="10.00")

        self.assertEqual(ids("candidates"), [enough.pk])
        self.assertEqual(ids("later"), [later_ok.pk])

    @override_settings(RESELLING_SCANNER=_cfg(window_hours=6))
    def test_window_hours_narrows_candidates_but_not_later(self):
        inside = _candidate("win-in", 5)
        _candidate("win-out", 10)            # ending soon (<24h) but past the 6h review window
        later = _candidate("win-later", 50)  # Ending later is past any window by definition

        self.assertEqual(ids("candidates"), [inside.pk])
        self.assertEqual(ids("later"), [later.pk])

    @override_settings(RESELLING_SCANNER=_cfg(hide_categories=["jewelry"], hide_sources=["maxsold"]))
    def test_hidden_categories_and_sources(self):
        kept = _candidate("hide-kept", 3)
        _candidate("hide-jewelry", 4, category="jewelry")
        _candidate("hide-maxsold", 5, source="maxsold")
        _candidate("hide-later-maxsold", 40, source="maxsold")

        self.assertEqual(ids("candidates"), [kept.pk])
        self.assertEqual(ids("later"), [])


@override_settings(RESELLING_SCANNER=_cfg())
class ValuationSummaryTests(TestCase):
    def test_coin_candidate_counts_coins_and_prices_it(self):
        lot = _candidate("val-coin", 3, coin_keys="mercury_dime", silver_oz=D("0.7234"), melt_value=D("22.79"),
                         expected_sale=D("26.50"), flags=["key_date_verify"])

        card = review_queue(scope="candidates")[0]

        self.assertEqual(card["lot_id"], lot.pk)
        self.assertEqual(card["kind_display"], "Candidate")
        self.assertEqual(card["why_label"], "The valuation")
        self.assertEqual(card["why"], [
            "Identified: 10 Mercury dimes (0.72 oz silver pure)",
            "Melt value $22.79 · expected sale $26.50",
            "Max bid $50.00 · headroom $40.00",
            "Confidence: high",
            "Famous key date - verify (cheap ones are usually fakes)",
        ])
        self.assertEqual((card["current_bid"], card["max_bid"], card["headroom"]), (10.0, 50.0, 40.0))
        self.assertTrue(card["time_remaining"])

    def test_jewelry_candidate_shows_karat_and_approximate_weight(self):
        ev = LotEvaluation(category="jewelry", coin_keys="jewelry_gold_14k", gold_oz=D("0.0975"), silver_oz=D("0"),
                           melt_value=D("250.00"), expected_sale=D("230.00"), max_bid=D("180.00"),
                           headroom=D("60.00"), confidence="medium", flags=["mostly_stones"])

        lines = valuation_summary(ev)

        self.assertEqual(lines[0], "Identified: 14k gold, ~5.2g (0.098 oz gold pure)")
        self.assertIn("Confidence: medium", lines)
        self.assertIn("Stones may be most of the weight", lines)

    def test_sterling_and_mixed_keys(self):
        sterling = valuation_summary(LotEvaluation(coin_keys="jewelry_sterling", silver_oz=D("1.00"),
                                                   gold_oz=D("0"), confidence="high"))
        mixed = valuation_summary(LotEvaluation(coin_keys="morgan_dollar,peace_dollar", silver_oz=D("1.5468"),
                                                gold_oz=D("0"), confidence="high"))

        self.assertEqual(sterling[0], "Identified: Sterling silver, ~33.6g (1.00 oz silver pure)")
        self.assertEqual(mixed[0], "Identified: Morgan dollar; Peace dollar (1.55 oz silver pure)")

    def test_check_and_lead_cards_keep_their_reasons(self):
        _check("val-check", 3)

        card = review_queue(scope="review")[0]

        self.assertEqual(card["why_label"], "Why it's here")
        self.assertIn("Low-confidence read - the scanner isn't sure, you decide", card["why"])


@override_settings(RESELLING_SCANNER=_cfg())
class ScopePageTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="tester", password="pw-not-real-12345")
        self.client.login(username="tester", password="pw-not-real-12345")
        self.soon = [_candidate("page-soon-1", 2), _candidate("page-soon-2", 3), _candidate("page-soon-3", 4)]
        self.later = [_candidate("page-later-1", 30), _candidate("page-later-2", 40)]
        self.check = _check("page-check", 5)

    def page(self, **params):
        return self.client.get(reverse("albright_reselling_app:review_queue"), params).content.decode()

    def embedded(self, content, element_id):
        return json.loads(re.search(rf'<script id="{element_id}" type="application/json">(.*?)</script>',
                                    content, re.S).group(1))

    def save_position(self, scope, lot):
        return self.client.post(reverse("albright_reselling_app:review_position"),
                                data=json.dumps({"scope": scope, "lot_id": lot.pk}), content_type="application/json")

    def test_opens_on_requested_scope_with_selector_and_progress(self):
        content = self.page(scope="candidates")

        self.assertIn("Reviewing:", content)
        self.assertIn('<option value="candidates" selected>Candidates ending soon (3)</option>', content)
        self.assertIn('<option value="later">Ending later (2)</option>', content)
        self.assertIn('<option value="review">Check by hand + Leads (1)</option>', content)
        self.assertIn('data-scope="candidates"', content)
        self.assertIn(">1 of 3</span>", content)
        self.assertIn(">The valuation</div>", content)

    def test_every_scope_embedded_for_switching_without_reload(self):
        queues = self.embedded(self.page(scope="later"), "review-queue-data")

        self.assertEqual([c["lot_id"] for c in queues["candidates"]], [lot.pk for lot in self.soon])
        self.assertEqual([c["lot_id"] for c in queues["later"]], [lot.pk for lot in self.later])
        self.assertEqual([c["lot_id"] for c in queues["review"]], [self.check.pk])

    def test_unknown_scope_falls_back_to_review(self):
        content = self.page(scope="bogus")

        self.assertIn('data-scope="review"', content)
        self.assertIn('<option value="review" selected>', content)

    def test_each_scope_keeps_its_own_position(self):
        self.assertEqual(self.save_position("candidates", self.soon[2]).status_code, 200)
        self.save_position("later", self.later[1])
        self.save_position("candidates", self.soon[1])   # moving within a scope replaces only that scope

        for scope in ("candidates", "later", "review"):  # switching scope never loses another's position
            with self.subTest(opened_on=scope):
                positions = self.embedded(self.page(scope=scope), "review-positions")
                self.assertEqual(positions, {"candidates": self.soon[1].pk, "later": self.later[1].pk})

    def test_positions_only_last_for_the_day(self):
        self.save_position("candidates", self.soon[2])
        session = self.client.session
        saved = session[POSITIONS_SESSION_KEY]
        saved["date"] = (timezone.localdate() - timedelta(days=1)).isoformat()
        session[POSITIONS_SESSION_KEY] = saved
        session.save()

        self.assertEqual(self.embedded(self.page(scope="candidates"), "review-positions"), {})

    def test_position_endpoint_validates(self):
        url = reverse("albright_reselling_app:review_position")
        bad_scope = self.client.post(url, data=json.dumps({"scope": "nope", "lot_id": 1}),
                                     content_type="application/json")
        bad_lot = self.client.post(url, data=json.dumps({"scope": "later", "lot_id": "x"}),
                                   content_type="application/json")

        self.assertEqual((bad_scope.status_code, bad_lot.status_code), (400, 400))
        self.assertEqual(self.client.get(url).status_code, 405)

    def test_empty_scope_still_loads_the_page_when_others_have_lots(self):
        LotEvaluation.objects.filter(lot=self.check).delete()

        content = self.page(scope="review")

        self.assertIn("js/review_queue.js", content)
        self.assertIn(">0 of 0</span>", content)
        self.assertIn('<option value="review" selected>Check by hand + Leads (0)</option>', content)

    def test_dashboard_start_review_buttons_carry_their_scope(self):
        content = self.client.get(reverse("albright_reselling_app:dashboard")).content.decode()
        base = reverse("albright_reselling_app:review_queue")

        for scope, count in (("candidates", 1), ("later", 1), ("review", 2)):
            self.assertEqual(content.count(f'href="{base}?scope={scope}" class="btn btn-primary btn-sm">Start review'),
                             count, scope)
