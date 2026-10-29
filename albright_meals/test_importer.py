"""Import a recipe from a link (importer.py, the Recipes page's import box,
Re-import, and the Bot API's meals/recipes/import). No real network: fetch()
gets a fake session, DNS is faked, and the pages are saved HTML fixtures
(test_fixtures/). The AI fallback gets a fake OpenAI client.

Covers JSON-LD with @graph and @id references, HowToSection steps, ISO
durations, ingredient parsing (fractions, unicode fractions, lines that don't
split), the microdata fallback, the AI fallback (marked needs_review, keeps
only what's on the page, counts toward the AI limits), "No recipe found",
SSRF blocks (localhost, 10.x, 169.254.x, redirects to private addresses, the
connect-time check), the size and time limits, duplicate links with tracking
parameters, re-import, the preview/save flow, adding to the grocery list,
and the Bot API endpoint with and without its scope."""
import json
import socket
from pathlib import Path
from unittest import mock

import requests
from django.test import SimpleTestCase, override_settings
from django.urls import reverse

from albright_bot_api.models import BotRequestLog, BotToken
from albright_crypto_app.models import AIExtraction

from . import importer
from .importer import RecipeImportError
from .models import GroceryItem, MealPlanEntry, Recipe
from .tests import API, Base

FIXTURES = Path(__file__).parent / "test_fixtures"
CHILI_URL = "https://www.examplekitchen.com/recipes/weeknight-chili/"
PUBLIC_IP = "93.184.216.34"
PRIVATE_HOSTS = {"localhost": "127.0.0.1", "internal.example": "10.0.0.8", "metadata.example": "169.254.169.254",
                 "rebind.example": "127.0.0.1", "v6local.example": "::1"}


