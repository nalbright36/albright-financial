"""Albright Notes: creating (quick capture), editing and autosave (with the
conflict check), pin, archive, delete; search and tags; sharing (view vs
edit, 404 for everyone else); Markdown sanitizing; checklist ticking; links
to records the user can open (and not to ones they can't); every Bot API
endpoint with and without its scope; the Home tile; the registry and
launcher entry; no access for new users; and the app's static files."""
import json
import re
from io import StringIO

from django.conf import settings
from django.contrib.auth.models import User
from django.contrib.staticfiles import finders
from django.core.cache import cache
from django.core.management import call_command
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from albright_bot_api.models import SCOPE_KEYS, BotRequestLog, BotToken
from albright_labs import app_access, apps_registry, tiles
from albright_labs.models import UserPreference
from albright_labs.testing import launcher_links, launcher_tiles, with_apps

from . import links, notes, render
from .models import ME, Note, NoteLink, NoteShare

PASSWORD = "pw-not-real-12345"
OUTLOOK = "albright_trading_app.views.get_market_outlook"
CRYPTO = {**settings.CRYPTO, "allowlist": ["github.com"]}


def url(name, *args):
    return reverse(f"albright_notes:{name}", args=args)


@override_settings(CRYPTO=CRYPTO)
class Base(TestCase):
    def setUp(self):
        cache.clear()
        self.me = with_apps(User.objects.create_user(username="nate", password=PASSWORD))
        self.wife = with_apps(User.objects.create_user(username="sam", password=PASSWORD), ["notes"])
        self.stranger = with_apps(User.objects.create_user(username="stranger", password=PASSWORD), ["notes"])
        self.client.force_login(self.me)

    def note(self, owner=None, title="Groceries", body="", **fields):
        return notes.create(owner or self.me, title, body, **fields)

    def post_json(self, path, body, client=None):
        return (client or self.client).post(path, data=json.dumps(body), content_type="application/json")

    def as_user(self, user):
        client = Client()
        client.force_login(user)
        return client


class CreateEditTests(Base):
    def test_quick_capture_first_line_is_the_title(self):
        response = self.client.post(url("capture"), {"text": "Call the plumber\n- ask about the water heater"})
        self.assertRedirects(response, url("list"))
        note = Note.objects.get()
        self.assertEqual((note.title, note.body, note.owner, note.created_by), ("Call the plumber", "- ask about the water heater", self.me, ME))
        self.assertContains(self.client.get(url("list")), "Call the plumber")

    def test_quick_capture_needs_text(self):
        self.client.post(url("capture"), {"text": "   "})
        self.assertFalse(Note.objects.exists())

    def test_autosave_saves_and_bumps_the_version(self):
        note = self.note()
        response = self.post_json(url("save", note.pk), {"title": "Groceries (Sat)", "body": "**milk**", "tags": ["Shop", "#home", "shop"], "version": 1})
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["version"], 2)
        self.assertIn("<strong>milk</strong>", data["html"])
        note.refresh_from_db()
        self.assertEqual((note.title, note.body, note.tags, note.version, note.updated_by), ("Groceries (Sat)", "**milk**", ["shop", "home"], 2, "nate"))

    def test_autosave_conflict_when_someone_else_saved_meanwhile(self):
        note = self.note()
        NoteShare.objects.create(note=note, user=self.wife, permission="edit")
        self.assertEqual(self.post_json(url("save", note.pk), {"body": "hers", "version": 1}, self.as_user(self.wife)).status_code, 200)
        response = self.post_json(url("save", note.pk), {"body": "mine", "version": 1})
        self.assertEqual(response.status_code, 409)
        self.assertEqual((response.json()["conflict"], response.json()["updated_by"], response.json()["body"]), (True, "sam", "hers"))
        note.refresh_from_db()
        self.assertEqual(note.body, "hers")
        # "Keep mine (overwrite)"
        self.assertEqual(self.post_json(url("save", note.pk), {"body": "mine", "version": 1, "force": True}).status_code, 200)
        note.refresh_from_db()
        self.assertEqual((note.body, note.version), ("mine", 3))

    def test_pin_archive_delete(self):
        note = self.note()
        self.client.post(url("pin", note.pk))
        note.refresh_from_db()
        self.assertTrue(note.pinned)
        self.client.post(url("archive", note.pk))
        note.refresh_from_db()
        self.assertTrue(note.archived)
        self.assertNotContains(self.client.get(url("list")), f'id="note-{note.pk}"')
        self.assertContains(self.client.get(url("list") + "?archived=1"), f'id="note-{note.pk}"')
        page = self.client.get(url("note", note.pk)).content.decode()
        self.assertIn('data-confirm="Delete', page)
        self.client.post(url("delete", note.pk))
        self.assertFalse(Note.objects.exists())

    def test_list_pinned_first_then_most_recently_updated(self):
        old_pinned = self.note(title="Old pinned", pinned=True)
        older = self.note(title="Older")
        newer = self.note(title="Newer")
        Note.objects.filter(pk=old_pinned.pk).update(updated_at="2020-01-01T00:00:00Z")
        Note.objects.filter(pk=older.pk).update(updated_at="2021-01-01T00:00:00Z")
        content = self.client.get(url("list")).content.decode()
        order = [int(pk) for pk in re.findall(r'id="note-(\d+)"', content)]
        self.assertEqual(order, [old_pinned.pk, newer.pk, older.pk])

    def test_list_row_shows_preview_tags_shared_icon_chips_and_time(self):
        from albright_meals.models import Recipe
        note = self.note(title="Taco night", body="## Plan\nBuy **tortillas** first", tags=["food"])
        NoteShare.objects.create(note=note, user=self.wife)
        links.add(note, links.BY_KEY[("meals", "recipe")], Recipe.objects.create(title="Fish tacos"))
        row = self.client.get(url("list")).content.decode().split(f'id="note-{note.pk}"', 1)[1].split("</a>", 1)[0]
        self.assertIn('<span class="nt-row__preview">Plan</span>', row)
        self.assertIn("#food", row)
        self.assertIn('class="nt-shared"', row)
        self.assertIn('<span class="nt-chip" data-accent="meals">Fish tacos</span>', row)
        self.assertIn("updated just now", row)


