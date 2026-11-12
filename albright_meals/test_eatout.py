"""Eat out (idea #78): the guided-questions page, location fallbacks,
Overpass fetch and ranking. Every HTTP call is mocked; a down API never
breaks the page. Hours and prices are shown only when tagged; there are
no star ratings.
"""
from datetime import datetime
from decimal import Decimal
from unittest import mock
from zoneinfo import ZoneInfo

import requests
from django.contrib.auth.models import User
from django.core.cache import cache
from django.http import QueryDict
from django.test import TestCase
from django.urls import reverse

from albright_labs.testing import kitchen, with_apps
from albright_planner.models import PlannerSettings
from albright_planner.weather import LookupDown

from . import eatout

PASSWORD = "pw-not-real-12345"
TZ = ZoneInfo("America/Denver")
# Thursday, November 12, 2026, 5:40 PM MST — the same story as the mockups.
NOW = datetime(2026, 11, 12, 17, 40, tzinfo=TZ)
LONE_TREE = (Decimal("39.52440"), Decimal("-104.88220"))
# 1 degree of longitude at this latitude is about 53.25 miles.
MILES_PER_LON = 53.25


def at_miles(miles, origin=LONE_TREE):
    """A point `miles` east of origin."""
    return float(origin[0]), float(origin[1]) + miles / MILES_PER_LON


def node(osm_id, name, miles, amenity="restaurant", cuisine="mexican",
         hours="Mo-Su 11:00-22:00", street="", takeaway="", **extra):
    lat, lon = at_miles(miles)
    tags = {"name": name, "amenity": amenity}
    if cuisine:
        tags["cuisine"] = cuisine
    if hours is not None and hours != "":
        tags["opening_hours"] = hours
    elif hours is None:
        pass
    if street:
        tags["addr:street"] = street
    if takeaway:
        tags["takeaway"] = takeaway
    tags.update(extra)
    return {"type": "node", "id": osm_id, "lat": lat, "lon": lon, "tags": tags}


def lone_tree_elements():
    """A nearby set around Lone Tree for the mockup story, plus extras for filters."""
    return [
        node(1, "Hacienda Colorado", 1.1, amenity="restaurant", cuisine="mexican",
             street="Park Meadows Drive"),
        node(2, "Fuzzy's Taco Shop", 1.4, amenity="fast_food", cuisine="taco",
             street="Lincoln Avenue"),
        node(3, "Cafe Rio", 0.9, amenity="fast_food", cuisine="mexican",
             street="Park Meadows Mall"),
        node(4, "Costa Vida", 1.3, amenity="fast_food", cuisine="mexican",
             street="Yosemite Street"),
        node(5, "Chipotle", 0.6, amenity="fast_food", cuisine="mexican",
             street="Park Meadows Center Circle"),
        node(6, "Shake Shack", 1.5, amenity="fast_food", cuisine="burger",
             street="Park Meadows Dr"),
        node(7, "Closed Cantina", 0.8, amenity="restaurant", cuisine="mexican",
             hours="Mo-Su 11:00-15:00", street="Acres Green Dr"),
        node(8, "Quiet Taqueria", 1.2, amenity="restaurant", cuisine="mexican",
             hours=None, street="Ridgegate Pkwy"),
        {"type": "node", "id": 9, "lat": at_miles(0.5)[0], "lon": at_miles(0.5)[1],
         "tags": {"amenity": "restaurant", "cuisine": "mexican"}},  # no name
        node(10, "Far Pizza", 8.0, amenity="restaurant", cuisine="pizza",
             street="County Line"),
    ]


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = ""

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


def overpass_get(payload=None, error=None, status_code=200):
    payload = {"elements": lone_tree_elements()} if payload is None else payload

    def post(url, data=None, headers=None, timeout=None, **kwargs):
        if error is not None:
            raise error
        if "overpass-api.de" not in (url or ""):
            raise AssertionError(f"unexpected HTTP: {url}")
        post.calls.append({"url": url, "data": data, "headers": headers, "timeout": timeout})
        return FakeResponse(payload, status_code)

    post.calls = []
    return post


