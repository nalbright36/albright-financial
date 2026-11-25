"""Photos on a recipe (albright_meals.recipe_attachments, Option C).

e1: meals is an attachments parent; cover / recipe-card flags; the header, Change cover, Recipe card button,
Add files (several at once), delete confirm; permissions follow the recipe (space, library, view-only share).
e2 tests (ingredient / step links, public share page) are in RecipeAttachmentLinkTests below.
"""
from unittest import mock

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from albright_labs import attachments
from albright_labs.test_attachments import Helpers, png, upload_file
from albright_labs.testing import kitchen, share_with, with_apps

from . import recipe_attachments
from .models import Recipe

PASSWORD = "pw-not-real-12345"
UPLOAD = reverse("labs:attachment_upload")
OUTLOOK = "albright_trading_app.views.get_market_outlook"


class Base(Helpers, TestCase):
    def setUp(self):
        outlook = mock.patch(OUTLOOK, return_value=None)
        outlook.start()
        self.addCleanup(outlook.stop)
        self.nate = with_apps(User.objects.create_user(username="nate", password=PASSWORD, first_name="Nathan"),
                              ["meals", "money"])
        self.sam = with_apps(User.objects.create_user(username="sam", password=PASSWORD, first_name="Sam"), ["meals"])
        self.kim = with_apps(User.objects.create_user(username="kim", password=PASSWORD, first_name="Kim"), ["meals"])
        self.ours = kitchen(self.nate, self.sam, name="Albrights")
        self.theirs = kitchen(self.kim, name="Kim's place")
        self.client.force_login(self.nate)
        self.recipe = Recipe.objects.create(
            space=self.ours, title="Papa Dave's Dressing", created_by=self.nate,
            ingredients=[{"raw": "3/4 cup olive oil", "name": "olive oil"}],
            steps=["Shake everything in a jar."],
            image_url="https://example.com/imported-dish.jpg",
        )

    def as_user(self, user):
        c = Client()
        c.force_login(user)
        return c

    def page(self, client=None, pk=None):
        return (client or self.client).get(reverse("albright_meals:recipe", args=[pk or self.recipe.pk]))

    def cover_url(self):
        return reverse("albright_meals:recipe_attachment_cover", args=[self.recipe.pk])

    def card_url(self):
        return reverse("albright_meals:recipe_attachment_card", args=[self.recipe.pk])


