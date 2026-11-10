"""Activity and notifications (albright_labs.activity): events only for shared things and circle spaces (never private
ones); merging repeats and note edits; who sees what checked when they look (unsharing, leaving or opting out hides
events at once); never your own actions; Money's threshold; Fitness's diary and body data never produce events;
preferences (All / Involves me / Off) and quiet hours; unread counts and marking read; the digest email; the Bot
API's activity endpoint; 90-day pruning."""
import json
from datetime import timedelta
from decimal import Decimal
from io import StringIO
from unittest import mock

from django.contrib.auth.models import User
from django.core import mail
from django.core.cache import cache
from django.core.management import call_command
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from albright_bot_api.models import BotToken
from albright_fitness.models import Workout
from albright_meals.models import GroceryItem, MealPlanEntry, Recipe, RecipeRating
from albright_money import budget as budgets
from albright_money.models import Account, Category, Transaction
from albright_notes import notes
from albright_notes.models import Note
from albright_planner import checklist, planner
from albright_planner.models import ScheduleItem

from . import activity, circles, sharing
from .models import ActivityEvent, NotificationSettings, Space
from .testing import allow_api_keys, personal, share_with, with_apps

PASSWORD = "pw-not-real-12345"
API = "/api/bot/v1/"
APPS = ["planner", "notes", "meals", "money", "fitness"]


@override_settings(BOT_API_REQUIRE_HTTPS=False, USDA_API_KEY="")
class Base(TestCase):
    def setUp(self):
        cache.clear()
        outlook = mock.patch("albright_trading_app.views.get_market_outlook", return_value=None)
        outlook.start()
        self.addCleanup(outlook.stop)
        self.nate = with_apps(User.objects.create_user(username="nate", password=PASSWORD, first_name="Nathan",
                                                       email="nate@example.com"), APPS)
        self.kate = with_apps(User.objects.create_user(username="kate", password=PASSWORD, first_name="Kate",
                                                       email="kate@example.com"), APPS)
        self.kim = with_apps(User.objects.create_user(username="kim", password=PASSWORD, first_name="Kim"), APPS)
        self.client.force_login(self.nate)
        self.today = timezone.localdate()

    def as_user(self, user):
        c = Client()
        c.force_login(user)
        return c

    def family(self, kate_apps=APPS, keep_join=False):
        """Nathan's circle sharing every app; Kate joined it with kate_apps (her "joined" event dropped unless
        keep_join)."""
        c = circles.create(self.nate, "Albright Family", APPS)
        invite, _raw, _s, _r = circles.invite(self.nate, c, "kate")
        circles.accept(self.kate, invite, list(kate_apps))
        if not keep_join:
            ActivityEvent.objects.filter(app="circles").delete()
        return c

    def kitchen_of(self, circle):
        return Space.objects.get(circle=circle)

    def seen(self, user):
        """What this person is notified about, as text."""
        return [activity.text(e) for e in activity.notifications(user)]

    def item(self, title="Pack", **kw):
        return ScheduleItem.objects.create(owner=self.nate, title=title, date=self.today, **kw)


# ---------------------------------------------------------------------------
# What gets recorded
# ---------------------------------------------------------------------------