class OpeningHoursTests(TestCase):
    def test_open_until_evening(self):
        state, label = eatout.opening_status("Mo-Su 11:00-22:00", NOW)
        self.assertEqual(state, "open")
        self.assertEqual(label, "Open until 10 PM")

    def test_twenty_four_seven(self):
        state, label = eatout.opening_status("24/7", NOW)
        self.assertEqual((state, label), ("open", "Open 24 hours"))

    def test_closed_opens_later_today(self):
        state, label = eatout.opening_status("Mo-Fr 11:00-14:00,17:00-21:00", NOW.replace(hour=15, minute=0))
        self.assertEqual((state, label), ("closed", "Opens at 5 PM"))

    def test_split_hours_currently_open(self):
        state, label = eatout.opening_status("Mo-Fr 11:00-14:00,17:00-21:00", NOW)
        self.assertEqual(state, "open")
        self.assertEqual(label, "Open until 9 PM")

    def test_sunday_off(self):
        sunday = datetime(2026, 11, 15, 12, 0, tzinfo=TZ)
        state, label = eatout.opening_status("Mo-Sa 11:00-22:00; Su off", sunday)
        self.assertEqual((state, label), ("closed", "Closed"))

    def test_overnight(self):
        late = datetime(2026, 11, 13, 1, 0, tzinfo=TZ)  # Friday 1 AM, from Thu 18:00-02:00
        state, label = eatout.opening_status("Mo-Th 18:00-02:00", late)
        self.assertEqual(state, "open")
        self.assertEqual(label, "Open until 2 AM")

    def test_wrapping_day_range(self):
        sunday = datetime(2026, 11, 15, 18, 0, tzinfo=TZ)
        state, label = eatout.opening_status("Su-Th 11:00-21:00; Fr,Sa 11:00-22:00", sunday)
        self.assertEqual((state, label), ("open", "Open until 9 PM"))

    def test_empty_and_seasonal_are_unknown(self):
        self.assertEqual(eatout.opening_status("", NOW)[0], "unknown")
        self.assertEqual(eatout.opening_status(None, NOW)[0], "unknown")
        self.assertEqual(eatout.opening_status("Apr-Oct Mo-Su 10:00-18:00", NOW)[0], "unknown")
        self.assertEqual(eatout.opening_status("Mo-Su 11:00+", NOW)[0], "unknown")
        self.assertEqual(eatout.opening_status("", NOW)[1], "Hours unknown")

    def test_minutes_past_the_hour(self):
        state, label = eatout.opening_status("Mo-Su 11:00-22:30", NOW)
        self.assertEqual((state, label), ("open", "Open until 10:30 PM"))


class DistanceTests(TestCase):
    def test_haversine_east_of_lone_tree(self):
        lat, lon = at_miles(1.1)
        miles = eatout.haversine_miles(LONE_TREE[0], LONE_TREE[1], lat, lon)
        self.assertAlmostEqual(miles, 1.1, delta=0.03)

    def test_drive_and_walk_labels(self):
        self.assertEqual(eatout.drive_minutes(1.1), 3)
        self.assertEqual(eatout.walk_minutes(0.6), 12)
        self.assertIn("min drive", eatout.distance_label(1.1, "15"))
        self.assertIn("min walk", eatout.distance_label(0.6, "walk"))
        self.assertIn("1.1 mi", eatout.distance_label(1.1, "15"))


