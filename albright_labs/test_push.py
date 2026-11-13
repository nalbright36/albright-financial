"""Web Push (albright_labs.push): subscribe / unsubscribe / remove; banners after activity,
respecting By app / By circle / quiet hours; expired subscriptions are dropped; a failed send
never breaks the request; /sw.js and /offline/; the Notifications page's This device card.
Every call to the push service is mocked."""
import json
from datetime import time, timedelta
from types import SimpleNamespace
from unittest import mock

from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from io import StringIO

from cryptography.hazmat.primitives import serialization
from py_vapid import Vapid
from py_vapid.utils import b64urlencode
from pywebpush import WebPushException

from albright_planner.models import ScheduleItem
from albright_labs.models import NotificationSettings, PushSubscription

from . import activity, push
from .testing import share_with, with_apps
from .test_activity import APPS, PASSWORD, Base


def _vapid_pair():
    keys = Vapid()
    keys.generate_keys()
    private = b64urlencode(keys.private_key.private_numbers().private_value.to_bytes(32, "big"))
    public = b64urlencode(keys.public_key.public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.UncompressedPoint,
    ))
    return public, private


VAPID_PUBLIC, VAPID_PRIVATE = _vapid_pair()
VAPID = dict(
    VAPID_PUBLIC_KEY=VAPID_PUBLIC,
    VAPID_PRIVATE_KEY=VAPID_PRIVATE,
    VAPID_SUBJECT="mailto:test@example.com",
)
P256 = "BAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
AUTH = "AAAAAAAAAAAAAAAAAAAAAA"


def _sub(user, suffix="kate", ua="Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605 Safari/604"):
    return PushSubscription.objects.create(
        user=user,
        endpoint=f"https://fcm.googleapis.com/fake/{suffix}",
        p256dh=P256,
        auth=AUTH,
        user_agent=ua,
    )


class DeviceNameTests(TestCase):
    def test_iphone_safari_and_chrome_on_mac(self):
        self.assertEqual(push.device_name(
            "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
        ), "iPhone · Safari")
        self.assertEqual(push.device_name(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ), "Mac · Chrome")
        self.assertEqual(push.device_name(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0"
        ), "Windows · Firefox")
        self.assertEqual(push.device_name(""), "This device")

    def test_vapid_disabled_when_keys_are_missing(self):
        with override_settings(VAPID_PUBLIC_KEY="", VAPID_PRIVATE_KEY="", VAPID_SUBJECT=""):
            self.assertFalse(push.vapid_enabled())
        with override_settings(**VAPID):
            self.assertTrue(push.vapid_enabled())
            self.assertEqual(push.subject(), "mailto:test@example.com")
        with override_settings(VAPID_PUBLIC_KEY=VAPID_PUBLIC, VAPID_PRIVATE_KEY=VAPID_PRIVATE,
                               VAPID_SUBJECT="ops@example.com"):
            self.assertEqual(push.subject(), "mailto:ops@example.com")


class GenerateKeysTests(TestCase):
    def test_prints_env_lines_and_does_not_write_a_file(self):
        out = StringIO()
        call_command("generate_vapid_keys", stdout=out)
        text = out.getvalue()
        self.assertIn("VAPID_PUBLIC_KEY=", text)
        self.assertIn("VAPID_PRIVATE_KEY=", text)
        self.assertIn("VAPID_SUBJECT=", text)
        self.assertIn("Never commit", text)
        # The printed private key can be loaded back.
        private = next(line.split("=", 1)[1].strip() for line in text.splitlines() if line.startswith("VAPID_PRIVATE_KEY="))
        loaded = Vapid.from_string(private)
        self.assertTrue(loaded.private_key)


