"""Tests for faster Leads/Check by hand review: table thumbnails, the
RESELLING_SCANNER["REVIEW"] settings (window, min headroom, hidden
categories/sources) and their "Showing N (M hidden...)" line, and the
/review/ queue - its order and exclusions, "Why it's here" wording, the
Watchlist/Dismiss/Undo/AI review endpoints, and the shortcut legend."""
import json
import re
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.conf import settings
from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from albright_reselling_app.review_views import SHORTCUTS
from albright_reselling_app.scanner.dashboard import get_scanner_dashboard_context
from albright_reselling_app.scanner.review_queue import review_queue, why_its_here
from albright_reselling_app.scanner_models import AIReview, BidWatch, LotDismissal, LotEvaluation, SourcedLot

AI_REVIEW_LOT = "albright_reselling_app.review_views.review_lot"


def _review_cfg(**overrides):
    base = {"window_hours": 24, "min_headroom": 30, "hide_categories": [], "hide_sources": []}
    return {**settings.RESELLING_SCANNER, "REVIEW": {**base, **overrides}}


def _lot(external_id, end_in=timedelta(hours=3), source="shopgoodwill", image_url="", current_price="10.00",
         description=""):
    return SourcedLot.objects.create(
        source=source, external_id=external_id, url=f"https://example.com/{external_id}",
        title=f"Review Lot {external_id}", current_price=Decimal(current_price),
        end_time=timezone.now() + end_in, image_url=image_url, description=description, raw={},
    )


def _check(external_id, headroom="40.00", category="coins", flags=None, **lot_kwargs):
    lot = _lot(external_id, **lot_kwargs)
    LotEvaluation.objects.create(
        lot=lot, category=category, confidence="low", max_bid=Decimal("10.00") + Decimal(headroom),
        headroom=Decimal(headroom), flags=flags or [],
    )
    return lot


def _lead(external_id, category="games", flags=None, lead_reason="n64: bulk lot, sealed", **lot_kwargs):
    lot = _lot(external_id, **lot_kwargs)
    LotEvaluation.objects.create(
        lot=lot, category=category, is_lead=True, lead_reason=lead_reason,
        flags=flags if flags is not None else ["bulk lot", "sealed"],
    )
    return lot


def _eval(category="coins", flags=None, lead_reason=""):
    return LotEvaluation(category=category, flags=flags or [], lead_reason=lead_reason)


class LoggedInTestCase(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="tester", password="pw-not-real-12345")
        self.client.login(username="tester", password="pw-not-real-12345")

    def dashboard(self):
        return self.client.get(reverse("albright_reselling_app:dashboard")).content.decode()

    def post_json(self, url, data=None):
        return self.client.post(url, data=json.dumps(data or {}), content_type="application/json")


def _table(content, table_id):
    return content.split(f'id="{table_id}"')[1].split("</table>")[0]


@override_settings(RESELLING_SCANNER=_review_cfg())
class ThumbnailTests(LoggedInTestCase):
    def test_check_by_hand_row_starts_with_lazy_thumbnail(self):
        _check("thumb-1", image_url="https://img.example.com/thumb-1.jpg")

        table = _table(self.dashboard(), "table-check-by-hand")
        row = re.search(r'<tr class="scanner-row".*?</tr>', table, re.S).group(0)
        first_cell = re.search(r"<td[^>]*>.*?</td>", row, re.S).group(0)

        self.assertIn('class="lot-thumb-cell"', first_cell)
        self.assertIn('src="https://img.example.com/thumb-1.jpg"', first_cell)
        self.assertIn('loading="lazy"', first_cell)
        self.assertIn('data-full-src="https://img.example.com/thumb-1.jpg"', first_cell)
        self.assertIn("js-lightbox", first_cell)

    def test_lead_row_thumbnail_and_placeholder(self):
        _lead("thumb-2", image_url="https://img.example.com/thumb-2.jpg")
        _lead("thumb-3")

        table = _table(self.dashboard(), "table-leads")

        self.assertIn('src="https://img.example.com/thumb-2.jpg"', table)
        self.assertIn('class="lot-thumb lot-thumb--empty"', table)

    def test_lightbox_on_dashboard(self):
        _check("thumb-4", image_url="https://img.example.com/thumb-4.jpg")

        content = self.dashboard()

        self.assertIn('<dialog id="lightbox"', content)
        self.assertIn("js/lightbox.js", content)

    def test_detail_row_spans_the_new_column(self):
        _check("thumb-5")
        _lead("thumb-6")

        content = self.dashboard()

        self.assertIn('colspan="10"', _table(content, "table-check-by-hand"))
        self.assertIn('colspan="9"', _table(content, "table-leads"))