class RecordingTests(Base):
    def test_private_things_record_nothing(self):
        item = self.item()
        self.client.post(reverse("albright_planner:toggle", args=[item.pk]), {"date": self.today.isoformat()})
        note = Note.objects.create(owner=self.nate, title="Diary")
        with activity.acting(self.nate):
            notes.save(note, "nate", body="private")
            GroceryItem.objects.create(space=personal(self.nate), name="Milk", added_by=self.nate)
            Recipe.objects.create(space=personal(self.nate), title="Solo soup", created_by=self.nate)
        account = Account.objects.create(owner=self.nate, space=personal(self.nate), name="Mine", starting_date=self.today)
        self.client.post(reverse("albright_money:quick_add"), {"account": account.pk, "text": "500 Rent"})
        self.assertFalse(ActivityEvent.objects.exists())

    def test_shared_planner_item_completed_edited_and_checked(self):
        c = self.family()
        item = self.item("Pack", kind="task")
        checklist.create_rows(item, ["Socks", "Charger", "Passport"])
        share_with(item, circle=c, permission="edit")
        with activity.acting(self.nate):
            for row in list(item.checklist.all())[:2]:
                checklist.set_checked(row, self.today, True, self.nate, check_task=False)
            planner.change(item, self.today, planner.ALL, {"title": "Pack bags", "date": self.today + timedelta(days=1)})
        self.client.post(reverse("albright_planner:toggle", args=[item.pk]),
                         {"date": (self.today + timedelta(days=1)).isoformat()})
        texts = self.seen(self.kate)
        self.assertIn("Nathan checked 2 items in “Pack” · 2 of 3 done", texts)                     # merged ticks
        self.assertTrue(any(t.startswith("Nathan changed “Pack bags” – renamed it from “Pack”, moved it to") for t in texts))
        self.assertTrue(any(t.startswith("Nathan checked off “Pack bags”") for t in texts))

    def test_repeats_merge_within_15_minutes(self):
        c = self.family()
        space = self.kitchen_of(c)
        with activity.acting(self.kate):
            for name in ("Milk", "Eggs", "Bread", "Apples", "Rice"):
                GroceryItem.objects.create(space=space, name=name, added_by=self.kate)
        self.assertEqual(self.seen(self.nate), ["Kate added 5 items to the grocery list"])
        ActivityEvent.objects.update(updated_at=timezone.now() - timedelta(minutes=16))
        with activity.acting(self.kate):
            GroceryItem.objects.create(space=space, name="Butter", added_by=self.kate)
        self.assertEqual(self.seen(self.nate)[0], "Kate added “Butter” to the grocery list")
        self.assertEqual(ActivityEvent.objects.count(), 2)

    def test_note_edits_merge_within_10_minutes(self):
        note = Note.objects.create(owner=self.nate, title="Packing list")
        share_with(note, self.kate, "edit")
        with activity.acting(self.kate):
            notes.save(note, "kate", body="one")
            notes.save(note, "kate", body="two")
        self.assertEqual(ActivityEvent.objects.get().count, 2)
        ActivityEvent.objects.update(updated_at=timezone.now() - timedelta(minutes=11))
        with activity.acting(self.kate):
            notes.save(note, "kate", body="three")
            notes.save(note, "kate", body="three")                    # nothing changed: nothing recorded
        self.assertEqual(ActivityEvent.objects.count(), 2)
        self.assertEqual(self.seen(self.nate)[0], "Kate edited the note “Packing list”")

    def test_meals_plan_rating_and_kitchen(self):
        c = self.family()
        space = self.kitchen_of(c)
        with activity.acting(self.kate):
            recipe = Recipe.objects.create(space=space, title="Chili", created_by=self.kate)
            MealPlanEntry.objects.create(space=space, date=self.today, slot="dinner", recipe=recipe, created_by=self.kate)
            RecipeRating.objects.create(recipe=recipe, user=self.kate, level=RecipeRating.FAVORITE)
        texts = self.seen(self.nate)
        self.assertIn("Kate added the recipe “Chili” to the Albright Family kitchen", texts)
        self.assertTrue(any(t.startswith("Kate planned “Chili” for dinner on") for t in texts))
        self.assertIn("Kate rated “Chili” “Favorite”", texts)
        share_with(recipe, self.kim, "view")                        # shared beyond the space: no ratings for Kim
        kim_texts = self.seen(self.kim)
        self.assertNotIn("Kate rated “Chili” “Favorite”", kim_texts)
        self.assertIn("Kate added the recipe “Chili” to the Albright Family kitchen", kim_texts)

    def test_bot_actions_say_via_the_bot(self):
        c = self.family()
        allow_api_keys(self.nate)
        _t, raw = BotToken.issue(self.nate, "Recipe Bot", ["schedule:write"])
        response = Client().post(API + "schedule", json.dumps({"title": "Trash night", "date": self.today.isoformat(),
                                                               "share": {"circle": c.pk}}),
                                 content_type="application/json", HTTP_AUTHORIZATION=f"Bearer {raw}")
        self.assertEqual(response.status_code, 201)
        self.assertEqual(self.seen(self.kate), ["Nathan added the task “Trash night” via Recipe Bot"])

    def test_sharing_and_circle_membership_events(self):
        c = self.family(keep_join=True)
        note = Note.objects.create(owner=self.nate, title="Gift ideas")
        self.client.post(reverse("labs:share", args=["albright_notes.Note", note.pk]),
                         {"action": "add", "target": "person", "who": "kim", "permission": "view"})
        self.assertEqual(self.seen(self.kim), ["Nathan shared the note “Gift ideas” with you"])
        self.assertNotIn("Nathan shared the note “Gift ideas” with you", self.seen(self.kate))
        self.assertIn("Kate accepted the invite and joined Albright Family", self.seen(self.nate))
        circles.leave(self.kate, c)
        self.assertIn("Kate left Albright Family", self.seen(self.nate))


