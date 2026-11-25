"""Public recipe share links (albright_meals.share_links): the page shows only its allow-list - never a rating, the
private notes or the owner's username / email - and a turned-off link is a 404."""
from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import Client, TestCase
from django.urls import reverse

from albright_labs.testing import personal, with_apps

from . import share_links
from .models import Recipe, RecipeRating


class PublicShareLinkTests(TestCase):
    def setUp(self):
        cache.clear()
        outlook = mock.patch("albright_trading_app.views.get_market_outlook", return_value=None)
        outlook.start()
        self.addCleanup(outlook.stop)

    def test_public_page_shows_only_the_allow_list_and_a_revoked_link_is_404(self):
        owner = with_apps(User.objects.create_user("zq_owner_handle", email="zq.owner@example.com", password="pw-x-1",
                                                   first_name="Nathan"), ["meals"])
        recipe = Recipe.objects.create(
            space=personal(owner), created_by=owner, title="White Chicken Chili", description="Creamy and mild.",
            servings=6, total_time_minutes=370, notes="SECRET-PRIVATE-NOTE add more cumin next time",
            ingredients=[{"name": "chicken breasts", "quantity": "1", "unit": "lb", "note": ""}],
            steps=["Add everything to the slow cooker."], tags=["zq-private-tag"])
        RecipeRating.objects.create(recipe=recipe, user=owner, level=RecipeRating.FAVORITE, note="ZQ-RATING-NOTE")
        link, _created = share_links.get_or_create(recipe, owner)
        page = reverse("albright_meals:shared_recipe", args=[link.token])

        visitor = Client()                                               # no sign-in
        reply = visitor.get(page)
        self.assertEqual(reply.status_code, 200)
        html = reply.content.decode()
        self.assertIn("White Chicken Chili", html)
        self.assertIn("Shared by Nathan", html)
        self.assertIn("6 hr 10 min", html)
        for private in ("SECRET-PRIVATE-NOTE", "ZQ-RATING-NOTE", "zq-private-tag", "zq_owner_handle",
                        "zq.owner@example.com", "Favorite", "Rating"):
            self.assertNotIn(private, html)
        self.assertEqual(reply["Referrer-Policy"], "no-referrer")
        self.assertIn("noindex", reply["X-Robots-Tag"])

        share_links.turn_off(recipe)
        gone = visitor.get(page)
        self.assertEqual(gone.status_code, 404)
        self.assertIn("This link is no longer active", gone.content.decode())
        self.assertNotIn("White Chicken Chili", gone.content.decode())
        self.assertEqual(visitor.get(reverse("albright_meals:shared_recipe", args=["not-a-real-token"])).status_code, 404)
