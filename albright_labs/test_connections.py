"""Connections and the share picker (albright_labs.connections, Option C slice 1): implicit connections
from circle co-membership and past person shares; Remove hides grants both ways without deleting them
and restores them when you join a circle together; the type-ahead and search endpoint only ever
return the connected set; My account > People lists people and offers Remove.

Also the view-panel modal fix: the no-JS <select> stays a single-line dropdown, the picker
initializes when the Share box is injected after load, and sharing a Planner task from that
markup with target=user:<id> saves."""
import pathlib
import re

from django.contrib.auth.models import User
from django.contrib.staticfiles import finders
from django.core.cache import cache
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from albright_notes.models import Note
from albright_planner.models import ScheduleItem

from . import connections, sharing
from .models import Connection, ShareGrant
from .testing import share_with, with_apps

PASSWORD = "pw-not-real-12345"
APPS = ["planner", "notes", "meals", "fitness"]


@override_settings(BOT_API_REQUIRE_HTTPS=False, USDA_API_KEY="")
class Base(TestCase):
    def setUp(self):
        cache.clear()
        self.nate = with_apps(User.objects.create_user(username="nate", password=PASSWORD, first_name="Nathan",
                                                       email="nate@example.com"), APPS)
        self.sam = with_apps(User.objects.create_user(username="sam", password=PASSWORD, first_name="Sam",
                                                      email="sam@example.com"), APPS)
        self.kim = with_apps(User.objects.create_user(username="kim", password=PASSWORD, first_name="Kim",
                                                      email="kim@example.com"), APPS)
        self.kate = with_apps(User.objects.create_user(username="kate", password=PASSWORD, first_name="Kate",
                                                       email="kate@example.com"), APPS)
        self.stranger = with_apps(User.objects.create_user(username="jordan", password=PASSWORD, first_name="Jordan",
                                                           email="jordan@example.com"), APPS)
        self.client.force_login(self.nate)
        self.today = timezone.localdate()

    def as_user(self, user):
        c = Client()
        c.force_login(user)
        return c

    def family(self, *others):
        from . import circles
        c = circles.create(self.nate, "Albright Family", APPS)
        for person in others:
            invite, _raw, _s, _r = circles.invite(self.nate, c, person.username)
            circles.accept(person, invite, list(APPS))
        return c

    def note(self, owner=None, title="Packing list"):
        return Note.objects.create(owner=owner or self.nate, title=title)

    def item(self, owner=None, title="Trash night"):
        return ScheduleItem.objects.create(owner=owner or self.nate, title=title, date=self.today)


class ConnectedSetTests(Base):
    def test_circle_mates_and_past_shares_are_connected(self):
        self.family(self.sam)
        self.assertTrue(connections.is_connected(self.nate, self.sam))
        self.assertTrue(connections.is_connected(self.sam, self.nate))
        self.assertFalse(connections.is_connected(self.nate, self.kate))
        share_with(self.note(), self.kate)
        self.assertTrue(connections.is_connected(self.nate, self.kate))
        self.assertTrue(connections.is_connected(self.kate, self.nate))
        # The other direction: they shared with you.
        theirs = self.note(self.kim, "Kim's list")
        share_with(theirs, self.nate)
        self.assertTrue(connections.is_connected(self.nate, self.kim))
        self.assertNotIn(self.stranger, connections.connected_people(self.nate))
        names = [u.first_name for u in connections.connected_people(self.nate)]
        self.assertEqual(sorted(names), ["Kate", "Kim", "Sam"])

    def test_reason_lines(self):
        self.family(self.sam, self.kim)
        share_with(self.item(), self.kim)
        share_with(self.note(self.kate, "Kate's note"), self.nate)
        self.assertEqual(connections.reason(self.nate, self.sam), "In Albright Family")
        self.assertEqual(connections.reason(self.nate, self.kim), "In Albright Family · you share Planner items")
        self.assertEqual(connections.reason(self.nate, self.kate), "Shared notes with you")