# ---------------------------------------------------------------------------
# Who sees what
# ---------------------------------------------------------------------------

class VisibilityTests(Base):
    def test_unsharing_hides_events_at_once(self):
        note = Note.objects.create(owner=self.nate, title="Packing list")
        grant = share_with(note, self.kate, "view")
        with activity.acting(self.nate):
            notes.save(note, "nate", body="socks")
        event = ActivityEvent.objects.get()
        kate = self.as_user(self.kate)
        self.assertEqual(self.seen(self.kate), ["Nathan edited the note “Packing list”"])
        grant.delete()
        self.assertEqual(self.seen(self.kate), [])
        self.assertEqual(kate.get(reverse("labs:notification_open", args=[event.pk])).status_code, 404)
        self.assertNotIn("Packing list", kate.get(reverse("labs:activity")).content.decode())

    def test_leaving_or_opting_out_hides_circle_events(self):
        c = self.family()
        with activity.acting(self.nate):
            GroceryItem.objects.create(space=self.kitchen_of(c), name="Milk", added_by=self.nate)
        self.assertEqual(len(self.seen(self.kate)), 1)
        circles.set_participation(self.kate, c, "meals", False)
        self.assertEqual(self.seen(self.kate), [])
        circles.set_participation(self.kate, c, "meals", True)
        self.assertEqual(len(self.seen(self.kate)), 1)
        circles.leave(self.kate, c)
        self.assertEqual(self.seen(self.kate), [])

    def test_own_actions_are_never_notifications(self):
        c = self.family()
        with activity.acting(self.nate):
            GroceryItem.objects.create(space=self.kitchen_of(c), name="Milk", added_by=self.nate)
        self.assertNotIn("Nathan added", " ".join(self.seen(self.nate)))
        self.assertEqual(activity.unread_count(self.nate), activity.unread(self.nate, activity.notifications(self.nate)).count())
        self.assertIn("Nathan added “Milk” to the grocery list",                  # the feed shows my own, though
                      self.client.get(reverse("labs:activity")).content.decode())

    def test_circle_page_shows_its_recent_activity(self):
        c = self.family()
        with activity.acting(self.kate):
            GroceryItem.objects.create(space=self.kitchen_of(c), name="Milk", added_by=self.kate)
        page = self.client.get(reverse("labs:circle", args=[c.pk])).content.decode()
        self.assertIn('id="circle-activity"', page)
        self.assertIn("Kate added “Milk” to the grocery list", page)


# ---------------------------------------------------------------------------
# Money and Fitness
# ---------------------------------------------------------------------------

