"""Tests for the Watchlist Archive (BidWatch archive fields, scanner/
bid_watch.py, scanner/watch_archive.py, the Watchlist page's Archive tab):
SOFT remove (archived with a compare_max snapshot), HARD delete (gone,
never counted, no prune protection), auto-archive when an auction closes
on the watchlist, outcome below/at/above/unknown (incl. the $0.01 "at"
tolerance and the 3-day give-up), the summary %s and median split by my
max vs. the scanner's, and prune_lots keeping archived lots."""
import re
from datetime import timedelta
from decimal import Decimal
from io import StringIO
from unittest import mock

from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from albright_reselling_app.scanner.bid_watch import (
    RESOLVE_GIVE_UP_AFTER, archive_closed_watches, outcome_for, resolve_watches, soft_remove,
)
from albright_reselling_app.scanner.dashboard import _watched_lot_ids, watchlist_nav_summary
from albright_reselling_app.scanner.watch_archive import archive_summary, archived_watches, summarize
from albright_reselling_app.scanner_models import AlertSent, BidWatch, LotEvaluation, SourcedLot
from albright_labs.testing import with_apps

D = Decimal


def _lot(external_id, end_in=timedelta(hours=5), current_price="10.00", final_price=None, source="shopgoodwill"):
    return SourcedLot.objects.create(
        source=source, external_id=external_id, url=f"https://example.com/{external_id}",
        title=f"Archive Lot {external_id}", current_price=D(current_price), end_time=timezone.now() + end_in,
        final_price=D(final_price) if final_price is not None else None, is_closed=final_price is not None, raw={},
    )


def _watch(lot, my_max_bid=None, scanner_max=None, category="coins"):
    if scanner_max is not None:
        LotEvaluation.objects.create(lot=lot, category=category, max_bid=D(scanner_max))
    return BidWatch.objects.create(lot=lot, status="watching", my_max_bid=D(my_max_bid) if my_max_bid else None)


def _archived(external_id, compare_max, final_price, outcome, compare_source="manual", reason="closed_on_watchlist",
              source="shopgoodwill", category="coins"):
    """An already-resolved archived watch, for summary math."""
    lot = _lot(external_id, end_in=-timedelta(days=1), final_price=final_price, source=source)
    LotEvaluation.objects.create(lot=lot, category=category)
    return BidWatch.objects.create(
        lot=lot, status="unknown", archived_at=timezone.now() - timedelta(days=1), archive_reason=reason,
        compare_max=D(compare_max) if compare_max is not None else None, compare_source=compare_source,
        final_price=D(final_price) if final_price is not None else None, outcome=outcome,
        resolved_at=timezone.now() if outcome else None,
    )


class LoggedInTestCase(TestCase):
    def setUp(self):
        self.user = with_apps(User.objects.create_user(username="tester", password="pw-not-real-12345"))
        self.client.login(username="tester", password="pw-not-real-12345")


