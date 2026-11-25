"""The Recipes list's All-recipes default, ?from= filters, badges, memory, and permission checks."""
from django.db import connection
from django.test.utils import CaptureQueriesContext

from albright_labs import circles
from albright_labs.circles import remember
from albright_labs.models import CircleMembership
from albright_labs.testing import grants_of, personal, share_with
from django.urls import reverse

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


    def _all_href(self, response):
        for group in response.context["source_choices"]:
            for item in group["items"]:
                if item["token"] == "all":
                    return item["href"]
        self.fail("No All recipes item in the dropdown")

    def test_switching_back_to_all_from_a_circle(self):
        """Picking All after a circle must send ?from=all so memory does not keep the circle."""
        circled = self.client.get(url("recipes"), {"from": f"space-{self.ours.pk}"})
        self.assertEqual(titles(circled), ["Family Meatballs"])
        href = self._all_href(circled)
        self.assertIn("from=all", href)
        page = self.client.get(url("recipes") + href)
        self.assertEqual(page.context["source_key"], "all")
        self.assertEqual(titles(page), ["Family Meatballs", "Kim's chili", "Solo soup"])
        self.assertEqual(self.client.get(url("recipes")).context["source_key"], "all")

    def test_a_recipe_shared_with_a_circle_shows_for_members(self):
        """Share is a ShareGrant on the existing recipe (not a copy or a move). Members see it on All
        and on that circle's filter, even when the recipe still lives in the owner's Personal space."""
        kolaczki = self.recipe("Polish Kolaczki", space=self.mine)
        share_with(kolaczki, circle=self.ours.circle, by=self.nate)
        self.assertEqual(kolaczki.space, self.mine)     # still personal; the share did not move it
        wife = self.as_user(self.wife)
        all_page = wife.get(url("recipes"), {"from": "all"})
        self.assertIn("Polish Kolaczki", titles(all_page))
        self.assertEqual(badges(all_page)["Polish Kolaczki"], "Albrights")
        circle_page = wife.get(url("recipes"), {"from": f"space-{self.ours.pk}"})
        self.assertEqual(titles(circle_page), ["Family Meatballs", "Polish Kolaczki"])
        self.assertEqual(badges(circle_page)["Polish Kolaczki"], "Albrights")
        # View-only: she can open it, not edit
        self.assertFalse(next(c for c in all_page.context["cards"] if c["recipe"].title == "Polish Kolaczki")["own"])
        self.assertEqual(wife.get(url("recipe", kolaczki.pk)).status_code, 200)
        self.assertEqual(wife.get(url("recipe_edit", kolaczki.pk)).status_code, 302)
        # A stranger still cannot
        kim = self.as_user(self.kim)
        self.assertNotIn("Polish Kolaczki", titles(kim.get(url("recipes"), {"from": "all"})))
        self.assertEqual(kim.get(url("recipe", kolaczki.pk)).status_code, 302)

    def test_add_recipe_from_a_circle_filter_saves_into_that_space(self):
        """The Recipes dropdown does not change the Meals switcher. Add / import must still land in
        the circle (or Mine) being shown, not whatever space This week last remembered."""
        remember(self.nate, "meals", self.mine.pk)
        page = self.client.get(url("recipes"), {"from": f"space-{self.ours.pk}"})
        add = reverse("albright_meals:recipe_add")
        self.assertIn(f'{add}?space={self.ours.pk}', page.content.decode())
        # Import/save uses request.space, which ?space= sets
        self.client.get(add, {"space": self.ours.pk})
        self.client.post(add, {"title": "Imported kolaczki", "ingredients_text": "1 cup flour",
                               "steps_text": "Bake.", "tags_text": "", "description": "", "notes": "",
                               "source_url": "", "image_url": ""})
        saved = Recipe.objects.get(title="Imported kolaczki")
        self.assertEqual(saved.space, self.ours)
        wife = self.as_user(self.wife)
        self.assertIn("Imported kolaczki", titles(wife.get(url("recipes"), {"from": f"space-{self.ours.pk}"})))


    def test_moving_a_recipe_into_a_circle_is_hidden_until_the_member_has_meals_on(self):
        """The Share sheet radios MOVE the recipe's space (set_space), they do not create a ShareGrant.
        A member who joined without Recipes ticked — the accept boxes used to start unchecked — does
        not see the circle's space under All, even though they use Recipes for their own recipes."""
        remember(self.nate, "meals", self.mine.pk)
        dressing = self.recipe("Papa Dave's Dressing", space=self.mine, from_bot=True)
        # What the Share sheet radio does (not share_with):
        dressing.space = self.ours
        dressing.save(update_fields=["space", "updated_at"])
        self.assertEqual(grants_of(dressing), [])

        # A member who can use Meals but is not opted in for this circle (the live bug)
        membership = CircleMembership.objects.get(circle=self.ours.circle, user=self.wife)
        membership.apps = [a for a in (membership.apps or []) if a != "meals"]
        membership.save(update_fields=["apps"])
        chili = Recipe.objects.create(space=personal(self.wife), title="Grigg's Chili",
                                      created_by=self.wife, total_time_minutes=345)
        wife = self.as_user(self.wife)
        shown = titles(wife.get(url("recipes"), {"from": "all"}))
        self.assertEqual(shown, ["Grigg's Chili"])
        self.assertNotIn("Papa Dave's Dressing", shown)
        page = wife.get(url("recipes"), {"from": "all"}).content.decode()
        self.assertNotIn("Goon", page)
        self.assertNotIn("Albrights", page)     # circle is not in the Showing menu either

        # After the opt-in repair, All and the circle filter include the moved recipe
        counts = circles.opt_in_members_for_apps(circle=self.ours.circle, apps=["meals"], dry_run=False)
        self.assertEqual(counts["meals"]["memberships"], 1)
        wife_all = wife.get(url("recipes"), {"from": "all"})
        self.assertEqual(titles(wife_all), ["Family Meatballs", "Grigg's Chili", "Papa Dave's Dressing"])
        self.assertEqual(badges(wife_all)["Papa Dave's Dressing"], "Albrights")
        self.assertEqual(titles(wife.get(url("recipes"), {"from": f"space-{self.ours.pk}"})),
                         ["Family Meatballs", "Papa Dave's Dressing"])

    def test_share_sheet_radios_say_they_move_the_recipe(self):
        in_circle = self.client.get(url("recipe", self.meatballs.pk)).content.decode()
        self.assertIn('name="action" value="set_space"', in_circle)
        # Apostrophe is HTML-escaped in the template (circle&#x27;s).
        self.assertIn("This recipe is in this circle", in_circle)
        self.assertIn("Members with Recipes on for this circle can see it", in_circle)
        self.assertNotIn("Everyone in this circle who uses Recipes", in_circle)
        personal_page = self.client.get(url("recipe", self.solo.pk)).content.decode()
        self.assertIn("Move this recipe into this circle", personal_page)