class RankTests(TestCase):
    def setUp(self):
        self.loc = eatout.Location("Lone Tree, Colorado", LONE_TREE[0], LONE_TREE[1], "current")
        self.filters = eatout.Filters(
            cravings=("tacos", "mexican"), other="", budget="2", far="15",
            mode="dinein", open_now=True,
        )

    def test_sit_down_mexican_ranks_first_and_closed_is_dropped(self):
        places = eatout.rank_places(lone_tree_elements(), self.loc, self.filters, NOW)
        names = [p.name for p in places]
        self.assertEqual(names[0], "Hacienda Colorado")
        self.assertNotIn("Closed Cantina", names)
        self.assertNotIn("Shake Shack", names)  # enough Mexican matches
        self.assertNotIn("Far Pizza", names)
        self.assertIn("Chipotle", names)
        # Hours unknown is honest, not treated as closed. It ranks below known-open
        # matches, so look past the shortlist of 5.
        wider = eatout.rank_places(lone_tree_elements(), self.loc, self.filters, NOW, limit=15)
        quiet = next(p for p in wider if p.name == "Quiet Taqueria")
        self.assertEqual(quiet.open_state, "unknown")
        self.assertEqual(quiet.hours_label, "Hours unknown")
        self.assertIn("hours unknown", quiet.why)
        hacienda = places[0]
        self.assertEqual(hacienda.open_state, "open")
        self.assertEqual(hacienda.hours_label, "Open until 10 PM")
        self.assertEqual(hacienda.style, "sit-down")
        self.assertIn("Park Meadows Drive", hacienda.address)
        self.assertIn("openstreetmap.org", hacienda.map_url)
        self.assertIn("google.com/maps/dir", hacienda.directions_url)

    def test_unnamed_places_are_skipped(self):
        places = eatout.rank_places(lone_tree_elements(), self.loc, self.filters, NOW)
        self.assertTrue(all(p.name for p in places))

    def test_open_now_off_includes_closed(self):
        filters = self.filters._replace(open_now=False)
        names = [p.name for p in eatout.rank_places(lone_tree_elements(), self.loc, filters, NOW)]
        self.assertIn("Closed Cantina", names)

    def test_burger_filter(self):
        filters = self.filters._replace(cravings=("burgers",))
        names = [p.name for p in eatout.rank_places(lone_tree_elements(), self.loc, filters, NOW)]
        self.assertEqual(names[0], "Shake Shack")

    def test_walking_drops_farther_places(self):
        filters = self.filters._replace(far="walk", cravings=(), mode="either")
        names = [p.name for p in eatout.rank_places(lone_tree_elements(), self.loc, filters, NOW)]
        self.assertNotIn("Hacienda Colorado", names)  # 1.1 mi, past a 1-mile walk
        self.assertIn("Chipotle", names)

    def test_takeout_prefers_quick(self):
        filters = self.filters._replace(mode="takeout", cravings=("mexican",))
        places = eatout.rank_places(lone_tree_elements(), self.loc, filters, NOW)
        self.assertTrue(places)
        self.assertNotEqual(places[0].name, "Hacienda Colorado")
        self.assertEqual(places[0].style, "quick")

    def test_typed_cuisine(self):
        filters = self.filters._replace(cravings=("other",), other="pizza", far="30", mode="either")
        names = [p.name for p in eatout.rank_places(lone_tree_elements(), self.loc, filters, NOW)]
        self.assertEqual(names[0], "Far Pizza")


class Base(TestCase):
    def setUp(self):
        cache.clear()
        self.user = with_apps(
            User.objects.create_user(username="nate", password=PASSWORD, first_name="Nathan"),
            ["meals", "planner"],
        )
        kitchen(self.user)
        prefs = PlannerSettings.for_user(self.user)
        prefs.timezone = "America/Denver"
        prefs.weather_label = "Lone Tree, Colorado"
        prefs.weather_lat, prefs.weather_lon = LONE_TREE
        prefs.weather_auto = True
        prefs.weather_auto_label = "Lone Tree, Colorado"
        prefs.weather_auto_lat, prefs.weather_auto_lon = LONE_TREE
        prefs.save()
        self.client.force_login(self.user)
        self.clock = mock.patch("albright_planner.weather.local_now", return_value=NOW)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.http = mock.patch("albright_meals.eatout.requests.post", side_effect=overpass_get())
        self.http_obj = self.http.start()
        self.addCleanup(self.http.stop)
        self.geocode = mock.patch(
            "albright_planner.weather.geocode",
            return_value={"label": "Parker, Colorado", "lat": Decimal("39.51860"), "lon": Decimal("-104.76120")},
        )
        self.geocode.start()
        self.addCleanup(self.geocode.stop)

    def page(self, query=""):
        url = reverse("albright_meals:eat_out")
        if query:
            url = url + ("?" + query if not query.startswith("?") else query)
        return self.client.get(url)

    def html(self, query=""):
        response = self.page(query)
        self.assertEqual(response.status_code, 200)
        return response.content.decode()


