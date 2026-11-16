"""Attachments on Planner tasks and notes (albright_labs.attachments, attachment_views, attachment_storage): who may
upload, open and remove them (owner, a circle member with view access, an edit grant, a stranger - 404); what's
refused (svg/html, too big, a fake image, too many); images turned upright, stripped of EXIF/GPS, downscaled and
thumbnailed, HEIC to JPEG; the bytes in Postgres apart from the metadata; deleting a task or note deletes its
attachments; the paperclip on rows and cards; the sections; the note sanitizer; the Bot API; cleanup; My account."""
import io
import zipfile
from unittest import skipUnless

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from PIL import Image

from albright_bot_api.models import BotToken
from albright_notes import render as note_render
from albright_notes.models import Note
from albright_planner import planner
from albright_planner.tests import PASSWORD, TODAY, Base as PlannerBase

from . import attachments
from .models import Attachment, AttachmentBlob
from .testing import allow_api_keys, kitchen, share_with, with_apps

UPLOAD = reverse("labs:attachment_upload")


def png(size=(40, 30), color=(200, 30, 30)):
    out = io.BytesIO()
    Image.new("RGB", size, color).save(out, "PNG")
    return out.getvalue()


def jpeg_with_exif(size=(60, 40), orientation=6):
    """A JPEG that's sideways (EXIF orientation 6: turn 90° clockwise to show) with a GPS position and a camera."""
    img = Image.new("RGB", size, (10, 120, 200))
    exif = Image.Exif()
    exif[0x0112] = orientation                     # Orientation
    exif[0x010F] = "PhoneCam"                      # Make
    exif[0x8825] = {1: "N", 2: (40.0, 26.0, 46.0), 3: "W", 4: (79.0, 58.0, 56.0)}   # GPSInfo
    out = io.BytesIO()
    img.save(out, "JPEG", exif=exif)
    return out.getvalue()


def docx():
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("word/document.xml", "<w:document/>")
    return out.getvalue()


def upload_file(name, data, content_type="application/octet-stream"):
    return SimpleUploadedFile(name, data, content_type=content_type)


class Helpers:
    def post_file(self, client, parent, name, data, app="planner", ajax=True):
        headers = {"HTTP_X_REQUESTED_WITH": "XMLHttpRequest"} if ajax else {}
        return client.post(UPLOAD, {"parent": app, "id": parent.pk, "file": upload_file(name, data), "next": "/"},
                           **headers)

    def attach(self, user, parent, name="photo.png", data=None):
        return attachments.add(user, parent, upload_file(name, data if data is not None else png()))