class ReviewSettingsTests(LoggedInTestCase):
    def titles(self, key, context=None):
        return {row["title"] for row in (context or get_scanner_dashboard_context())[key]}

    @override_settings(RESELLING_SCANNER=_review_cfg(window_hours=24))
    def test_window_hours(self):
        inside = _check("win-in", end_in=timedelta(hours=20))
        outside = _check("win-out", end_in=timedelta(hours=30))
        lead_inside = _lead("win-lead-in", end_in=timedelta(hours=20))
        lead_outside = _lead("win-lead-out", end_in=timedelta(hours=30))  # still within LEAD_WINDOW_HOURS

        context = get_scanner_dashboard_context()

        self.assertEqual(self.titles("check_by_hand", context), {inside.title})
        self.assertEqual(self.titles("leads_to_review", context), {lead_inside.title})
        self.assertNotIn(outside.title, self.titles("check_by_hand", context))
        self.assertNotIn(lead_outside.title, self.titles("leads_to_review", context))
        self.assertEqual(context["check_by_hand_hidden"], 1)
        self.assertEqual(context["leads_hidden"], 1)

    @override_settings(RESELLING_SCANNER=_review_cfg(min_headroom=30))
    def test_min_headroom_applies_to_check_by_hand_only(self):
        enough = _check("hr-30", headroom="30.00")
        short = _check("hr-29", headroom="29.99")
        lead = _lead("hr-lead")  # no headroom at all

        context = get_scanner_dashboard_context()

        self.assertEqual(self.titles("check_by_hand", context), {enough.title})
        self.assertNotIn(short.title, self.titles("check_by_hand", context))
        self.assertEqual(self.titles("leads_to_review", context), {lead.title})

    @override_settings(RESELLING_SCANNER=_review_cfg(hide_categories=["jewelry", "cards"]))
    def test_hide_categories(self):
        coins = _check("cat-coins", category="coins")
        _check("cat-jewelry", category="jewelry")
        games = _lead("cat-games", category="games")
        _lead("cat-cards", category="cards", flags=["graded"], lead_reason="pokemon: graded")

        context = get_scanner_dashboard_context()

        self.assertEqual(self.titles("check_by_hand", context), {coins.title})
        self.assertEqual(self.titles("leads_to_review", context), {games.title})

    @override_settings(RESELLING_SCANNER=_review_cfg(hide_sources=["maxsold"]))
    def test_hide_sources(self):
        kept = _check("src-sgw", source="shopgoodwill")
        _check("src-ms", source="maxsold")
        _lead("src-ms-lead", source="maxsold")

        context = get_scanner_dashboard_context()

        self.assertEqual(self.titles("check_by_hand", context), {kept.title})
        self.assertEqual(context["leads_to_review"], [])
        self.assertEqual(context["leads_hidden"], 1)

    @override_settings(RESELLING_SCANNER=_review_cfg(min_headroom=30))
    def test_showing_line_with_hidden_count(self):
        _check("note-1", headroom="40.00")
        _check("note-2", headroom="5.00")
        _check("note-3", headroom="6.00")
        _lead("note-lead")

        content = self.dashboard()

        self.assertIn("Showing 1 (2 hidden by review settings)", content)
        self.assertIn("Showing 1 (0 hidden by review settings)", content)

    @override_settings(RESELLING_SCANNER=_review_cfg())
    def test_start_review_buttons_on_both_tables(self):
        content = self.dashboard()
        url = reverse("albright_reselling_app:review_queue")

        self.assertEqual(content.count(f'href="{url}" class="btn btn-primary btn-sm">Start review</a>'), 2)

    def test_missing_review_settings_fall_back_to_defaults(self):
        cfg = {k: v for k, v in settings.RESELLING_SCANNER.items() if k != "REVIEW"}
        _check("default-1", headroom="25.00")  # under the default 30
        with override_settings(RESELLING_SCANNER=cfg):
            context = get_scanner_dashboard_context()
        self.assertEqual(context["check_by_hand"], [])
        self.assertEqual(context["check_by_hand_hidden"], 1)


