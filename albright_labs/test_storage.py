"""Storage tiers and per-user usage (albright_labs.storage): the default Standard tier (1 GB); uploads, removals
and a task or note taking its attachments with it keep used_bytes right; an upload over the limit is refused and
changes nothing; a tier lowered below someone's usage keeps their files but turns Add files off; recalc fixes a
wrong cached value; Admin > Storage (superusers only - anyone else gets the Admin 404); the size formatter; My
account; the Bot API's me/storage."""
import io

from django.contrib.auth.models import User
from django.core.management import call_command
from django.template import Context, Template
from django.test import Client, TestCase
from django.urls import reverse

from albright_bot_api.models import BotToken
from albright_notes.models import Note

from . import storage
from .models import Attachment, StorageTier, UserStorage
from .test_attachments import PASSWORD, Helpers, docx, png
from .testing import allow_api_keys, with_apps

GB = 1024 ** 3


def used(user):
    return UserStorage.objects.get(user=user).used_bytes


class Base(Helpers, TestCase):
    def setUp(self):
        storage.default_tier()        # Standard, as the migration makes it (a flushed test database loses it)
        self.me = with_apps(User.objects.create_user(username="nate", password=PASSWORD))
        self.client.force_login(self.me)
        self.note = Note.objects.create(owner=self.me, title="Garden plan")

    def tier(self, name="Tiny", limit=100, **fields):
        return StorageTier.objects.create(name=name, slug=name.lower(), limit_bytes=limit, **fields)


class TierTests(Base):
    def test_the_default_tier_is_standard_at_1_gb(self):
        standard = StorageTier.objects.get(is_default=True)
        self.assertEqual((standard.name, standard.slug, standard.limit_bytes), ("Standard", "standard", GB))
        self.assertEqual(storage.get_storage_limit(self.me), GB)
        usage = storage.get_storage_usage(self.me)
        self.assertEqual((usage["used_bytes"], usage["limit_bytes"], usage["percent"], usage["remaining_bytes"],
                          usage["tier_name"]), (0, GB, 0.0, GB, "Standard"))

    def test_a_tier_change_changes_the_limit(self):
        big = self.tier("Big", 5 * GB)
        storage.set_tier(self.me, big)
        self.assertEqual(storage.get_storage_limit(self.me), 5 * GB)
        storage.set_tier(self.me, None)
        self.assertEqual(storage.get_storage_limit(self.me), GB)

    def test_changing_the_default_moves_everyone_without_a_tier(self):
        big = self.tier("Big", 2 * GB)
        storage.set_default_tier(big)
        self.assertEqual(StorageTier.objects.filter(is_default=True).get(), big)
        self.assertEqual(storage.get_storage_limit(self.me), 2 * GB)