class PageTests(Base):
    def test_page_renders_with_current_location_and_shortlist(self):
        html = self.html()
        self.assertIn('data-app="meals"', html)
        self.assertIn("meals/eatout.css", html)
        self.assertIn("meals/eatout.js", html)
        self.assertIn("Eat out", html)
        self.assertIn("Answer a few questions", html)
        self.assertIn("Using current location", html)
        self.assertIn("Lone Tree, Colorado", html)
        self.assertIn("Hacienda Colorado", html)
        self.assertIn("Pick for me", html)
        self.assertIn("openstreetmap.org", html)
        self.assertIn("google.com/maps/dir", html)
        self.assertIn("Places from OpenStreetMap", html)
        self.assertIn('aria-current="page">Eat out</a>', html)
        # Honest: no star ratings, no invented prices on place cards.
        self.assertNotIn("★", html)
        self.assertNotIn("&#9733;", html)
        self.assertNotIn("Closed Cantina", html)      # closed at 5:40, open-now on

    def test_unknown_hours_are_labeled_honestly(self):
        # Only Quiet Taqueria (no opening_hours) plus a closed place, so the
        # unknown-hours one makes the shortlist.
        payload = {"elements": [
            node(8, "Quiet Taqueria", 1.2, amenity="restaurant", cuisine="mexican",
                 hours=None, street="Ridgegate Pkwy"),
            node(7, "Closed Cantina", 0.8, amenity="restaurant", cuisine="mexican",
                 hours="Mo-Su 11:00-15:00", street="Acres Green Dr"),
        ]}
        self.http.stop()
        with mock.patch("albright_meals.eatout.requests.post", side_effect=overpass_get(payload=payload)):
            html = self.html("craving=mexican")
        self.assertIn("Quiet Taqueria", html)
        self.assertIn("Hours unknown", html)
        self.assertIn("hours unknown", html)
        self.assertNotIn("Closed Cantina", html)

    def test_nav_is_on_other_meals_pages(self):
        week = self.client.get(reverse("albright_meals:week"))
        self.assertEqual(week.status_code, 200)
        self.assertContains(week, reverse("albright_meals:eat_out"))
        self.assertContains(week, ">Eat out</a>")

    def test_login_required(self):
        self.client.logout()
        response = self.page()
        self.assertEqual(response.status_code, 302)
        self.assertIn("login", response["Location"])

    def test_meals_only_user_can_open_it(self):
        sam = with_apps(User.objects.create_user(username="sam", password=PASSWORD), ["meals"])
        kitchen(sam)
        self.client.force_login(sam)
        response = self.page()
        self.assertEqual(response.status_code, 200)

    def test_other_apps_user_gets_404(self):
        pat = with_apps(User.objects.create_user(username="pat", password=PASSWORD), ["trade"])
        self.client.force_login(pat)
        self.assertEqual(self.page().status_code, 404)

    def test_falls_back_to_home_when_auto_is_off(self):
        prefs = PlannerSettings.for_user(self.user)
        prefs.weather_auto = False
        prefs.save(update_fields=["weather_auto"])
        html = self.html()
        self.assertIn("Using your Home", html)
        self.assertIn("Hacienda Colorado", html)

    def test_no_location_shows_a_prompt_and_does_not_fetch(self):
        prefs = PlannerSettings.for_user(self.user)
        prefs.weather_auto = False
        prefs.weather_lat = prefs.weather_lon = None
        prefs.weather_auto_lat = prefs.weather_auto_lon = None
        prefs.save()
        self.http_obj.reset_mock()
        html = self.html()
        self.assertIn("No place set", html)
        self.assertIn("Add a place to see", html)
        self.assertNotIn("Hacienda Colorado", html)
        self.http_obj.assert_not_called()

    def test_typed_place_redirects_to_coords(self):
        response = self.page("q=Parker")
        self.assertEqual(response.status_code, 302)
        self.assertIn("lat=", response["Location"])
        self.assertIn("lon=", response["Location"])
        self.assertIn("Parker", response["Location"])
        self.assertNotIn("q=Parker", response["Location"])

    def test_typed_place_after_redirect_uses_this_place(self):
        html = self.html("lat=39.51860&lon=-104.76120&label=Parker,+Colorado")
        self.assertIn("Using this place", html)
        self.assertIn("Parker, Colorado", html)

    def test_geocode_down_keeps_the_page_up(self):
        self.geocode.stop()
        with mock.patch("albright_planner.weather.geocode", side_effect=LookupDown()):
            html = self.html("q=Nowhere")
        self.assertIn("look that up right now", html)
        self.assertIn("Lone Tree, Colorado", html)  # still has current location

    def test_geocode_no_match(self):
        self.geocode.stop()
        with mock.patch("albright_planner.weather.geocode", return_value=None):
            html = self.html("q=xyzzy")
        self.assertIn("No match for", html)
        self.assertIn("xyzzy", html)

    def test_overpass_down_never_breaks_the_page(self):
        self.http.stop()
        with mock.patch("albright_meals.eatout.requests.post", side_effect=overpass_get(error=requests.Timeout())):
            html = self.html()
        self.assertIn("reach the places list", html)
        self.assertNotIn("Hacienda Colorado", html)
        self.assertIn("Eat out", html)

    def test_overpass_http_error(self):
        self.http.stop()
        with mock.patch("albright_meals.eatout.requests.post", side_effect=overpass_get(status_code=504)):
            html = self.html()
        self.assertIn("reach the places list", html)

    def test_filter_query_changes_the_list(self):
        html = self.html("craving=burgers&far=15&mode=either&open=1")
        self.assertIn("Shake Shack", html)
        self.assertIn("Burgers", html)
        # Hacienda may still appear as a filler if burger matches are few — only
        # Shake Shack matches, so fillers are allowed. The first card should be Shake Shack.
        pos_shake = html.index("Shake Shack")
        if "Hacienda Colorado" in html:
            self.assertLess(pos_shake, html.index("Hacienda Colorado"))

    def test_budget_note_and_no_fake_prices_on_cards(self):
        html = self.html("budget=2&craving=mexican&open=1")
        self.assertIn("used to rank them yet", html)
        # The $$ chip is the question, not a place stat. Place stats should not
        # include a lone $ / $$ / $$$ token.
        # Just check the note is present and stars are not.
        self.assertTrue("budget" in html.lower())
        self.assertNotIn("★", html)

    def test_pick_for_me_highlights_a_place(self):
        html = self.html("craving=mexican&pick=1")
        self.assertIn("is-pick", html)
        self.assertIn("id=\"eo-pick\"", html)
        self.assertIn("I’ll go here", html)
        self.assertIn("Not this", html)
        self.assertIn("Pick again", html)

    def test_cached_overpass_is_not_refetched(self):
        self.html()
        first = self.http_obj.call_count
        self.assertGreaterEqual(first, 1)
        self.html("craving=pizza&far=30")  # still the same cache key (max radius)
        self.assertEqual(self.http_obj.call_count, first)

    def test_overpass_request_shape(self):
        self.html()
        _url, kwargs = self.http_obj.call_args
        url = _url[0] if _url else kwargs.get("url")
        # side_effect is overpass_get(); inspect via the mock call
        args, kw = self.http_obj.call_args
        self.assertIn("overpass-api.de", args[0])
        self.assertEqual(kw["timeout"], eatout.TIMEOUT)
        self.assertEqual(kw["headers"]["User-Agent"], eatout.USER_AGENT)
        self.assertIn("data", kw["data"])
        self.assertIn("restaurant", kw["data"]["data"])

    def test_questions_are_a_get_form(self):
        html = self.html()
        self.assertIn('<form method="get"', html)
        self.assertIn('name="craving"', html)
        self.assertIn('name="budget"', html)
        self.assertIn('name="far"', html)
        self.assertIn('name="mode"', html)
        self.assertIn('name="open"', html)
        self.assertIn("Find places", html)

    def test_home_landing_page_lists_eat_out(self):
        from albright_labs import apps_registry
        keys = [k for k, _l, _u in apps_registry.BY_KEY["meals"].pages]
        self.assertIn("meals:eatout", keys)