def fixture(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


def fake_getaddrinfo(host, port, *args, **kwargs):
    """Public address for every test host, except the private ones above (IP literals resolve to themselves)."""
    try:
        address = str(__import__("ipaddress").ip_address(host.strip("[]")))
    except ValueError:
        address = PRIVATE_HOSTS.get(host, PUBLIC_IP)
    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    return [(family, socket.SOCK_STREAM, 6, "", (address, port))]


class FakeResponse:
    def __init__(self, status=200, body=b"", headers=None, chunks=None):
        self.status_code = status
        self.headers = {"Content-Type": "text/html; charset=utf-8", **(headers or {})}
        self.chunks = chunks if chunks is not None else [body]
        self.closed = False

    @property
    def is_redirect(self):
        return self.status_code in (301, 302, 303, 307, 308) and "Location" in self.headers

    def iter_content(self, size):
        yield from self.chunks

    def close(self):
        self.closed = True


class FakeSession:
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        response = self.routes[url]
        if isinstance(response, Exception):
            raise response
        return response


def page(html):
    return FakeResponse(body=html.encode("utf-8"))


def redirect(location, status=302):
    return FakeResponse(status=status, headers={"Location": location})


def fake_ai(reply):
    """An OpenAI client whose responses.create answers with this JSON."""
    client = mock.Mock()
    client.responses.create.return_value = {"output_text": json.dumps(reply),
                                            "usage": {"input_tokens": 1000, "output_tokens": 200}}
    return client


SOUP_AI_REPLY = {
    "found": True, "title": "My Tomato Soup", "description": "",
    "ingredients": ["2 tbsp butter", "1 onion, chopped", "28 oz can whole tomatoes", "2 cups vegetable stock",
                    "1 cup heavy cream",          # only in a hidden element
                    "5 cups of sugar",            # only in a script
                    "3 carrots"],                 # invented
    "steps": ["Melt the butter and soften the onion.", "Add the tomatoes and stock and simmer for 20 minutes.",
              "Blend until smooth.", "Garnish with basil."],       # the last one is invented
    "servings": "Serves 4", "prep_time": "", "cook_time": "", "total_time": "about 30 minutes",
    "author": "Sam Cook"}


@mock.patch("albright_meals.importer.socket.getaddrinfo", side_effect=fake_getaddrinfo)
class ParsingTests(SimpleTestCase):
    def test_json_ld_graph(self, _dns):
        recipe = importer.parse_recipe_html(fixture("jsonld_graph.html"), CHILI_URL + "?utm_source=mail")
        self.assertEqual(recipe["method"], "json-ld")
        self.assertEqual(recipe["title"], "Weeknight Chili & Cornbread")            # entity decoded
        self.assertEqual(recipe["description"], "A quick chili for busy nights.")   # tags removed
        self.assertEqual(recipe["author"], "Ann Baker")                             # resolved from its @id
        self.assertEqual(recipe["image_url"], "https://cdn.examplekitchen.com/chili-16x9.jpg")
        self.assertEqual((recipe["servings"], recipe["prep_minutes"], recipe["cook_minutes"],
                          recipe["total_time_minutes"]), (6, 15, 90, 105))
        self.assertEqual(recipe["source_url"], CHILI_URL)                          # the canonical link
        self.assertEqual(recipe["source_name"], "Example Kitchen")                  # og:site_name
        self.assertEqual(len(recipe["ingredients"]), 8)
        self.assertFalse(recipe["needs_review"])

    def test_howto_sections_are_flattened_with_their_headings(self, _dns):
        recipe = importer.parse_recipe_html(fixture("jsonld_graph.html"), CHILI_URL)
        self.assertEqual(recipe["steps"], [
            "## For the chili", "Brown the beef in a large pot.", "Add the onion and garlic; cook 5 minutes.",
            "## To finish", "Stir in the tomatoes and spices, then simmer 1 hour.",
            "Season with salt and pepper & serve."])
        groups = importer.step_groups(recipe["steps"])
        self.assertEqual([(g["heading"], g["start"], len(g["steps"])) for g in groups],
                         [("For the chili", 1, 2), ("To finish", 3, 2)])   # numbering carries on

    def test_nested_recipe_url_type_and_string_instructions(self, _dns):
        recipe = importer.parse_recipe_html(fixture("jsonld_nested.html"), "https://pastaplace.example/lemon-pasta?x=1")
        self.assertEqual(recipe["title"], "Lemon Pasta")                       # inside WebPage.mainEntity
        self.assertEqual(recipe["steps"], ["Boil the pasta.", "Toss with lemon.", "Serve."])  # numbers dropped
        self.assertEqual(recipe["author"], "Luca, Maria")
        self.assertEqual(recipe["image_url"], "https://pastaplace.example/images/lemon.jpg")   # made absolute
        self.assertEqual((recipe["servings"], recipe["total_time_minutes"]), (2, 25))
        self.assertEqual(recipe["source_url"], "https://pastaplace.example/lemon-pasta")       # og:url
        self.assertEqual(recipe["source_name"], "Pasta Place")                                 # the publisher

    def test_microdata_fallback(self, _dns):
        recipe = importer.parse_recipe_html(fixture("microdata.html"), "https://oldfamily.example/pancakes")
        self.assertEqual(recipe["method"], "microdata")
        self.assertEqual(recipe["title"], "Grandma's Pancakes")        # not the nested review's name
        self.assertEqual([i["original"] for i in recipe["ingredients"]], ["1 ½ cups flour", "2 eggs", "1 cup milk"])
        self.assertEqual(recipe["ingredients"][0]["quantity"], "1.5")
        self.assertEqual(recipe["steps"], ["Whisk everything together.", "Cook on a hot griddle until golden."])
        self.assertEqual((recipe["prep_minutes"], recipe["cook_minutes"], recipe["servings"]), (10, 20, 4))
        self.assertEqual((recipe["author"], recipe["source_name"]), ("Rose Miller", "Old Family Recipes"))
        self.assertEqual(recipe["image_url"], "https://oldfamily.example/img/pancakes.jpg")

    def test_no_structured_recipe(self, _dns):
        self.assertIsNone(importer.parse_recipe_html(fixture("not_a_recipe.html"), "https://acme.example/about"))
        self.assertIsNone(importer.parse_recipe_html(fixture("no_structured_data.html"), "https://blog.example/s"))
        self.assertIsNone(importer.parse_recipe_html("", "https://blog.example/empty"))
        self.assertIsNone(importer.parse_recipe_html('<script type="application/ld+json">{"@type": "Recipe", '
                                                     '"name": "Only a name"}</script>', "https://x.example/"))

    def test_iso_durations(self, _dns):
        for value, minutes in [("PT15M", 15), ("PT1H30M", 90), ("P0DT1H45M", 105), ("PT90M", 90), ("PT45S", 1),
                               ("P1DT2H", 1560), ("pt2h", 120), ("PT1.5H", 90), (["PT10M"], 10),
                               ("1 hr 15 mins", 75), ("20 minutes", 20), ("PT0M", None), ("PT", None), ("", None),
                               (None, None), ("soon", None), ("P30D", None)]:
            with self.subTest(value=value):
                self.assertEqual(importer.iso_minutes(value), minutes)

    def test_servings(self, _dns):
        for value, servings in [(4, 4), ("6", 6), ("4-6 servings", 4), (["8", "8 slices"], 8), ("Serves 2", 2),
                                ("a crowd", None), (0, None), ("500 g", None), (True, None)]:
            with self.subTest(value=value):
                self.assertEqual(importer.servings_from(value), servings)

    def test_ingredient_parsing(self, _dns):
        cases = [
            ("2 lb ground beef", ("ground beef", "2", "lb", "")),
            ("1 1/2 cups diced onion", ("diced onion", "1.5", "cup", "")),
            ("½ tsp cayenne pepper", ("cayenne pepper", "0.5", "tsp", "")),
            ("1½ tablespoons chili powder", ("chili powder", "1.5", "tbsp", "")),
            ("2 ¼ cups flour", ("flour", "2.25", "cup", "")),
            ("1⁄3 cup sugar", ("sugar", "0.333", "cup", "")),
            ("2-3 cloves garlic, minced", ("garlic", "3", "clove", "minced")),
            ("3 to 4 cups (750 ml) broth", ("broth", "4", "cup", "750 ml")),
            ("1 (14 oz) can diced tomatoes", ("diced tomatoes", "1", "can", "14 oz")),
            ("2 cups of milk", ("milk", "2", "cup", "")),
            ("1 onion, diced", ("onion", "1", "", "diced")),
            ("4 chicken thighs (bone-in)", ("chicken thighs", "4", "", "bone-in")),
            ("Salt and pepper to taste", ("Salt and pepper to taste", "", "", "")),
            ("A handful of fresh cilantro", ("A handful of fresh cilantro", "", "", "")),
            ("For the garnish:", ("For the garnish:", "", "", "")),
            ("(optional)", ("(optional)", "", "", "")),
            ("1/2", ("1/2", "0.5", "", "")),
        ]
        for line, (name, quantity, unit, note) in cases:
            with self.subTest(line=line):
                parsed = importer.parse_ingredient(line)
                self.assertEqual((parsed["name"], parsed["quantity"], parsed["unit"], parsed["note"]),
                                 (name, quantity, unit, note))
                self.assertEqual(parsed["original"], line)            # always kept exactly
        recipe = importer.parse_recipe_html(fixture("jsonld_graph.html"), CHILI_URL)
        self.assertEqual([i["original"] for i in recipe["ingredients"]][-2:],
                         ["Salt and pepper to taste", "A handful of fresh cilantro"])   # never dropped

    def test_normalize_url(self, _dns):
        key = "examplekitchen.com/recipes/weeknight-chili"
        for url in ["https://www.examplekitchen.com/recipes/weeknight-chili/",
                    "http://WWW.ExampleKitchen.com/recipes/weeknight-chili",
                    "https://examplekitchen.com/recipes/weeknight-chili/?utm_source=newsletter&utm_medium=email",
                    "https://examplekitchen.com/recipes/weeknight-chili?fbclid=abc&gclid=1#comments",
                    "https://examplekitchen.com:443/recipes/weeknight-chili"]:
            with self.subTest(url=url):
                self.assertEqual(importer.normalize_url(url), key)
        self.assertEqual(importer.normalize_url("https://x.example/r?b=2&utm_campaign=z&a=1"), "x.example/r?a=1&b=2")
        self.assertNotEqual(importer.normalize_url("https://x.example/r?id=1"), importer.normalize_url("https://x.example/r?id=2"))
        self.assertEqual(importer.clean_url("https://X.example/Path?utm_source=a&id=3#top"), "https://x.example/Path?id=3")


@mock.patch("albright_meals.importer.socket.getaddrinfo", side_effect=fake_getaddrinfo)
class FetchTests(SimpleTestCase):
    def test_blocks_non_public_and_non_http_links(self, _dns):
        for url in ["http://localhost/recipe", "http://127.0.0.1/", "http://10.0.0.5/recipe", "http://10.1.2.3:443/",
                    "http://169.254.169.254/latest/meta-data/", "http://192.168.1.1/", "http://172.16.0.1/",
                    "http://[::1]/", "http://0.0.0.0/", "http://100.64.0.1/", "http://internal.example/",
                    "http://metadata.example/", "http://v6local.example/", "ftp://recipes.example/x",
                    "file:///etc/passwd", "javascript:alert(1)", "http://user:pw@recipes.example/",
                    "http://recipes.example:8080/", "https://recipes.example:22/", "", "https://"]:
            with self.subTest(url=url):
                session = FakeSession({})
                with self.assertRaises(RecipeImportError) as caught:
                    importer.fetch(url, session=session)
                self.assertEqual(caught.exception.code, "blocked")
                self.assertEqual(caught.exception.message, importer.BLOCKED)
                self.assertEqual(session.calls, [])                    # never requested

    def test_redirects_to_private_addresses_are_blocked(self, _dns):
        start = "https://recipes.example/r"
        for target in ["http://169.254.169.254/latest/meta-data/", "http://10.0.0.9/", "http://internal.example/",
                       "http://localhost:80/admin", "ftp://recipes.example/"]:
            with self.subTest(target=target):
                session = FakeSession({start: redirect(target), target: page("<p>nope</p>")})
                with self.assertRaises(RecipeImportError) as caught:
                    importer.fetch(start, session=session)
                self.assertEqual(caught.exception.code, "blocked")
                self.assertEqual([url for url, _kw in session.calls], [start])   # the private one never fetched
        # A relative redirect on the same public site is followed.
        session = FakeSession({start: redirect("/recipes/r"), "https://recipes.example/recipes/r": page("<p>ok</p>")})
        self.assertEqual(importer.fetch(start, session=session), ("https://recipes.example/recipes/r", "<p>ok</p>"))

    def test_at_most_three_redirects(self, _dns):
        hops = [f"https://recipes.example/{n}" for n in range(5)]
        routes = {hops[n]: redirect(hops[n + 1], status=301 if n % 2 else 307) for n in range(4)}
        routes[hops[4]] = page("<p>end</p>")
        session = FakeSession(dict(routes, **{hops[3]: page("<p>three</p>")}))
        self.assertEqual(importer.fetch(hops[0], session=session), (hops[3], "<p>three</p>"))
        session = FakeSession(routes)
        with self.assertRaises(RecipeImportError) as caught:
            importer.fetch(hops[0], session=session)
        self.assertIn("redirects too many times", caught.exception.message)
        self.assertEqual(len(session.calls), 4)                        # the first request and three redirects

    def test_request_settings(self, _dns):
        session = FakeSession({"https://recipes.example/r": page("<p>hi</p>")})
        importer.fetch("https://recipes.example/r", session=session)
        kwargs = session.calls[0][1]
        self.assertFalse(kwargs["allow_redirects"])                    # each hop is checked here instead
        self.assertTrue(kwargs["stream"])
        self.assertTrue(all(0 < t <= importer.TIMEOUT for t in kwargs["timeout"]))
        self.assertTrue(kwargs["headers"]["User-Agent"].startswith("Mozilla/5.0"))
        self.assertIn("AlbrightMeals", kwargs["headers"]["User-Agent"])
        self.assertIn("sportslabalgo.com", kwargs["headers"]["User-Agent"])

    def test_size_limit(self, _dns):
        big = FakeResponse(headers={"Content-Length": str(importer.MAX_BYTES + 1)}, chunks=[b"x"])
        streamed = FakeResponse(chunks=[b"x" * (1024 * 1024)] * 4)     # 4 MB with no Content-Length
        for response in (big, streamed):
            with self.subTest(response=response.headers):
                with self.assertRaises(RecipeImportError) as caught:
                    importer.fetch("https://recipes.example/r", session=FakeSession({"https://recipes.example/r": response}))
                self.assertEqual(caught.exception.code, "too_large")
                self.assertTrue(response.closed)
        ok = FakeResponse(chunks=[b"<p>" + b"x" * (1024 * 1024) + b"</p>"] * 2)   # 2 MB is fine
        self.assertTrue(importer.fetch("https://recipes.example/r", session=FakeSession({"https://recipes.example/r": ok}))[1])

    def test_timeout(self, _dns):
        session = FakeSession({"https://recipes.example/r": requests.Timeout("slow")})
        with self.assertRaises(RecipeImportError) as caught:
            importer.fetch("https://recipes.example/r", session=session)
        self.assertEqual((caught.exception.code, caught.exception.message), ("timeout", importer.TIMED_OUT))
        # A page that keeps trickling in past 10 seconds in all.
        ticks = iter([0, 1, 2, 6, 11, 12])
        slow = FakeResponse(chunks=[b"<p>", b"more", b"more", b"</p>"])
        with self.assertRaises(RecipeImportError) as caught:
            importer.fetch("https://recipes.example/r", session=FakeSession({"https://recipes.example/r": slow}),
                           clock=lambda: next(ticks))
        self.assertEqual(caught.exception.code, "timeout")
        # The redirects share the same 10 seconds.
        ticks = iter([0, 4, 11])
        session = FakeSession({"https://recipes.example/a": redirect("https://recipes.example/b"),
                               "https://recipes.example/b": page("<p>b</p>")})
        with self.assertRaises(RecipeImportError) as caught:
            importer.fetch("https://recipes.example/a", session=session, clock=lambda: next(ticks))
        self.assertEqual(caught.exception.code, "timeout")

    def test_errors_and_non_pages(self, _dns):
        for response, text in [(FakeResponse(status=404), "error (404)"),
                               (FakeResponse(headers={"Content-Type": "application/pdf"}), "isn't a web page"),
                               (requests.ConnectionError("refused"), "Couldn't load")]:
            with self.subTest(text=text):
                with self.assertRaises(RecipeImportError) as caught:
                    importer.fetch("https://recipes.example/r", session=FakeSession({"https://recipes.example/r": response}))
                self.assertIn(text, caught.exception.message)

    def test_the_connection_itself_only_reaches_public_addresses(self, _dns):
        """DNS checked again at connect time: a name that switches to a private address is still refused."""
        with mock.patch("albright_meals.importer.socket.create_connection") as connect:
            for connection in (importer._HTTPConnection("rebind.example", 80),
                               importer._HTTPSConnection("internal.example", 443)):
                with self.subTest(host=connection.host):
                    with self.assertRaises(RecipeImportError):
                        connection._new_conn()
            connect.assert_not_called()
            importer._HTTPSConnection("recipes.example", 443, timeout=5)._new_conn()
            connect.assert_called_once_with((PUBLIC_IP, 443), 5, source_address=None)

    def test_session_never_uses_a_proxy(self, _dns):
        session = importer.make_session()
        self.assertFalse(session.trust_env)
        adapter = session.get_adapter("https://recipes.example/")
        self.assertIsInstance(adapter, importer._PublicOnlyAdapter)
        self.assertIs(adapter.poolmanager.pool_classes_by_scheme["https"], importer._HTTPSPool)


@mock.patch("albright_meals.importer.socket.getaddrinfo", side_effect=fake_getaddrinfo)
class AiFallbackTests(Base):
    URL = "https://blog.example/soup"

    def read(self, client, html=None):
        session = FakeSession({self.URL: page(html or fixture("no_structured_data.html"))})
        return importer.read_page(self.URL, session=session, ai_client=client)

    def test_marked_needs_review_and_only_whats_on_the_page(self, _dns):
        client = fake_ai(SOUP_AI_REPLY)
        recipe = self.read(client)
        self.assertTrue(recipe["needs_review"])
        self.assertEqual(recipe["method"], "ai")
        self.assertEqual([i["original"] for i in recipe["ingredients"]],
                         ["2 tbsp butter", "1 onion, chopped", "28 oz can whole tomatoes", "2 cups vegetable stock"])
        self.assertEqual(recipe["steps"], ["Melt the butter and soften the onion.",
                                           "Add the tomatoes and stock and simmer for 20 minutes.",
                                           "Blend until smooth."])
        self.assertEqual((recipe["title"], recipe["servings"], recipe["total_time_minutes"], recipe["author"]),
                         ("My Tomato Soup", 4, 30, "Sam Cook"))
        self.assertEqual(recipe["source_name"], "Simple Blog")
        sent = client.responses.create.call_args.kwargs
        self.assertIn("never invent", sent["instructions"].lower())
        self.assertIn("Melt the butter", sent["input"])
        for hidden in ("secretInstructions", "5 cups of sugar", "heavy cream", "parmesan", "display: none"):
            self.assertNotIn(hidden, sent["input"])                   # visible text only
        row = AIExtraction.objects.get()
        self.assertEqual((row.purpose, row.status), ("recipe", "done"))   # counts toward the shared limits

    def test_nothing_on_the_page(self, _dns):
        for reply in ({"found": False}, {"found": True, "title": "Made up", "ingredients": ["3 carrots"],
                                         "steps": ["Garnish with basil."]}):
            with self.subTest(reply=reply):
                with self.assertRaises(RecipeImportError) as caught:
                    self.read(fake_ai(reply))
                self.assertEqual((caught.exception.code, caught.exception.message), ("not_found", importer.NOT_FOUND))
        client = fake_ai(SOUP_AI_REPLY)
        with self.assertRaises(RecipeImportError) as caught:                   # a page with almost no text
            self.read(client, html="<html><body><p>Hi</p></body></html>")
        self.assertEqual(caught.exception.message, "No recipe found on that page.")
        client.responses.create.assert_not_called()

    def test_structured_data_never_uses_the_ai(self, _dns):
        client = fake_ai(SOUP_AI_REPLY)
        session = FakeSession({CHILI_URL: page(fixture("jsonld_graph.html"))})
        self.assertFalse(importer.read_page(CHILI_URL, session=session, ai_client=client)["needs_review"])
        client.responses.create.assert_not_called()

    def test_daily_limit(self, _dns):
        from albright_crypto_app.extraction import ExtractionLimitReached
        client = fake_ai(SOUP_AI_REPLY)
        with mock.patch("albright_crypto_app.ai_tasks.check_limits", side_effect=ExtractionLimitReached("limit")):
            with self.assertRaises(RecipeImportError) as caught:
                self.read(client)
        self.assertEqual(caught.exception.code, "ai_limit")
        self.assertIn("AI limit", caught.exception.message)
        client.responses.create.assert_not_called()

    def test_ai_failure(self, _dns):
        client = mock.Mock()
        client.responses.create.side_effect = RuntimeError("provider down")
        with self.assertRaises(RecipeImportError) as caught:
            self.read(client)
        self.assertEqual(caught.exception.code, "ai_unavailable")
        self.assertEqual(AIExtraction.objects.get().status, "error")


# ---------------------------------------------------------------------------
# The website: import box, preview, save, duplicates, re-import
# ---------------------------------------------------------------------------

def fetching(html, final_url=CHILI_URL):
    return mock.patch("albright_meals.importer.fetch", return_value=(final_url, html))


class WebsiteImportTests(Base):
    XHR = {"HTTP_X_REQUESTED_WITH": "XMLHttpRequest"}

    def preview(self, url=CHILI_URL, **headers):
        return self.client.post(reverse("albright_meals:recipe_import"), {"url": url}, **(headers or self.XHR))

    def test_recipes_page_has_the_import_box(self):
        response = self.client.get(reverse("albright_meals:recipes"))
        self.assertContains(response, 'id="recipe-import-form"')
        self.assertContains(response, f'action="{reverse("albright_meals:recipe_import")}"')
        self.assertRegex(response.content.decode(), r'<script src="/static/meals/recipe_import\.js\?v=\d+" defer>')

    def test_preview_then_save(self):
        with fetching(fixture("jsonld_graph.html")) as fetch:
            data = self.preview(CHILI_URL + "?utm_source=newsletter").json()
        fetch.assert_called_once()
        self.assertEqual(data["status"], "preview")
        html = data["html"]
        self.assertIn('value="Weeknight Chili &amp; Cornbread"', html)
        self.assertIn("1 1/2 cups diced onion\n", html)
        self.assertIn("## For the chili\nBrown the beef", html)
        self.assertIn('name="servings" value="6"', html)
        self.assertIn('name="cook_minutes" value="90"', html)
        self.assertIn("Example Kitchen", html)
        self.assertNotIn("Needs review", html)
        self.assertEqual(Recipe.objects.count(), 0)                  # nothing saved until Save
        import_id = data["html"].split('name="import_id" value="', 1)[1].split('"', 1)[0]
        response = self.client.post(reverse("albright_meals:recipe_import_save"), {
            "import_id": import_id, "title": "Chili", "servings": "8", "prep_minutes": "15", "cook_minutes": "90",
            "total_time_minutes": "105", "description": "Quick chili.",
            "ingredients_text": "2 lb ground beef\n1 1/2 cups diced onion\n3 cloves garlic",
            "steps_text": "## For the chili\nBrown the beef in a large pot.\nSimmer."})
        recipe = Recipe.objects.get()
        self.assertRedirects(response, reverse("albright_meals:recipe", args=[recipe.pk]), fetch_redirect_response=False)
        self.assertEqual((recipe.title, recipe.servings, recipe.description), ("Chili", 8, "Quick chili."))
        self.assertEqual(recipe.ingredients[0], {"name": "ground beef", "quantity": "2", "unit": "lb", "note": "",
                                                 "original": "2 lb ground beef"})
        self.assertEqual(recipe.ingredients[2]["original"], "3 cloves garlic")     # an edited line, parsed again
        self.assertEqual(recipe.steps, ["## For the chili", "Brown the beef in a large pot.", "Simmer."])
        self.assertEqual((recipe.source_url, recipe.source_key, recipe.source_name, recipe.author),
                         (CHILI_URL, "examplekitchen.com/recipes/weeknight-chili", "Example Kitchen", "Ann Baker"))
        self.assertEqual(recipe.image_url, "https://cdn.examplekitchen.com/chili-16x9.jpg")
        self.assertEqual((recipe.created_by, recipe.from_bot, recipe.needs_review), (self.owner, False, False))
        self.assertIsNotNone(recipe.imported_at)
        self.assertEqual(self.client.session["meals_imports"], {})     # the preview is used up

        detail = self.client.get(reverse("albright_meals:recipe", args=[recipe.pk]))
        self.assertContains(detail, f'Source: <a href="{CHILI_URL}" target="_blank" rel="noopener noreferrer nofollow">'
                                    'Example Kitchen</a> &middot; by Ann Baker', html=False)
        self.assertContains(detail, '<h3 class="meals-steps__heading">For the chili</h3>')
        self.assertContains(detail, "<li>2 lb ground beef</li>")
        self.assertContains(detail, 'id="recipe-reimport"')
        self.assertNotContains(detail, "Needs review")

    def test_preview_without_js_is_its_own_page(self):
        with fetching(fixture("microdata.html"), "https://oldfamily.example/pancakes"):
            response = self.preview("https://oldfamily.example/pancakes", HTTP_ACCEPT="text/html")
        self.assertTemplateUsed(response, "albright_meals/recipe_import.html")
        self.assertContains(response, 'id="recipe-import-preview"')
        self.assertContains(response, "Grandma&#x27;s Pancakes")

    def test_save_with_errors_shows_the_preview_again(self):
        with fetching(fixture("jsonld_graph.html")):
            html = self.preview().json()["html"]
        import_id = html.split('name="import_id" value="', 1)[1].split('"', 1)[0]
        response = self.client.post(reverse("albright_meals:recipe_import_save"), {
            "import_id": import_id, "title": "", "servings": "500", "ingredients_text": "x", "steps_text": ""})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "field-error")
        self.assertEqual(Recipe.objects.count(), 0)

    def test_expired_preview(self):
        response = self.client.post(reverse("albright_meals:recipe_import_save"), {"import_id": "gone", "title": "X"},
                                    follow=True)
        self.assertContains(response, "That preview has expired")
        self.assertEqual(Recipe.objects.count(), 0)

    def test_already_saved_link_with_tracking_parameters(self):
        saved = self.recipe("Chili", [], source_url=CHILI_URL)
        for url in ["https://examplekitchen.com/recipes/weeknight-chili?utm_source=newsletter&utm_medium=email",
                    "http://www.examplekitchen.com/recipes/weeknight-chili/?fbclid=abc#comments"]:
            with self.subTest(url=url), fetching(fixture("jsonld_graph.html")) as fetch:
                data = self.preview(url).json()
                self.assertEqual(data["status"], "already_saved")
                self.assertIn(f'href="{reverse("albright_meals:recipe", args=[saved.pk])}"', data["html"])
                fetch.assert_not_called()                              # recognized before fetching
        self.assertEqual(Recipe.objects.count(), 1)

    def test_already_saved_by_its_canonical_link(self):
        """A different link (a short or AMP link) whose page names a saved recipe's canonical URL."""
        saved = self.recipe("Chili", [], source_url=CHILI_URL)
        with fetching(fixture("jsonld_graph.html"), "https://examplekitchen.com/r/123?ref=share"):
            data = self.preview("https://examplekitchen.com/r/123?ref=share").json()
        self.assertEqual(data["status"], "already_saved")
        self.assertIn(saved.title, data["html"])

    def test_save_checks_for_duplicates_again(self):
        with fetching(fixture("jsonld_graph.html")):
            html = self.preview().json()["html"]
        import_id = html.split('name="import_id" value="', 1)[1].split('"', 1)[0]
        other = self.recipe("Saved meanwhile", [], source_url=CHILI_URL + "?utm_source=bot")
        response = self.client.post(reverse("albright_meals:recipe_import_save"), {
            "import_id": import_id, "title": "Chili", "ingredients_text": "1 onion", "steps_text": "Cook."})
        self.assertRedirects(response, reverse("albright_meals:recipe", args=[other.pk]), fetch_redirect_response=False)
        self.assertEqual(Recipe.objects.count(), 1)

    def test_errors_are_shown(self):
        for error, text in [(RecipeImportError(importer.NOT_FOUND, "not_found"), "No recipe found on that page."),
                            (RecipeImportError(importer.BLOCKED, "blocked"), "only public http(s) web pages")]:
            with self.subTest(text=text), mock.patch("albright_meals.importer.fetch", side_effect=error):
                data = self.preview("https://recipes.example/x").json()
                self.assertEqual(data["status"], "error")
                self.assertIn(text, data["html"])
        data = self.preview("javascript:alert(1)").json()
        self.assertEqual(data["status"], "error")
        self.assertEqual(Recipe.objects.count(), 0)

    @mock.patch("albright_crypto_app.ai_tasks.make_client")
    def test_ai_recipe_shows_needs_review(self, make_client):
        make_client.return_value = fake_ai(SOUP_AI_REPLY)
        with fetching(fixture("no_structured_data.html"), "https://blog.example/soup"):
            data = self.preview("https://blog.example/soup").json()
        self.assertEqual(data["status"], "needs_review")
        self.assertIn('<span class="badge warn meals-review-badge">Needs review</span>', data["html"])
        import_id = data["html"].split('name="import_id" value="', 1)[1].split('"', 1)[0]
        self.client.post(reverse("albright_meals:recipe_import_save"), {
            "import_id": import_id, "title": "Tomato Soup", "ingredients_text": "2 tbsp butter", "steps_text": "Melt."})
        recipe = Recipe.objects.get()
        self.assertTrue(recipe.needs_review)
        self.assertContains(self.client.get(reverse("albright_meals:recipe", args=[recipe.pk])), "Needs review")
        self.assertContains(self.client.get(reverse("albright_meals:recipes")), "meals-review-badge")
        # Unticking it in Edit clears it.
        self.client.post(reverse("albright_meals:recipe_edit", args=[recipe.pk]), {
            "title": "Tomato Soup", "ingredients_text": "2 tbsp butter", "steps_text": "Melt.", "tags_text": "",
            "description": "", "notes": "", "source_url": recipe.source_url, "image_url": ""})
        recipe.refresh_from_db()
        self.assertFalse(recipe.needs_review)
        self.assertEqual(recipe.ingredients[0]["original"], "2 tbsp butter")    # unchanged line kept as it was

    def test_reimport(self):
        recipe = self.recipe("Old chili", [{"name": "beef", "quantity": "1", "unit": "lb", "note": ""}],
                             steps=["Old step."], tags=["beef"], notes="Kids like it.", source_url=CHILI_URL,
                             needs_review=True)
        with fetching(fixture("jsonld_graph.html")) as fetch:
            response = self.client.post(reverse("albright_meals:recipe_reimport", args=[recipe.pk]), follow=True)
        fetch.assert_called_once_with(CHILI_URL, session=None)
        self.assertContains(response, "Re-imported Weeknight Chili &amp; Cornbread from Example Kitchen.")
        recipe.refresh_from_db()
        self.assertEqual(recipe.title, "Weeknight Chili & Cornbread")
        self.assertEqual(len(recipe.ingredients), 8)
        self.assertEqual(recipe.steps[0], "## For the chili")
        self.assertEqual((recipe.cook_minutes, recipe.servings, recipe.author), (90, 6, "Ann Baker"))
        self.assertEqual((recipe.tags, recipe.notes, recipe.created_by), (["beef"], "Kids like it.", self.owner))
        self.assertFalse(recipe.needs_review)
        self.assertEqual(Recipe.objects.count(), 1)

    def test_failed_reimport_changes_nothing(self):
        recipe = self.recipe("Old chili", [{"name": "beef"}], source_url=CHILI_URL)
        with mock.patch("albright_meals.importer.fetch", side_effect=RecipeImportError(importer.TIMED_OUT, "timeout")):
            response = self.client.post(reverse("albright_meals:recipe_reimport", args=[recipe.pk]), follow=True)
        self.assertContains(response, "Couldn&#x27;t re-import: That page took too long")
        recipe.refresh_from_db()
        self.assertEqual((recipe.title, recipe.ingredients), ("Old chili", [{"name": "beef"}]))

    def test_no_reimport_without_a_source(self):
        recipe = self.recipe("Hand-written", [{"name": "beef"}])
        self.assertNotContains(self.client.get(reverse("albright_meals:recipe", args=[recipe.pk])), "recipe-reimport")
        with mock.patch("albright_meals.importer.fetch") as fetch:
            self.client.post(reverse("albright_meals:recipe_reimport", args=[recipe.pk]))
        fetch.assert_not_called()

    def test_source_link_only_for_web_links(self):
        recipe = self.recipe("Odd", [], author="Pat")
        Recipe.objects.filter(pk=recipe.pk).update(source_url="javascript:alert(1)")
        detail = self.client.get(reverse("albright_meals:recipe", args=[recipe.pk]))
        self.assertNotContains(detail, "javascript:")
        self.assertContains(detail, "By Pat")

    def test_source_key_set_on_every_save(self):
        recipe = self.recipe("Chili", [], source_url="https://WWW.examplekitchen.com/recipes/weeknight-chili/?utm_source=x")
        self.assertEqual(recipe.source_key, "examplekitchen.com/recipes/weeknight-chili")
        recipe.source_url = ""
        recipe.save(update_fields=["source_url"])
        recipe.refresh_from_db()
        self.assertEqual(recipe.source_key, "")