class RemoveAndRestoreTests(Base):
    def test_remove_hides_grants_both_ways_and_restore_unhides_them(self):
        mine = self.note(title="Gift ideas")
        theirs = self.note(self.kate, "Kate's list")
        g1 = share_with(mine, self.kate, "edit")
        g2 = share_with(theirs, self.nate, "view")
        self.assertTrue(sharing.can_edit(self.kate, mine))
        self.assertTrue(sharing.can_view(self.nate, theirs))

        connections.remove(self.nate, self.kate)
        g1.refresh_from_db()
        g2.refresh_from_db()
        self.assertIsNotNone(g1.hidden_at)
        self.assertIsNotNone(g2.hidden_at)
        self.assertTrue(ShareGrant.objects.filter(pk=g1.pk).exists())          # not deleted
        self.assertFalse(sharing.can_view(self.kate, mine))
        self.assertFalse(sharing.can_view(self.nate, theirs))
        self.assertFalse(connections.is_connected(self.nate, self.kate))
        self.assertFalse(connections.is_connected(self.kate, self.nate))
        self.assertEqual(self.as_user(self.kate).get(reverse("albright_notes:note", args=[mine.pk])).status_code, 404)
        # Hidden grants don't show as chips or on Shared with me.
        page = self.client.get(reverse("albright_notes:note", args=[mine.pk])).content.decode()
        self.assertNotIn("Kate", page.split('id="share-box"', 1)[1].split("</section>", 1)[0])
        self.assertNotIn("Gift ideas", self.as_user(self.kate).get(reverse("labs:shared_with_me")).content.decode())

        # Either side can remove (already removed: vague error). The other side also no longer sees you.
        with self.assertRaises(connections.ConnectionError):
            connections.remove(self.kate, self.nate)
        self.assertNotIn(self.nate, connections.connected_people(self.kate))

        # Joining a circle together restores the same grants and permission.
        self.family(self.kate)
        g1.refresh_from_db()
        g2.refresh_from_db()
        self.assertIsNone(g1.hidden_at)
        self.assertIsNone(g2.hidden_at)
        self.assertEqual(g1.permission, "edit")
        self.assertTrue(sharing.can_edit(self.kate, mine))
        self.assertTrue(sharing.can_view(self.nate, theirs))
        self.assertTrue(connections.is_connected(self.nate, self.kate))
        self.assertIn("Gift ideas", self.as_user(self.kate).get(reverse("labs:shared_with_me")).content.decode())

    def test_cannot_remove_a_circle_mate(self):
        self.family(self.sam)
        with self.assertRaises(connections.ConnectionError) as ctx:
            connections.remove(self.nate, self.sam)
        self.assertIn("Leave the circle", str(ctx.exception))
        self.assertTrue(connections.is_connected(self.nate, self.sam))
        self.assertFalse(Connection.objects.filter(status=Connection.REMOVED).exists())

    def test_either_side_can_remove(self):
        share_with(self.note(), self.kate)
        connections.remove(self.kate, self.nate)
        self.assertFalse(connections.is_connected(self.nate, self.kate))
        self.assertFalse(sharing.can_view(self.kate, Note.objects.get(title="Packing list")))

    def test_circle_grants_are_untouched_by_removing_someone_else(self):
        from . import circles
        family = self.family(self.sam)
        item = self.item()
        share_with(item, circle=family, permission="view")
        share_with(self.note(), self.kate)
        connections.remove(self.nate, self.kate)
        self.assertTrue(sharing.can_view(self.sam, item))
        circles.leave(self.sam, family)
        self.assertFalse(sharing.can_view(self.sam, item))            # leaving the circle still drops circle access

    def test_removed_person_cannot_be_shared_with_by_exact_match(self):
        share_with(self.note(), self.kate)
        connections.remove(self.nate, self.kate)
        item = self.item()
        with self.assertRaises(sharing.ShareError) as ctx:
            sharing.share(self.nate, item, who="kate")
        self.assertIn("connections", str(ctx.exception).lower())
        self.assertFalse(sharing.can_view(self.kate, item))
        # The hidden grant on the note is still there, still hidden.
        self.assertTrue(ShareGrant.objects.filter(user=self.kate, hidden_at__isnull=False).exists())

    def test_leaving_a_circle_keeps_person_shares_until_you_remove(self):
        from . import circles
        family = self.family(self.sam)
        item = self.item()
        share_with(item, self.sam, "view")
        circles.leave(self.sam, family)
        self.assertTrue(connections.is_connected(self.nate, self.sam))
        self.assertTrue(sharing.can_view(self.sam, item))
        connections.remove(self.nate, self.sam)
        self.assertFalse(sharing.can_view(self.sam, item))