class SoftRemoveTests(LoggedInTestCase):
    def remove(self, lot):
        return self.client.post(reverse("albright_reselling_app:remove_watch", args=[lot.pk]),
                                HTTP_X_REQUESTED_WITH="XMLHttpRequest")

    def test_archives_with_my_max_snapshot(self):
        lot = _lot("soft-1")
        watch = _watch(lot, my_max_bid="25.00", scanner_max="30.00")

        self.assertEqual(self.remove(lot).status_code, 200)

        watch.refresh_from_db()
        self.assertIsNotNone(watch.archived_at)
        self.assertEqual(watch.archive_reason, "soft_removed")
        self.assertEqual(watch.compare_max, D("25.00"))
        self.assertEqual(watch.compare_source, "manual")
        self.assertIsNone(watch.outcome)

    def test_falls_back_to_scanner_max_snapshot(self):
        lot = _lot("soft-2")
        watch = _watch(lot, scanner_max="30.00")

        self.remove(lot)

        watch.refresh_from_db()
        self.assertEqual((watch.compare_max, watch.compare_source), (D("30.00"), "scanner"))

    def test_snapshot_does_not_follow_later_max_changes(self):
        lot = _lot("soft-3")
        watch = _watch(lot, my_max_bid="25.00")
        self.remove(lot)

        watch.refresh_from_db()
        watch.my_max_bid = D("99.00")
        watch.save()
        watch.refresh_from_db()

        self.assertEqual(watch.compare_max, D("25.00"))

    def test_leaves_the_watchlist_but_stays_in_the_archive(self):
        lot = _lot("soft-4")
        _watch(lot, my_max_bid="25.00")

        self.remove(lot)

        self.assertEqual(watchlist_nav_summary()["count"], 0)
        self.assertNotIn(lot.pk, _watched_lot_ids())  # back on the dashboard tables
        self.assertEqual([w.lot_id for w in archived_watches()], [lot.pk])

    def test_plain_post_redirects_to_watchlist(self):
        lot = _lot("soft-5")
        _watch(lot, my_max_bid="25.00")

        response = self.client.post(reverse("albright_reselling_app:remove_watch", args=[lot.pk]))

        self.assertRedirects(response, reverse("albright_reselling_app:watchlist"))

    def test_requires_post_and_an_active_watch(self):
        lot = _lot("soft-6")
        self.assertEqual(self.client.get(reverse("albright_reselling_app:remove_watch", args=[lot.pk])).status_code, 405)
        self.assertEqual(self.remove(lot).status_code, 404)

    def test_soft_removed_then_closed_resolves_against_snapshot(self):
        lot = _lot("soft-7")
        watch = _watch(lot, my_max_bid="25.00")
        self.remove(lot)
        SourcedLot.objects.filter(pk=lot.pk).update(
            end_time=timezone.now() - timedelta(hours=1), is_closed=True, final_price=D("20.00"),
        )

        resolve_watches(lookup=False)

        watch.refresh_from_db()
        self.assertEqual(watch.outcome, "below")
        self.assertEqual(watch.final_price, D("20.00"))
        self.assertEqual(watch.archive_reason, "soft_removed")
        self.assertEqual(watch.status, "watching")  # never bid on - no likely_won/lost

    def test_star_restores_a_soft_removed_lot(self):
        lot = _lot("soft-8")
        watch = _watch(lot, my_max_bid="25.00")
        self.remove(lot)

        response = self.client.post(reverse("albright_reselling_app:toggle_watch", args=[lot.pk]))

        self.assertEqual(response.json(), {"watching": True})
        watch.refresh_from_db()
        self.assertIsNone(watch.archived_at)
        self.assertEqual(watch.archive_reason, "")
        self.assertIsNone(watch.compare_max)
        self.assertEqual(watchlist_nav_summary()["count"], 1)


class HardDeleteTests(LoggedInTestCase):
    def test_deletes_watch_and_watchlist_alerts(self):
        lot = _lot("hard-1")
        _watch(lot, my_max_bid="25.00")
        AlertSent.objects.create(lot=lot, kind="watchlist")
        AlertSent.objects.create(lot=lot, kind="candidate")

        response = self.client.post(reverse("albright_reselling_app:delete_watch", args=[lot.pk]),
                                    HTTP_X_REQUESTED_WITH="XMLHttpRequest")

        self.assertEqual(response.status_code, 200)
        self.assertFalse(BidWatch.objects.filter(lot=lot).exists())
        self.assertFalse(AlertSent.objects.filter(lot=lot, kind="watchlist").exists())
        self.assertTrue(AlertSent.objects.filter(lot=lot, kind="candidate").exists())

    def test_hard_deleted_never_counted(self):
        kept = _archived("hard-kept", "25.00", "20.00", "below")
        gone = _archived("hard-gone", "25.00", "40.00", "above")
        self.client.post(reverse("albright_reselling_app:delete_watch", args=[gone.lot_id]))

        summary = archive_summary(archived_watches())

        self.assertEqual(summary["all"]["resolved"], 1)
        self.assertEqual(summary["all"]["pct_below"], 100.0)
        self.assertEqual([w.pk for w in archived_watches()], [kept.pk])
        content = self.client.get(reverse("albright_reselling_app:watchlist")).content.decode()
        archive_tab = content.split('id="archive"')[1]  # (the flash message naming it sits above)
        self.assertNotIn("Archive Lot hard-gone", archive_tab)
        self.assertIn("Archive Lot hard-kept", archive_tab)

    def test_can_delete_from_the_archive_too(self):
        watch = _archived("hard-archived", "25.00", "20.00", "below")

        self.client.post(reverse("albright_reselling_app:delete_watch", args=[watch.lot_id]))

        self.assertFalse(BidWatch.objects.filter(pk=watch.pk).exists())

    def test_page_buttons(self):
        lot = _lot("hard-buttons")
        _watch(lot, my_max_bid="25.00")

        content = self.client.get(reverse("albright_reselling_app:watchlist")).content.decode()

        self.assertIn(f'data-action-url="{reverse("albright_reselling_app:remove_watch", args=[lot.pk])}"', content)
        self.assertIn(f'data-action-url="{reverse("albright_reselling_app:delete_watch", args=[lot.pk])}"', content)
        self.assertIn('data-confirm="Delete and stop tracking?"', content)
        self.assertRegex(content, r'remove-watch/"\s+title="[^"]*">Remove</button>')
        self.assertRegex(content, r'data-confirm="Delete and stop tracking\?"[^>]*>Delete</button>')


