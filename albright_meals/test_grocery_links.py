"""A recipe's ingredients to the grocery list: name matching (lowercase, singular, without "fresh"/"chopped"), the
"On list" badges, adding only the ticked ones - same unit combined, another unit added and linked as a possible
duplicate, the "Added · Combined · possible duplicate" summary - and on the list, the "2 recipes" badge, the
"Possible duplicate" flag with Merge, and "Clear checked items" archiving instead of deleting."""
from decimal import Decimal

from django.urls import reverse

from . import groceries
from .models import GroceryItem
from .tests import Base

CHILI = [{"name": "ground beef", "quantity": "1", "unit": "lb"}, {"name": "onion", "quantity": "1", "unit": ""},
         {"name": "kidney beans", "quantity": "2", "unit": "cans"}]
TACOS = [{"name": "ground beef", "quantity": "0.5", "unit": "lb"},           # same unit as Chili's: combines
         {"name": "fresh cilantro, chopped", "quantity": "1", "unit": "bunch"},
         {"name": "onions", "quantity": "2", "unit": ""},                    # "onion" on the list: combines
         {"name": "tomatoes", "quantity": "2", "unit": "cups"},              # "1 lb tomato" on the list: duplicate
         {"name": "tortillas", "quantity": "8", "unit": ""}]


def page(client, recipe):
    return client.get(reverse("albright_meals:recipe", args=[recipe.pk])).content.decode()


def add(client, recipe, *indexes):
    return client.post(reverse("albright_meals:recipe_groceries", args=[recipe.pk]),
                       {"picked": "1", "ingredient": [str(i) for i in indexes]}, follow=True)


class NormalizationTests(Base):
    def test_ingredient_names_match_regardless_of_wording(self):
        for a, b in (("Onions", "onion"), ("tomatoes", "Tomato"), ("fresh basil", "basil"),
                     ("chopped fresh cilantro", "Cilantro"), ("garlic, minced", "garlic"),
                     ("large eggs", "egg"), ("berries", "berry"), ("boneless skinless chicken thighs", "chicken thigh")):
            with self.subTest(a=a, b=b):
                self.assertEqual(groceries.name_key(a.split(",")[0]), groceries.name_key(b))
        self.assertNotEqual(groceries.name_key("ground beef"), groceries.name_key("beef broth"))
        self.assertEqual(groceries.name_key("fresh"), "fresh")              # nothing left: keep the word itself
        self.assertEqual(groceries.name_key("glass"), "glass")              # "ss" isn't a plural


class OnListTests(Base):
    def test_on_list_badge_and_starts_unticked(self):
        chili = self.recipe("Chili", CHILI)
        groceries.add_recipes(self.household, [chili], user=self.owner)
        tacos = self.recipe("Tacos", TACOS)
        html = page(self.client, tacos)
        beef = html.split('value="0"', 1)[1].split("</li>", 1)[0]
        self.assertIn("On list: 1 lb (from Chili)", beef)
        self.assertNotIn("checked", html.split('name="ingredient" value="0"', 1)[1].split(">", 1)[0])
        onions = html.split('name="ingredient" value="2"', 1)[1].split("</li>", 1)[0]
        self.assertIn("On list: 1 (from Chili)", onions)
        tortillas = html.split('name="ingredient" value="4"', 1)[1].split(">", 1)[0]
        self.assertIn("checked", tortillas)                                  # not on the list: ticked
        self.assertIn('Add <span data-pick-count>3</span> item<span data-pick-plural>s</span>', html)   # cilantro, tomatoes, tortillas

    def test_checked_and_cleared_lines_dont_count(self):
        chili = self.recipe("Chili", CHILI)
        GroceryItem.objects.create(household=self.household, name="ground beef", quantity=1, unit="lb", checked=True)
        GroceryItem.objects.create(household=self.household, name="onion", quantity=1, archived_at="2026-11-01T10:00:00Z", checked=True)
        self.assertNotIn("On list:", page(self.client, chili))


class AddSelectedTests(Base):
    def setUp(self):
        super().setUp()
        self.chili = self.recipe("Chili", CHILI)
        groceries.add_recipes(self.household, [self.chili], user=self.owner)
        GroceryItem.objects.create(household=self.household, name="tomato", quantity=1, unit="lb")
        self.tacos = self.recipe("Tacos", TACOS)

    def test_only_the_ticked_ones_are_added(self):
        add(self.client, self.tacos, 4)
        self.assertTrue(GroceryItem.objects.filter(name="tortillas").exists())
        self.assertFalse(GroceryItem.objects.filter(name__icontains="cilantro").exists())

    def test_same_unit_quantities_combine_into_one_line(self):
        add(self.client, self.tacos, 0, 2)
        beef = GroceryItem.objects.get(name="ground beef")
        self.assertEqual((beef.quantity, beef.unit), (Decimal("1.5"), "lb"))
        self.assertEqual(sorted(beef.recipes.values_list("title", flat=True)), ["Chili", "Tacos"])
        self.assertEqual(GroceryItem.objects.get(name="onion").quantity, Decimal("3"))
        self.assertEqual(GroceryItem.objects.filter(name__in=["ground beef", "onion", "onions"]).count(), 2)

    def test_different_units_stay_separate_and_are_linked(self):
        add(self.client, self.tacos, 3)
        lines = GroceryItem.objects.filter(name__in=["tomato", "tomatoes"]).order_by("pk")
        self.assertEqual([(l.quantity, l.unit) for l in lines], [(Decimal("1"), "lb"), (Decimal("2"), "cup")])
        self.assertEqual(lines[1].duplicate_of, lines[0])

    def test_summary_counts(self):
        response = add(self.client, self.tacos, 0, 1, 2, 3, 4)
        self.assertContains(response, "Added 2 · Combined 2 · 1 possible duplicate.")
        self.assertEqual(groceries.summary({"added": 1, "duplicates": 2}), "Added 1 · 2 possible duplicates")
        response = add(self.client, self.tacos)
        self.assertContains(response, "Tick the ingredients to add first.")

    def test_the_same_ingredient_twice_in_one_recipe_is_one_line(self):
        soup = self.recipe("Soup", [{"name": "carrots", "quantity": "2", "unit": ""},
                                    {"name": "carrot", "quantity": "1", "unit": ""}])
        response = add(self.client, soup, 0, 1)
        self.assertContains(response, "Added 1.")
        self.assertEqual(GroceryItem.objects.get(name="carrots").quantity, Decimal("3"))

    def test_quick_add_links_a_different_unit_too(self):
        self.client.post(reverse("albright_meals:groceries"), {"line": "2 cups ground beef"})
        added = GroceryItem.objects.get(name="ground beef", unit="cup")
        self.assertEqual(added.duplicate_of.unit, "lb")