class RecipeAttachmentPageTests(Base):
    def test_cover_falls_back_to_the_imported_image_url(self):
        cover = recipe_attachments.cover_for(self.recipe)
        self.assertEqual(cover["kind"], "image_url")
        self.assertEqual(cover["url"], self.recipe.image_url)
        page = self.page()
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, 'class="ra-headpic"')
        self.assertContains(page, self.recipe.image_url)
        self.assertContains(page, "Change cover")
        self.assertContains(page, "Add files")
        self.assertContains(page, 'name="file" multiple')
        self.assertNotContains(page, 'class="ra-card-btn"')

    def test_first_uploaded_image_is_the_cover_until_one_is_flagged(self):
        first = self.attach(self.nate, self.recipe, "dish.png", png())
        second = self.attach(self.nate, self.recipe, "jar.png", png((50, 40), (20, 160, 40)))
        cover = recipe_attachments.cover_for(self.recipe)
        self.assertEqual(cover["kind"], "attachment")
        self.assertEqual(cover["id"], first.pk)
        self.assertFalse(cover["flagged"])
        recipe_attachments.set_cover(self.recipe, second)
        cover = recipe_attachments.cover_for(self.recipe)
        self.assertEqual(cover["id"], second.pk)
        self.assertTrue(cover["flagged"])
        # Clearing the flag falls back to the first upload, not image_url.
        recipe_attachments.set_cover(self.recipe, None)
        self.assertEqual(recipe_attachments.cover_for(self.recipe)["id"], first.pk)

    def test_multiple_files_upload_in_one_post(self):
        files = [upload_file("card.png", png((40, 30), (200, 30, 30))),
                 upload_file("garlic.png", png((41, 31), (30, 180, 40)))]
        res = self.client.post(UPLOAD, {"parent": "meals", "id": self.recipe.pk, "file": files, "next": "/"},
                               HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(res.status_code, 201, res.content)
        data = res.json()
        self.assertTrue(data["ok"])
        self.assertEqual(len(data["attachments"]), 2)
        self.assertEqual(attachments.for_parent(self.recipe).count(), 2)
        page = self.page()
        self.assertContains(page, "card.png")
        self.assertContains(page, "garlic.png")
        self.assertContains(page, "data-att-delete")
        self.assertContains(page, "Remove?")

    def test_recipe_card_button_opens_the_viewer(self):
        photo = self.attach(self.nate, self.recipe, "card.png")
        res = self.client.post(self.card_url(), {"attachment": photo.pk, "next": reverse("albright_meals:recipe",
                                                                                          args=[self.recipe.pk])})
        self.assertEqual(res.status_code, 302)
        page = self.page()
        self.assertContains(page, "Recipe card")
        self.assertContains(page, f'data-att-view')
        card = recipe_attachments.recipe_card_for(self.recipe)
        self.assertEqual(card["id"], photo.pk)
        self.assertContains(page, reverse("labs:attachment", args=[photo.pk]))

    def test_change_cover_posts_and_uses_imported_photo(self):
        photo = self.attach(self.nate, self.recipe, "plated.png")
        self.client.post(self.cover_url(), {"attachment": photo.pk})
        self.assertEqual(recipe_attachments.cover_for(self.recipe)["id"], photo.pk)
        self.client.post(self.cover_url(), {"attachment": "image_url"})
        self.recipe.refresh_from_db()
        cover = recipe_attachments.cover_for(self.recipe)
        self.assertEqual(cover["kind"], "image_url")
        self.assertEqual(cover["url"], self.recipe.image_url)


class RecipeAttachmentPermissionTests(Base):
    def test_a_space_member_can_upload_and_see_photos(self):
        photo = self.attach(self.nate, self.recipe, "dish.png")
        sam = self.as_user(self.sam)
        res = self.post_file(sam, self.recipe, "sam.png", png((42, 32), (10, 10, 200)), app="meals")
        self.assertEqual(res.status_code, 201, res.content)
        page = self.page(sam)
        self.assertContains(page, "Change cover")
        self.assertContains(page, "Add files")
        self.assertEqual(sam.get(reverse("labs:attachment", args=[photo.pk])).status_code, 200)

    def test_view_only_share_can_see_but_not_edit(self):
        photo = self.attach(self.nate, self.recipe, "dish.png")
        share_with(self.recipe, self.kim, permission="view", by=self.nate)
        kim = self.as_user(self.kim)
        page = self.page(kim)
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "read-only")
        self.assertNotContains(page, "Change cover")
        self.assertNotContains(page, "Add files")
        self.assertNotContains(page, "data-att-delete")
        self.assertEqual(kim.get(reverse("labs:attachment", args=[photo.pk])).status_code, 200)
        self.assertEqual(self.post_file(kim, self.recipe, "nope.png", png(), app="meals").status_code, 404)
        self.assertEqual(kim.post(self.cover_url(), {"attachment": photo.pk}).status_code, 404)
        self.assertEqual(kim.post(reverse("labs:attachment_delete", args=[photo.pk]), {"next": "/"}).status_code, 404)

    def test_edit_share_can_attach(self):
        share_with(self.recipe, self.kim, permission="edit", by=self.nate)
        kim = self.as_user(self.kim)
        res = self.post_file(kim, self.recipe, "kim.png", png(), app="meals")
        self.assertEqual(res.status_code, 201, res.content)
        page = self.page(kim)
        self.assertContains(page, "Change cover")
        self.assertContains(page, "Add files")

    def test_library_reader_can_see_but_not_edit(self):
        photo = self.attach(self.nate, self.recipe, "dish.png")
        self.recipe.visibility = Recipe.LIBRARY
        self.recipe.save(update_fields=["visibility"])
        kim = self.as_user(self.kim)
        page = self.page(kim)
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "Recipe library")
        self.assertNotContains(page, "Change cover")
        self.assertNotContains(page, "Add files")
        self.assertEqual(kim.get(reverse("labs:attachment", args=[photo.pk])).status_code, 200)
        self.assertEqual(self.post_file(kim, self.recipe, "nope.png", png(), app="meals").status_code, 404)

    def test_a_non_member_cannot_see_or_attach(self):
        photo = self.attach(self.nate, self.recipe, "dish.png")
        kim = self.as_user(self.kim)
        # The recipe page sends them back to Meals with a note (item_page); the file itself is a 404.
        self.assertNotEqual(self.page(kim).status_code, 200)
        self.assertEqual(kim.get(reverse("labs:attachment", args=[photo.pk])).status_code, 404)
        self.assertEqual(self.post_file(kim, self.recipe, "nope.png", png(), app="meals").status_code, 404)
        self.assertEqual(kim.post(self.cover_url(), {"attachment": photo.pk}).status_code, 404)

    def test_anonymous_visitors_cannot_open_a_photo(self):
        photo = self.attach(self.nate, self.recipe, "dish.png")
        guest = Client()
        self.assertEqual(guest.get(reverse("labs:attachment", args=[photo.pk])).status_code, 404)
        self.assertEqual(self.post_file(guest, self.recipe, "nope.png", png(), app="meals").status_code, 404)