class AutoArchiveTests(LoggedInTestCase):
    def test_closed_on_watchlist_snapshots_at_close(self):
        lot = _lot("auto-1", end_in=-timedelta(minutes=5))
        watch = _watch(lot, scanner_max="30.00")

        archived = archive_closed_watches()

        self.assertEqual([w.pk for w in archived], [watch.pk])
        watch.refresh_from_db()
        self.assertEqual(watch.archive_reason, "closed_on_watchlist")
        self.assertEqual((watch.compare_max, watch.compare_source), (D("30.00"), "scanner"))

    def test_open_lot_not_archived(self):
        _watch(_lot("auto-2"), my_max_bid="25.00")

        self.assertEqual(archive_closed_watches(), [])

    def test_sweep_archives_then_resolves_and_settles_status(self):
        lot = _lot("auto-3", end_in=-timedelta(hours=2), final_price="22.00")
        watch = _watch(lot, my_max_bid="25.00")

        resolved = resolve_watches(lookup=False)

        self.assertEqual([w.pk for w in resolved], [watch.pk])
        watch.refresh_from_db()
        self.assertEqual(watch.archive_reason, "closed_on_watchlist")
        self.assertEqual(watch.outcome, "below")
        self.assertEqual(watch.status, "likely_won")
        self.assertIsNotNone(watch.resolved_at)

    def test_watchlist_page_archives_a_just_closed_lot(self):
        lot = _lot("auto-4", end_in=-timedelta(minutes=1))
        watch = _watch(lot, my_max_bid="25.00")

        content = self.client.get(reverse("albright_reselling_app:watchlist")).content.decode()

        watch.refresh_from_db()
        self.assertEqual(watch.archive_reason, "closed_on_watchlist")
        self.assertIn(lot.title, content.split('id="table-archive"')[1])

    @mock.patch("albright_reselling_app.scanner.bid_watch._find_final_price", return_value=D("31.00"))
    @mock.patch("albright_reselling_app.scanner.bid_watch.ADAPTERS",
                {"shopgoodwill": mock.Mock(return_value=mock.Mock())})
    def test_sweep_uses_per_lot_lookup_when_price_missing(self, mock_find):
        lot = _lot("auto-5", end_in=-timedelta(hours=2))
        watch = _watch(lot, my_max_bid="25.00")

        resolve_watches()

        watch.refresh_from_db()
        lot.refresh_from_db()
        self.assertEqual(watch.outcome, "above")
        self.assertEqual(watch.status, "lost")
        self.assertEqual(lot.final_price, D("31.00"))

    def test_track_closed_flow_resolves_without_lookup(self):
        """track_closed calls resolve_watches(lookup=False) after recording
        prices - so it never triggers a per-lot network lookup."""
        lot = _lot("auto-6", end_in=-timedelta(hours=2))
        watch = _watch(lot, my_max_bid="25.00")

        with mock.patch("albright_reselling_app.scanner.bid_watch._find_final_price") as mock_find:
            resolve_watches(lookup=False)

        mock_find.assert_not_called()
        watch.refresh_from_db()
        self.assertIsNone(watch.outcome)  # pending until a price shows up