@override_settings(RESELLING_SCANNER=_review_cfg())
class QueueOrderTests(LoggedInTestCase):
    def test_check_by_hand_first_then_leads_each_by_end_time(self):
        lead_soon = _lead("q-lead-soon", end_in=timedelta(hours=1))
        check_late = _check("q-check-late", end_in=timedelta(hours=5))
        lead_late = _lead("q-lead-late", end_in=timedelta(hours=6))
        check_soon = _check("q-check-soon", end_in=timedelta(hours=2))

        queue = review_queue()

        self.assertEqual([c["lot_id"] for c in queue], [check_soon.pk, check_late.pk, lead_soon.pk, lead_late.pk])
        self.assertEqual([c["kind"] for c in queue], ["check", "check", "lead", "lead"])

    def test_excludes_watchlisted_and_dismissed(self):
        keep = _check("q-keep")
        watched = _check("q-watched")
        dismissed = _lead("q-dismissed")
        BidWatch.objects.create(lot=watched, status="watching")
        LotDismissal.objects.create(lot=dismissed, dismissed_at=timezone.now())

        self.assertEqual([c["lot_id"] for c in review_queue()], [keep.pk])

    def test_dismissed_check_reopens_once_headroom_grows(self):
        lot = _check("q-reopen", headroom="80.00")
        LotDismissal.objects.create(lot=lot, dismissed_at=timezone.now(), headroom_at_dismissal=Decimal("40.00"))

        self.assertEqual([c["lot_id"] for c in review_queue()], [lot.pk])

    def test_lot_that_is_both_appears_once_as_check(self):
        lot = _check("q-both")
        LotEvaluation.objects.filter(lot=lot).update(is_lead=True, lead_reason="14k gold, no weight stated")

        queue = review_queue()

        self.assertEqual([(c["lot_id"], c["kind"]) for c in queue], [(lot.pk, "check")])

    def test_review_settings_apply_to_queue(self):
        _check("q-short", headroom="5.00")
        _check("q-far", end_in=timedelta(hours=30))
        _lead("q-lead-far", end_in=timedelta(hours=30))

        self.assertEqual(review_queue(), [])

    def test_card_fields(self):
        lot = _check("q-card", headroom="40.00", image_url="https://img.example.com/q.jpg",
                     description="x" * 450, flags=["no_weight"])

        card = review_queue()[0]

        self.assertEqual(card["title"], lot.title)
        self.assertEqual(card["url"], lot.url)
        self.assertEqual(card["image_url"], "https://img.example.com/q.jpg")
        self.assertEqual(card["source_display"], "ShopGoodwill")
        self.assertEqual(card["current_bid"], 10.0)
        self.assertEqual(card["max_bid"], 50.0)
        self.assertEqual(card["headroom"], 40.0)
        self.assertEqual(len(card["description_preview"]), 400)
        self.assertEqual(len(card["description_more"]), 50)
        self.assertIn("No weight stated", card["why"])

    def test_lead_card_has_no_max_or_headroom(self):
        _lead("q-lead-card")

        card = review_queue()[0]

        self.assertIsNone(card["max_bid"])
        self.assertIsNone(card["headroom"])


class WhyItsHereTests(TestCase):
    def test_common_coin_and_jewelry_flags(self):
        self.assertIn("No weight stated", why_its_here(_eval("jewelry", ["no_weight"]), "check"))
        self.assertIn("Stones may be most of the weight", why_its_here(_eval("jewelry", ["mostly_stones"]), "check"))
        self.assertIn("Famous key date - verify (cheap ones are usually fakes)",
                      why_its_here(_eval("coins", ["key_date_verify"]), "check"))
        self.assertIn("Stated weight doesn't fit this coin - possible fake",
                      why_its_here(_eval("coins", ["weight_mismatch"]), "check"))
        self.assertIn("Reserve not met yet - the current bid isn't a real signal",
                      why_its_here(_eval("coins", ["reserve_not_met"]), "check"))

    def test_designer_flag(self):
        reasons = why_its_here(_eval("jewelry", ["designer:tiffany"]), "lead")
        self.assertIn("Designer piece (Tiffany) - may sell well above scrap", reasons)

    def test_check_by_hand_always_says_low_confidence(self):
        self.assertIn("Low-confidence read - the scanner isn't sure, you decide", why_its_here(_eval(), "check"))

    def test_lead_signals_in_plain_words(self):
        reasons = why_its_here(_eval("cards", ["graded", "sealed"], "pokemon: graded, sealed"), "lead")

        self.assertIn("Pokemon lead - value signals: graded, sealed", reasons)
        self.assertIn("No price data for this category - price it by hand", reasons)
        self.assertNotIn("Graded coin - may sell above melt", reasons)

    def test_short_platform_reads_as_acronym(self):
        reasons = why_its_here(_eval("games", ["bulk lot", "complete in box"], "n64: bulk lot, complete in box"), "lead")
        self.assertIn("N64 lead - value signals: bulk lot, complete in box", reasons)

    def test_jewelry_lead_uses_its_reason_without_no_price_line(self):
        reasons = why_its_here(_eval("jewelry", [], "14k gold, no weight stated"), "lead")

        self.assertEqual(reasons, ["14k gold, no weight stated"])

    def test_unknown_flag_humanized(self):
        self.assertIn("Some new flag", why_its_here(_eval("coins", ["some_new_flag"]), "check"))