class SearchTagTests(Base):
    def test_full_text_search_over_title_body_and_tags_with_prefixes(self):
        a = self.note(title="Water heater", body="Plumber said the anode rod is shot")
        b = self.note(title="Gift list", body="Socks", tags=["christmas"])
        c = self.note(title="Unrelated")
        for q, expected in (("anode", [a]), ("plumb", [a]), ("christ", [b]), ("water rod", [a]), ("gift socks", [b])):
            with self.subTest(q=q):
                found = list(notes.search(Note.objects.all(), q))
                self.assertEqual(found, expected)
        page = self.client.get(url("list") + "?q=plumb").content.decode()
        self.assertIn(f'id="note-{a.pk}"', page)
        self.assertNotIn(f'id="note-{c.pk}"', page)

    def test_tag_filter_and_tag_list(self):
        a = self.note(title="A", tags=["house"])
        b = self.note(title="B", tags=["car"])
        page = self.client.get(url("list") + "?tag=house").content.decode()
        self.assertIn(f'id="note-{a.pk}"', page)
        self.assertNotIn(f'id="note-{b.pk}"', page)
        self.assertIn('<option value="car">#car</option>', page)

    def test_tags_are_cleaned(self):
        self.assertEqual(notes.clean_tags("#Taxes, house , , taxes,  Big   Trip"), ["taxes", "house", "big trip"])
        self.assertEqual(len(notes.clean_tags([f"t{i}" for i in range(40)])), 20)


