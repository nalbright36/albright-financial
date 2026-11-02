"""Sharing in the Planner: items shared with you appear everywhere yours do (Day, Week, Month, overdue, Home's Today
panel, the launcher's count, the Bot API) - one-off and repeating; a check-off is shared, with "Done by <name>";
the owner's initial and "Shared by <name>", a dashed border, the "shared" mark on your own shared items, and the
All / Mine / Shared with me filter; "Share with" on the new and edit forms with "Remember as my default", which
new items and quick add use; View vs Edit; only the owner deletes or changes sharing."""
import json
from datetime import date, timedelta

from django.test import Client, override_settings

from albright_bot_api.models import BotToken

from . import planner
from .models import ItemCompletion, ItemShare, PlannerSettings, ScheduleItem, TaskChecklistItem
from .tests import TODAY, Base, url

SAM_ITEM = "Pilates with Sam"


def d(day):
    return date(2026, 11, day)


class SharedBase(Base):
    def setUp(self):
        super().setUp()
        self.wife.first_name = "Sam"
        self.wife.save()
        self.me.first_name = "Nathan"
        self.me.save()
        self.wife_client = self.as_user(self.wife)

    def hers(self, title=SAM_ITEM, permission="view", **fields):
        item = self.item(title=title, owner=self.wife, **fields)
        ItemShare.objects.create(item=item, user=self.me, permission=permission)
        return item

    def page(self, view, day=TODAY, client=None, query=""):
        return (client or self.client).get(url(view) + f"?date={day.isoformat()}{query}").content.decode()


class SharedItemsEverywhereTests(SharedBase):
    def test_a_one_off_shared_item_shows_in_day_week_and_month(self):
        self.hers()
        for view in ("day", "week", "month"):
            with self.subTest(view=view):
                self.assertIn(SAM_ITEM, self.page(view))

    def test_a_repeating_shared_item_expands(self):
        self.hers(title="Gym", date=d(2), recurrence="FREQ=WEEKLY;BYDAY=MO,WE,FR", recurrence_count=100)
        self.assertIn("Gym", self.page("week"))
        self.assertEqual([o.date for o in planner.occurrences(self.me, d(2), d(8))], [d(2), d(4), d(6)])   # Mon, Wed, Fri
        day_items = lambda day: self.page("day", day).split('id="pl-day"', 1)[1]     # not the quick-add placeholder
        self.assertIn("Gym", day_items(d(6)))
        self.assertNotIn("Gym", day_items(d(5)))
        self.assertEqual(len([o for o in planner.occurrences(self.me, d(1), d(30)) if o.item.title == "Gym"]), 13)   # 5 Mon + 4 Wed + 4 Fri

    def test_overdue_today_panel_and_counts_include_them(self):
        self.hers(title="Late one", date=TODAY - timedelta(days=2))
        self.hers(title="Today one")
        self.assertIn("Late one", self.page("day"))
        todays, overdue = planner.open_tasks_today(self.me)
        self.assertEqual(([o.title for o in todays], [o.title for o in overdue]), (["Today one"], ["Late one"]))
        panel = planner.today_panel(self.me)
        self.assertEqual((panel["overdue"], [o.title for o in panel["tasks"]]), (1, ["Today one"]))
        from albright_labs import launcher
        self.assertEqual(launcher.planner(self.me), "2 tasks today")

    @override_settings(BOT_API_REQUIRE_HTTPS=False)
    def test_bot_api_lists_them_for_the_tokens_owner(self):
        self.hers()
        from albright_labs.testing import allow_api_keys
        allow_api_keys(self.me)
        _token, raw = BotToken.issue(self.me, "Grok bot", ["schedule:read"])
        data = Client().get(f"/api/bot/v1/schedule?start={TODAY}&end={TODAY}", HTTP_AUTHORIZATION=f"Bearer {raw}").json()
        row = next(r for r in data["occurrences"] if r["title"] == SAM_ITEM)
        self.assertEqual((row["owner"], row["mine"], row["done_by"]), ("Sam", False, None))

    def test_not_shared_still_404s(self):
        private = self.item(title="Hers alone", owner=self.wife)
        self.assertNotIn("Hers alone", self.page("day"))
        self.assertEqual(self.client.get(url("edit", private.pk)).status_code, 404)
        self.assertEqual(self.client.post(url("toggle", private.pk), {"date": TODAY.isoformat()}).status_code, 404)