class UsageTests(Base):
    def test_upload_counts_the_file_and_thumbnail(self):
        res = self.post_file(self.client, self.note, "photo.png", png((900, 700)), app="notes")
        self.assertEqual(res.status_code, 201, res.content)
        a = Attachment.objects.get()
        self.assertTrue(a.has_thumb)
        self.assertGreater(a.thumb_size_bytes, 0)
        self.assertEqual(used(self.me), a.size_bytes + a.thumb_size_bytes)
        self.assertEqual(res.json()["storage"]["used_bytes"], a.stored_bytes)
        self.assertIn("of 1 GB used", res.json()["storage"]["label"])

    def test_remove_gives_the_bytes_back(self):
        a = self.attach(self.me, self.note, "plan.docx", docx())
        self.assertEqual(used(self.me), a.size_bytes)
        res = self.client.post(reverse("labs:attachment_delete", args=[a.pk]), HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(used(self.me), 0)
        self.assertEqual(res.json()["storage"]["used_bytes"], 0)

    def test_deleting_the_note_gives_its_attachments_bytes_back(self):
        self.attach(self.me, self.note)
        self.attach(self.me, self.note, "plan.docx", docx())
        other = Note.objects.create(owner=self.me, title="Other")
        kept = self.attach(self.me, other, "kept.docx", docx())
        self.note.delete()
        self.assertEqual(used(self.me), kept.stored_bytes)

    def test_files_shared_with_you_count_for_the_uploader_only(self):
        sam = with_apps(User.objects.create_user(username="sam", password=PASSWORD), ["notes"])
        a = self.attach(sam, self.note, "theirs.docx", docx())
        self.assertEqual(used(sam), a.size_bytes)
        self.assertEqual(storage.get_storage_usage(self.me)["used_bytes"], 0)

    def test_over_the_limit_is_refused_and_changes_nothing(self):
        storage.set_tier(self.me, self.tier("Tiny", 100))
        res = self.post_file(self.client, self.note, "plan.docx", docx(), app="notes")
        self.assertEqual(res.status_code, 400)
        self.assertIn("Delete some attachments to free up space.", " ".join(res.json()["errors"]))
        self.assertFalse(Attachment.objects.exists())
        self.assertEqual(used(self.me), 0)

    def test_full_message(self):
        self.assertEqual(storage.full_message(GB, GB),
                         "You've used 1.0 GB of your 1 GB storage. Delete some attachments to free up space.")

    def test_a_tier_below_usage_keeps_the_files_but_turns_add_files_off(self):
        a = self.attach(self.me, self.note, "plan.docx", docx())
        storage.set_tier(self.me, self.tier("Tiny", 100))
        self.assertTrue(Attachment.objects.filter(pk=a.pk).exists())
        usage = storage.get_storage_usage(self.me)
        self.assertTrue(usage["full"])
        self.assertEqual((usage["status"], usage["bar_css"]), ("full", "100.0%"))
        self.assertGreater(usage["percent"], 100)
        page = self.client.get(reverse("albright_notes:note", args=[self.note.pk])).content.decode()
        self.assertIn("Storage full: delete some attachments", page)
        self.assertRegex(page, r"data-att-input disabled")
        res = self.post_file(self.client, self.note, "more.docx", docx(), app="notes")
        self.assertEqual(res.status_code, 400)
        self.assertEqual(Attachment.objects.count(), 1)

    def test_warn_and_ok_status(self):
        self.assertEqual(storage.status_for(79.9), "ok")
        self.assertEqual(storage.status_for(80), "warn")
        self.assertEqual(storage.status_for(100), "full")

    def test_the_upload_line_shows_usage(self):
        page = self.client.get(reverse("albright_notes:note", args=[self.note.pk])).content.decode()
        self.assertIn("0 B of 1 GB used", page)
        self.assertIn("att__storage--ok", page)

    def test_recalc_fixes_a_wrong_value(self):
        a = self.attach(self.me, self.note, "plan.docx", docx())
        UserStorage.objects.filter(user=self.me).update(used_bytes=999_999)
        out = io.StringIO()
        call_command("recalc_storage_usage", stdout=out)
        self.assertIn("nate", out.getvalue())
        self.assertEqual(used(self.me), a.size_bytes)
        out = io.StringIO()
        call_command("recalc_storage_usage", "--user", "nate", stdout=out)
        self.assertIn("Every cached value was right", out.getvalue())


class FormatTests(TestCase):
    def test_format_bytes(self):
        for n, text in ((0, "0 B"), (1536, "1.5 KB"), (312 * 1024 * 1024, "312.0 MB"), (GB, "1.0 GB")):
            with self.subTest(n=n):
                self.assertEqual(storage.format_bytes(n), text)
        self.assertEqual(storage.format_limit(GB), "1 GB")

    def test_filter(self):
        html = Template("{% load storage_tags %}{{ n|filesize }} / {{ n|filesize_limit }}").render(Context({"n": GB}))
        self.assertEqual(html, "1.0 GB / 1 GB")


class AccountAndApiTests(Base):
    def test_my_account_storage_card(self):
        self.attach(self.me, self.note, "plan.docx", docx())
        page = self.client.get(reverse("labs:account")).content.decode()
        self.assertIn("Standard plan", page)
        self.assertIn("of 1 GB used", page)
        self.assertIn('class="storage-bar storage-bar--ok"', page)
        self.assertIn("1 attachment on Planner", page)

    def test_bot_api_me_storage(self):
        a = self.attach(self.me, self.note, "plan.docx", docx())
        allow_api_keys(self.me)
        _token, raw = BotToken.issue(self.me, "Bot", ["notes:read"])
        with self.settings(BOT_API_REQUIRE_HTTPS=False):
            res = Client().get("/api/bot/v1/me/storage", HTTP_AUTHORIZATION=f"Bearer {raw}")
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.json(), {"used_bytes": a.size_bytes, "limit_bytes": GB,
                                      "percent": round(a.size_bytes / GB * 100, 1),
                                      "remaining_bytes": GB - a.size_bytes, "tier": "Standard"})


