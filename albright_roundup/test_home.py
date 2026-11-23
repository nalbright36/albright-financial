"""Home's Briefing card: the intro (Turn on / Not now), then the latest briefing; never breaks Home."""
from unittest import mock

from django.contrib.auth.models import User
from django.urls import reverse

from albright_labs.testing import with_apps

from .models import PUSH, WEEK, Roundup
from .tests import PASSWORD, Base

HOME = reverse("labs:hub")


class HomeCardTests(Base):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.me)

    def test_intro_then_turn_on(self):
        self.assertContains(self.client.get(HOME), "Get a briefing of your week")
        r = self.client.post(reverse("roundup:turn_on"), {"next": HOME})
        self.assertRedirects(r, HOME, fetch_redirect_response=False)
        week = Roundup.objects.get(owner=self.me, scope=WEEK)
        self.assertTrue(week.enabled)
        self.assertEqual(week.channels, [PUSH])
        page = self.client.get(HOME)
        self.assertContains(page, "Your week ahead")
        self.assertContains(page, "Open briefing")
        self.assertContains(page, 'id="rb-sheet"')
        self.assertNotContains(page, "Get a briefing of your week")

    def test_not_now_hides_it(self):
        self.client.post(reverse("roundup:not_now"))
        self.assertNotContains(self.client.get(HOME), "rb-home")

    def test_no_card_without_the_planner(self):
        plain = with_apps(User.objects.create_user(username="plain", password=PASSWORD), ["meals", "notes"])
        self.client.force_login(plain)
        self.assertNotContains(self.client.get(HOME), "rb-home")

    def test_a_broken_card_never_breaks_home(self):
        with mock.patch("albright_roundup.views.home_card", side_effect=RuntimeError("boom")):
            r = self.client.get(HOME)
        self.assertEqual(r.status_code, 200)
        self.assertNotContains(r, "rb-home")