class SharedCompletionTests(SharedBase):
    def test_either_person_checking_it_off_shows_done_for_both_with_who(self):
        item = self.hers(permission="edit")
        self.client.post(url("toggle", item.pk), {"date": TODAY.isoformat()})
        for client in (self.client, self.wife_client):
            day = self.page("day", client=client)
            li = day.split(f'data-item="{item.pk}"', 1)[0].rsplit("<li", 1)[1] + day.split(f'data-item="{item.pk}"', 1)[1].split("</li>", 1)[0]
            self.assertIn("is-done", li)
            self.assertIn("Done by Nathan", li)
        self.assertIn('id="pl-done-by">Done by Nathan', self.wife_client.get(url("edit", item.pk)).content.decode())
        ItemCompletion.objects.all().delete()
        self.wife_client.post(url("toggle", item.pk), {"date": TODAY.isoformat()})
        self.assertIn("Done by Sam", self.page("day"))

    def test_repeating_completion_is_per_occurrence_and_shared(self):
        item = self.hers(title="Walk", permission="edit", date=d(2), recurrence="FREQ=DAILY", recurrence_count=100)
        self.wife_client.post(url("toggle", item.pk), {"date": TODAY.isoformat()})
        mine = {o.date: (o.done, planner.person(o.done_by) if o.done_by else "") for o in
                planner.occurrences(self.me, d(3), d(5))}
        self.assertEqual(mine, {d(3): (False, ""), d(4): (True, "Sam"), d(5): (False, "")})

    def test_no_done_by_on_an_unshared_item(self):
        item = self.item(title="Just mine")
        self.client.post(url("toggle", item.pk), {"date": TODAY.isoformat()})
        self.assertNotIn("Done by", self.page("day"))


class OwnerBadgeAndFilterTests(SharedBase):
    def test_owner_initial_tooltip_and_dashed_style(self):
        item = self.hers()
        day = self.page("day")
        li = day.split(f'data-item="{item.pk}"', 1)[0].rsplit("<li", 1)[1]
        self.assertIn("is-shared-in", li)
        body = day.split(f'data-item="{item.pk}"', 1)[1].split("</li>", 1)[0]
        self.assertIn('title="Shared by Sam"', body)
        self.assertIn('class="pl-avatar"', body)
        self.assertIn(">S</span>", body)
        month = self.page("month")
        self.assertIn('title="Shared by Sam"', month)
        self.assertIn("pl-chip is-shared-in", month)
        detail = self.client.get(url("edit", item.pk)).content.decode()
        self.assertIn('id="pl-shared-by">Shared by Sam</span> (view only)', detail)

    def test_my_shared_items_get_the_shared_mark(self):
        mine = self.item(title="Family dinner")
        plain = self.item(title="Solo errand")
        ItemShare.objects.create(item=mine, user=self.wife)
        day = self.page("day")
        self.assertIn('title="Shared with Sam" aria-label="Shared"', day.split(f'data-item="{mine.pk}"', 1)[1].split("</li>", 1)[0])
        self.assertNotIn("pl-shared-out", day.split(f'data-item="{plain.pk}"', 1)[1].split("</li>", 1)[0])
        self.assertNotIn("is-shared-in", day.split(f'data-item="{mine.pk}"', 1)[0].rsplit("<li", 1)[1])
        self.assertIn('id="pl-shared-with">Shared with Sam', self.client.get(url("edit", mine.pk)).content.decode())

    def test_all_mine_shared_filter(self):
        self.item(title="My task")
        self.hers()
        self.assertIn('<option value="shared">Shared with me</option>', self.page("day"))
        everything, mine, shared = (self.page("day", query=q) for q in ("", "&who=mine", "&who=shared"))
        self.assertIn("My task", everything)
        self.assertIn(SAM_ITEM, everything)
        self.assertIn("My task", mine)
        self.assertNotIn(SAM_ITEM, mine)
        self.assertIn(SAM_ITEM, shared)
        self.assertNotIn("My task", shared.split('id="pl-day"')[1])
        self.assertIn('<option value="mine" selected>', mine)
        self.assertIn("who=mine", mine.split('id="pl-next"')[0].rsplit("href=", 1)[1])   # the filter follows the arrows