class SharingTests(Base):
    def test_others_get_404_for_every_page_and_action(self):
        note = self.note()
        stranger = self.as_user(self.stranger)
        self.assertEqual(stranger.get(url("note", note.pk)).status_code, 404)
        for name in ("pin", "archive", "delete", "share_add", "link_add"):
            with self.subTest(name=name):
                self.assertEqual(stranger.post(url(name, note.pk)).status_code, 404)
        for name in ("save", "check"):
            self.assertEqual(self.post_json(url(name, note.pk), {"body": "x", "index": 0, "checked": True, "version": 1}, stranger).status_code, 404)
        self.assertNotContains(stranger.get(url("list")), "Groceries")
        self.assertTrue(Note.objects.filter(pk=note.pk, pinned=False, archived=False).exists())

    def test_view_share_can_read_but_not_change(self):
        note = self.note(body="- [ ] milk")
        NoteShare.objects.create(note=note, user=self.wife, permission="view")
        wife = self.as_user(self.wife)
        page = wife.get(url("note", note.pk)).content.decode()
        self.assertIn("view only", page)
        self.assertNotIn('id="nt-body"', page)
        self.assertIn("disabled", page)                     # the checkbox can't be ticked
        self.assertEqual(self.post_json(url("save", note.pk), {"body": "x", "version": 1}, wife).status_code, 404)
        self.assertEqual(self.post_json(url("check", note.pk), {"index": 0, "checked": True, "version": 1}, wife).status_code, 404)
        self.assertEqual(wife.post(url("delete", note.pk)).status_code, 404)
        self.assertEqual(wife.post(url("pin", note.pk)).status_code, 404)
        self.assertContains(wife.get(url("list") + "?shared=1"), f'id="note-{note.pk}"')

    def test_edit_share_can_change_content_but_not_share_or_delete(self):
        note = self.note()
        NoteShare.objects.create(note=note, user=self.wife, permission="edit")
        wife = self.as_user(self.wife)
        self.assertIn('id="nt-body"', wife.get(url("note", note.pk)).content.decode())
        self.assertEqual(self.post_json(url("save", note.pk), {"body": "eggs", "version": 1}, wife).status_code, 200)
        self.assertEqual(wife.post(url("share_add", note.pk), {"user": self.stranger.pk}).status_code, 404)
        self.assertEqual(wife.post(url("delete", note.pk)).status_code, 404)
        self.assertEqual(wife.post(url("archive", note.pk)).status_code, 404)

    def test_owner_shares_only_with_people_who_can_open_notes(self):
        note = self.note()
        no_notes = with_apps(User.objects.create_user(username="meals_only", password=PASSWORD), ["meals"])
        page = self.client.get(url("note", note.pk)).content.decode()
        self.assertIn(f'<option value="{self.wife.pk}">sam</option>', page)
        self.assertNotIn("meals_only", page)
        self.client.post(url("share_add", note.pk), {"user": no_notes.pk, "permission": "edit"})
        self.assertFalse(NoteShare.objects.exists())
        self.client.post(url("share_add", note.pk), {"user": self.wife.pk, "permission": "edit"})
        share = NoteShare.objects.get()
        self.assertEqual((share.user, share.permission), (self.wife, "edit"))
        self.client.post(url("share_update", note.pk, share.pk), {"permission": "view"})
        share.refresh_from_db()
        self.assertEqual(share.permission, "view")
        self.client.post(url("share_update", note.pk, share.pk), {"remove": "1"})
        self.assertFalse(NoteShare.objects.exists())
        self.assertEqual(self.as_user(self.wife).get(url("note", note.pk)).status_code, 404)


class MarkdownTests(Base):
    def test_dangerous_html_is_stripped(self):
        html = str(render.render(
            '<script>alert(1)</script>\n\n<img src=x onerror="alert(2)">\n\n<iframe src="https://evil.example"></iframe>\n\n'
            '[click](javascript:alert(3)) <a href="javascript:alert(4)" onclick="alert(5)">x</a>\n\n'
            '<p onmouseover="alert(6)" style="color:red">hi</p>\n\n[data](data:text/html,hi)'))
        for bad in ("<script", "alert(1)", "onerror", "<img", "<iframe", "javascript:", "onclick", "onmouseover", "style=", "data:"):
            with self.subTest(bad=bad):
                self.assertNotIn(bad, html)
        self.assertIn("<p>hi</p>", html)

    def test_links_open_safely_and_go_through_the_link_check(self):
        html = str(render.render("[gh](https://github.com/x) [other](https://unknown.example/a?b=1) [site](/meals/)"))
        self.assertIn('href="https://github.com/x"', html)
        self.assertIn('href="/notes/leave/?to=https%3A%2F%2Funknown.example%2Fa%3Fb%3D1"', html)
        self.assertIn('href="/meals/"', html)
        self.assertEqual(html.count('rel="noopener noreferrer"'), 3)
        self.assertEqual(html.count('target="_blank"'), 3)

    def test_leave_page_shows_the_domain(self):
        response = self.client.get(url("leave") + "?to=https%3A%2F%2Funknown.example%2Fa")
        self.assertContains(response, "unknown.example")
        self.assertRedirects(self.client.get(url("leave") + "?to=https://github.com/x"), "https://github.com/x",
                             fetch_redirect_response=False)

    def test_formatting_survives(self):
        html = str(render.render("# Head\n\n- a\n- b\n\n1. one\n\n`code`\n\n| a | b |\n|---|---|\n| 1 | 2 |"))
        for good in ("<h1>Head</h1>", "<li>a</li>", "<ol>", "<code>code</code>", "<table>", "<td>1</td>"):
            self.assertIn(good, html)