@override_settings(**VAPID)
class SubscribeTests(Base):
    def post_json(self, name, body, client=None, **headers):
        c = client or self.client
        return c.post(reverse(name), json.dumps(body), content_type="application/json",
                      HTTP_ACCEPT="application/json", **headers)

    def test_login_required(self):
        guest = Client()
        response = guest.post(reverse("labs:push_subscribe"), json.dumps({
            "endpoint": "https://fcm.googleapis.com/fake/x", "keys": {"p256dh": P256, "auth": AUTH},
        }), content_type="application/json")
        self.assertEqual(response.status_code, 302)

    def test_subscribe_upserts_this_device(self):
        response = self.post_json("labs:push_subscribe", {
            "endpoint": "https://fcm.googleapis.com/fake/phone",
            "keys": {"p256dh": P256, "auth": AUTH},
        }, HTTP_USER_AGENT="Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605 Safari/604")
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()
        self.assertTrue(data["ok"])
        sub = PushSubscription.objects.get(user=self.nate)
        self.assertEqual(sub.endpoint, "https://fcm.googleapis.com/fake/phone")
        self.assertEqual(sub.device_name(), "iPhone · Safari")
        # Same endpoint again: upsert, still one row, last_seen_at moves.
        earlier = sub.last_seen_at - timedelta(days=2)
        PushSubscription.objects.filter(pk=sub.pk).update(last_seen_at=earlier)
        self.post_json("labs:push_subscribe", {
            "endpoint": "https://fcm.googleapis.com/fake/phone",
            "keys": {"p256dh": P256, "auth": AUTH},
        })
        self.assertEqual(PushSubscription.objects.filter(user=self.nate).count(), 1)
        self.assertGreater(PushSubscription.objects.get(pk=sub.pk).last_seen_at, earlier)

    def test_invalid_endpoint_is_rejected(self):
        response = self.post_json("labs:push_subscribe", {
            "endpoint": "http://evil.example/push", "keys": {"p256dh": P256, "auth": AUTH},
        })
        self.assertEqual(response.status_code, 400)
        self.assertFalse(PushSubscription.objects.exists())

    def test_unsubscribe_deletes_only_this_users_endpoint(self):
        mine = _sub(self.nate, "nate")
        other = _sub(self.kate, "kate")
        response = self.post_json("labs:push_unsubscribe", {"endpoint": mine.endpoint})
        self.assertEqual(response.json(), {"ok": True})
        self.assertFalse(PushSubscription.objects.filter(pk=mine.pk).exists())
        self.assertTrue(PushSubscription.objects.filter(pk=other.pk).exists())

    def test_remove_own_device_and_not_someone_elses(self):
        mine = _sub(self.nate, "nate-laptop", ua="Mozilla/5.0 (Macintosh) Chrome/120 Safari/537.36")
        other = _sub(self.kate, "kate")
        response = self.client.post(reverse("labs:push_remove", args=[mine.pk]))
        self.assertEqual(response.status_code, 302)
        self.assertFalse(PushSubscription.objects.filter(pk=mine.pk).exists())
        response = self.client.post(reverse("labs:push_remove", args=[other.pk]))
        self.assertEqual(response.status_code, 404)
        self.assertTrue(PushSubscription.objects.filter(pk=other.pk).exists())

    def test_csrf_is_required(self):
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.nate)
        response = csrf_client.post(reverse("labs:push_subscribe"), json.dumps({
            "endpoint": "https://fcm.googleapis.com/fake/x", "keys": {"p256dh": P256, "auth": AUTH},
        }), content_type="application/json")
        self.assertEqual(response.status_code, 403)