class ShareOnCreateAndEditTests(SharedBase):
    FORM = {"title": "Date night", "kind": "event", "date": "2026-11-07", "repeat": "none", "ends": "never",
            "priority": "none", "share_section": "1"}

    def test_new_item_shared_as_chosen(self):
        self.client.post(url("new"), {**self.FORM, f"share_{self.wife.pk}": "edit"})
        item = ScheduleItem.objects.get(title="Date night")
        self.assertEqual(list(item.shares.values_list("user", "permission")), [(self.wife.pk, "edit")])
        self.assertIn("Date night", self.page("day", d(7), client=self.wife_client))
        self.assertEqual(PlannerSettings.for_user(self.me).default_shares, {})      # not remembered

    def test_new_item_form_lists_planner_people_with_view_or_edit(self):
        page = self.client.get(url("new")).content.decode()
        self.assertIn(f'name="share_{self.wife.pk}"', page)
        self.assertIn(f'name="share_{self.stranger.pk}"', page)
        self.assertIn('<option value="view">', page)
        self.assertIn('<option value="edit">', page)
        self.assertIn('name="remember_shares"', page)

    def test_edit_changes_sharing_owner_only(self):
        item = self.item(title="Plan trip")
        form = {**self.FORM, "title": "Plan trip", "kind": "task", "date": TODAY.isoformat(),
                f"share_{self.wife.pk}": "view"}
        self.client.post(url("edit", item.pk), form)
        self.assertEqual(item.shares.get().permission, "view")
        page = self.client.get(url("edit", item.pk)).content.decode()
        self.assertIn('<option value="view" selected>', page.split(f'name="share_{self.wife.pk}"', 1)[1].split("</select>", 1)[0])
        self.client.post(url("edit", item.pk), {**form, f"share_{self.wife.pk}": ""})
        self.assertFalse(item.shares.exists())
        # An editor's save never touches sharing, even if they send the fields.
        ItemShare.objects.create(item=item, user=self.wife, permission="edit")
        self.wife_client.post(url("edit", item.pk), {**form, f"share_{self.wife.pk}": "", f"share_{self.stranger.pk}": "edit"})
        self.assertEqual(list(item.shares.values_list("user", flat=True)), [self.wife.pk])
        self.assertNotIn("share_section", self.wife_client.get(url("edit", item.pk)).content.decode())

    def test_only_people_with_the_planner(self):
        from django.contrib.auth.models import User
        from albright_labs.testing import with_apps
        notes_only = with_apps(User.objects.create_user(username="notes_only", password="pw-not-real-12345"), ["notes"])
        self.client.post(url("new"), {**self.FORM, f"share_{notes_only.pk}": "edit"})
        self.assertFalse(ItemShare.objects.exists())