class ChecklistTests(Base):
    BODY = "Shopping\n- [ ] milk\n- [x] eggs\n\n```\n- [ ] not a task\n```\n* [ ] bread"

    def test_checklist_renders_as_checkboxes_in_order(self):
        html = str(render.render(self.BODY, editable=True))
        boxes = re.findall(r'<input type="checkbox" class="nt-check" data-task="(\d)"( checked)?', html)
        self.assertEqual(boxes, [("0", ""), ("1", " checked"), ("2", "")])
        self.assertIn("- [ ] not a task", html)                       # code blocks stay text
        self.assertNotIn("data-task", str(render.render(self.BODY, editable=False)))

    def test_ticking_rewrites_that_markdown_line(self):
        note = self.note(body=self.BODY)
        response = self.post_json(url("check", note.pk), {"index": 2, "checked": True, "version": 1})
        self.assertEqual(response.status_code, 200)
        note.refresh_from_db()
        self.assertEqual(note.body, self.BODY.replace("* [ ] bread", "* [x] bread"))
        self.post_json(url("check", note.pk), {"index": 1, "checked": False, "version": 2})
        note.refresh_from_db()
        self.assertIn("- [ ] eggs", note.body)
        self.assertIn("- [ ] not a task", note.body)
        self.assertEqual(self.post_json(url("check", note.pk), {"index": 9, "checked": True, "version": 3}).status_code, 400)
        self.assertEqual(self.post_json(url("check", note.pk), {"index": 0, "checked": True, "version": 1}).status_code, 409)


class LinkTests(Base):
    def setUp(self):
        super().setUp()
        from albright_meals.models import Recipe
        from albright_reselling_app.tests.test_ledger import _make_entry
        self.recipe = Recipe.objects.create(title="Chili")
        self.mine = _make_entry(self.me, item="Morgan dollar")
        self.theirs = _make_entry(self.stranger, item="Their coin")
        self.note_ = self.note()

    def test_picker_searches_only_what_the_user_can_open(self):
        results = self.client.get(url("link_search") + "?q=o").json()["results"]
        self.assertEqual(results, [])                                  # under 2 characters
        labels = [r["label"] for r in self.client.get(url("link_search") + "?q=coin").json()["results"]]
        self.assertEqual(labels, [])                                   # "Their coin" isn't mine
        labels = [r["label"] for r in self.client.get(url("link_search") + "?q=dollar").json()["results"]]
        self.assertEqual(labels, ["Morgan dollar"])
        wife = self.as_user(self.wife)                                 # Notes only: no Meals, no Reselling
        self.assertEqual(wife.get(url("link_search") + "?q=chili").json()["results"], [])

    def test_link_by_picker_and_by_pasted_url(self):
        self.client.post(url("link_add", self.note_.pk), {"app": "resell", "type": "ledger", "id": self.mine.pk})
        self.client.post(url("link_add", self.note_.pk), {"url": f"http://testserver/meals/recipes/{self.recipe.pk}/"})
        self.assertEqual(sorted(NoteLink.objects.values_list("label", flat=True)), ["Chili", "Morgan dollar"])
        page = self.client.get(url("note", self.note_.pk)).content.decode()
        self.assertIn(f'href="{reverse("albright_meals:recipe", args=[self.recipe.pk])}"', page)

    def test_cant_link_records_the_user_cant_open(self):
        self.client.post(url("link_add", self.note_.pk), {"app": "resell", "type": "ledger", "id": self.theirs.pk})
        self.client.post(url("link_add", self.note_.pk), {"url": f"/albright_reselling_app/ledger/{self.theirs.pk}/manage/"})
        self.client.post(url("link_add", self.note_.pk), {"url": "https://evil.example/meals/recipes/1/"})
        self.client.post(url("link_add", self.note_.pk), {"app": "admin", "type": "idea", "id": 1})
        self.assertFalse(NoteLink.objects.exists())
        NoteShare.objects.create(note=self.note_, user=self.wife, permission="edit")
        self.as_user(self.wife).post(url("link_add", self.note_.pk), {"app": "meals", "type": "recipe", "id": self.recipe.pk})
        self.assertFalse(NoteLink.objects.exists())

    def test_shared_viewer_sees_only_the_app_of_a_record_they_cant_open(self):
        links.add(self.note_, links.BY_KEY[("resell", "ledger")], self.mine)
        NoteShare.objects.create(note=self.note_, user=self.wife)
        page = self.as_user(self.wife).get(url("note", self.note_.pk)).content.decode()
        self.assertNotIn("Morgan dollar", page)
        self.assertIn("Reselling record", page)
        self.assertNotIn(reverse("albright_reselling_app:ledger_manage", args=[self.mine.pk]), page)

    def test_remove_link(self):
        link = links.add(self.note_, links.BY_KEY[("meals", "recipe")], self.recipe)
        self.client.post(url("link_remove", self.note_.pk, link.pk))
        self.assertFalse(NoteLink.objects.exists())