class MoneyTests(Base):
    def test_threshold_imports_and_budget(self):
        c = self.family()
        space = self.kitchen_of(c)
        account = Account.objects.create(owner=self.nate, space=space, name="Joint", starting_date=self.today)
        add = lambda text: self.client.post(reverse("albright_money:quick_add"), {"account": account.pk, "text": text})  # noqa: E731
        add("42.18 Publix")
        self.assertEqual(self.seen(self.kate), [])                               # small: never shown
        add("142.00 Costco")
        self.assertEqual(self.seen(self.kate), ["Nathan spent $142.00 – Costco"])
        self.client.post(reverse("labs:circle", args=[c.pk]), {"action": "threshold", "amount": "40"})
        c.refresh_from_db()
        self.assertEqual(c.money_threshold, Decimal("40.00"))
        add("42.18 Publix")
        self.assertEqual(self.seen(self.kate)[0], "Nathan spent $42.18 – Publix")
        from albright_money import imports
        rows = [{"date": self.today.isoformat(), "amount": f"-{n}.00", "payee": f"Shop {n}", "duplicate": False}
                for n in range(1, 4)]
        with activity.acting(self.nate):
            imports.import_rows(account, rows, self.nate)
        self.assertEqual(self.seen(self.kate)[0], "Nathan imported 3 transactions into Joint")
        self.assertEqual(Transaction.objects.filter(account=account).count(), 6)
        groceries = Category.objects.create(owner=self.nate, space=space, name="Groceries")
        with activity.acting(self.kate):
            budgets.save_plan(self.kate, space, budgets.this_month(), {groceries.pk: Decimal("600")})
        self.assertIn(f"Kate updated the {budgets.this_month():%B} budget", self.seen(self.nate))

    def test_deleted_transaction_and_moved_account_hide_their_events(self):
        c = self.family()
        space = self.kitchen_of(c)
        account = Account.objects.create(owner=self.nate, space=space, name="Joint", starting_date=self.today)
        self.client.post(reverse("albright_money:quick_add"), {"account": account.pk, "text": "250 Vet"})
        self.assertEqual(self.seen(self.kate), ["Nathan spent $250.00 – Vet"])
        t = Transaction.objects.get(account=account, payee__icontains="Vet")
        self.client.post(reverse("albright_money:transaction_delete", args=[t.pk]))
        self.assertEqual(self.seen(self.kate), [])
        self.client.post(reverse("albright_money:quick_add"), {"account": account.pk, "text": "300 Dentist"})
        from albright_money import imports
        with activity.acting(self.nate):
            imports.import_rows(account, [{"date": self.today.isoformat(), "amount": "-5.00", "payee": "Shop",
                                           "duplicate": False}], self.nate)
        self.assertEqual(len(self.seen(self.kate)), 2)
        mine = personal(self.nate)
        r = self.client.post(reverse("albright_money:account", args=[account.pk]),
                             {"name": "Joint", "kind": account.kind, "institution": "", "last4": "",
                              "starting_balance": "0.00", "starting_date": self.today.isoformat(), "space": mine.pk})
        self.assertEqual(r.status_code, 302)
        account.refresh_from_db()
        self.assertEqual(account.space_id, mine.pk)
        self.assertEqual(self.seen(self.kate), [])


class FitnessTests(Base):
    def test_shared_workouts_only_never_diary_or_body(self):
        allow_api_keys(self.nate)
        _t, raw = BotToken.issue(self.nate, "bot", ["fitness:write"])
        auth = {"HTTP_AUTHORIZATION": f"Bearer {raw}"}
        post = lambda path, body: Client().post(API + path, json.dumps(body), content_type="application/json", **auth)  # noqa: E731
        self.assertEqual(post("fitness/food", {"label": "Latte", "calories": 190}).status_code, 201)
        self.assertIn(post("fitness/weight", {"weight": 182.4}).status_code, (200, 201))
        self.assertEqual(post("fitness/workouts", {"text": "pushups 20x3"}).status_code, 201)   # not shared
        self.assertFalse(ActivityEvent.objects.exists())
        from albright_fitness import programs
        workout = Workout.objects.create(owner=self.nate, date=self.today, name="Leg day")
        share_with(workout, self.kate, "view")
        programs.finish(workout)
        self.assertEqual(self.seen(self.kate), ["Nathan finished the workout “Leg day”"])
        self.assertFalse(ActivityEvent.objects.exclude(app="fitness").exists())


# ---------------------------------------------------------------------------
# Preferences, quiet hours, unread and the bell
# ---------------------------------------------------------------------------

