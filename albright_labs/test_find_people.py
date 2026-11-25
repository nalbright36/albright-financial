"""People > Find people (albright_labs.connections.find): never someone who chose "Nobody", never a partial email,
and never an email address in the results."""
from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse

from .models import UserPreference


class FindPeopleTests(TestCase):
    def setUp(self):
        cache.clear()
        self.me = User.objects.create_user("seeker", email="seeker@example.com", password="pw-not-real-1")
        self.open = User.objects.create_user("robin_open", email="robin.open@example.com", password="pw-2",
                                             first_name="Robin")
        self.hidden = User.objects.create_user("robin_hidden", email="robin.hidden@example.com", password="pw-3",
                                               first_name="Robin")
        UserPreference.objects.create(user=self.hidden, findable=UserPreference.FIND_NOBODY)
        self.client.force_login(self.me)

    def find(self, q):
        page = self.client.get(reverse("labs:people"), {"q": q})
        self.assertEqual(page.status_code, 200)
        return page.content.decode(), {p["username"] for p in page.context["found"]}

    def test_nobody_partial_emails_and_no_emails_in_results(self):
        html, found = self.find("robin")                                  # name / username prefix
        self.assertEqual(found, {"robin_open"})
        html_email, found_email = self.find("ROBIN.OPEN@example.com")    # exact email, any case
        self.assertEqual(found_email, {"robin_open"})
        for page in (html, html_email):                                  # the results never show an email
            results = page.split('id="find-results"', 1)[1].split("</section>", 1)[0]
            self.assertIn("@robin_open", results)
            self.assertNotIn("example.com", results)
            self.assertNotIn("robin_hidden", page)

        for q in ("robin.hidden@example.com", "robin_hidden", "robin_h"):   # "Nobody": never found, any way
            _html, found = self.find(q)
            self.assertEqual(found, set(), q)
        for q in ("robin.open@", "robin.open@example", "@example.com", "example.com", "open@example.com", "ro"):
            _html, found = self.find(q)                                    # partial emails, too-short queries
            self.assertEqual(found, set(), q)
        missing_html, _found = self.find("robin.hidden@example.com")      # the same message as an unknown address
        unknown_html, _found = self.find("nobody-here@example.com")
        self.assertIn("No one found.", missing_html)
        self.assertIn("No one found.", unknown_html)