@override_settings(BOT_API_REQUIRE_HTTPS=False, CRYPTO=CRYPTO)
class BotApiTests(TestCase):
    def setUp(self):
        self.owner = with_apps(User.objects.create_user(username="owner", password=PASSWORD))
        self.token, self.raw = BotToken.issue(self.owner, "Note Bot", ["notes:read", "notes:write"])
        self.bare, self.bare_raw = BotToken.issue(self.owner, "No scopes", [])
        self.api = Client()

    def call(self, method, path, body=None, raw=None):
        headers = {"HTTP_AUTHORIZATION": f"Bearer {raw or self.raw}"}
        if method == "GET":
            return self.api.get(path, **headers)
        return getattr(self.api, method.lower())(path, data=json.dumps(body or {}), content_type="application/json", **headers)

    def test_scopes_are_offered_in_the_token_forms_and_docs(self):
        self.assertIn("notes:read", SCOPE_KEYS)
        self.assertIn("notes:write", SCOPE_KEYS)
        docs = self.call("GET", reverse("bot_api_v1:docs")).content.decode()
        self.assertIn("POST notes", docs)
        self.assertIn("PATCH notes/<id>", docs)
        from albright_control.views import TokenForm
        self.assertIn("notes:write", dict(TokenForm().fields["scopes"].choices))

    def test_create_with_tags_pin_and_link_labeled_with_the_bot(self):
        from albright_meals.models import Recipe
        recipe = Recipe.objects.create(title="Chili")
        response = self.call("POST", reverse("bot_api_v1:notes"), {
            "title": "Chili notes", "body": "Add **more** cumin", "tags": ["Food"], "pinned": True,
            "link": {"app": "meals", "type": "recipe", "id": recipe.pk}})
        self.assertEqual(response.status_code, 201, response.content)
        note = Note.objects.get()
        self.assertEqual((note.owner, note.created_by, note.bot_token, note.tags, note.pinned),
                         (self.owner, "Note Bot", self.token, ["food"], True))
        self.assertEqual(note.links.get().label, "Chili")
        self.assertEqual(response.json()["linked"], "Chili")
        log = BotRequestLog.objects.latest("pk")
        self.assertEqual((log.status, log.method, log.token_name), (201, "POST", "Note Bot"))

    def test_create_rejects_a_link_the_owner_cant_open_and_bad_input(self):
        other = User.objects.create_user(username="x", password=PASSWORD)
        from albright_reselling_app.tests.test_ledger import _make_entry
        theirs = _make_entry(other, item="Not theirs")
        for body in ({"title": "t", "link": {"app": "resell", "type": "ledger", "id": theirs.pk}},
                     {"title": ""}, {"title": "t", "tags": "nope"}, {"title": "t", "pinned": "yes"}):
            with self.subTest(body=body):
                self.assertEqual(self.call("POST", reverse("bot_api_v1:notes"), body).status_code, 400)
        self.assertFalse(Note.objects.exists())

    def test_every_endpoint_needs_its_scope(self):
        note = notes.create(self.owner, "Mine", token=self.token)
        for method, path, body in (("GET", reverse("bot_api_v1:notes"), None),
                                   ("POST", reverse("bot_api_v1:notes"), {"title": "t"}),
                                   ("PATCH", reverse("bot_api_v1:note", args=[note.pk]), {"append": "x"})):
            with self.subTest(method=method):
                self.assertEqual(self.call(method, path, body, raw=self.bare_raw).status_code, 403)
                self.assertEqual(self.api.generic(method, path).status_code, 401)
        read_only, raw = BotToken.issue(self.owner, "Reader", ["notes:read"])
        self.assertEqual(self.call("POST", reverse("bot_api_v1:notes"), {"title": "t"}, raw=raw).status_code, 403)
        self.assertEqual(self.call("GET", reverse("bot_api_v1:notes"), raw=raw).status_code, 200)
        self.assertEqual(BotRequestLog.objects.filter(status=403).count(), 4)

    def test_owner_without_notes_access_is_refused(self):
        UserPreference.objects.filter(user=self.owner).update(allowed_apps=["meals"])
        self.assertEqual(self.call("GET", reverse("bot_api_v1:notes")).status_code, 403)
        self.assertEqual(self.call("POST", reverse("bot_api_v1:notes"), {"title": "t"}).status_code, 403)

    def test_list_only_the_owners_own_notes_with_search_and_tag(self):
        other = with_apps(User.objects.create_user(username="other", password=PASSWORD), ["notes"])
        mine = notes.create(self.owner, "Water heater", "anode rod", tags=["house"])
        notes.create(self.owner, "Gift ideas", tags=["xmas"])
        theirs = notes.create(other, "Their private note")
        NoteShare.objects.create(note=theirs, user=self.owner, permission="edit")
        archived = notes.create(self.owner, "Old")
        Note.objects.filter(pk=archived.pk).update(archived=True)
        titles = [n["title"] for n in self.call("GET", reverse("bot_api_v1:notes")).json()["notes"]]
        self.assertEqual(sorted(titles), ["Gift ideas", "Water heater"])
        data = self.call("GET", reverse("bot_api_v1:notes") + "?q=anode").json()["notes"]
        self.assertEqual([(n["id"], n["tags"]) for n in data], [(mine.pk, ["house"])])
        self.assertEqual(set(data[0]), {"id", "title", "tags", "pinned", "archived", "created_by", "created_at", "updated_at"})
        self.assertEqual([n["title"] for n in self.call("GET", reverse("bot_api_v1:notes") + "?tag=xmas").json()["notes"]], ["Gift ideas"])

    def test_patch_only_its_own_notes_or_ones_shared_to_edit(self):
        own = notes.create(self.owner, "Bot's", "line one", tags=["a"], token=self.token)
        mine = notes.create(self.owner, "Written by me")
        other = with_apps(User.objects.create_user(username="other", password=PASSWORD), ["notes"])
        shared_edit = notes.create(other, "Shared to edit")
        NoteShare.objects.create(note=shared_edit, user=self.owner, permission="edit")
        shared_view = notes.create(other, "Shared to view")
        NoteShare.objects.create(note=shared_view, user=self.owner, permission="view")
        response = self.call("PATCH", reverse("bot_api_v1:note", args=[own.pk]), {"append": "line two", "tags": ["B"]})
        self.assertEqual(response.status_code, 200, response.content)
        own.refresh_from_db()
        self.assertEqual((own.body, own.tags, own.updated_by), ("line one\n\nline two", ["a", "b"], "Note Bot"))
        self.assertEqual(self.call("PATCH", reverse("bot_api_v1:note", args=[own.pk]), {"body": "new"}).status_code, 200)
        own.refresh_from_db()
        self.assertEqual(own.body, "new")
        self.assertEqual(self.call("PATCH", reverse("bot_api_v1:note", args=[shared_edit.pk]), {"append": "ok"}).status_code, 200)
        for note in (mine, shared_view):
            with self.subTest(note=note.title):
                self.assertEqual(self.call("PATCH", reverse("bot_api_v1:note", args=[note.pk]), {"append": "no"}).status_code, 404)
        self.assertEqual(self.call("PATCH", reverse("bot_api_v1:note", args=[own.pk]), {"append": "x", "body": "y"}).status_code, 400)
        self.assertEqual(self.call("PATCH", reverse("bot_api_v1:note", args=[own.pk]), {}).status_code, 400)