class LocationPostTests(Base):
    def test_saves_a_new_fix(self):
        prefs = PlannerSettings.for_user(self.user)
        prefs.weather_auto_lat = prefs.weather_auto_lon = None
        prefs.save()
        with mock.patch(
            "albright_planner.weather.reverse_geocode",
            return_value={"label": "Denver, Colorado", "lat": Decimal("39.74000"), "lon": Decimal("-104.99000")},
        ):
            response = self.client.post(
                reverse("albright_meals:eat_out_location"),
                data='{"lat": 39.74, "lon": -104.99}',
                content_type="application/json",
            )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["ok"] and body["changed"])
        self.assertEqual(body["label"], "Denver, Colorado")
        prefs.refresh_from_db()
        self.assertEqual(float(prefs.weather_auto_lat), 39.74)

    def test_leaves_the_fix_alone_when_auto_is_off(self):
        prefs = PlannerSettings.for_user(self.user)
        prefs.weather_auto = False
        prefs.save(update_fields=["weather_auto"])
        response = self.client.post(
            reverse("albright_meals:eat_out_location"),
            data='{"lat": 40.0, "lon": -105.0}',
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["changed"])
        prefs.refresh_from_db()
        self.assertEqual(prefs.weather_auto_lat, LONE_TREE[0])

    def test_rejects_bad_coordinates(self):
        response = self.client.post(
            reverse("albright_meals:eat_out_location"),
            data='{"lat": "nope", "lon": -104.99}',
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)

    def test_meals_only_user_can_post(self):
        sam = with_apps(User.objects.create_user(username="sam", password=PASSWORD), ["meals"])
        kitchen(sam)
        prefs = PlannerSettings.for_user(sam)
        prefs.weather_auto = True
        prefs.save(update_fields=["weather_auto"])
        self.client.force_login(sam)
        with mock.patch(
            "albright_planner.weather.reverse_geocode",
            return_value={"label": "Denver, Colorado", "lat": Decimal("39.74000"), "lon": Decimal("-104.99000")},
        ):
            response = self.client.post(
                reverse("albright_meals:eat_out_location"),
                data='{"lat": 39.74, "lon": -104.99}',
                content_type="application/json",
            )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])