# ---------------------------------------------------------------------------
# Groceries: "Add ingredients to grocery list"
# ---------------------------------------------------------------------------

class AddToGroceriesTests(Base):
    def test_merges_and_records_the_recipes(self):
        chili = self.recipe("Chili", [{"name": "onions", "quantity": "2"}, {"name": "garlic", "quantity": "3",
                                                                           "unit": "clove"}])
        tacos = self.recipe("Tacos", [{"name": "onion", "quantity": "1"}])
        onion = GroceryItem.objects.create(name="onion", quantity=1, aisle="produce")
        GroceryItem.objects.create(name="garlic", quantity=1, unit="clove", checked=True)    # checked: not merged
        self.client.post(reverse("albright_meals:recipe_groceries", args=[chili.pk]))
        onion.refresh_from_db()
        self.assertEqual(onion.quantity, 3)
        self.assertEqual(list(onion.recipes.all()), [chili])
        garlic = GroceryItem.objects.get(name="garlic", checked=False)
        self.assertEqual((garlic.quantity, garlic.from_recipe, list(garlic.recipes.all())), (3, chili, [chili]))

        response = self.client.post(reverse("albright_meals:recipe_groceries", args=[chili.pk]), follow=True)
        self.assertContains(response, "Everything from this recipe is already on the grocery list.")
        onion.refresh_from_db()
        self.assertEqual(onion.quantity, 3)                           # not doubled
        self.assertEqual(GroceryItem.objects.count(), 3)

        self.client.post(reverse("albright_meals:recipe_groceries", args=[tacos.pk]))
        onion.refresh_from_db()
        self.assertEqual(onion.quantity, 4)
        self.assertEqual(set(onion.recipes.all()), {chili, tacos})
        self.assertContains(self.client.get(reverse("albright_meals:groceries")), "from Chili, Tacos")