class OutcomeTests(TestCase):
    def test_below_at_above_with_one_cent_tolerance(self):
        self.assertEqual(outcome_for(D("24.98"), D("25.00")), "below")
        self.assertEqual(outcome_for(D("24.99"), D("25.00")), "at")
        self.assertEqual(outcome_for(D("25.00"), D("25.00")), "at")
        self.assertEqual(outcome_for(D("25.01"), D("25.00")), "at")
        self.assertEqual(outcome_for(D("25.02"), D("25.00")), "above")

    def test_unknown_without_a_max(self):
        self.assertEqual(outcome_for(D("25.00"), None), "unknown")

    def test_pending_inside_three_days_then_unknown(self):
        self.assertEqual(RESOLVE_GIVE_UP_AFTER, timedelta(days=3))
        inside = _lot("out-inside", end_in=-(RESOLVE_GIVE_UP_AFTER - timedelta(hours=1)))
        outside = _lot("out-outside", end_in=-(RESOLVE_GIVE_UP_AFTER + timedelta(hours=1)))
        w_inside = _watch(inside, my_max_bid="25.00")
        w_outside = _watch(outside, my_max_bid="25.00")

        resolve_watches(lookup=False)

        w_inside.refresh_from_db()
        w_outside.refresh_from_db()
        self.assertIsNone(w_inside.outcome)
        self.assertEqual(w_outside.outcome, "unknown")
        self.assertEqual(w_outside.status, "unknown")

    def test_resolved_watch_not_reresolved(self):
        lot = _lot("out-once", end_in=-timedelta(hours=2), final_price="20.00")
        _watch(lot, my_max_bid="25.00")
        self.assertEqual(len(resolve_watches(lookup=False)), 1)

        self.assertEqual(resolve_watches(lookup=False), [])


class SummaryTests(LoggedInTestCase):
    def test_percentages_and_median_split_by_manual_and_scanner(self):
        # manual: below (-5), at (0), above (+10)
        _archived("sum-m1", "25.00", "20.00", "below")
        _archived("sum-m2", "25.00", "25.00", "at")
        _archived("sum-m3", "25.00", "35.00", "above")
        # scanner: below (-10), below (-2)
        _archived("sum-s1", "30.00", "20.00", "below", compare_source="scanner")
        _archived("sum-s2", "30.00", "28.00", "below", compare_source="scanner")
        # never in the %s
        _archived("sum-unknown", None, "20.00", "unknown", compare_source="")
        _archived("sum-pending", "25.00", None, None)

        summary = archive_summary(archived_watches())

        manual, scanner, everything = summary["manual"], summary["scanner"], summary["all"]
        self.assertEqual(manual["resolved"], 3)
        self.assertEqual(manual["pct_at_or_below"], 66.7)
        self.assertEqual((manual["pct_below"], manual["pct_at"], manual["pct_above"]), (33.3, 33.3, 33.3))
        self.assertEqual(manual["median_diff"], D("0.00"))
        self.assertEqual(manual["pending"], 1)

        self.assertEqual(scanner["resolved"], 2)
        self.assertEqual((scanner["pct_at_or_below"], scanner["pct_below"]), (100.0, 100.0))
        self.assertEqual(scanner["median_diff"], D("-6.00"))

        self.assertEqual(everything["resolved"], 5)
        self.assertEqual(everything["pct_at_or_below"], 80.0)
        self.assertEqual(everything["median_diff"], D("-2.00"))
        self.assertEqual((everything["unknown"], everything["pending"]), (1, 1))

    def test_breakdowns_by_source_category_and_reason(self):
        _archived("bd-1", "25.00", "20.00", "below", source="hibid", category="jewelry", reason="soft_removed")
        _archived("bd-2", "25.00", "30.00", "above", source="shopgoodwill", category="coins")

        summary = archive_summary(archived_watches())

        by_source = {row["key"]: row for row in summary["by_source"]}
        self.assertEqual(by_source["hibid"]["label"], "HiBid")
        self.assertEqual(by_source["hibid"]["pct_below"], 100.0)
        self.assertEqual(by_source["shopgoodwill"]["pct_above"], 100.0)
        by_category = {row["key"]: row for row in summary["by_category"]}
        self.assertEqual(by_category["jewelry"]["resolved"], 1)
        by_reason = {row["key"]: row for row in summary["by_reason"]}
        self.assertEqual(by_reason["soft_removed"]["label"], "Removed early")
        self.assertEqual(by_reason["closed_on_watchlist"]["label"], "Closed on watchlist")

    def test_empty_summary(self):
        stats = summarize([])

        self.assertEqual(stats["resolved"], 0)
        self.assertIsNone(stats["pct_at_or_below"])
        self.assertIsNone(stats["median_diff"])


