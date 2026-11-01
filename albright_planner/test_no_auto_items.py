"""Only items you add yourself: every automatic source is off by default (nothing is made by scans, watchlist
changes, a markdown coming due or an opportunity deadline, nor by opening the Planner), a deleted source item
never comes back even with its source on, your own / note-linked / bot items are untouched, and data migration
0004 removes only the items the sync made."""
import importlib
from datetime import timedelta
from decimal import Decimal

from django.apps import apps as django_apps
from django.utils import timezone

from albright_notes.models import Note

from . import services, sources
from .models import DeletedSourceItem, ItemCompletion, ItemException, PlannerSettings, ScheduleItem
from .tests import TODAY, Base, url

migration_0004 = importlib.import_module("albright_planner.migrations.0004_delete_auto_items")


class Fixtures:
    def source_records(self):
        """A reseller scan's lot plus a watchlist change, a markdown coming due, and an opportunity deadline."""
        from albright_crypto_app.models import Opportunity, Source
        from albright_reselling_app.models import BidWatch, LedgerEntry, SourcedLot
        today = timezone.localdate()
        lot, _ = SourcedLot.objects.update_or_create(          # what a scan does with each lot it finds
            source="shopgoodwill", external_id="scan-1",
            defaults={"url": "https://shopgoodwill.com/item/scan-1", "title": "Morgan dollar lot",
                      "end_time": timezone.now() + timedelta(days=1)})
        watch = BidWatch.objects.create(lot=lot)               # added to the watchlist...
        watch.my_max_bid = Decimal("25.00")                     # ...and changed
        watch.save()
        lot.end_time = timezone.now() + timedelta(days=2)      # the next scan moves its end time
        lot.save()
        LedgerEntry.objects.create(owner=self.me, item="Silver spoon", cost=Decimal("5"), status="holding",
                                   listed_at=today - timedelta(days=20), list_price=Decimal("30"))
        feed = Source.objects.create(kind="rss", name="feed", url="https://example.org/feed")
        Opportunity.objects.create(source=feed, external_id="1", url="https://example.org/1", title="Grant",
                                   status="saved", application_deadline=timezone.now() + timedelta(days=3))
        return lot

    def mine(self):
        """One of each kind of item that must survive: my own, note-linked, bot-posted."""
        manual = ScheduleItem.objects.create(owner=self.me, title="Call dentist", date=TODAY, created_by="nate")
        note = Note.objects.create(owner=self.me, title="Packing list", body="Passport")
        self.client.post(url("from_note", note.pk), {"date": TODAY.isoformat()})
        linked = ScheduleItem.objects.get(note=note)
        bot = ScheduleItem.objects.create(owner=self.me, title="Water plants", date=TODAY, created_by="Grok bot",
                                          from_bot=True)
        return [manual, linked, bot]


class DefaultsTests(Fixtures, Base):
    def test_every_source_is_off_by_default(self):
        prefs = PlannerSettings.for_user(self.me)
        self.assertEqual(prefs.sources_on, [])
        self.assertFalse(any(prefs.source_on(k) for k in sources.SOURCE_KEYS))
        page = self.client.get(url("settings")).content.decode()
        for source in sources.SOURCES:
            self.assertIn(f'value="{source.key}">', page)                      # listed, not checked
            self.assertNotIn(f'value="{source.key}" checked', page)
        self.assertIn("Automatically add auction end times from my Watchlist", page)

    def test_no_automatic_items_with_default_settings(self):
        self.source_records()
        for view in ("day", "week", "month", "home", "settings"):
            self.client.get(url(view))
        sources.sync(self.me)                                   # even an explicit sync adds nothing
        self.client.post(url("settings"), {"show_meals": "1"})  # saving settings with nothing ticked
        self.assertFalse(ScheduleItem.objects.exists())

    def test_page_loads_never_sync_even_with_a_source_on(self):
        self.source_records()
        PlannerSettings.objects.update_or_create(user=self.me, defaults={"sources_on": ["watchlist"]})
        for view in ("day", "week", "month"):
            self.client.get(url(view))
        self.assertFalse(ScheduleItem.objects.exists())