@override_settings(CRYPTO=CRYPTO)
class AppEntryTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = with_apps(User.objects.create_user(username="pat", password=PASSWORD))
        self.client.force_login(self.user)

    def test_registry_entry(self):
        app = apps_registry.BY_KEY["notes"]
        self.assertEqual((app.label, app.short_label, app.brand, app.accent, app.color, app.landing_url_name),
                         ("Notes", "Notes", "Albright Notes", "notes", "#EC4899", "albright_notes:list"))
        self.assertIn("notes", apps_registry.grantable_keys())
        self.assertIn("notes:list", apps_registry.PAGES)
        css = (settings.BASE_DIR / "albright_trading_app" / "static" / "style.css").read_text(encoding="utf-8")
        self.assertIn("--app-notes: #F472B6;", css)
        self.assertIn('body[data-app="notes"]', css)

    def test_launcher_tile_brand_and_accent(self):
        from unittest import mock
        with mock.patch(OUTLOOK, return_value=None):
            page = self.client.get(url("list")).content.decode()
        self.assertIn('<body data-app="notes">', page)
        self.assertIn("<title>Notes · Albright Notes</title>", page)
        self.assertIn(("notes", "Notes"), launcher_tiles(page))
        self.assertEqual(launcher_links(page)["Notes"], url("list"))

    def test_new_users_have_no_access(self):
        newbie = User.objects.create_user(username="new", password=PASSWORD)
        self.assertFalse(app_access.can_use(newbie, UserPreference.for_user(newbie), "notes"))
        client = Client()
        client.force_login(newbie)
        self.assertEqual(client.get(url("list")).status_code, 404)
        pref = app_access.new_user_preference(User.objects.create_user(username="meals", password=PASSWORD), ["meals"])
        self.assertNotIn("notes", pref.allowed_apps)
        with_apps(newbie, ["notes"])
        self.assertEqual(client.get(url("list")).status_code, 200)

    def test_home_tile(self):
        self.assertEqual(tiles.notes(self.user)["value"], "No notes yet")
        notes.create(self.user, "First", pinned=True)
        notes.create(self.user, "Second")
        latest = notes.create(self.user, "Latest one")
        tile = tiles.notes(self.user)
        self.assertEqual((tile["value"], tile["note"]), ("Latest one", "1 pinned · latest"))
        Note.objects.filter(pk=latest.pk).update(archived=True)
        self.assertEqual(tiles.notes(self.user)["value"], "Second")
        response = self.client.get(reverse("labs:tile", args=["notes"]))
        self.assertEqual(response.status_code, 200)

    def test_every_stylesheet_and_script_on_notes_pages_exists(self):
        note = notes.create(self.user, "N", "- [ ] x")
        for path in (url("list"), url("list") + "?archived=1", url("note", note.pk), url("leave") + "?to=https%3A%2F%2Fx.example"):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                content = response.content.decode()
                self.assertIsNone(response.get("Content-Security-Policy"))
                self.assertRegex(content, r'<link rel="stylesheet" href="/static/albright_notes/notes\.css\?v=\d+">')
                self.assertRegex(content, r'<script src="/static/albright_notes/notes\.js\?v=\d+"></script>')
                for ref in re.findall(r'(?:href|src)="/static/([^"?]+)', content):
                    self.assertTrue(finders.find(ref), ref)
        # Other apps don't load Notes' files.
        self.assertNotIn("albright_notes/notes.css", self.client.get(reverse("albright_meals:week")).content.decode())

    def test_collectstatic_finds_the_files(self):
        out = StringIO()
        call_command("collectstatic", "--dry-run", "--noinput", "-v", "2", stdout=out)
        listing = out.getvalue().replace("\\", "/")
        for path in ("albright_notes/notes.css", "albright_notes/notes.js"):
            self.assertRegex(listing, rf"(Copying|Pretending to copy|Skipping) .*{re.escape(path)}")