class PlannerAttachmentTests(Helpers, PlannerBase):
    def setUp(self):
        super().setUp()
        self.task = self.item(title="Fix the fence")

    # ---------- Owner ----------
    def test_the_owner_uploads_and_opens_an_image(self):
        res = self.post_file(self.client, self.task, "fence.png", png())
        self.assertEqual(res.status_code, 201, res.content)
        data = res.json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["count"], 1)
        row = data["attachments"][0]
        self.assertEqual((row["name"], row["type"], row["is_image"]), ("fence.png", "image/png", True))
        self.assertIn('data-att-view', row["html"])
        a = Attachment.objects.get()
        self.assertEqual((a.owner, a.object_id, a.width, a.height, a.has_thumb), (self.me, self.task.pk, 40, 30, True))
        self.assertRegex(a.key, rf"^attachments/{self.me.pk}/\d{{4}}/\d{{2}}/[0-9a-f]{{32}}\.png$")
        self.assertNotIn("fence", a.key)
        self.assertTrue(AttachmentBlob.objects.filter(attachment=a).exists())

        got = self.client.get(reverse("labs:attachment", args=[a.pk]))
        self.assertEqual(got.status_code, 200)
        self.assertEqual(got["Content-Type"], "image/png")
        self.assertTrue(got["Content-Disposition"].startswith("inline"))
        self.assertEqual(got["X-Content-Type-Options"], "nosniff")
        self.assertEqual(got["Cache-Control"], "private, max-age=86400")
        self.assertEqual(got["ETag"], f'"{a.sha256}"')
        body = b"".join(got.streaming_content)
        self.assertEqual(int(got["Content-Length"]), len(body))
        self.assertEqual(Image.open(io.BytesIO(body)).format, "PNG")

        again = self.client.get(reverse("labs:attachment", args=[a.pk]), HTTP_IF_NONE_MATCH=f'"{a.sha256}"')
        self.assertEqual(again.status_code, 304)

        thumb = self.client.get(reverse("labs:attachment_thumb", args=[a.pk]))
        self.assertEqual(thumb.status_code, 200)
        self.assertEqual(thumb["Content-Type"], "image/webp")
        self.assertEqual(Image.open(io.BytesIO(b"".join(thumb.streaming_content))).format, "WEBP")

    def test_listing_attachments_never_loads_the_bytes(self):
        self.attach(self.me, self.task)
        with self.assertNumQueries(1):
            rows = list(attachments.for_parent(self.task))
        self.assertFalse(hasattr(rows[0], "data"))
        self.assertNotIn("data", [f.name for f in Attachment._meta.concrete_fields])

    def test_a_plain_form_post_redirects_back(self):
        res = self.post_file(self.client, self.task, "fence.png", png(), ajax=False)
        self.assertEqual(res.status_code, 302)
        self.assertEqual(Attachment.objects.count(), 1)

    def test_other_files_download_and_pdfs_open_inline(self):
        doc = self.attach(self.me, self.task, "plan.docx", docx())
        csv = self.attach(self.me, self.task, "costs.csv", b"item,cost\nwood,40\n")
        pdf = self.attach(self.me, self.task, "quote.pdf", b"%PDF-1.4\n%\xe2\xe3\n1 0 obj<<>>endobj\n%%EOF")
        self.assertEqual(doc.mime_type, "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
        for a, disposition in ((doc, "attachment"), (csv, "attachment"), (pdf, "inline")):
            with self.subTest(a=a.original_name):
                res = self.client.get(reverse("labs:attachment", args=[a.pk]))
                self.assertTrue(res["Content-Disposition"].startswith(disposition), res["Content-Disposition"])
                self.assertEqual(res["X-Content-Type-Options"], "nosniff")
        self.assertTrue(self.client.get(reverse("labs:attachment", args=[pdf.pk]) + "?download=1")["Content-Disposition"]
                        .startswith("attachment"))
        self.assertEqual(self.client.get(reverse("labs:attachment_thumb", args=[pdf.pk])).status_code, 404)

    # ---------- Who ----------
    def test_a_stranger_gets_404_for_everything(self):
        a = self.attach(self.me, self.task)
        stranger = self.as_user(self.stranger)
        self.assertEqual(stranger.get(reverse("labs:attachment", args=[a.pk])).status_code, 404)
        self.assertEqual(stranger.get(reverse("labs:attachment_thumb", args=[a.pk])).status_code, 404)
        self.assertEqual(self.post_file(stranger, self.task, "x.png", png()).status_code, 404)
        self.assertEqual(stranger.post(reverse("labs:attachment_delete", args=[a.pk])).status_code, 404)
        self.assertEqual(Client().get(reverse("labs:attachment", args=[a.pk])).status_code, 404)    # signed out
        self.assertEqual(Attachment.objects.count(), 1)

    def test_a_circle_member_with_view_access_opens_them_but_cant_add_or_remove(self):
        space = kitchen(self.me, self.wife, name="Family", apps=("planner",))
        share_with(self.task, circle=space.circle, permission="view")
        a = self.attach(self.me, self.task)
        wife = self.as_user(self.wife)
        self.assertEqual(wife.get(reverse("labs:attachment", args=[a.pk])).status_code, 200)
        self.assertEqual(wife.get(reverse("labs:attachment_thumb", args=[a.pk])).status_code, 200)
        self.assertEqual(self.post_file(wife, self.task, "mine.png", png()).status_code, 404)
        self.assertEqual(wife.post(reverse("labs:attachment_delete", args=[a.pk])).status_code, 404)
        self.assertEqual(Attachment.objects.count(), 1)
        panel = wife.get(reverse("albright_planner:item", args=[self.task.pk])).content.decode()
        self.assertIn('data-att-view', panel)
        self.assertNotIn("data-att-form", panel)                 # no Add files
        self.assertNotIn("data-att-delete", panel)               # no trash

    def test_someone_with_edit_access_adds_and_removes(self):
        share_with(self.task, self.wife, "edit")
        wife = self.as_user(self.wife)
        self.assertEqual(self.post_file(wife, self.task, "hers.png", png()).status_code, 201)
        a = Attachment.objects.get()
        self.assertEqual(a.owner, self.wife)
        res = wife.post(reverse("labs:attachment_delete", args=[a.pk]), HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual((res.json()["ok"], res.json()["count"]), (True, 0))
        self.assertFalse(Attachment.objects.exists())
        self.assertFalse(AttachmentBlob.objects.exists())

    def test_a_locked_item_takes_no_uploads(self):
        locked = self.item(title="Auction ends", source_app="watchlist", source_id="9", checkable=False, kind="event")
        self.assertEqual(self.post_file(self.client, locked, "x.png", png()).status_code, 404)

    # ---------- What's refused ----------
    def test_svg_and_html_are_refused(self):
        svg = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
        for name, data in (("x.svg", svg), ("page.html", b"<html><script>alert(1)</script></html>"),
                           ("page.htm", b"<p>hi</p>")):
            with self.subTest(name=name):
                res = self.post_file(self.client, self.task, name, data)
                self.assertEqual(res.status_code, 400)
                self.assertIn("SVG and HTML", res.json()["errors"][0])
        self.assertFalse(Attachment.objects.exists())

    def test_markup_in_a_text_file_is_refused(self):
        res = self.post_file(self.client, self.task, "notes.txt", b"hello <script>alert(1)</script>")
        self.assertEqual(res.status_code, 400)
        res = self.post_file(self.client, self.task, "page.txt", b"<!DOCTYPE html><html></html>")
        self.assertEqual(res.status_code, 400)

    @override_settings(ATTACHMENT_MAX_BYTES=1000)
    def test_an_oversized_file_is_refused(self):
        res = self.post_file(self.client, self.task, "big.pdf", b"%PDF-" + b"0" * 2000)
        self.assertEqual(res.status_code, 400)
        self.assertIn("over the 1000 B limit", res.json()["errors"][0])
        self.assertFalse(Attachment.objects.exists())

    def test_a_fake_image_is_refused(self):
        for name, data in (("cat.png", b"this is not a picture at all"), ("cat.jpg", png()),      # a PNG called .jpg
                           ("cat.gif", b"GIF89a" + b"\x00" * 20)):
            with self.subTest(name=name):
                res = self.post_file(self.client, self.task, name, data)
                self.assertEqual(res.status_code, 400, res.content)
        self.assertFalse(Attachment.objects.exists())

    def test_fake_documents_are_refused(self):
        for name, data in (("quote.pdf", b"not a pdf"), ("plan.docx", b"PK not really a zip"),
                           ("plan.xlsx", docx()),                     # a Word file called .xlsx
                           ("bin.txt", b"abc\x00def"), ("tool.exe", b"MZ")):
            with self.subTest(name=name):
                self.assertEqual(self.post_file(self.client, self.task, name, data).status_code, 400)

    @override_settings(ATTACHMENT_MAX_PER_PARENT=2)
    def test_at_most_n_per_task(self):
        self.attach(self.me, self.task)
        self.attach(self.me, self.task)
        res = self.post_file(self.client, self.task, "third.png", png())
        self.assertEqual(res.status_code, 400)
        self.assertIn("at most 2 attachments", res.json()["errors"][0])

    # ---------- Images ----------
    def test_exif_orientation_is_applied_and_metadata_stripped(self):
        a = self.attach(self.me, self.task, "IMG_1.jpg", jpeg_with_exif((60, 40)))
        self.assertEqual((a.width, a.height), (40, 60))                    # turned upright
        stored = b"".join(self.client.get(reverse("labs:attachment", args=[a.pk])).streaming_content)
        img = Image.open(io.BytesIO(stored))
        self.assertEqual(img.size, (40, 60))
        self.assertEqual(dict(img.getexif()), {})                           # no orientation, camera or GPS
        self.assertNotIn(b"PhoneCam", stored)
        self.assertNotIn(b"Exif", stored)

    def test_a_big_image_is_downscaled(self):
        a = self.attach(self.me, self.task, "wide.png", png((3000, 1500)))
        self.assertEqual((a.width, a.height), (2560, 1280))
        thumb = b"".join(self.client.get(reverse("labs:attachment_thumb", args=[a.pk])).streaming_content)
        self.assertEqual(max(Image.open(io.BytesIO(thumb)).size), 400)

    @skipUnless(attachments.HEIF_SUPPORTED, "pillow-heif isn't installed")
    def test_heic_becomes_jpeg(self):
        out = io.BytesIO()
        Image.new("RGB", (50, 30), (0, 200, 0)).save(out, "HEIF")
        a = self.attach(self.me, self.task, "IMG_0042.HEIC", out.getvalue())
        self.assertEqual((a.mime_type, a.original_name), ("image/jpeg", "IMG_0042.jpg"))
        self.assertTrue(a.key.endswith(".jpg"))
        stored = b"".join(self.client.get(reverse("labs:attachment", args=[a.pk])).streaming_content)
        self.assertEqual(Image.open(io.BytesIO(stored)).format, "JPEG")

    # ---------- Deleting ----------
    def test_deleting_the_task_deletes_its_attachments_and_bytes(self):
        self.attach(self.me, self.task)
        self.attach(self.me, self.task, "plan.docx", docx())
        other = self.attach(self.me, self.item(title="Other"))
        self.client.post(reverse("albright_planner:remove", args=[self.task.pk]), {"scope": "all", "next": "/"})
        self.assertFalse(planner.ScheduleItem.objects.filter(pk=self.task.pk).exists())
        self.assertEqual(list(Attachment.objects.values_list("pk", flat=True)), [other.pk])
        self.assertEqual(list(AttachmentBlob.objects.values_list("attachment_id", flat=True)), [other.pk])

    def test_a_fork_of_a_repeating_task_doesnt_copy_them(self):
        from datetime import timedelta
        series = self.item(title="Water plants", recurrence="FREQ=DAILY")
        self.attach(self.me, series)
        planner.change(series, TODAY + timedelta(days=3), planner.FOLLOWING, {"title": "Water the plants"})
        new = planner.ScheduleItem.objects.exclude(pk__in=[series.pk, self.task.pk]).get()
        self.assertEqual(attachments.for_parent(new).count(), 0)
        self.assertEqual(attachments.for_parent(series).count(), 1)

    # ---------- Where they show ----------
    def test_the_paperclip_on_rows_and_the_sections(self):
        self.attach(self.me, self.task)
        self.attach(self.me, self.task, "plan.docx", docx())
        day = self.client.get(reverse("albright_planner:day") + f"?date={TODAY.isoformat()}").content.decode()
        self.assertIn('aria-label="2 attachments"', day)
        panel = self.client.get(reverse("albright_planner:item", args=[self.task.pk])).content.decode()
        self.assertIn('class="att"', panel)
        self.assertIn("data-att-form", panel)
        self.assertIn('accept="image/*', panel)
        self.assertIn("plan.docx", panel)
        self.assertIn("labs/attachments.js", panel)
        form = self.client.get(reverse("albright_planner:edit", args=[self.task.pk])).content.decode()
        self.assertIn('id="pl-attachments"', form)
        bare = self.item(title="Nothing attached")
        day = self.client.get(reverse("albright_planner:day") + f"?date={TODAY.isoformat()}").content.decode()
        row = day.split(f'data-item="{bare.pk}"', 1)[1].split("</li>", 1)[0]
        self.assertNotIn("att-mark", row)

    def test_bot_api_lists_attachment_metadata(self):
        a = self.attach(self.me, self.task)
        allow_api_keys(self.me)
        _token, raw = BotToken.issue(self.me, "Grok bot", ["schedule:read"])
        with self.settings(BOT_API_REQUIRE_HTTPS=False):
            data = Client().get(f"/api/bot/v1/schedule?start={TODAY}&end={TODAY}",
                                HTTP_AUTHORIZATION=f"Bearer {raw}").json()
        row = next(r for r in data["occurrences"] if r["item_id"] == self.task.pk)
        self.assertEqual(len(row["attachments"]), 1)
        meta = row["attachments"][0]
        self.assertEqual(set(meta), {"id", "name", "type", "size", "url"})
        self.assertEqual((meta["id"], meta["name"], meta["type"]), (a.pk, "photo.png", "image/png"))
        self.assertTrue(meta["url"].endswith(f"/attachments/{a.pk}/"))


class NoteAttachmentTests(Helpers, TestCase):
    def setUp(self):
        self.me = with_apps(User.objects.create_user(username="nate", password=PASSWORD))
        self.wife = with_apps(User.objects.create_user(username="sam", password=PASSWORD), ["notes"])
        self.stranger = with_apps(User.objects.create_user(username="stranger", password=PASSWORD), ["notes"])
        self.client.force_login(self.me)
        self.note = Note.objects.create(owner=self.me, title="Garden plan", body="Beds")

    def test_upload_to_a_note_and_the_paperclip_on_its_card(self):
        self.assertEqual(self.post_file(self.client, self.note, "bed.png", png(), app="notes").status_code, 201)
        page = self.client.get(reverse("albright_notes:note", args=[self.note.pk])).content.decode()
        self.assertIn('aria-label="1 attachment"', page)
        self.assertIn('id="nt-insert-image"', page)
        self.assertIn("data-att-form", page)
        self.assertIn("bed.png", page)

    def test_a_stranger_and_a_view_only_reader(self):
        a = self.attach(self.me, self.note)
        stranger = Client()
        stranger.force_login(self.stranger)
        self.assertEqual(stranger.get(reverse("labs:attachment", args=[a.pk])).status_code, 404)
        self.assertEqual(self.post_file(stranger, self.note, "x.png", png(), app="notes").status_code, 404)
        share_with(self.note, self.wife, "view")
        wife = Client()
        wife.force_login(self.wife)
        self.assertEqual(wife.get(reverse("labs:attachment", args=[a.pk])).status_code, 200)
        self.assertEqual(self.post_file(wife, self.note, "x.png", png(), app="notes").status_code, 404)
        page = wife.get(reverse("albright_notes:note", args=[self.note.pk])).content.decode()
        self.assertIn("data-att-view", page)
        self.assertNotIn("data-att-form", page)
        self.assertNotIn('id="nt-insert-image"', page)

    def test_deleting_the_note_deletes_its_attachments(self):
        self.attach(self.me, self.note)
        self.client.post(reverse("albright_notes:delete", args=[self.note.pk]))
        self.assertFalse(Note.objects.filter(pk=self.note.pk).exists())
        self.assertFalse(Attachment.objects.exists())
        self.assertFalse(AttachmentBlob.objects.exists())

    def test_the_note_sanitizer_keeps_attachment_images_only(self):
        a = self.attach(self.me, self.note)
        html = note_render.render(f"![bed](/attachments/{a.pk}/)\n\n![thumb](/attachments/{a.pk}/thumb/)\n\n"
                                  "![tracker](https://evil.example/pixel.png)\n\n"
                                  '<img src="https://evil.example/x.png" onerror="alert(1)">\n\n'
                                  '<img src="/attachments/1/../../admin/" alt="sneaky">\n\n'
                                  '<img src="javascript:alert(1)">')
        self.assertIn(f'src="/attachments/{a.pk}/"', html)
        self.assertIn(f'src="/attachments/{a.pk}/thumb/"', html)
        self.assertIn('alt="bed"', html)
        self.assertNotIn("evil.example", html)
        self.assertNotIn("onerror", html)
        self.assertNotIn("javascript", html)
        self.assertNotIn("sneaky", html)
        self.assertEqual(html.count("<img"), 2)

    def test_bot_api_note_metadata(self):
        a = self.attach(self.me, self.note, "plan.docx", docx())
        allow_api_keys(self.me)
        _token, raw = BotToken.issue(self.me, "Note Bot", ["notes:read"])
        with self.settings(BOT_API_REQUIRE_HTTPS=False):
            data = Client().get("/api/bot/v1/notes", HTTP_AUTHORIZATION=f"Bearer {raw}").json()
        row = next(n for n in data["notes"] if n["id"] == self.note.pk)
        self.assertEqual([m["id"] for m in row["attachments"]], [a.pk])
        self.assertEqual(row["attachments"][0]["size"], a.size_bytes)


class CleanupAndAccountTests(Helpers, TestCase):
    def setUp(self):
        self.me = with_apps(User.objects.create_user(username="nate", password=PASSWORD))
        self.client.force_login(self.me)

    def test_cleanup_orphan_attachments(self):
        note = Note.objects.create(owner=self.me, title="Keep")
        kept = self.attach(self.me, note)
        gone = Note.objects.create(owner=self.me, title="Gone")
        orphan = self.attach(self.me, gone, "orphan.png")
        Note.objects.filter(pk=gone.pk)._raw_delete(Note.objects.db)     # no signals: leaves the attachment behind
        out = io.StringIO()
        call_command("cleanup_orphan_attachments", "--dry-run", stdout=out)
        self.assertIn("would delete", out.getvalue())
        self.assertIn("orphan.png", out.getvalue())
        self.assertTrue(Attachment.objects.filter(pk=orphan.pk).exists())
        call_command("cleanup_orphan_attachments", stdout=io.StringIO())
        self.assertEqual(list(Attachment.objects.values_list("pk", flat=True)), [kept.pk])
        self.assertEqual(AttachmentBlob.objects.count(), 1)

    def test_my_account_shows_storage_used(self):
        page = self.client.get(reverse("labs:account")).content.decode()
        self.assertIn("haven't attached any files", page)
        note = Note.objects.create(owner=self.me, title="N")
        a = self.attach(self.me, note, "plan.docx", docx())
        page = self.client.get(reverse("labs:account")).content.decode()
        self.assertIn(f"{attachments.size_label(a.stored_bytes)} of 1 GB used", page)
        self.assertIn("Standard plan", page)
        self.assertIn("1 attachment on Planner", page)

    def test_admin_lists_them(self):
        admin = User.objects.create_superuser("boss", "boss@example.com", PASSWORD)
        note = Note.objects.create(owner=self.me, title="N")
        self.attach(self.me, note, "plan.docx", docx())
        client = Client()
        client.force_login(admin)
        page = client.get(reverse("admin:albright_labs_attachment_changelist")).content.decode()
        self.assertIn("plan.docx", page)
        self.assertIn(f"note #{note.pk}", page)
