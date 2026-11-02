"""An item's notes: long notes save (form and Bot API, up to 50,000 characters) and anything over the limit is
refused out loud with the text kept; the New / Edit notes box is the shared Markdown editor (also Notes'), and
the view panel shows the notes rendered and sanitized exactly like a note; the view panel is solid."""
import json
import pathlib
import re

from django.contrib.staticfiles import finders
from django.test import Client
from django.urls import reverse

from albright_bot_api.models import BotToken
from albright_labs.testing import allow_api_keys
from albright_notes import notes as notes_app
from albright_notes.render import render

from .models import NOTES_MAX, ScheduleItem
from .templatetags.planner_extras import notes_markdown
from .tests import TODAY, Base, url

LONG = ("A line of notes about the trip, with enough words to fill it out nicely.\n" * 160)[:12_000]


def form(title="Trip", **changes):
    data = {"title": title, "kind": "task", "date": TODAY.isoformat(), "priority": "none", "repeat": "none",
            "ends": "never", "checkable": "on", "next": "/planner/day/"}
    data.update(changes)
    return data


def textarea(page):
    return re.search(r"<textarea[^>]*name=\"notes\"[^>]*>", page).group(0)


class LongNotesTests(Base):
    def test_the_field_is_text_with_a_generous_limit(self):
        field = ScheduleItem._meta.get_field("notes")
        self.assertEqual(field.get_internal_type(), "TextField")
        self.assertEqual(NOTES_MAX, 50_000)
        self.assertEqual(field.max_length, NOTES_MAX)

    def test_new_form_saves_10000_plus_characters(self):
        response = self.client.post(url("new"), form(notes=LONG))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(ScheduleItem.objects.get(title="Trip").notes, LONG.strip())
        self.assertGreater(len(LONG.strip()), 10_000)

    def test_edit_form_saves_10000_plus_characters(self):
        item = self.item("Trip")
        response = self.client.post(url("edit", item.pk), form(notes=LONG))
        self.assertEqual(response.status_code, 302)
        item.refresh_from_db()
        self.assertEqual(item.notes, LONG.strip())

    def test_the_box_has_no_maxlength_to_cut_a_paste_off(self):
        box = textarea(self.client.get(url("new")).content.decode())
        self.assertNotIn("maxlength", box)
        self.assertIn('rows="14"', box)

    def test_over_the_limit_shows_a_clear_error_and_keeps_the_text(self):
        too_long = "x" * (NOTES_MAX + 25)
        response = self.client.post(url("new"), form(title="Too much", notes=too_long))
        self.assertEqual(response.status_code, 200)
        page = response.content.decode()
        self.assertIn('id="pl-form-errors"', page)                    # "Not saved yet" at the top
        self.assertIn("Notes can be up to 50,000 characters - these are 50,025. Shorten them by 25", page)
        self.assertIn('id="pl-notes-error"', page)
        self.assertIn(too_long, page)                                  # nothing typed is lost
        self.assertIn('value="Too much"', page)
        self.assertFalse(ScheduleItem.objects.filter(title="Too much").exists())

    def test_over_the_limit_on_edit_keeps_the_text_and_the_saved_notes(self):
        item = self.item("Trip", notes="Old notes")
        too_long = "y" * (NOTES_MAX + 1)
        page = self.client.post(url("edit", item.pk), form(notes=too_long)).content.decode()
        self.assertIn(too_long, page)
        self.assertIn("Notes can be up to 50,000 characters", page)
        item.refresh_from_db()
        self.assertEqual(item.notes, "Old notes")

    def test_exactly_the_limit_saves(self):
        response = self.client.post(url("new"), form(notes="z" * NOTES_MAX))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(len(ScheduleItem.objects.get(title="Trip").notes), NOTES_MAX)


class BotApiNotesTests(Base):
    PATH = "/api/bot/v1/schedule"

    def setUp(self):
        super().setUp()
        allow_api_keys(self.me)
        _token, self.raw = BotToken.issue(self.me, "Grok bot", ["schedule:read", "schedule:write"])
        self.api = Client()

    def create(self, notes):
        return self.api.post(self.PATH, data=json.dumps({"title": "Trip", "date": "2026-11-04", "notes": notes}),
                             content_type="application/json", HTTP_AUTHORIZATION=f"Bearer {self.raw}", secure=True)

    def test_long_notes_save(self):
        response = self.create(LONG)
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(ScheduleItem.objects.get(pk=response.json()["id"]).notes, LONG.strip())

    def test_over_the_limit_is_refused_with_a_reason(self):
        response = self.create("x" * (NOTES_MAX + 1))
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], '"notes" is longer than 50000 characters.')
        self.assertFalse(ScheduleItem.objects.exists())