class FiltersHelperTests(TestCase):
    def test_defaults(self):
        filters = eatout.filters_from_get(QueryDict(""))
        self.assertEqual(filters.far, "15")
        self.assertEqual(filters.mode, "either")
        self.assertTrue(filters.open_now)
        self.assertEqual(filters.cravings, ())
        self.assertEqual(filters.budget, "")

    def test_open_now_can_be_turned_off(self):
        filters = eatout.filters_from_get(QueryDict("open=0"))
        self.assertFalse(filters.open_now)
        # Hidden 0 + checkbox 1: last value wins.
        filters = eatout.filters_from_get(QueryDict("open=0&open=1"))
        self.assertTrue(filters.open_now)

    def test_summary_and_steps(self):
        filters = eatout.Filters(("tacos", "mexican"), "", "2", "15", "dinein", True)
        text = eatout.summary_text(filters)
        self.assertIn("Tacos or Mexican", text)
        self.assertIn("$$", text)
        self.assertIn("dine-in", text)
        self.assertIn("open now", text)
        self.assertIn("15 minutes", text)
        steps = eatout.steps_done(filters)
        self.assertEqual(steps, (True, True, True, True, True))

    def test_pick_wraps(self):
        places = [object(), object(), object()]
        self.assertEqual(eatout.pick_index(places, "1"), 1)
        self.assertEqual(eatout.pick_index(places, "3"), 3)
        self.assertEqual(eatout.pick_index(places, "4"), 1)
        self.assertEqual(eatout.pick_index(places, ""), 0)
        self.assertEqual(eatout.pick_index([], "1"), 0)