class ArchiveTabTests(LoggedInTestCase):
    def test_tab_rows_badges_markers_and_win_link(self):
        below = _archived("tab-below", "25.00", "20.00", "below")
        at = _archived("tab-at", "25.00", "25.01", "at")
        above = _archived("tab-above", "25.00", "40.00", "above", compare_source="scanner")
        pending = _archived("tab-pending", "25.00", None, None, reason="soft_removed")

        content = self.client.get(reverse("albright_reselling_app:watchlist")).content.decode()
        table = content.split('id="table-archive"')[1].split("</table>")[0]

        def row(watch):
            return re.search(rf'<tr class="scanner-row" data-lot-id="{watch.lot_id}".*?</tr>', table, re.S).group(0)

        self.assertIn('class="badge gain">Below<', row(below))
        self.assertIn('class="badge warn">At<', row(at))
        self.assertIn('class="badge loss">Above<', row(above))
        self.assertIn('class="badge neutral">Pending<', row(pending))
        self.assertIn("$-5.00", row(below))  # same negative style as the dashboard's headroom cells
        self.assertIn("+$15.00", row(above))
        self.assertIn('class="js-archive-scanner"', row(above))
        self.assertNotIn("js-archive-scanner", row(below))
        self.assertIn("Removed early", row(pending))
        win = "albright_reselling_app:ledger_win_lot"
        self.assertIn(reverse(win, args=[below.lot_id]), row(below))
        self.assertIn(reverse(win, args=[at.lot_id]), row(at))
        self.assertNotIn(reverse(win, args=[above.lot_id]), row(above))
        self.assertNotIn(reverse(win, args=[pending.lot_id]), row(pending))

    def test_tab_has_summary_breakdowns_and_filters(self):
        _archived("tab-sum", "25.00", "20.00", "below")

        content = self.client.get(reverse("albright_reselling_app:watchlist")).content.decode()

        self.assertIn('data-tab="archive">Archive', content)
        for block in ("all", "manual", "scanner"):
            self.assertIn(f'id="archive-summary-{block}"', content)
        for table in ("archive-by-source", "archive-by-category", "archive-by-reason"):
            self.assertIn(f'id="{table}"', content)
        for control in ("ar-outcome", "ar-source", "ar-category", "ar-date-from", "ar-date-to"):
            self.assertIn(f'id="{control}"', content)
        self.assertNotIn('id="recently-closed"', content)


class PruneTests(TestCase):
    def _stale_closed_lot(self, external_id):
        lot = _lot(external_id, end_in=-timedelta(days=200), final_price="5.00")
        SourcedLot.objects.filter(pk=lot.pk).update(last_seen=timezone.now() - timedelta(days=200))
        return lot

    def test_archived_lots_are_kept(self):
        archived = self._stale_closed_lot("prune-archived")
        watch = _watch(archived, my_max_bid="25.00")
        soft_remove(watch)
        unprotected = self._stale_closed_lot("prune-plain")

        call_command("prune_lots", stdout=StringIO())

        self.assertTrue(SourcedLot.objects.filter(pk=archived.pk).exists())
        self.assertFalse(SourcedLot.objects.filter(pk=unprotected.pk).exists())

    def test_hard_deleted_get_no_protection(self):
        lot = self._stale_closed_lot("prune-hard")
        BidWatch.objects.create(lot=lot, status="watching")
        user = with_apps(User.objects.create_user(username="pruner", password="pw-not-real-12345"))
        self.client.force_login(user)
        self.client.post(reverse("albright_reselling_app:delete_watch", args=[lot.pk]))

        call_command("prune_lots", stdout=StringIO())

        self.assertFalse(SourcedLot.objects.filter(pk=lot.pk).exists())