class PermanentDeleteTests(Fixtures, Base):
    def setUp(self):
        super().setUp()
        PlannerSettings.objects.update_or_create(user=self.me, defaults={"sources_on": list(sources.SOURCE_KEYS)})

    def test_deleted_source_items_never_come_back(self):
        self.source_records()
        sources.sync(self.me)
        made = {i.source_app: i for i in ScheduleItem.objects.all()}
        self.assertEqual(set(made), {"watchlist", "markdown", "opportunities"})
        for item in made.values():                              # incl. the locked watchlist item: the owner may
            response = self.client.post(url("remove", item.pk), {"scope": "all"})   # delete it, not edit it
            self.assertEqual(response.status_code, 302)
        self.assertFalse(ScheduleItem.objects.exists())
        self.assertEqual(DeletedSourceItem.objects.filter(owner=self.me).count(), 3)
        sources.sync(self.me)
        self.client.post(url("settings"), {"source": list(sources.SOURCE_KEYS)})
        self.assertFalse(ScheduleItem.objects.exists())
        watch = made["watchlist"]
        self.assertEqual(services.upsert_source_item(self.me, "watchlist", watch.source_id, "x", TODAY), (None, False))

    def test_locked_item_page_offers_delete_to_its_owner(self):
        self.source_records()
        sources.sync(self.me)
        item = ScheduleItem.objects.get(source_app="watchlist")
        page = self.client.get(url("edit", item.pk)).content.decode()
        self.assertIn('id="pl-delete"', page)
        self.assertEqual(self.client.post(url("edit", item.pk), {"title": "x"}).status_code, 404)   # still not editable

    def test_a_delete_is_per_person_and_per_record(self):
        services.upsert_source_item(self.me, "markdown", "5:14", "Send offers", TODAY)
        self.client.post(url("remove", ScheduleItem.objects.get().pk), {"scope": "all"})
        self.assertIsNone(services.upsert_source_item(self.me, "markdown", "5:14", "Send offers", TODAY)[0])
        self.assertTrue(services.upsert_source_item(self.me, "markdown", "5:30", "Mark down", TODAY)[1])  # next step
        self.assertTrue(services.upsert_source_item(self.wife, "markdown", "5:14", "Send offers", TODAY)[1])

    def test_a_stale_item_the_sync_removed_can_come_back(self):
        lot = self.source_records()
        sources.sync(self.me)
        lot.bid_watch.delete()
        sources.sync(self.me)                                   # gone from the watchlist: removed, not "deleted"
        self.assertFalse(ScheduleItem.objects.filter(source_app="watchlist").exists())
        self.assertFalse(DeletedSourceItem.objects.exists())


class MyItemsTests(Fixtures, Base):
    def test_manual_note_and_bot_items_are_unaffected(self):
        mine = self.mine()
        self.source_records()
        sources.sync(self.me)                                   # all off
        PlannerSettings.objects.update_or_create(user=self.me, defaults={"sources_on": list(sources.SOURCE_KEYS)})
        sources.sync(self.me)                                   # all on
        PlannerSettings.objects.filter(user=self.me).update(sources_on=[])
        sources.sync(self.me)                                   # off again (source items archived)
        for item in mine:
            item.refresh_from_db()
            self.assertFalse(item.archived)
        self.assertEqual(ScheduleItem.objects.filter(source_app="", archived=False).count(), 3)
        html = self.client.get(url("day") + f"?date={TODAY}").content.decode()
        for title in ("Call dentist", "Packing list", "Water plants"):
            self.assertIn(title, html)

    def test_quick_add_still_works(self):
        self.client.post(url("quick_add"), {"text": "Buy milk", "date": TODAY.isoformat()})
        self.assertTrue(ScheduleItem.objects.filter(title__icontains="milk", source_app="").exists())


class DataMigrationTests(Fixtures, Base):
    def test_removes_only_auto_created_items(self):
        mine = self.mine()
        auto = [services.upsert_source_item(self.me, key, f"{key}-1", f"Auto {key}", TODAY)[0]
                for key in ("watchlist", "markdown", "opportunities")]
        theirs = services.upsert_source_item(self.wife, "opportunities", "deadline:9", "Apply by: X", TODAY)[0]
        ItemCompletion.objects.create(item=auto[1], occurrence_date=TODAY)
        ItemException.objects.create(item=auto[2], occurrence_date=TODAY, action=ItemException.SKIP)
        other_app = services.upsert_source_item(self.me, "someapp", "1", "From another app", TODAY)[0]
        hand_made = ScheduleItem.objects.create(owner=self.me, title="Mine, from the ledger", date=TODAY,
                                                source_app="markdown", source_id="hand-1", created_by="nate")
        bot_sourced = ScheduleItem.objects.create(owner=self.me, title="Bot", date=TODAY, source_app="watchlist",
                                                  source_id="bot-1", created_by="watchlist", from_bot=True)
        ItemCompletion.objects.create(item=mine[0], occurrence_date=TODAY)

        migration_0004.forward(django_apps, None)

        left = set(ScheduleItem.objects.values_list("pk", flat=True))
        self.assertEqual(left, {i.pk for i in mine} | {other_app.pk, hand_made.pk, bot_sourced.pk})
        self.assertFalse(ScheduleItem.objects.filter(pk=theirs.pk).exists())
        self.assertEqual(list(ItemCompletion.objects.values_list("item_id", flat=True)), [mine[0].pk])
        self.assertFalse(ItemException.objects.exists())
        self.assertEqual(set(DeletedSourceItem.objects.values_list("owner_id", "source_app", "source_id")),
                         {(self.me.pk, "watchlist", "watchlist-1"), (self.me.pk, "markdown", "markdown-1"),
                          (self.me.pk, "opportunities", "opportunities-1"),
                          (self.wife.pk, "opportunities", "deadline:9")})
        migration_0004.forward(django_apps, None)               # running it again changes nothing
        self.assertEqual(set(ScheduleItem.objects.values_list("pk", flat=True)), left)