@override_settings(RESELLING_SCANNER=_review_cfg())
class QueueActionTests(LoggedInTestCase):
    def url(self, name, lot):
        return reverse(f"albright_reselling_app:{name}", args=[lot.pk])

    def test_watch_adds_to_watchlist_and_leaves_queue(self):
        lot = _check("act-watch")

        response = self.post_json(self.url("review_watch", lot))

        self.assertEqual(response.status_code, 200)
        self.assertTrue(BidWatch.objects.filter(lot=lot, status="watching").exists())
        self.assertEqual(review_queue(), [])

    def test_watch_twice_is_fine(self):
        lot = _check("act-watch-2")
        self.post_json(self.url("review_watch", lot))
        self.post_json(self.url("review_watch", lot))
        self.assertEqual(BidWatch.objects.filter(lot=lot).count(), 1)

    def test_watch_closed_lot_rejected(self):
        lot = _check("act-closed", end_in=-timedelta(hours=1))

        response = self.post_json(self.url("review_watch", lot))

        self.assertEqual(response.status_code, 400)
        self.assertFalse(BidWatch.objects.filter(lot=lot).exists())

    def test_dismiss_without_reason(self):
        lot = _check("act-dismiss", headroom="40.00")

        response = self.post_json(self.url("review_dismiss", lot))

        self.assertEqual(response.json()["reason"], "")
        dismissal = LotDismissal.objects.get(lot=lot)
        self.assertEqual(dismissal.reason, "")
        self.assertEqual(dismissal.headroom_at_dismissal, Decimal("40.00"))
        self.assertEqual(review_queue(), [])

    def test_dismiss_then_reason_updates_the_same_dismissal(self):
        lot = _check("act-reason")
        self.post_json(self.url("review_dismiss", lot))

        response = self.post_json(self.url("review_dismiss", lot), {"reason": "too_far"})

        self.assertEqual(response.json()["reason"], "too_far")
        self.assertEqual(LotDismissal.objects.filter(lot=lot).count(), 1)
        self.assertEqual(LotDismissal.objects.get(lot=lot).reason, "too_far")

    def test_dismiss_with_form_post_reason_and_bad_reason(self):
        lot = _lead("act-form")
        self.client.post(self.url("review_dismiss", lot), {"reason": "bad_photos"})
        self.assertEqual(LotDismissal.objects.get(lot=lot).reason, "bad_photos")

        self.post_json(self.url("review_dismiss", lot), {"reason": "made_up"})
        self.assertEqual(LotDismissal.objects.get(lot=lot).reason, "")

    def test_undo_watch(self):
        lot = _check("act-undo-watch")
        self.post_json(self.url("review_watch", lot))

        response = self.post_json(self.url("review_undo", lot), {"action": "watch"})

        self.assertEqual(response.json()["undone"], "watch")
        self.assertFalse(BidWatch.objects.filter(lot=lot).exists())
        self.assertEqual([c["lot_id"] for c in review_queue()], [lot.pk])

    def test_undo_dismiss(self):
        lot = _lead("act-undo-dismiss")
        self.post_json(self.url("review_dismiss", lot), {"reason": "reviewed"})

        self.post_json(self.url("review_undo", lot), {"action": "dismiss"})

        self.assertFalse(LotDismissal.objects.filter(lot=lot).exists())
        self.assertEqual([c["lot_id"] for c in review_queue()], [lot.pk])

    def test_undo_needs_a_known_action(self):
        lot = _check("act-undo-bad")

        self.assertEqual(self.post_json(self.url("review_undo", lot), {"action": "nope"}).status_code, 400)

    def test_actions_require_post_and_login(self):
        lot = _check("act-auth")
        self.assertEqual(self.client.get(self.url("review_watch", lot)).status_code, 405)
        self.client.logout()
        self.assertEqual(self.post_json(self.url("review_dismiss", lot)).status_code, 302)
        self.assertFalse(LotDismissal.objects.exists())

    @mock.patch(AI_REVIEW_LOT)
    def test_ai_review_returns_range_for_card(self, mock_review):
        lot = _check("act-ai")
        mock_review.side_effect = lambda lot_arg: AIReview.objects.create(
            lot=lot_arg, status="done", resale_low=Decimal("60"), resale_high=Decimal("90"),
            suggested_max_bid=Decimal("40"), confidence="medium", cost_usd=Decimal("0.05"),
            result={"summary": "Looks right."},
        )

        response = self.post_json(self.url("review_ai", lot))

        self.assertEqual(response.status_code, 200)
        ai = response.json()["ai_review"]
        self.assertEqual((ai["resale_low"], ai["resale_high"], ai["suggested_max_bid"]), (60.0, 90.0, 40.0))
        self.assertEqual(ai["summary"], "Looks right.")
        # the lot stays in the queue, now carrying the review
        self.assertEqual(review_queue()[0]["ai_review"]["resale_low"], 60.0)

    @mock.patch(AI_REVIEW_LOT)
    def test_ai_review_reuses_recent_one(self, mock_review):
        lot = _check("act-ai-recent")
        AIReview.objects.create(lot=lot, status="done", resale_low=Decimal("20"), resale_high=Decimal("30"),
                                cost_usd=Decimal("0.05"))

        response = self.post_json(self.url("review_ai", lot))

        mock_review.assert_not_called()
        self.assertEqual(response.json()["ai_review"]["resale_low"], 20.0)

    @mock.patch(AI_REVIEW_LOT)
    def test_ai_review_error_is_reported(self, mock_review):
        lot = _check("act-ai-error")
        mock_review.side_effect = lambda lot_arg: AIReview.objects.create(
            lot=lot_arg, status="error", error="Provider timed out", cost_usd=Decimal("0"),
        )

        response = self.post_json(self.url("review_ai", lot))

        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["error"], "Provider timed out")