@override_settings(**VAPID)
class SendTests(Base):
    def setUp(self):
        super().setUp()
        self.item = self.item("Pack")
        share_with(self.item, self.kate)
        self.kates = _sub(self.kate, "kate")
        self.nates = _sub(self.nate, "nate")

    def record(self, webpush=None):
        with self.captureOnCommitCallbacks(execute=True):
            return activity.created(self.item, actor=self.nate)

    def test_push_goes_to_the_other_person_not_the_actor(self):
        with mock.patch("albright_labs.push.webpush") as webpush:
            event = self.record(webpush)
        self.assertIsNotNone(event)
        self.assertEqual(webpush.call_count, 1)
        info = webpush.call_args.kwargs["subscription_info"]
        self.assertEqual(info["endpoint"], self.kates.endpoint)
        payload = json.loads(webpush.call_args.kwargs["data"])
        self.assertEqual(payload["title"], "Albright")
        self.assertIn("Pack", payload["body"])
        self.assertEqual(webpush.call_args.kwargs["vapid_claims"]["sub"], "mailto:test@example.com")

    def test_quiet_hours_skip_the_send(self):
        now = timezone.localtime()
        start = (now - timedelta(hours=1)).time().replace(microsecond=0)
        end = (now + timedelta(hours=1)).time().replace(microsecond=0)
        prefs = NotificationSettings.for_user(self.kate)
        prefs.quiet_start, prefs.quiet_end = start, end
        prefs.save()
        with mock.patch("albright_labs.push.webpush") as webpush:
            self.record()
        webpush.assert_not_called()

    def test_off_for_the_app_skips_the_send(self):
        prefs = NotificationSettings.for_user(self.kate)
        prefs.levels = {"app:planner": NotificationSettings.OFF}
        prefs.save()
        with mock.patch("albright_labs.push.webpush") as webpush:
            self.record()
        webpush.assert_not_called()

    def test_expired_subscription_is_deleted(self):
        def fail(*args, **kwargs):
            raise WebPushException("gone", response=SimpleNamespace(status_code=410, text="Gone", headers={}))
        with mock.patch("albright_labs.push.webpush", side_effect=fail):
            self.record()
        self.assertFalse(PushSubscription.objects.filter(pk=self.kates.pk).exists())
        self.assertTrue(PushSubscription.objects.filter(pk=self.nates.pk).exists())

    def test_a_failed_send_does_not_break_the_request_or_drop_the_row(self):
        def fail(*args, **kwargs):
            raise WebPushException("server", response=SimpleNamespace(status_code=500, text="no", headers={}))
        with mock.patch("albright_labs.push.webpush", side_effect=fail):
            event = self.record()
        self.assertIsNotNone(event)
        self.assertTrue(PushSubscription.objects.filter(pk=self.kates.pk).exists())

    def test_no_keys_means_no_send(self):
        with override_settings(VAPID_PUBLIC_KEY="", VAPID_PRIVATE_KEY="", VAPID_SUBJECT=""):
            with mock.patch("albright_labs.push.webpush") as webpush:
                self.record()
            webpush.assert_not_called()


class PageTests(Base):
    def test_settings_page_has_the_device_card_and_states(self):
        _sub(self.nate, "laptop", ua="Mozilla/5.0 (Macintosh) Chrome/120 Safari/537.36")
        with override_settings(**VAPID):
            page = self.client.get(reverse("labs:notification_settings")).content.decode()
        self.assertIn("Notify this device", page)
        self.assertIn("Add Albright to your Home Screen first", page)
        self.assertIn("Notifications are blocked", page)
        self.assertIn("This browser can", page)  # can't / can&rsquo;t
        self.assertIn("Mac · Chrome", page)
        self.assertIn("Last used", page)
        self.assertIn(reverse("labs:push_subscribe"), page)
        self.assertIn(reverse("labs:push_unsubscribe"), page)
        self.assertIn(VAPID_PUBLIC, page)
        # The settings form does not wrap the Remove form (invalid nested forms).
        device_at = page.index('id="notif-device"')
        form_at = page.index('class="notif-settings"')
        self.assertLess(device_at, form_at)
        self.assertIn("/push/", page)
        self.assertIn("/remove/", page)

    def test_settings_without_keys_marks_push_unavailable(self):
        with override_settings(VAPID_PUBLIC_KEY="", VAPID_PRIVATE_KEY="", VAPID_SUBJECT=""):
            page = self.client.get(reverse("labs:notification_settings")).content.decode()
        self.assertIn("Notify this device", page)
        self.assertIn('data-vapid=""', page)
        self.assertIn("Banners aren", page)

    def test_sw_js_is_public_with_root_scope(self):
        guest = Client()
        response = guest.get("/sw.js")
        self.assertEqual(response.status_code, 200)
        self.assertIn("javascript", response["Content-Type"])
        self.assertEqual(response["Service-Worker-Allowed"], "/")
        body = response.content.decode()
        self.assertIn("/offline/", body)
        self.assertIn("addEventListener(\"push\"", body)
        self.assertIn("skipWaiting", body)

    def test_offline_page_is_public_and_keeps_the_header(self):
        guest = Client()
        response = guest.get("/offline/")
        self.assertEqual(response.status_code, 200)
        page = response.content.decode()
        self.assertIn("offline", page.lower())
        self.assertIn("You", page)
        self.assertIn("navbar", page)
        self.assertIn("Albright", page)
        self.assertIn("Try again", page)
        self.assertIn("labs/push.js", page)