class PreferenceTests(Base):
    def setUp(self):
        super().setUp()
        self.circle = self.family()
        self.space = self.kitchen_of(self.circle)
        mine = self.item("Kate's errand")
        mine.owner = self.kate
        mine.save()
        share_with(mine, self.nate, "edit")                               # shared with Nathan directly
        theirs = Note.objects.create(owner=self.kate, title="Circle note")
        share_with(theirs, circle=self.circle)
        with activity.acting(self.kate):
            planner.toggle(mine, self.today, self.kate)
            notes.save(theirs, "kate", body="x")
            GroceryItem.objects.create(space=self.space, name="Milk", added_by=self.kate)

    def test_all_involves_me_and_off(self):
        self.assertEqual(len(self.seen(self.nate)), 3)
        page = self.client.get(reverse("labs:notification_settings")).content.decode()
        self.assertIn('name="app:planner"', page)
        self.assertIn(f'name="circle:{self.circle.pk}"', page)
        self.client.post(reverse("labs:notification_settings"), {"app:meals": "off"})
        self.assertEqual(len(self.seen(self.nate)), 2)
        levels = {"app:meals": "off", f"circle:{self.circle.pk}": "mine"}     # the form sends every level
        self.client.post(reverse("labs:notification_settings"), levels)
        self.assertEqual(self.seen(self.nate), ["Kate checked off “Kate's errand”"])    # shared with me directly
        self.client.post(reverse("labs:notification_settings"), {**levels, "app:planner": "off"})
        self.assertEqual(self.seen(self.nate), [])

    def test_quiet_hours_hide_the_count(self):
        self.assertEqual(activity.unread_count(self.nate), 3)
        self.assertIn('id="notif-bell-count">3<', self.client.get(reverse("labs:account")).content.decode())
        now = timezone.localtime()
        start, end = (now - timedelta(hours=1)).time().replace(microsecond=0), (now + timedelta(hours=1)).time().replace(microsecond=0)
        self.client.post(reverse("labs:notification_settings"), {"quiet": "1", "quiet_start": start.strftime("%H:%M"),
                                                                 "quiet_end": end.strftime("%H:%M")})
        self.assertEqual(activity.unread_count(self.nate), 0)
        self.assertIn('id="notif-bell-count" hidden', self.client.get(reverse("labs:account")).content.decode())
        prefs = NotificationSettings.for_user(self.nate)
        self.assertFalse(activity.in_quiet_hours(prefs, timezone.now() + timedelta(hours=3)))

    def test_unread_open_and_mark_all_read(self):
        panel = self.client.get(reverse("labs:notifications_panel")).content.decode()
        self.assertEqual(panel.count("act-row is-unread"), 3)
        event = activity.notifications(self.nate).filter(app="meals").get()
        response = self.client.get(reverse("labs:notification_open", args=[event.pk]))
        self.assertEqual(response["Location"], event.url)
        self.assertEqual(activity.unread_count(self.nate), 2)
        with activity.acting(self.kate):                                   # it grows: unread again
            GroceryItem.objects.create(space=self.space, name="Eggs", added_by=self.kate)
        self.assertEqual(activity.unread_count(self.nate), 3)
        self.assertEqual(self.client.post(reverse("labs:notifications_read_all"), HTTP_X_REQUESTED_WITH="XMLHttpRequest").json(),
                         {"ok": True, "unread": 0})
        self.assertEqual(activity.unread_count(self.nate), 0)
        self.assertIn("Kate added 2 items to the grocery list",
                      self.client.get(reverse("labs:activity") + "?app=meals").content.decode())
        self.assertNotIn("act-row", self.client.get(reverse("labs:activity") + "?unread=1").content.decode()
                         .split('id="activity-feed"', 1)[1].split("</section>", 1)[0])

    def test_activity_page_filters(self):
        url = reverse("labs:activity")
        self.assertIn("Milk", self.client.get(url + "?app=meals").content.decode())
        self.assertNotIn("Milk", self.client.get(url + "?app=planner").content.decode())
        self.assertNotIn("Kate&#x27;s errand", self.client.get(url + f"?circle={self.circle.pk}").content.decode())
        self.assertIn("Milk", self.client.get(url + f"?person={self.kate.pk}").content.decode())
        self.assertNotIn("Milk", self.client.get(url + f"?person={self.nate.pk}").content.decode())