@override_settings(RESELLING_SCANNER=_review_cfg())
class ReviewPageTests(LoggedInTestCase):
    def page(self):
        return self.client.get(reverse("albright_reselling_app:review_queue")).content.decode()

    def test_legend_lists_every_shortcut_as_key_badges(self):
        _check("page-legend")

        content = self.page()
        legend = content.split('id="rq-legend-main"')[1].split("</ul>")[0]

        for keys, label, action in SHORTCUTS:
            with self.subTest(action=action):
                row = re.search(rf'<li data-legend="{action}">(.*?)</li>', legend, re.S).group(1)
                for key in keys:
                    self.assertIn(f'<kbd class="kbd">{key}</kbd>', row)
                self.assertIn(label, row)
        for expected in ("Watchlist", "Dismiss", "Calculator", "AI review", "Skip", "Back", "Open listing",
                         "Undo", "Help"):
            self.assertIn(expected, legend)

    def test_legend_has_the_four_dismiss_reasons(self):
        _check("page-reasons")

        reasons = self.page().split('id="rq-legend-reasons"')[1].split("</ul>")[0]

        for key, label in (("1", "Not interested"), ("2", "Too far"), ("3", "Bad photos"), ("4", "Reviewed")):
            self.assertIn(f'<kbd class="kbd">{key}</kbd></span><span>{label}</span>', reasons)

    def test_page_embeds_queue_and_tools(self):
        first = _check("page-1", end_in=timedelta(hours=1))
        second = _lead("page-2")

        content = self.page()
        queue = json.loads(re.search(
            r'<script id="review-queue-data" type="application/json">(.*?)</script>', content, re.S,
        ).group(1))

        self.assertEqual([c["lot_id"] for c in queue], [first.pk, second.pk])
        self.assertIn('id="rq-progress"', content)
        self.assertIn("1 of 2", content)
        self.assertIn('<dialog id="calc-dialog"', content)
        self.assertIn('<dialog id="lightbox"', content)
        self.assertIn('id="rq-help"', content)
        self.assertIn("js/review_queue.js", content)
        self.assertIn('id="rq-why-box"', content)

    def test_phone_buttons_for_each_action(self):
        _check("page-buttons")

        actions = self.page().split('id="rq-actions"')[1].split("</div>\n\n")[0]

        for _keys, _label, action in SHORTCUTS:
            if action != "help":
                self.assertIn(f'data-action="{action}"', actions)

    def test_empty_queue_is_all_caught_up(self):
        content = self.page()

        self.assertIn("All caught up", content)
        self.assertIn(f'href="{reverse("albright_reselling_app:dashboard")}"', content)
        self.assertNotIn("review_queue.js", content)

    def test_requires_login(self):
        self.client.logout()
        self.assertEqual(self.client.get(reverse("albright_reselling_app:review_queue")).status_code, 302)