class AdminStorageTests(Base):
    def setUp(self):
        super().setUp()
        self.boss = User.objects.create_superuser("boss", "boss@example.com", PASSWORD)
        self.admin = Client()
        self.admin.force_login(self.boss)
        self.url = reverse("albright_control:storage")

    def test_superusers_only(self):
        self.assertEqual(self.admin.get(self.url).status_code, 200)
        # Anyone else gets the Admin area's 404 (access.superuser_required), like every Admin page.
        self.assertEqual(self.client.get(self.url).status_code, 404)
        self.assertEqual(self.client.get(reverse("albright_control:storage_user", args=[self.me.pk])).status_code, 404)
        self.assertEqual(self.client.post(self.url, {"action": "recalc", "user": self.me.pk}).status_code, 404)
        self.assertEqual(Client().get(self.url).status_code, 404)

    def test_the_page_lists_users_sorted_and_searchable(self):
        self.attach(self.me, self.note, "plan.docx", docx())
        page = self.admin.get(self.url).content.decode()
        self.assertIn('id="storage-user-%d"' % self.me.pk, page)
        self.assertIn("Standard", page)
        self.assertLess(page.index('id="storage-user-%d"' % self.me.pk), page.index('id="storage-user-%d"' % self.boss.pk))
        page = self.admin.get(self.url + "?q=bos&sort=username").content.decode()
        self.assertNotIn('id="storage-user-%d"' % self.me.pk, page)
        self.assertIn('id="storage-user-%d"' % self.boss.pk, page)

    def test_user_page_lists_names_without_links_to_files(self):
        a = self.attach(self.me, self.note, "plan.docx", docx())
        page = self.admin.get(reverse("albright_control:storage_user", args=[self.me.pk])).content.decode()
        self.assertIn("plan.docx", page)
        self.assertIn("Note: Garden plan", page)
        self.assertNotIn(reverse("labs:attachment", args=[a.pk]), page)

    def test_change_tier_and_recalculate(self):
        big = self.tier("Big", 5 * GB)
        self.admin.post(self.url, {"action": "set_tier", "user": self.me.pk, "tier": big.pk})
        self.assertEqual(storage.get_storage_limit(self.me), 5 * GB)
        self.admin.post(self.url, {"action": "set_tier", "user": self.me.pk, "tier": ""})
        self.assertEqual(storage.get_storage_limit(self.me), GB)
        a = self.attach(self.me, self.note, "plan.docx", docx())
        UserStorage.objects.filter(user=self.me).update(used_bytes=5)
        self.admin.post(self.url, {"action": "recalc", "user": self.me.pk})
        self.assertEqual(used(self.me), a.size_bytes)

    def test_tiers_add_edit_default_and_delete(self):
        self.admin.post(self.url, {"action": "tier_save", "name": "Family", "slug": "", "limit_gb": "2.5",
                                   "sort_order": "1"})
        family = StorageTier.objects.get(slug="family")
        self.assertEqual(family.limit_bytes, int(2.5 * GB))
        self.admin.post(self.url, {"action": "tier_save", "tier_id": family.pk, "name": "Family", "slug": "family",
                                   "limit_gb": "3", "sort_order": "1"})
        family.refresh_from_db()
        self.assertEqual(family.limit_bytes, 3 * GB)
        storage.set_tier(self.me, family)
        self.admin.post(self.url, {"action": "tier_delete", "tier_id": family.pk})
        self.assertTrue(StorageTier.objects.filter(pk=family.pk).exists())        # in use: refused
        standard = StorageTier.objects.get(slug="standard")
        self.admin.post(self.url, {"action": "tier_delete", "tier_id": standard.pk})
        self.assertTrue(StorageTier.objects.filter(pk=standard.pk).exists())      # the default: refused
        self.admin.post(self.url, {"action": "tier_default", "tier_id": family.pk})
        self.assertEqual(StorageTier.objects.get(is_default=True), family)
        self.admin.post(self.url, {"action": "tier_default", "tier_id": standard.pk})
        storage.set_tier(self.me, None)
        self.admin.post(self.url, {"action": "tier_delete", "tier_id": family.pk})
        self.assertFalse(StorageTier.objects.filter(pk=family.pk).exists())