class MarkdownTests(Base):
    def panel(self, item):
        return self.client.get(url("item", item.pk) + f"?date={item.date.isoformat()}&panel=1").content.decode()

    def test_the_view_renders_headings_bold_and_lists(self):
        item = self.item("Trip", notes="## Plan\n**Pack** the *car*\n\n- tent\n- stove\n\n1. drive\n2. camp\n\n- [x] tickets")
        panel = self.panel(item)
        notes = re.search(r'<div class="pl-view__notes md-body" data-clamp>(.*?)</div>', panel, re.S).group(1)
        self.assertIn("<h2>Plan</h2>", notes)
        self.assertIn("<strong>Pack</strong>", notes)
        self.assertIn("<em>car</em>", notes)
        self.assertRegex(notes, r"<ul>\s*<li>tent</li>\s*<li>stove</li>\s*</ul>")
        self.assertRegex(notes, r"<ol>\s*<li>drive</li>\s*<li>camp</li>\s*</ol>")
        self.assertIn('class="nt-check" disabled checked', notes)      # a checklist line shows, read-only

    def test_the_same_rendering_as_notes(self):
        text = "# Title\nSome **bold** text and a [link](/planner/)\n- [ ] one"
        self.assertEqual(notes_markdown(text), render(text))

    def test_sanitizing_strips_scripts_iframes_handlers_and_javascript_links(self):
        item = self.item("Trip", notes='<script>alert("x")</script>\n\n[click](javascript:alert(1))\n\n'
                                       '<iframe src="https://example.com"></iframe>\n\n'
                                       '<a href="/planner/" onclick="steal()">home</a> <img src=x onerror="bad()">')
        notes = re.search(r'<div class="pl-view__notes md-body" data-clamp>(.*?)</div>', self.panel(item), re.S).group(1)
        for bad in ("<script", "javascript:", "<iframe", "onclick", "onerror", "<img"):
            self.assertNotIn(bad, notes.lower())
        self.assertIn("click", notes)                                  # the link's text stays, as text

    def test_preview_returns_the_same_sanitized_html(self):
        text = "## Hi\n- a\n<script>bad()</script>"
        response = self.client.post(url("notes_preview"), {"notes": text})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["html"], render(text))
        self.assertNotIn("<script", response.json()["html"])
        self.assertEqual(self.client.get(url("notes_preview")).status_code, 405)
        anonymous = Client().post(url("notes_preview"), {"notes": text})
        self.assertEqual(anonymous.status_code, 302)


class SharedEditorTests(Base):
    def test_the_planner_form_uses_the_shared_editor(self):
        for page in (self.client.get(url("new")), self.client.get(url("edit", self.item("Trip").pk))):
            page = page.content.decode()
            self.assertIn("data-md-editor", page)
            self.assertIn(f'data-preview-url="{url("notes_preview")}"', page)
            for kind in ("bold", "italic", "heading", "bullet", "number", "check", "link"):
                self.assertIn(f'data-fmt="{kind}"', page)
            self.assertIn('data-md-mode="write"', page)
            self.assertIn('data-md-mode="preview"', page)
            self.assertIn("data-md-input", textarea(page))
            self.assertIn("js/md_editor.js", page)

    def test_notes_uses_the_same_editor_script_and_rendered_styles(self):
        note = notes_app.create(self.me, "Packing", "- tent")
        page = self.client.get(reverse("albright_notes:note", args=[note.pk])).content.decode()
        self.assertIn("js/md_editor.js", page)
        self.assertLess(page.index("js/md_editor.js"), page.index("albright_notes/notes.js"))   # loaded first
        self.assertIn('class="nt-md md-body" id="nt-preview"', page)
        notes_js = pathlib.Path(finders.find("albright_notes/notes.js")).read_text(encoding="utf-8")
        for use in ("MdEditor.format(", "MdEditor.continueList(", "MdEditor.shortcut("):
            self.assertIn(use, notes_js)
        self.assertNotIn("function continueList", notes_js)            # not a copy of its own
        shared = pathlib.Path(finders.find("js/md_editor.js")).read_text(encoding="utf-8")
        self.assertIn("window.MdEditor", shared)

    def test_the_view_panel_is_solid(self):
        css = pathlib.Path(finders.find("style.css")).read_text(encoding="utf-8")
        rule = re.search(r"\n\.pl-view-dialog \{(.*?)\n\}", css, re.S).group(1)
        self.assertIn("background: var(--glass-bg-solid", rule)
        self.assertIn("backdrop-filter: none", rule)
        self.assertNotIn("glass-bg-dialog", rule)
        self.assertIn(".pl-view-dialog::backdrop { background: rgba(0, 0, 0, 0.5); }", css)
