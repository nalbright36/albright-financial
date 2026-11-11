"""The view panel's linked note: shown in full (rendered and sanitized like a note, its title above it), its
checklist boxes tick right there - saved to the note - for someone who can edit it and open Notes, and are
read-only for anyone else; the panel's middle scrolls between a fixed header and footer; the Day / Week /
Month tabs are page links, never the view panel."""
import json
import re

from django.contrib.auth.models import User
from django.test import Client

from albright_labs.testing import share_with, with_apps
from albright_notes.models import Note

from .tests import PASSWORD, Base, url


def note_section(panel):
    match = re.search(r'<section class="pl-view__section" id="pl-view-note".*?</section>', panel, re.S)
    return match.group(0) if match else ""


class LinkedNoteTests(Base):
    BODY = ("# Packing\n**Bring** the *big* tent\n\n- [ ] stove\n- [x] matches\n\n1. load the car\n\n"
            "<script>steal()</script>\n[bad](javascript:alert(1)) <a href=\"/x\" onclick=\"evil()\">x</a>")

    def panel(self, item, client=None):
        response = (client or self.client).get(url("item", item.pk) + f"?date={item.date.isoformat()}&panel=1")
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def setUp(self):
        super().setUp()
        self.note = Note.objects.create(owner=self.me, title="Camping checklist", body=self.BODY)
        self.task = self.item("Camping", note=self.note)

    def test_the_whole_note_shows_rendered_with_its_title(self):
        section = note_section(self.panel(self.task))
        self.assertIn('<h4 class="pl-view__note-title" id="pl-view-note-h">Camping checklist</h4>', section)
        self.assertIn("<h1>Packing</h1>", section)
        self.assertIn("<strong>Bring</strong> the <em>big</em> tent", section)
        self.assertRegex(section, r"<ol>\s*<li>load the car</li>\s*</ol>")
        self.assertIn('class="pl-view__note md-body"', section)
        self.assertNotIn("pl-note-card__preview", section)              # the whole note, not a preview
        self.assertIn(f'href="/notes/{self.note.pk}/" class="pl-view__open-note">Open in Notes', section)

    def test_the_note_is_sanitized(self):
        section = note_section(self.panel(self.task)).lower()
        for bad in ("<script", "steal()", "javascript:", "onclick"):
            self.assertNotIn(bad, section)

    def test_an_editor_can_tick_and_it_saves_to_the_note(self):
        section = note_section(self.panel(self.task))
        self.assertIn(f'data-note-check-url="/notes/{self.note.pk}/check/"', section)
        self.assertIn(f'data-note-version="{self.note.version}"', section)
        self.assertIn('class="nt-check" data-task="0"', section)
        self.assertIn('class="nt-check" data-task="1" checked', section)
        # The panel's box posts here (planner.js) - Notes' own endpoint.
        response = self.client.post(f"/notes/{self.note.pk}/check/", data=json.dumps(
            {"index": 0, "checked": True, "version": self.note.version}), content_type="application/json")
        self.assertEqual(response.status_code, 200)
        self.note.refresh_from_db()
        self.assertIn("- [x] stove", self.note.body)
        self.assertIn('data-task="0" checked', response.json()["html"])
        # ...and the panel shows it ticked, at the new version.
        section = note_section(self.panel(self.task))
        self.assertIn('class="nt-check" data-task="0" checked', section)
        self.assertIn(f'data-note-version="{self.note.version}"', section)

    def test_a_stale_tick_is_a_conflict_not_an_overwrite(self):
        version = self.note.version
        self.client.post(f"/notes/{self.note.pk}/check/", data=json.dumps({"index": 0, "checked": True, "version": version}),
                         content_type="application/json")
        stale = self.client.post(f"/notes/{self.note.pk}/check/", data=json.dumps(
            {"index": 1, "checked": False, "version": version}), content_type="application/json")
        self.assertEqual(stale.status_code, 409)
        self.note.refresh_from_db()
        self.assertIn("- [x] matches", self.note.body)

    def test_view_only_note_is_read_only_here(self):
        sam = self.wife
        with_apps(sam, ["planner", "notes"])
        share_with(self.note, sam, "view")
        share_with(self.task, sam, "edit")
        client = Client()
        client.force_login(sam)
        section = note_section(self.panel(self.task, client))
        self.assertIn("<h1>Packing</h1>", section)
        self.assertNotIn("data-note-check-url", section)
        self.assertNotIn("data-task=", section)
        self.assertIn("disabled", section)
        response = client.post(f"/notes/{self.note.pk}/check/", data=json.dumps({"index": 0, "checked": True}),
                               content_type="application/json")
        self.assertEqual(response.status_code, 404)

    def test_no_ticking_without_notes_access(self):
        planner_only = with_apps(User.objects.create_user(username="pat", password=PASSWORD), ["planner"])
        note = Note.objects.create(owner=planner_only, title="Mine", body="- [ ] one")
        item = self.item("Pat's task", owner=planner_only, note=note)
        client = Client()
        client.force_login(planner_only)
        section = note_section(self.panel(item, client))
        self.assertIn("Mine", section)
        self.assertNotIn("data-note-check-url", section)
        self.assertIn("disabled", section)

    def test_a_note_they_cant_see_or_an_empty_one(self):
        hidden = Note.objects.create(owner=self.wife, title="Private", body="secret")
        self.assertNotIn('id="pl-view-note"', self.panel(self.item("Theirs", note=hidden)))
        empty = Note.objects.create(owner=self.me, title="Blank", body="")
        section = note_section(self.panel(self.item("Empty", note=empty)))
        self.assertIn("This note is empty.", section)

    def test_header_and_footer_frame_a_scrolling_middle(self):
        panel = self.panel(self.task)
        head, body, foot = panel.index('class="pl-view__head"'), panel.index('class="pl-view__body"'), \
            panel.index('class="pl-view__foot"')
        self.assertLess(head, body)
        self.assertLess(body, foot)
        middle = panel[panel.rindex("<div", 0, body):foot]
        self.assertIn('id="pl-view-note"', middle)
        self.assertEqual(middle.count("<div") - middle.count("</div>"), 0)   # the body closes before the footer
        self.assertNotIn("pl-view__close", middle)

    def test_on_phones_the_panel_is_a_centered_card_sized_to_its_content(self):
        import pathlib
        from django.contrib.staticfiles import finders
        css = pathlib.Path(finders.find("style.css")).read_text(encoding="utf-8")
        phone = css[css.index("/* Phones: a centered card"):]
        phone = phone[:phone.index("\n}")]
        rule = re.search(r"\.pl-view-dialog \{(.*?)\}", phone, re.S).group(1)
        for part in ("width: 92%", "height: fit-content", "margin: auto", "border-radius: 16px",
                     "top: env(safe-area-inset-top)", "bottom: env(safe-area-inset-bottom)",
                     "max-height: min(90dvh, calc(100dvh - env(safe-area-inset-top) - env(safe-area-inset-bottom)"):
            self.assertIn(part, rule)
        self.assertNotIn("height: 100%", phone)                         # no longer a full-screen sheet
        self.assertNotIn("margin-top: auto", phone)                     # Edit / Close sit right under the content

    def test_switching_views_never_opens_the_view_panel(self):
        # Day / Week / Month are ordinary page links on the main display (data-tab), never data-view links.
        page = self.client.get(url("day")).content.decode()
        nav = page[page.index('id="pl-views"'):page.index("</nav>", page.index('id="pl-views"'))]
        self.assertEqual(re.findall(r'data-tab="(\w+)"', nav), ["day", "week", "month"])
        self.assertNotIn("pl-tab", page)
        self.assertEqual(re.findall(r'<a [^>]*data-view[^>]*>', page.split('id="pl-head"', 1)[1].split("</dialog>", 2)[0]), [])
        self.assertRegex(page, r'class="pl-item__body" data-view\s')