class PickerAndSearchTests(Base):
    def test_picker_lists_connected_people_and_circles_not_strangers(self):
        self.family(self.sam)
        share_with(self.note(), self.kate)
        item = self.item()
        page = self.client.get(reverse("albright_planner:item", args=[item.pk])).content.decode()
        box = page.split('id="share-box"', 1)[1]
        self.assertIn("Search people or circles", box)
        self.assertIn(f'value="circle:', box)
        self.assertIn(f'value="user:{self.sam.pk}"', box)
        self.assertIn(f'value="user:{self.kate.pk}"', box)
        self.assertNotIn(f'value="user:{self.stranger.pk}"', box)
        self.assertNotIn("Exact username or email", box)
        self.assertIn("Albright Family", box)
        self.assertIn("/home/account/people/search/", box)

    def test_fallback_select_is_a_single_line_dropdown(self):
        self.family(self.sam)
        page = self.client.get(reverse("albright_planner:item", args=[self.item().pk])).content.decode()
        self.assertRegex(page, r'<select name="target"[^>]*\bsize="1"')
        self.assertIn("Share with…", page)
        css = pathlib.Path(finders.find("style.css")).read_text(encoding="utf-8")
        self.assertIn("select.share-add__target, select[data-share-target], select[data-share-mode]", css)
        self.assertIn("max-height: 44px", css)
        self.assertIn(".share-add .share-add__target, .share-add .share-add__who { flex: 0 0 auto;", css)

    def test_picker_js_initializes_dynamically_inserted_share_boxes(self):
        js = pathlib.Path(finders.find("labs/share.js")).read_text(encoding="utf-8")
        self.assertIn("window.AlbrightShare", js)
        self.assertIn("MutationObserver", js)
        self.assertIn('headers["X-CSRFToken"]', js)
        planner = pathlib.Path(finders.find("albright_planner/planner.js")).read_text(encoding="utf-8")
        self.assertRegex(planner, r'box\.innerHTML = html;\s*if \(window\.AlbrightShare')

    def test_share_js_posts_to_the_action_attribute_not_form_action(self):
        """The Share forms have <input name="action">, which in every major browser
        shadows HTMLFormElement.action (form.action is the input, not the URL).
        fetch(form.action) throws Failed to fetch and the status line shows
        "Not saved - try again" without a request ever leaving the phone."""
        js = pathlib.Path(finders.find("labs/share.js")).read_text(encoding="utf-8")
        self.assertIn('form.getAttribute("action")', js)
        self.assertIn("fetch(formAction(form)", js)
        self.assertNotIn("fetch(form.action,", js)
        # The server's error text is what the status line shows; a non-JSON body
        # (403, a login page) includes the HTTP status so it's diagnosable.
        self.assertIn('data.error || "Not saved - try again."', js)
        self.assertIn('(HTTP "', js)
        self.family(self.sam)
        page = self.client.get(reverse("albright_planner:item", args=[self.item().pk])).content.decode()
        self.assertIn('name="action" value="add"', page)
        self.assertRegex(page, r'<form method="post" action="[^"]+" class="share-add"')

    def test_share_error_json_includes_the_server_message(self):
        """A failed add (empty target, as if the 'Share with…' placeholder was
        posted) returns the ShareError text in JSON so share.js can show it."""
        self.family(self.sam)
        item = self.item()
        url = reverse("labs:share", args=["albright_planner.ScheduleItem", item.pk])
        empty = self.client.post(url, {"action": "add", "target": "", "permission": "view"},
                                 HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(empty.status_code, 400)
        data = empty.json()
        self.assertFalse(data["ok"])
        self.assertTrue(data["error"])
        self.assertNotEqual(data["error"], "Not saved - try again.")

    def test_sharing_a_planner_task_from_the_view_panel_markup(self):
        """Two circle-mates: the view panel (the modal's markup) lists the other person, and
        posting the fallback form's fields the way share.js does (AJAX, target=user:<id>) saves."""
        self.family(self.sam)
        item = ScheduleItem.objects.create(owner=self.sam, title="Test from Abby's phone", date=self.today)
        client = self.as_user(self.sam)
        url = reverse("albright_planner:item", args=[item.pk])
        panel = client.get(url, {"panel": "1", "date": self.today.isoformat()}).content.decode()
        self.assertIn('id="pl-view-panel"', panel)
        self.assertIn('id="share-box"', panel)
        self.assertIn(f'value="user:{self.nate.pk}"', panel)
        share_url = reverse("labs:share", args=["albright_planner.ScheduleItem", item.pk])
        self.assertIn(share_url, panel)
        response = client.post(share_url, {"action": "add", "target": f"user:{self.nate.pk}", "permission": "view"},
                               HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["ok"], data)
        self.assertTrue(sharing.can_view(self.nate, item))
        self.assertIn("share-chip", data["html"])
        self.assertIn("Nathan", data["html"])

    def test_share_via_user_id_requires_a_connection(self):
        self.family(self.sam)
        item = self.item()
        url = reverse("labs:share", args=["albright_planner.ScheduleItem", item.pk])
        ok = self.client.post(url, {"action": "add", "target": f"user:{self.sam.pk}", "permission": "edit"})
        self.assertEqual(ok.status_code, 302)
        self.assertTrue(sharing.can_edit(self.sam, item))
        # A stranger's id is refused; the error does not confirm they exist.
        bad = self.client.post(url, {"action": "add", "target": f"user:{self.stranger.pk}", "permission": "view"},
                               HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(bad.status_code, 400)
        self.assertIn("connected", bad.json()["error"].lower())
        self.assertFalse(sharing.can_view(self.stranger, item))
        # Already shared is marked in the picker payload.
        people = sharing.control(self.nate, item)["people"]
        by_id = {p["id"]: p for p in people}
        self.assertTrue(by_id[self.sam.pk]["already"])

    def test_search_only_returns_the_connected_set(self):
        self.family(self.sam)
        share_with(self.note(), self.kate)
        url = reverse("labs:people_search")
        # Exact email / username of a stranger: the same empty list as a typo. Never a directory hit.
        for q in ("jordan@example.com", "jordan", "Jordan", "no-such-user"):
            data = self.client.get(url, {"q": q, "app": "notes"}).json()
            self.assertEqual(data["people"], [], q)
            self.assertEqual(data["circles"], [], q)
        # A connected person is found by a partial name, and by their email - but the email is not returned.
        found = self.client.get(url, {"q": "ka", "app": "notes"}).json()
        names = [p["name"] for p in found["people"]]
        self.assertIn("Kate", names)
        self.assertNotIn("Sam", names)
        self.assertNotIn("Jordan", names)
        self.assertTrue(all("email" not in p and "username" not in p for p in found["people"]))
        # Circles by name.
        self.assertEqual(self.client.get(url, {"q": "albright", "app": "planner"}).json()["circles"][0]["name"],
                         "Albright Family")
        # Empty query: the whole connected set.
        everyone = self.client.get(url, {"q": "", "app": "notes"}).json()
        self.assertEqual(sorted(p["name"] for p in everyone["people"]), ["Kate", "Sam"])

    def test_search_requires_login_and_is_rate_limited(self):
        url = reverse("labs:people_search")
        self.assertEqual(Client().get(url, {"q": "s"}).status_code, 302)
        self.family(self.sam)
        for _ in range(60):
            self.assertEqual(self.client.get(url, {"q": "s", "app": "planner"}).status_code, 200)
        limited = self.client.get(url, {"q": "s", "app": "planner"})
        self.assertEqual(limited.status_code, 429)
        self.assertEqual(limited.json()["people"], [])
        self.assertEqual(limited["Retry-After"], "60")


class PeoplePageTests(Base):
    def test_lists_people_and_remove_hides_them(self):
        self.family(self.sam)
        share_with(self.note(), self.kate)
        page = self.client.get(reverse("labs:people")).content.decode()
        self.assertIn("Your people", page)
        self.assertIn("Sam", page)
        self.assertIn("Kate", page)
        self.assertNotIn("Jordan", page)
        self.assertIn("Leave the circle to disconnect", page)
        def row(pk):
            m = re.search(r'<div class="people-row" data-person="%s">(.*?)</div>' % pk, page, re.S)
            return m.group(1) if m else ""
        self.assertIn("Remove", row(self.kate.pk))
        self.assertNotIn("Remove", row(self.sam.pk))
        self.assertIn("Leave the circle", row(self.sam.pk))
        # Linked from My account, Circles, and the account menu.
        self.assertIn(reverse("labs:people"), self.client.get(reverse("labs:account")).content.decode())
        self.assertIn(reverse("labs:people"), self.client.get(reverse("labs:circles")).content.decode())
        self.assertIn('id="nav-people-link"', self.client.get(reverse("labs:account")).content.decode())

        response = self.client.post(reverse("labs:people_remove", args=[self.kate.pk]), follow=True)
        gone = response.content.decode()
        self.assertIn("hidden until you reconnect", gone)
        self.assertNotIn(f'data-person="{self.kate.pk}"', gone)
        self.assertIn(f'data-person="{self.sam.pk}"', gone)
        self.assertFalse(connections.is_connected(self.nate, self.kate))
        self.assertTrue(ShareGrant.objects.filter(user=self.kate, hidden_at__isnull=False).exists())

        # Removing a circle-mate is refused; removing a stranger is a vague error (no 404 that confirms an id).
        refused = self.client.post(reverse("labs:people_remove", args=[self.sam.pk]), follow=True)
        self.assertIn("Leave the circle", refused.content.decode())
        self.assertTrue(connections.is_connected(self.nate, self.sam))
        stranger = self.client.post(reverse("labs:people_remove", args=[self.stranger.pk]), follow=True)
        self.assertIn("not in your people", stranger.content.decode())

    def test_people_page_requires_login(self):
        self.assertEqual(Client().get(reverse("labs:people")).status_code, 302)
        self.assertEqual(Client().post(reverse("labs:people_remove", args=[1])).status_code, 302)