class GroceryListBadgeTests(Base):
    def list_html(self):
        return self.client.get(reverse("albright_meals:grocery_list")).json()["html"]

    def test_multi_recipe_badge_lists_them(self):
        chili, tacos = self.recipe("Chili", CHILI), self.recipe("Tacos", TACOS)
        groceries.add_recipes(self.household, [chili], user=self.owner)
        add(self.client, tacos, 0)
        html = self.list_html()
        beef = html.split(f'data-item="{GroceryItem.objects.get(name="ground beef").pk}"', 1)[1].split("</li>", 1)[0]
        self.assertIn("2 recipes</summary>", beef)
        self.assertIn(">Chili</a>", beef)
        self.assertIn(">Tacos</a>", beef)
        onion = html.split(f'data-item="{GroceryItem.objects.get(name="onion").pk}"', 1)[1].split("</li>", 1)[0]
        self.assertNotIn("recipes</summary>", onion)
        self.assertIn("from Chili", onion)

    def test_possible_duplicate_flag_and_merge(self):
        lb = GroceryItem.objects.create(household=self.household, name="ground beef", quantity=1, unit="lb")
        cup, status = groceries.add_item_detail(self.household, "ground beef", Decimal(2), "cups")
        self.assertEqual(status, groceries.DUPLICATE)
        html = self.list_html()
        flagged = html.split(f'data-item="{cup.pk}"', 1)[1].split("</li>", 1)[0]
        self.assertIn("Possible duplicate", flagged)
        self.assertIn(reverse("albright_meals:grocery_merge", args=[cup.pk]), flagged)
        original = html.split(f'data-item="{lb.pk}"', 1)[1].split("</li>", 1)[0]
        self.assertIn("Possible duplicate", original)                        # flagged both ways; Merge on the new one
        self.assertNotIn("/merge/", original)
        response = self.client.post(reverse("albright_meals:grocery_merge", args=[cup.pk]), follow=True)
        self.assertContains(response, "Merged into ground beef.")
        self.assertFalse(GroceryItem.objects.filter(pk=cup.pk).exists())
        lb.refresh_from_db()
        self.assertEqual((lb.quantity, lb.unit, lb.note), (Decimal("1"), "lb", "+ 2 cup"))
        self.assertNotIn("Possible duplicate", self.list_html())

    def test_merge_same_unit_adds_quantities_and_keeps_recipes(self):
        chili = self.recipe("Chili", CHILI)
        lb = GroceryItem.objects.create(household=self.household, name="ground beef", quantity=1, unit="lb")
        other = GroceryItem.objects.create(household=self.household, name="beef", quantity=Decimal("0.5"), unit="lb", duplicate_of=lb)
        other.recipes.add(chili)
        self.assertEqual(groceries.merge(other), lb)
        lb.refresh_from_db()
        self.assertEqual(lb.quantity, Decimal("1.5"))
        self.assertEqual(list(lb.recipes.all()), [chili])

    def test_no_merge_once_the_other_line_is_checked(self):
        lb = GroceryItem.objects.create(household=self.household, name="ground beef", quantity=1, unit="lb", checked=True)
        cup = GroceryItem.objects.create(household=self.household, name="ground beef", quantity=2, unit="cup", duplicate_of=lb)
        self.assertNotIn("Possible duplicate", self.list_html())
        self.assertIsNone(groceries.merge(cup))


class ClearCheckedTests(Base):
    def test_clear_checked_archives_instead_of_deleting(self):
        milk = GroceryItem.objects.create(household=self.household, name="milk", checked=True)
        eggs = GroceryItem.objects.create(household=self.household, name="eggs")
        self.client.post(reverse("albright_meals:grocery_clear"))
        milk.refresh_from_db()
        self.assertIsNotNone(milk.archived_at)                              # still in the database
        html = self.client.get(reverse("albright_meals:grocery_list")).json()["html"]
        self.assertNotIn(f'data-item="{milk.pk}"', html)
        self.assertIn(f'data-item="{eggs.pk}"', html)
        self.assertEqual(self.client.post(reverse("albright_meals:grocery_toggle", args=[milk.pk])).status_code, 404)
        # Never matched again: adding milk makes a new line.
        groceries.add_item(self.household, "milk")
        self.assertEqual(GroceryItem.objects.active().filter(name="milk").count(), 1)
        # Off the Bot API's list too.
        self.assertNotIn(milk.pk, [i.pk for i in GroceryItem.objects.active()])