# ---------------------------------------------------------------------------
# The digest, the Bot API and pruning
# ---------------------------------------------------------------------------

@override_settings(EMAIL_CONFIGURED=True, EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class DigestAndApiTests(Base):
    def setUp(self):
        super().setUp()
        self.circle = self.family()
        with activity.acting(self.kate):
            GroceryItem.objects.create(space=self.kitchen_of(self.circle), name="Milk", added_by=self.kate)

    def test_daily_digest(self):
        self.assertEqual(activity.send_digests(), (0, 0))                     # off by default
        prefs = NotificationSettings.for_user(self.nate)
        prefs.digest = True
        prefs.save()
        out = StringIO()
        call_command("send_activity_digest", stdout=out)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["nate@example.com"])
        self.assertIn("Kate added “Milk” to the grocery list", mail.outbox[0].body)
        self.assertIn("Sent 1 digest", out.getvalue())
        self.assertEqual(activity.send_digests(), (0, 1))                     # nothing new since
        with mock.patch("albright_labs.emails.send", return_value=(True, "Sent.")) as send:
            with activity.acting(self.kate):
                GroceryItem.objects.create(space=self.kitchen_of(self.circle), name="Eggs", added_by=self.kate)
            prefs.refresh_from_db()
            prefs.quiet_start, prefs.quiet_end = (timezone.localtime() - timedelta(hours=1)).time(), \
                (timezone.localtime() + timedelta(hours=1)).time()
            prefs.save()
            self.assertEqual(activity.send_digests(), (0, 1))                 # quiet hours: held
            send.assert_not_called()

    def test_bot_activity_endpoint(self):
        allow_api_keys(self.nate)
        _t, raw = BotToken.issue(self.nate, "bot", ["notes:read"])
        no_scope = Client().get(API + "activity", HTTP_AUTHORIZATION=f"Bearer {raw}")
        self.assertEqual(no_scope.status_code, 403)
        token, raw = BotToken.issue(self.nate, "summary bot", ["activity:read"])
        data = Client().get(API + "activity", HTTP_AUTHORIZATION=f"Bearer {raw}").json()
        self.assertEqual([e["text"] for e in data["events"]], ["Kate added “Milk” to the grocery list"])
        self.assertEqual((data["events"][0]["circle"]["name"], data["events"][0]["unread"]), ("Albright Family", True))
        later = (timezone.now() + timedelta(minutes=1)).isoformat()
        self.assertEqual(Client().get(API + "activity", {"since": later}, HTTP_AUTHORIZATION=f"Bearer {raw}").json()["events"], [])
        self.assertEqual(Client().get(API + "activity?since=yesterday", HTTP_AUTHORIZATION=f"Bearer {raw}").status_code, 400)
        _t2, kate_raw = BotToken.issue(allow_api_keys(self.kate), "kate bot", ["activity:read"])
        self.assertEqual(Client().get(API + "activity", HTTP_AUTHORIZATION=f"Bearer {kate_raw}").json()["events"], [])  # her own
        docs = Client().get(API + "docs", HTTP_AUTHORIZATION=f"Bearer {raw}").content.decode()
        self.assertIn("GET  activity?since=", docs)
        keys_page = self.client.get(reverse("albright_bot_api:my_keys")).content.decode()
        self.assertIn('value="activity:read"', keys_page)

    def test_prune_after_90_days(self):
        old = ActivityEvent.objects.get()
        old.updated_at = timezone.now() - timedelta(days=91)
        old.save()
        with activity.acting(self.kate):
            GroceryItem.objects.create(space=self.kitchen_of(self.circle), name="Eggs", added_by=self.kate)
        ActivityEvent.objects.exclude(pk=old.pk).update(updated_at=timezone.now() - timedelta(days=89))
        out = StringIO()
        call_command("prune_activity", stdout=out)
        self.assertIn("Deleted 1 activity event", out.getvalue())
        self.assertFalse(ActivityEvent.objects.filter(pk=old.pk).exists())
        self.assertEqual(ActivityEvent.objects.count(), 1)