# ---------------------------------------------------------------------------
# Bot API: POST meals/recipes/import
# ---------------------------------------------------------------------------

@override_settings(BOT_API_REQUIRE_HTTPS=False)
class BotImportTests(Base):
    PATH = API + "meals/recipes/import"

    def setUp(self):
        super().setUp()
        _token, self.raw = BotToken.issue(self.owner, "Recipe Bot", ["meals:read", "meals:write"])
        _token, self.read_raw = BotToken.issue(self.owner, "Reader", ["meals:read"])

    def post(self, body, raw=None):
        return self.client.post(self.PATH, data=json.dumps(body), content_type="application/json",
                                HTTP_AUTHORIZATION=f"Bearer {raw or self.raw}")

    def test_needs_meals_write(self):
        with fetching(fixture("jsonld_graph.html")) as fetch:
            self.assertEqual(self.post({"url": CHILI_URL}, raw=self.read_raw).status_code, 403)
            self.assertEqual(self.client.post(self.PATH, data="{}", content_type="application/json").status_code, 401)
            self.assertEqual(self.client.get(self.PATH, HTTP_AUTHORIZATION=f"Bearer {self.raw}").status_code, 405)
        fetch.assert_not_called()
        self.assertEqual(Recipe.objects.count(), 0)

    def test_created_then_already_saved(self):
        with fetching(fixture("jsonld_graph.html")):
            response = self.post({"url": CHILI_URL + "?utm_source=grok"})
        self.assertEqual(response.status_code, 201)
        recipe = Recipe.objects.get()
        self.assertEqual(response.json(), {"id": recipe.pk, "title": "Weeknight Chili & Cornbread", "status": "created",
                                           "url": "http://testserver" + reverse("albright_meals:recipe", args=[recipe.pk])})
        self.assertTrue(recipe.from_bot)
        self.assertIsNone(recipe.created_by)
        log = BotRequestLog.objects.filter(endpoint=self.PATH).get()
        self.assertEqual(log.status, 201)
        self.assertIn(f"import from www.examplekitchen.com: created recipe #{recipe.pk}", log.summary)

        with fetching(fixture("jsonld_graph.html")) as fetch:
            again = self.post({"url": "https://examplekitchen.com/recipes/weeknight-chili#comments"})
        fetch.assert_not_called()
        self.assertEqual(again.status_code, 200)
        self.assertEqual((again.json()["id"], again.json()["status"]), (recipe.pk, "already_saved"))
        self.assertEqual(Recipe.objects.count(), 1)

    @mock.patch("albright_crypto_app.ai_tasks.make_client")
    def test_needs_review(self, make_client):
        make_client.return_value = fake_ai(SOUP_AI_REPLY)
        with fetching(fixture("no_structured_data.html"), "https://blog.example/soup"):
            response = self.post({"url": "https://blog.example/soup"})
        self.assertEqual((response.status_code, response.json()["status"]), (201, "needs_review"))
        self.assertTrue(Recipe.objects.get().needs_review)

    def test_add_to_plan_and_groceries(self):
        GroceryItem.objects.create(name="ground beef", quantity=1, unit="lb")
        body = {"url": CHILI_URL, "add_to_plan": {"date": self.today.isoformat(), "slot": "dinner"},
                "add_to_groceries": True}
        with fetching(fixture("jsonld_graph.html")):
            data = self.post(body).json()
        recipe = Recipe.objects.get()
        entry = MealPlanEntry.objects.get(date=self.today, slot="dinner")
        self.assertEqual((entry.recipe, entry.from_bot), (recipe, True))
        self.assertEqual(data["plan"]["id"], entry.pk)
        self.assertEqual(data["groceries"], {"added": 7, "merged": 1, "already_listed": 0})
        beef = GroceryItem.objects.get(name="ground beef")
        self.assertEqual((beef.quantity, list(beef.recipes.all())), (3, [recipe]))
        self.assertTrue(GroceryItem.objects.get(name="garlic").from_bot)
        # Again: already saved, and its ingredients are already on the list.
        with fetching(fixture("jsonld_graph.html")):
            data = self.post(body).json()
        self.assertEqual((data["status"], data["groceries"]), ("already_saved", {"added": 0, "merged": 0,
                                                                                 "already_listed": 8}))
        self.assertIn("groceries", BotRequestLog.objects.filter(endpoint=self.PATH).order_by("-pk").first().summary)

    def test_validation(self):
        bodies = [({}, '"url" is required'), ({"url": "ftp://x.example/r"}, "http(s) URL"), ({"url": 5}, "string"),
                  ({"url": CHILI_URL, "add_to_plan": "tonight"}, '"add_to_plan" must be'),
                  ({"url": CHILI_URL, "add_to_plan": {"date": "soon", "slot": "dinner"}}, "add_to_plan.date"),
                  ({"url": CHILI_URL, "add_to_plan": {"date": self.today.isoformat()}}, "add_to_plan.slot"),
                  ({"url": CHILI_URL, "add_to_plan": {"date": self.today.isoformat(), "slot": "brunch"}}, '"slot"'),
                  ({"url": CHILI_URL, "add_to_groceries": "yes"}, "true or false")]
        with fetching(fixture("jsonld_graph.html")) as fetch:
            for body, text in bodies:
                with self.subTest(body=body):
                    response = self.post(body)
                    self.assertEqual(response.status_code, 400)
                    self.assertIn(text, response.json()["error"])
        fetch.assert_not_called()

    def test_import_errors_are_json(self):
        for error, status in [(RecipeImportError(importer.NOT_FOUND, "not_found"), 422),
                              (RecipeImportError(importer.BLOCKED, "blocked"), 400),
                              (RecipeImportError(importer.TIMED_OUT, "timeout"), 504),
                              (RecipeImportError(importer.TOO_LARGE, "too_large"), 422),
                              (RecipeImportError("Couldn't load that page.", "fetch"), 502)]:
            with self.subTest(code=error.code), mock.patch("albright_meals.importer.fetch", side_effect=error):
                response = self.post({"url": "https://recipes.example/x"})
                self.assertEqual(response.status_code, status)
                self.assertEqual(response.json(), {"error": error.message})
                log = BotRequestLog.objects.filter(endpoint=self.PATH).order_by("-pk").first()
                self.assertEqual((log.status, log.summary), (status, f"import from recipes.example failed: {error.code}"))
        self.assertEqual(Recipe.objects.count(), 0)

    @mock.patch("albright_meals.importer.socket.getaddrinfo", side_effect=fake_getaddrinfo)
    def test_private_addresses_are_refused(self, _dns):
        for url in ["http://localhost/recipe", "http://10.0.0.5/", "http://169.254.169.254/latest/meta-data/"]:
            with self.subTest(url=url):
                response = self.post({"url": url})
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json()["error"], importer.BLOCKED)

    def test_documented(self):
        docs = self.client.get(API + "docs", HTTP_AUTHORIZATION=f"Bearer {self.raw}").content.decode()
        self.assertIn("POST meals/recipes/import", docs)
        self.assertIn('Example: {"url": "https://www.example.com/recipes/chili"', docs)
        self.assertIn('"status": "created"|"already_saved"|', docs)
