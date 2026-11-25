"""The Recipes list's All-recipes default, ?from= filters, badges, memory, and permission checks."""
from django.db import connection
from django.test.utils import CaptureQueriesContext

from albright_labs.testing import personal, share_with

from .models import Recipe
from .test_spaces import Base, url


def titles(response):
    return [r.title for r in response.context["recipes"]]


def badges(response):
    return {c["recipe"].title: c["badge"] for c in response.context["cards"]}


def list_html(response):
    page = response.content.decode()
    return page.split('id="recipe-list"', 1)[1] if 'id="recipe-list"' in page else page


class AllRecipesListTests(Base):
    def setUp(self):
        super().setUp()
        self.mine = personal(self.nate)
        self.kim_space = self.theirs
        self.solo = self.recipe("Solo soup", space=self.mine)
        self.meatballs = self.recipe("Family Meatballs", space=self.ours)
        self.chili = Recipe.objects.create(space=self.kim_space, title="Kim's chili", created_by=self.kim)
        share_with(self.chili, user=self.nate, by=self.kim)
        self.pie = Recipe.objects.create(space=self.kim_space, title="Public pie", created_by=self.kim,
                                         visibility=Recipe.LIBRARY)
        self.private = Recipe.objects.create(space=self.kim_space, title="Private pie", created_by=self.kim)

    def test_default_is_the_union_without_duplicates_or_the_library(self):
        share_with(self.meatballs, user=self.nate, by=self.wife)     # already in a space he sees
        page = self.client.get(url("recipes"))
        self.assertEqual(titles(page), ["Family Meatballs", "Kim's chili", "Solo soup"])
        self.assertNotIn("Public pie", titles(page))
        self.assertNotIn("Private pie", titles(page))
        self.assertEqual(len(page.context["recipes"]), 3)
        self.assertContains(page, 'aria-label="Showing: All recipes"')
        self.assertNotContains(page, 'id="space-switch"')
        self.assertContains(page, "Showing:")

    def test_a_circle_you_are_not_in_does_not_leak(self):
        kim = self.as_user(self.kim)
        shown = titles(kim.get(url("recipes")))
        self.assertEqual(shown, ["Kim's chili", "Private pie", "Public pie"])
        self.assertNotIn("Family Meatballs", shown)
        self.assertNotIn("Solo soup", shown)
        page = kim.get(url("recipes")).content.decode()
        self.assertNotIn("Solo soup", page)
        # The dropdown never offers a circle they aren't in
        self.assertNotIn("Albrights", page)

    def test_from_mine_is_personal_only(self):
        page = self.client.get(url("recipes"), {"from": "mine"})
        self.assertEqual(titles(page), ["Solo soup"])
        self.assertEqual(badges(page), {"Solo soup": None})
        self.assertContains(page, 'aria-label="Showing: Mine"')

    def test_from_space_is_that_circle(self):
        # A space they cannot see is ignored (no memory yet, so All)
        other = self.client.get(url("recipes"), {"from": f"space-{self.kim_space.pk}"})
        self.assertEqual(titles(other), ["Family Meatballs", "Kim's chili", "Solo soup"])
        self.assertNotIn("Private pie", titles(other))
        page = self.client.get(url("recipes"), {"from": f"space-{self.ours.pk}"})
        self.assertEqual(titles(page), ["Family Meatballs"])
        self.assertEqual(badges(page)["Family Meatballs"], "Albrights")
        self.assertContains(page, f'href="?from=space-{self.ours.pk}"')

    def test_from_person_is_what_they_shared(self):
        # Someone who never shared with you is ignored (no memory yet, so All)
        other = self.client.get(url("recipes"), {"from": f"person-{self.wife.pk}"})
        self.assertEqual(titles(other), ["Family Meatballs", "Kim's chili", "Solo soup"])
        page = self.client.get(url("recipes"), {"from": f"person-{self.kim.pk}"})
        self.assertEqual(titles(page), ["Kim's chili"])
        self.assertEqual(badges(page)["Kim's chili"], "Kim")
        self.assertContains(page, "From Kim")

    def test_from_shared_and_library_and_legacy_tabs(self):
        self.assertEqual(titles(self.client.get(url("recipes"), {"from": "shared"})), ["Kim's chili"])
        self.assertEqual(titles(self.client.get(url("recipes") + "?tab=shared")), ["Kim's chili"])
        self.assertEqual(titles(self.client.get(url("recipes"), {"from": "library"})), ["Public pie"])
        self.assertEqual(titles(self.client.get(url("recipes") + "?tab=library")), ["Public pie"])
        # Legacy tab=mine is the space being shown (the circle, from kitchen()), not Personal
        self.assertEqual(titles(self.client.get(url("recipes") + "?tab=mine")), ["Family Meatballs"])

    def test_search_and_chips_still_work_on_all(self):
        page = self.client.get(url("recipes"), {"q": "chili"})
        self.assertEqual(titles(page), ["Kim's chili"])
        self.assertContains(page, 'aria-label="Showing: All recipes"')
        tagged = self.recipe("Quick pasta", space=self.mine, tags=["quick"])
        found = self.client.get(url("recipes"), {"tag": "quick"})
        self.assertEqual(titles(found), ["Quick pasta"])
        # Chips keep the source
        mined = self.client.get(url("recipes"), {"from": "mine", "q": "soup"})
        self.assertEqual(titles(mined), ["Solo soup"])
        self.assertIn("from=mine", mined.context["chips"][0]["href"])

    def test_view_only_stays_view_only_on_the_card(self):
        page = self.client.get(url("recipes"))
        chili = next(c for c in page.context["cards"] if c["recipe"].title == "Kim's chili")
        self.assertFalse(chili["own"])
        html = list_html(page)
        # Edit/Delete are on his own cards, not on Kim's
        self.assertIn(f'/meals/recipes/{self.solo.pk}/edit/', html)
        self.assertNotIn(f'/meals/recipes/{self.chili.pk}/edit/', html)
        self.assertNotIn(f'/meals/recipes/{self.chili.pk}/delete/', html)

    def test_choice_is_remembered_and_invalid_from_falls_back(self):
        self.client.get(url("recipes"), {"from": "mine"})
        remembered = self.client.get(url("recipes"))
        self.assertEqual(titles(remembered), ["Solo soup"])
        self.assertEqual(remembered.context["source_key"], "mine")
        # A bad ?from= does not overwrite the memory; this request uses the memory
        bad = self.client.get(url("recipes"), {"from": "not-a-source"})
        self.assertEqual(titles(bad), ["Solo soup"])
        still = self.client.get(url("recipes"))
        self.assertEqual(still.context["source_key"], "mine")
        # Picking All remembers All
        self.client.get(url("recipes"), {"from": "all"})
        self.assertEqual(self.client.get(url("recipes")).context["source_key"], "all")
        self.assertEqual(titles(self.client.get(url("recipes"))), ["Family Meatballs", "Kim's chili", "Solo soup"])

    def test_dropdown_lists_circles_and_people_not_strangers(self):
        page = self.client.get(url("recipes"))
        tokens = [item["token"] for group in page.context["source_choices"] for item in group["items"]]
        self.assertEqual(tokens[0], "all")
        self.assertIn("mine", tokens)
        self.assertIn(f"space-{self.ours.pk}", tokens)
        self.assertIn("shared", tokens)
        self.assertIn(f"person-{self.kim.pk}", tokens)
        self.assertIn("library", tokens)
        self.assertNotIn(f"space-{self.kim_space.pk}", tokens)
        self.assertNotIn(f"person-{self.wife.pk}", tokens)
        html = page.content.decode()
        self.assertIn("All recipes", html)
        self.assertIn("Mine", html)
        self.assertIn("Albrights", html)
        self.assertIn("From Kim", html)
        self.assertIn("Recipe library", html)

    def test_badges_on_cards(self):
        self.assertEqual(badges(self.client.get(url("recipes"))), {
            "Family Meatballs": "Albrights",
            "Kim's chili": "Kim",
            "Solo soup": None,
        })
        html = list_html(self.client.get(url("recipes")))
        self.assertIn('class="rl-card__badge">Albrights</span>', html)
        self.assertIn('class="rl-card__badge">Kim</span>', html)
        # Personal recipe has no badge
        solo_card = html.split(f'data-recipe="{self.solo.pk}"', 1)[1].split("</article>", 1)[0]
        self.assertNotIn("rl-card__badge", solo_card)

    def test_queries_do_not_grow_with_each_card(self):
        # A handful more recipes and shares should not add a query per card.
        for i in range(8):
            extra = Recipe.objects.create(space=self.kim_space, title=f"Batch {i}", created_by=self.kim)
            share_with(extra, user=self.nate, by=self.kim)
        with CaptureQueriesContext(connection) as ctx:
            page = self.client.get(url("recipes"))
        self.assertEqual(len(titles(page)), 11)     # 3 originals in the union + 8 shares
        self.assertLessEqual(len(ctx.captured_queries), 45)