class DefaultShareTests(SharedBase):
    def remember(self):
        self.client.post(url("new"), {**ShareOnCreateAndEditTests.FORM, f"share_{self.wife.pk}": "edit",
                                      "remember_shares": "1"})

    def test_remembered_default_preselects_the_new_form(self):
        self.remember()
        self.assertEqual(PlannerSettings.for_user(self.me).default_shares, {str(self.wife.pk): "edit"})
        page = self.client.get(url("new")).content.decode()
        self.assertIn('<option value="edit" selected>', page.split(f'name="share_{self.wife.pk}"', 1)[1].split("</select>", 1)[0])

    def test_quick_add_uses_the_default_with_a_chip(self):
        self.remember()
        day = self.page("day")
        self.assertIn('id="pl-quick-share-label">Shared with Sam</summary>', day)
        self.client.post(url("quick_add"), {"text": "Call plumber tomorrow"})          # no chip fields: the default
        item = ScheduleItem.objects.get(title__icontains="plumber")
        self.assertEqual(list(item.shares.values_list("user", "permission")), [(self.wife.pk, "edit")])
        # Changed on the chip before saving: not shared this time.
        self.client.post(url("quick_add"), {"text": "Buy gift", "share_section": "1", f"share_{self.wife.pk}": ""})
        self.assertFalse(ScheduleItem.objects.get(title__icontains="gift").shares.exists())

    def test_quick_add_without_a_default_is_private(self):
        self.assertIn('id="pl-quick-share-label">Only me</summary>', self.page("day"))
        self.client.post(url("quick_add"), {"text": "Read book"})
        self.assertFalse(ItemShare.objects.exists())

    def test_items_from_notes_groceries_and_bots_use_the_default(self):
        self.remember()
        from albright_meals.models import GroceryItem
        from albright_notes.models import Note
        note = Note.objects.create(owner=self.me, title="Packing", body="x")
        self.client.post(url("from_note", note.pk), {"date": TODAY.isoformat()})
        line = GroceryItem.objects.create(name="milk")
        self.client.post(url("from_groceries"), {"item": [line.pk], "date": TODAY.isoformat()})
        for title in ("Packing", "Grocery run"):
            self.assertTrue(ItemShare.objects.filter(item__title=title, user=self.wife, permission="edit").exists(), title)


class PermissionTests(SharedBase):
    def test_view_sees_it_and_its_checklist_but_changes_nothing(self):
        item = self.hers(title="Shop")
        row = TaskChecklistItem.objects.create(task=item, text="Bread")
        page = self.client.get(url("edit", item.pk)).content.decode()
        self.assertIn("Bread", page)
        self.assertNotIn('id="pl-cl-add"', page)
        for name, args, data in (("toggle", [item.pk], {"date": TODAY.isoformat()}),
                                 ("edit", [item.pk], {"title": "x"}),
                                 ("move", [item.pk], {"date": TODAY.isoformat(), "to": "2026-11-06"}),
                                 ("checklist_add", [item.pk], {"text": "Eggs"}),
                                 ("checklist_toggle", [item.pk, row.pk], {})):
            with self.subTest(name=name):
                self.assertEqual(self.client.post(url(name, *args), data).status_code, 404)
        self.assertEqual(item.checklist.count(), 1)

    def test_edit_can_change_move_check_off_and_add_to_the_checklist(self):
        item = self.hers(title="Shop", permission="edit")
        self.client.post(url("checklist_add", item.pk), {"text": "Eggs"})
        self.client.post(url("checklist_add", item.pk), {"text": "Milk"})
        self.assertEqual(list(item.checklist.values_list("text", "order")), [("Eggs", 0), ("Milk", 1)])
        row = item.checklist.first()
        self.client.post(url("checklist_toggle", item.pk, row.pk))
        row.refresh_from_db()
        self.assertTrue(row.done)
        self.client.post(url("move", item.pk), json.dumps({"date": TODAY.isoformat(), "to": "2026-11-06"}),
                         content_type="application/json")
        item.refresh_from_db()
        self.assertEqual(item.date, d(6))
        self.assertIn('id="pl-cl-add"', self.client.get(url("edit", item.pk)).content.decode())

    def test_only_the_owner_deletes_or_reshares(self):
        item = self.hers(permission="edit")
        self.assertEqual(self.client.post(url("remove", item.pk), {"scope": "all"}).status_code, 404)
        self.assertEqual(self.client.post(url("share_add", item.pk), {"user": self.stranger.pk}).status_code, 404)
        share = item.shares.get()
        self.assertEqual(self.client.post(url("share_update", item.pk, share.pk), {"remove": "1"}).status_code, 404)
        page = self.client.get(url("edit", item.pk)).content.decode()
        self.assertNotIn('id="pl-delete"', page)
        self.assertNotIn('id="pl-share-with"', page)
        self.assertTrue(ScheduleItem.objects.filter(pk=item.pk).exists())
        self.wife_client.post(url("remove", item.pk), {"scope": "all"})                  # the owner can
        self.assertFalse(ScheduleItem.objects.filter(pk=item.pk).exists())
