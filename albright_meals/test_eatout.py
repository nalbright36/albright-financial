"""Eat out (eatout.py, eatout_views.py): the filters, strict matching, Type of place, ranking, the empty state, the
household's ratings, presets, the last-used filters, location fallbacks and the Overpass fetch. Every HTTP call is
mocked (the provider is OpenStreetMap Overpass); a down API never breaks the page."""
from datetime import date, datetime, timedelta
from decimal import Decimal
from io import StringIO
from unittest import mock
from zoneinfo import ZoneInfo

import requests
from django.contrib.auth.models import User
from django.core.cache import cache
from django.core.management import call_command
from django.http import QueryDict
from django.test import TestCase, override_settings
from django.urls import reverse

from albright_labs import circles
from albright_labs.templatetags.nav_icons import nav_icon
from albright_labs.testing import kitchen, with_apps
from albright_planner.models import PlannerSettings
from albright_planner.weather import LookupDown

from . import eatout
from .models import EatOutPreset, EatOutSettings, RestaurantVisit

PASSWORD = "pw-not-real-12345"
MAIN = "https://overpass-api.de/api/interpreter"
FR = "https://overpass.openstreetmap.fr/api/interpreter"
TZ = ZoneInfo("America/Denver")
# Thursday, November 12, 2026, 5:40 PM MST.
NOW = datetime(2026, 11, 12, 17, 40, tzinfo=TZ)
LONE_TREE = (Decimal("39.52440"), Decimal("-104.88220"))
MILES_PER_LON = 53.25                  # 1 degree of longitude at this latitude


def at_miles(miles, origin=LONE_TREE):
    """A point `miles` east of origin."""
    return float(origin[0]), float(origin[1]) + miles / MILES_PER_LON


def node(osm_id, name, miles, amenity="restaurant", cuisine="mexican", hours="Mo-Su 11:00-22:00", street="",
         takeaway="", element=None, **extra):
    lat, lon = at_miles(miles)
    tags = {"name": name, "amenity": amenity}
    if cuisine:
        tags["cuisine"] = cuisine
    if hours:
        tags["opening_hours"] = hours
    if street:
        tags["addr:street"] = street
    if takeaway:
        tags["takeaway"] = takeaway
    tags.update({k.replace("__", ":"): v for k, v in extra.items()})
    return {"type": "node", "id": osm_id, "lat": lat, "lon": lon, "tags": tags, **(element or {})}


def lone_tree_elements():
    return [
        node(1, "Hacienda Colorado", 1.1, cuisine="mexican", street="Park Meadows Drive", outdoor_seating="yes",
             website="https://haciendacolorado.com", phone="+1 303-555-0100"),
        node(2, "Fuzzy's Taco Shop", 1.4, amenity="fast_food", cuisine="taco", street="Lincoln Avenue"),
        node(3, "Cafe Rio", 0.9, amenity="fast_food", cuisine="mexican", street="Park Meadows Mall"),
        node(4, "Costa Vida", 1.3, amenity="fast_food", cuisine="mexican", street="Yosemite Street"),
        node(5, "Chipotle", 0.6, amenity="fast_food", cuisine="mexican", street="Park Meadows Center Circle"),
        node(6, "Shake Shack", 1.5, amenity="fast_food", cuisine="burger", street="Park Meadows Dr"),
        node(7, "Closed Cantina", 0.8, cuisine="mexican", hours="Mo-Su 11:00-15:00", street="Acres Green Dr"),
        node(8, "Quiet Taqueria", 1.2, cuisine="mexican", hours="", street="Ridgegate Pkwy"),
        {"type": "node", "id": 9, "lat": at_miles(0.5)[0], "lon": at_miles(0.5)[1],
         "tags": {"amenity": "restaurant", "cuisine": "mexican"}},                       # no name
        node(10, "Far Pizza", 8.0, cuisine="pizza", street="County Line"),
    ]


class FakeResponse:
    def __init__(self, payload, status_code=200, text=""):
        self._payload = payload
        self.status_code = status_code
        self.text = text

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


def overpass_post(payload=None, error=None, status_code=200, text=""):
    payload = {"elements": lone_tree_elements()} if payload is None else payload

    def post(url, data=None, headers=None, timeout=None, **kwargs):
        if error is not None:
            raise error
        if url not in eatout.OVERPASS_URLS:
            raise AssertionError(f"unexpected HTTP: {url}")
        return FakeResponse(payload, status_code, text)
    return post


def filters(**changes):
    return eatout.DEFAULTS._replace(**changes)


HERE = eatout.Location("Lone Tree, Colorado", LONE_TREE[0], LONE_TREE[1], "current")


def names(elements, now=NOW, visits=None, **changes):
    return [p.name for p in eatout.rank(elements, HERE, filters(**changes), now, visits)[0]]


# ---------------------------------------------------------------------------
# Units: hours, distance, type of place, features, ranking
# ---------------------------------------------------------------------------

class OpeningHoursTests(TestCase):
    def test_open_until_evening(self):
        self.assertEqual(eatout.opening_status("Mo-Su 11:00-22:00", NOW), ("open", "Open until 10 PM"))

    def test_twenty_four_seven(self):
        self.assertEqual(eatout.opening_status("24/7", NOW), ("open", "Open 24 hours"))

    def test_closed_opens_later_today(self):
        self.assertEqual(eatout.opening_status("Mo-Fr 11:00-14:00,17:00-21:00", NOW.replace(hour=15, minute=0)),
                         ("closed", "Opens at 5 PM"))

    def test_split_hours_currently_open(self):
        self.assertEqual(eatout.opening_status("Mo-Fr 11:00-14:00,17:00-21:00", NOW), ("open", "Open until 9 PM"))

    def test_sunday_off(self):
        sunday = datetime(2026, 11, 15, 12, 0, tzinfo=TZ)
        self.assertEqual(eatout.opening_status("Mo-Sa 11:00-22:00; Su off", sunday), ("closed", "Closed"))

    def test_overnight(self):
        late = datetime(2026, 11, 13, 1, 0, tzinfo=TZ)  # Friday 1 AM, from Thu 18:00-02:00
        self.assertEqual(eatout.opening_status("Mo-Th 18:00-02:00", late), ("open", "Open until 2 AM"))

    def test_wrapping_day_range(self):
        sunday = datetime(2026, 11, 15, 18, 0, tzinfo=TZ)
        self.assertEqual(eatout.opening_status("Su-Th 11:00-21:00; Fr,Sa 11:00-22:00", sunday),
                         ("open", "Open until 9 PM"))

    def test_empty_and_seasonal_are_unknown(self):
        for spec in ("", None, "Apr-Oct Mo-Su 10:00-18:00", "Mo-Su 11:00+"):
            self.assertEqual(eatout.opening_status(spec, NOW), ("unknown", "Hours unknown"))

    def test_minutes_past_the_hour(self):
        self.assertEqual(eatout.opening_status("Mo-Su 11:00-22:30", NOW), ("open", "Open until 10:30 PM"))


class DistanceTests(TestCase):
    def test_haversine_east_of_lone_tree(self):
        lat, lon = at_miles(1.1)
        self.assertAlmostEqual(eatout.haversine_miles(LONE_TREE[0], LONE_TREE[1], lat, lon), 1.1, delta=0.03)

    def test_drive_and_walk_labels(self):
        self.assertEqual(eatout.drive_minutes(1.1), 3)
        self.assertEqual(eatout.walk_minutes(0.6), 12)
        self.assertEqual(eatout.distance_label(1.1), "1.1 mi · 3 min drive")
        self.assertEqual(eatout.distance_label(0.6), "0.6 mi · 12 min walk")


class PlaceTypeTests(TestCase):
    """place_type(): one mapping, one test per Type of place."""

    def kind(self, name="Somewhere", **tags):
        return eatout.place_type({"name": name, **{k.replace("__", ":"): v for k, v in tags.items()}}, name)

    def test_sit_down_is_a_restaurant_that_isnt_fast_food_takeout_or_a_cafe(self):
        self.assertEqual(self.kind(amenity="restaurant", cuisine="italian"), "sitdown")
        self.assertEqual(self.kind(amenity="restaurant", cuisine="mexican", indoor_seating="yes"), "sitdown")

    def test_fast_food_and_drive_thru(self):
        self.assertEqual(self.kind(amenity="fast_food", cuisine="burger"), "fastfood")
        self.assertEqual(self.kind(amenity="fast_food", cuisine="mexican", drive_through="yes"), "fastfood")
        self.assertEqual(self.kind(amenity="restaurant", takeaway="only"), "fastfood")       # takeout only

    def test_fast_casual(self):
        self.assertEqual(self.kind(amenity="fast_food", cuisine="poke"), "fastcasual")
        self.assertEqual(self.kind("Chipotle Mexican Grill", amenity="restaurant", indoor_seating="yes"), "fastcasual")
        self.assertEqual(self.kind("Panera Bread", amenity="cafe"), "fastcasual")

    def test_fine_dining(self):
        self.assertEqual(self.kind(amenity="restaurant", cuisine="fine_dining"), "fine")
        self.assertEqual(self.kind(amenity="restaurant", cuisine="french", reservation="required"), "fine")
        self.assertEqual(self.kind(amenity="restaurant", price_range="$$$$"), "fine")

    def test_bar_pub_brewery(self):
        for tags in ({"amenity": "bar"}, {"amenity": "pub"}, {"amenity": "biergarten"},
                     {"amenity": "restaurant", "microbrewery": "yes"}):
            self.assertEqual(self.kind(**tags), "bar", tags)

    def test_cafe_bakery_dessert(self):
        for tags in ({"amenity": "cafe"}, {"amenity": "ice_cream"}, {"shop": "bakery"},
                     {"amenity": "fast_food", "cuisine": "donut"}, {"amenity": "restaurant", "cuisine": "dessert"}):
            self.assertEqual(self.kind(**tags), "cafe", tags)

    def test_known_fast_food_chains_always_count_as_fast_food(self):
        for name in ("McDonald's", "Wendy's #4412", "Burger King", "Taco Bell", "Chick-fil-A", "Popeyes", "Subway"):
            self.assertEqual(self.kind(name, amenity="restaurant", indoor_seating="yes", reservation="yes"),
                             "fastfood", name)
        self.assertEqual(self.kind("Store 12", amenity="restaurant", brand="Taco Bell"), "fastfood")

    def test_sit_down_filter_excludes_fast_food_takeout_and_chains_even_with_dine_in(self):
        elements = [
            node(1, "Trattoria", 1.0, cuisine="italian"),
            node(2, "McDonald's", 1.0, amenity="restaurant", cuisine="burger", indoor_seating="yes"),
            node(3, "Wok To Go", 1.0, cuisine="chinese", takeaway="only", indoor_seating="yes"),
            node(4, "Burger Barn", 1.0, amenity="fast_food", cuisine="burger", indoor_seating="yes"),
            node(5, "Chipotle", 1.0, amenity="restaurant", indoor_seating="yes"),
        ]
        self.assertEqual(names(elements, types=("sitdown",)), ["Trattoria"])


class FeatureAndFilterTests(TestCase):
    def test_unknown_feature_data_is_excluded_unless_include_unknown(self):
        elements = [node(1, "Patio Place", 1.0, outdoor_seating="yes"), node(2, "No Patio", 1.0, outdoor_seating="no"),
                    node(3, "Who Knows", 1.0)]
        self.assertEqual(names(elements, features=("outdoor",)), ["Patio Place"])
        self.assertEqual(sorted(names(elements, features=("outdoor",), include_unknown=True)),
                         ["Patio Place", "Who Knows"])

    def test_exclude_chains(self):
        elements = [node(1, "Local Cantina", 1.0), node(2, "Chili's", 1.0, brand="Chili's", brand__wikidata="Q1072948"),
                    node(3, "Taco Bell", 1.0, amenity="fast_food")]
        self.assertEqual(names(elements, no_chains=True), ["Local Cantina"])
        self.assertEqual(len(names(elements)), 3)

    def test_cuisine_and_price_are_strict(self):
        elements = [node(1, "Pizza Uno", 1.0, cuisine="pizza", price_range="$$"), node(2, "Taqueria", 1.0),
                    node(3, "Mystery", 1.0, cuisine="")]
        self.assertEqual(names(elements, cuisines=("pizza",)), ["Pizza Uno"])
        self.assertEqual(names(elements, prices=("2",)), ["Pizza Uno"])                # no price tag = unknown
        self.assertEqual(sorted(names(elements, prices=("2",), include_unknown=True)), ["Mystery", "Pizza Uno",
                                                                                         "Taqueria"])

    def test_open_at_a_chosen_day_and_time(self):
        elements = [node(1, "Late Friday", 1.0, hours="Fr 17:00-01:00"), node(2, "Early Close", 1.0,
                                                                              hours="Mo-Su 11:00-22:00")]
        self.assertEqual(names(elements, when="at", at_day=4, at_time="23:30"), ["Late Friday"])
        self.assertEqual(names(elements, when="at", at_day=5, at_time="00:30"), ["Late Friday"])   # past midnight
        self.assertEqual(sorted(names(elements, when="at", at_day=4, at_time="19:00")), ["Early Close", "Late Friday"])

    def test_late_night_means_open_after_10_pm(self):
        elements = [node(1, "Night Owl", 1.0, hours="Mo-Su 17:00-02:00"), node(2, "Supper Club", 1.0,
                                                                               hours="Mo-Su 17:00-21:00")]
        self.assertEqual(names(elements, when="any", meal="late"), ["Night Owl"])
        self.assertEqual(sorted(names(elements, when="any", meal="dinner")), ["Night Owl", "Supper Club"])

    def test_min_reviews_and_min_rating_when_the_provider_has_ratings(self):
        rated = lambda i, name, r, n: node(i, name, 1.0, element={"rating": r, "review_count": n})  # noqa: E731
        elements = [rated(1, "Lots", 4.6, 800), rated(2, "Few", 5.0, 3), node(3, "Unrated", 1.0)]
        self.assertEqual(sorted(names(elements)), ["Lots", "Unrated"])                  # 3 reviews < 50
        self.assertEqual(sorted(names(elements, min_reviews=0)), ["Few", "Lots", "Unrated"])
        self.assertEqual(names(elements, min_rating=4.5), ["Lots"])                     # unrated = unknown
        self.assertEqual(sorted(names(elements, min_rating=4.5, include_unknown=True)), ["Lots", "Unrated"])

    def test_craving_matches_name_cuisine_and_tags(self):
        elements = [node(1, "Menya Ramen", 1.0, cuisine="japanese"), node(2, "Oyster Bar", 1.0, cuisine="seafood"),
                    node(3, "Taqueria", 1.0)]
        self.assertEqual(names(elements, craving="spicy ramen"), ["Menya Ramen"])
        self.assertEqual(names(elements, craving="oysters"), ["Oyster Bar"])


class RankingTests(TestCase):
    def test_bayesian_rating_beats_a_perfect_score_from_three_reviews(self):
        self.assertGreater(eatout.bayesian(4.6, 800), eatout.bayesian(5.0, 3))
        elements = [node(1, "Five Stars Three Reviews", 1.0, element={"rating": 5.0, "review_count": 3}),
                    node(2, "Solid And Busy", 1.0, element={"rating": 4.6, "review_count": 800})]
        self.assertEqual(names(elements, min_reviews=0), ["Solid And Busy", "Five Stars Three Reviews"])

    def test_closer_ranks_higher_and_sorts_work(self):
        elements = [node(1, "Near", 0.5), node(2, "Far", 4.0, price_range="$"), node(3, "Mid", 2.0, price_range="$$$")]
        self.assertEqual(names(elements), ["Near", "Mid", "Far"])
        self.assertEqual(names(elements, sort="distance"), ["Near", "Mid", "Far"])
        self.assertEqual(names(elements, sort="price"), ["Far", "Mid", "Near"])       # unknown price last

    def test_wont_eat_again_is_hidden_and_favorites_get_the_bonus(self):
        elements = [node(1, "Near", 0.5), node(2, "Loved", 3.5), node(3, "Hated", 0.2)]
        visits = {"osm:node/2": RestaurantVisit(level=RestaurantVisit.FAVORITE),
                  "osm:node/3": RestaurantVisit(level=RestaurantVisit.WONT_EAT)}
        self.assertEqual(names(elements), ["Hated", "Near", "Loved"])
        places, _b, hidden, _t = eatout.rank(elements, HERE, filters(), NOW, visits)
        self.assertEqual([p.name for p in places], ["Loved", "Near"])
        self.assertEqual(hidden, 1)
        not_for_us = {"osm:node/1": RestaurantVisit(level=RestaurantVisit.NOT_FOR_US)}
        self.assertNotIn("Near", names(elements, visits=not_for_us))

    def test_somewhere_new_and_our_favorites(self):
        elements = [node(1, "Been", 1.0), node(2, "New", 1.0), node(3, "Meh", 1.0)]
        visits = {"osm:node/1": RestaurantVisit(level=RestaurantVisit.STAPLE),
                  "osm:node/3": RestaurantVisit(level=RestaurantVisit.NEEDS_TWEAKS)}
        self.assertEqual(names(elements, visits=visits, seen="new"), ["New"])
        self.assertEqual(names(elements, visits=visits, seen="favorites"), ["Been"])

    def test_empty_state_names_the_most_restrictive_filter(self):
        elements = [node(i, f"Place {i}", 1.0, outdoor_seating="yes" if i < 4 else "") for i in range(1, 7)]
        places, blocked, _h, total = eatout.rank(elements, HERE, filters(features=("live_music", "outdoor")), NOW)
        self.assertEqual((places, total), ([], 6))
        self.assertEqual([(b["key"], b["count"]) for b in blocked],
                         [("unknown", 6), ("feat:live_music", 6), ("feat:outdoor", 3), ("distance", None)])
        live = blocked[1]                                          # the most restrictive filter
        self.assertEqual(live["relax_label"], "Drop “Live music”")
        self.assertNotIn("feat=live_music", live["relax_qs"])
        self.assertIn("feat=outdoor", live["relax_qs"])
        self.assertEqual(blocked[0]["relax_label"], "Include places with unknown info")
        self.assertEqual(blocked[-1]["relax_label"], "Widen to 10 mi")

    def test_relaxing_type_offers_the_type_that_would_add_most(self):
        elements = [node(1, "Counter A", 1.0, amenity="fast_food", cuisine="poke"),
                    node(2, "Counter B", 1.0, amenity="fast_food", cuisine="salad")]
        _p, blocked, _h, _t = eatout.rank(elements, HERE, filters(types=("sitdown",)), NOW)
        relax = next(b for b in blocked if b["key"] == "type")
        self.assertEqual(relax["relax_label"], "Include fast casual")
        self.assertIn("type=fastcasual", relax["relax_qs"])


class FiltersHelperTests(TestCase):
    def test_defaults(self):
        f = eatout.filters_from_get(QueryDict(""))
        self.assertEqual((f.miles, f.when, f.types, f.include_unknown, f.min_reviews, f.sort),
                         ("5", "now", eatout.ALL_TYPES, False, 50, "best"))

    def test_round_trip(self):
        f = filters(types=("sitdown", "bar"), cuisines=("thai",), prices=("2",), miles="10", when="at", at_day=4,
                    at_time="19:30", meal="dinner", features=("outdoor",), services=("takeout",), no_chains=True,
                    seen="new", craving="pad see ew", include_unknown=True, sort="distance")
        self.assertEqual(eatout.filters_from_get(QueryDict(eatout.query_string(f))), f)

    def test_bad_values_fall_back_and_say_so(self):
        get = QueryDict("f=1&mi=50&when=at&time=25:99&feat=jacuzzi")
        f = eatout.filters_from_get(get)
        self.assertEqual((f.miles, f.when, f.types, f.features), ("5", "now", eatout.ALL_TYPES, ()))
        errors = eatout.filter_errors(get)
        self.assertEqual(set(errors), {"type", "mi", "when", "feat"})

    def test_pick_for_us_is_one_of_the_top_five(self):
        rng = mock.Mock(randint=mock.Mock(return_value=4))
        self.assertEqual(eatout.random_pick(list(range(20)), rng), 4)
        rng.randint.assert_called_with(1, 5)
        self.assertEqual(eatout.random_pick([], rng), 0)


# ---------------------------------------------------------------------------
# The page
# ---------------------------------------------------------------------------

@override_settings(EAT_OUT_OVERPASS_URLS=[MAIN])
class Base(TestCase):
    def setUp(self):
        cache.clear()
        self.user = with_apps(User.objects.create_user(username="nate", password=PASSWORD, first_name="Nathan"),
                              ["meals", "planner"])
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
        for target, kwargs in (("albright_meals.eatout._now", {"side_effect": lambda zone: NOW.astimezone(zone)}),
                               ("albright_meals.eatout.lookup_zone", {"return_value": None}),
                               ("albright_planner.weather.geocode", {"return_value": {
                                   "label": "Parker, Colorado", "lat": Decimal("39.51860"),
                                   "lon": Decimal("-104.76120")}})):
            patcher = mock.patch(target, **kwargs)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.use()

    def use(self, **kwargs):
        if getattr(self, "_http", None):
            self._http.stop()
        self._http = mock.patch("albright_meals.eatout.requests.post", side_effect=overpass_post(**kwargs))
        self.http = self._http.start()
        self.addCleanup(lambda: self._http.stop() if self._http else None)

    def page(self, query=""):
        return self.client.get(reverse("albright_meals:eat_out") + (f"?{query}" if query else ""))

    def html(self, query=""):
        response = self.page(query)
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    @property
    def space(self):
        return circles.current_space(self.user, "meals", None)


class PageTests(Base):
    def test_page_renders_with_current_location_and_results(self):
        html = self.html()
        self.assertIn('data-app="meals"', html)
        self.assertIn("meals/eatout.css", html)
        self.assertIn("meals/eatout.js", html)
        self.assertIn("Using current location", html)
        self.assertIn("Hacienda Colorado", html)
        self.assertIn("Pick for us", html)
        self.assertIn("Places from OpenStreetMap", html)
        self.assertIn(f'aria-current="page">{nav_icon("store")}Eat out</a>', html)
        self.assertNotIn("Closed Cantina", html)                 # closed at 5:40, Open now on
        self.assertNotIn("Quiet Taqueria", html)                 # hours unknown - strict by default
        self.assertNotIn("Far Pizza", html)                      # 8 mi, past 5
        self.assertIn("Sit-down · Mexican", html)
        self.assertIn("Fast casual · Mexican", html)             # Chipotle
        self.assertIn("Open until 10 PM", html)
        self.assertIn('href="tel:+13035550100"', html)

    def test_the_form_has_every_filter(self):
        html = self.html()
        for name in ('name="type"', 'name="crave"', 'name="mi"', 'name="when"', 'name="day"', 'name="time"',
                     'name="cuisine"', 'name="price"', 'name="meal"', 'name="feat"', 'name="svc"', 'name="nochain"',
                     'name="seen"', 'name="unknown"', 'name="sort"', 'id="eo-more"', "More filters", "Find places"):
            self.assertIn(name, html)
        self.assertNotIn('name="mode"', html)                    # the old Dine-in / takeout question is gone
        self.assertIn("A server comes to your table", html)

    def test_include_unknown_shows_unknown_hours(self):
        html = self.html("f=1&unknown=1")
        self.assertIn("Quiet Taqueria", html)
        self.assertIn("Hours unknown", html)

    def test_type_of_place_filter(self):
        html = self.html("f=1&type=sitdown")
        self.assertIn("Hacienda Colorado", html)
        self.assertNotIn('eo-place__name">Chipotle', html)
        self.assertNotIn('eo-place__name">Shake Shack', html)

    def test_why_chips_show_what_matched(self):
        html = self.html("f=1&type=sitdown&feat=outdoor")
        card = html.split("Hacienda Colorado", 1)[1].split("</article>", 1)[0]
        self.assertIn("<li>Sit-down</li>", card)
        self.assertIn("<li>Outdoor</li>", card)
        self.assertIn("<li>Open now</li>", card)

    def test_external_links_go_through_the_warning_page(self):
        html = self.html()
        leave = reverse("albright_meals:eat_out_leave")
        self.assertIn(f'href="{leave}?to=https%3A%2F%2Fhaciendacolorado.com"', html)
        self.assertIn(f'href="{leave}?to=https%3A%2F%2Fwww.google.com%2Fmaps%2Fdir', html)
        response = self.client.get(leave + "?to=https%3A%2F%2Fhaciendacolorado.com")
        self.assertContains(response, "isn't on your verified list")
        self.assertContains(response, 'id="leave-continue"')

    def test_empty_state_lists_the_most_restrictive_filter_with_relax_buttons(self):
        html = self.html("f=1&feat=live_music")
        self.assertIn('id="eo-empty"', html)
        self.assertIn("No places matched these filters.", html)
        self.assertIn("Live music: ruled out 8", html)       # every place within 5 mi
        self.assertIn("Drop “Live music”", html)
        self.assertIn("Widen to 10 mi", html)
        self.assertNotIn("unavailable", html)

    def test_pick_for_us_highlights_one_of_the_top_five(self):
        with mock.patch("albright_meals.eatout.random.randint", return_value=2) as pick:
            html = self.html("f=1&pick=random")
        pick.assert_called_with(1, 5)
        self.assertIn('class="card eo-place is-pick" id="eo-pick"', html)
        self.assertIn("Pick again", html)

    def test_login_and_meals_access(self):
        self.client.logout()
        self.assertEqual(self.page().status_code, 302)
        pat = with_apps(User.objects.create_user(username="pat", password=PASSWORD), ["trade"])
        self.client.force_login(pat)
        self.assertEqual(self.page().status_code, 404)
        sam = with_apps(User.objects.create_user(username="sam", password=PASSWORD), ["meals"])
        kitchen(sam)
        self.client.force_login(sam)
        self.assertEqual(self.page().status_code, 200)

    def test_nav_and_landing_page(self):
        week = self.client.get(reverse("albright_meals:week"))
        self.assertContains(week, ">Eat out</a>")
        from albright_labs import apps_registry
        self.assertIn("meals:eatout", [k for k, _l, _u in apps_registry.BY_KEY["meals"].pages])

    def test_falls_back_to_home_and_prompts_without_a_place(self):
        prefs = PlannerSettings.for_user(self.user)
        prefs.weather_auto = False
        prefs.save(update_fields=["weather_auto"])
        self.assertIn("Using your Home", self.html())
        prefs.weather_lat = prefs.weather_lon = None
        prefs.save()
        self.http.reset_mock()
        html = self.html()
        self.assertIn("No place set", html)
        self.http.assert_not_called()

    def test_typed_place_redirects_to_coords_keeping_filters(self):
        response = self.page("q=Parker&f=1&type=bar&mi=10")
        self.assertEqual(response.status_code, 302)
        for part in ("lat=39.51860", "lon=-104.76120", "label=Parker", "type=bar", "mi=10"):
            self.assertIn(part, response["Location"])
        self.assertIn("Using this place", self.html("lat=39.51860&lon=-104.76120&label=Parker,+Colorado"))

    def test_geocode_trouble_keeps_the_page_up(self):
        with mock.patch("albright_planner.weather.geocode", side_effect=LookupDown()):
            self.assertIn("look that up right now", self.html("q=Nowhere"))
        with mock.patch("albright_planner.weather.geocode", return_value=None):
            self.assertIn("No match for", self.html("q=xyzzy"))


class TimeZoneTests(Base):
    def test_a_typed_place_is_checked_in_its_own_time_zone(self):
        """9:30 PM in Denver is 11:30 PM in New York: a New York place open until 11 PM is closed there."""
        late = datetime(2026, 11, 12, 21, 30, tzinfo=TZ)
        payload = {"elements": [node(1, "Manhattan Diner", 0, hours="Mo-Su 11:00-23:00",
                                     element={"lat": 40.7505, "lon": -73.9905}),
                                node(2, "Lone Tree Diner", 1.0, hours="Mo-Su 11:00-23:00")]}
        self.use(payload=payload)
        clock = mock.patch("albright_meals.eatout._now", side_effect=lambda zone: late.astimezone(zone))
        clock.start()
        self.addCleanup(clock.stop)
        new_york = "f=1&lat=40.75000&lon=-73.99000&label=New+York"
        with mock.patch("albright_meals.eatout.lookup_zone", return_value="America/New_York"):
            self.assertNotIn("Manhattan Diner", self.html(new_york))   # 11:30 PM there: closed
        cache.clear()
        self.assertIn("Manhattan Diner", self.html(new_york))          # read in Denver time it would look open
        self.assertIn("Lone Tree Diner", self.html("f=1&src=current"))  # around me: my own zone


class LookupZoneTests(TestCase):
    def setUp(self):
        cache.clear()

    def test_asks_once_and_caches(self):
        response = mock.Mock(status_code=200, json=mock.Mock(return_value={"timezone": "America/New_York"}))
        with mock.patch("albright_meals.eatout.requests.get", return_value=response) as get:
            self.assertEqual(eatout.lookup_zone(40.75, -73.99), "America/New_York")
            self.assertEqual(eatout.lookup_zone(40.76, -73.98), "America/New_York")
        self.assertEqual(get.call_count, 1)

    def test_failure_is_none(self):
        with mock.patch("albright_meals.eatout.requests.get", side_effect=requests.Timeout()):
            self.assertIsNone(eatout.lookup_zone(10, 10))


class HouseholdTests(Base):
    def test_we_went_here_saves_a_household_rating_and_it_shows(self):
        response = self.client.post(reverse("albright_meals:eat_out_visit"), {
            "place_id": "osm:node/1", "name": "Hacienda Colorado", "level": RestaurantVisit.STAPLE,
            "note": "Get the queso", "visited_on": "2026-11-10", "next": "f=1&mi=5"})
        self.assertRedirects(response, reverse("albright_meals:eat_out") + "?f=1&mi=5", fetch_redirect_response=False)
        visit = RestaurantVisit.objects.get()
        self.assertEqual((visit.space, visit.level, visit.note, visit.visited_on, visit.created_by),
                         (self.space, RestaurantVisit.STAPLE, "Get the queso", date(2026, 11, 10), self.user))
        html = self.html("f=1")
        self.assertIn("Your household: Staple - Get the queso", html)
        self.assertIn("Rate again", html)
        # Going again updates the same row.
        self.client.post(reverse("albright_meals:eat_out_visit"), {"place_id": "osm:node/1", "name": "Hacienda Colorado",
                                                                   "level": RestaurantVisit.WONT_EAT})
        self.assertEqual(RestaurantVisit.objects.get().level, RestaurantVisit.WONT_EAT)
        html = self.html("f=1")
        self.assertNotIn('eo-place__name">Hacienda Colorado', html)
        self.assertIn('id="eo-hidden"', html)

    def test_rejects_bad_visits(self):
        url = reverse("albright_meals:eat_out_visit")
        future = (date.today() + timedelta(days=3)).isoformat()
        for data in ({"place_id": "javascript:1", "name": "X", "level": 4}, {"place_id": "osm:node/1", "name": "X"},
                     {"place_id": "osm:node/1", "name": "X", "level": 4, "visited_on": future}):
            self.client.post(url, data)
        self.assertFalse(RestaurantVisit.objects.exists())
        response = self.client.post(url, {"place_id": "osm:node/1", "name": "X", "level": 4,
                                          "next": "https://evil.example/"})
        self.assertEqual(response["Location"], reverse("albright_meals:eat_out"))

    def test_the_household_shares_ratings(self):
        RestaurantVisit.objects.create(space=self.space, place_id="osm:node/2", name="Fuzzy's Taco Shop",
                                       level=RestaurantVisit.FAVORITE, visited_on=date(2026, 11, 1))
        other = with_apps(User.objects.create_user(username="kim", password=PASSWORD), ["meals"])
        kitchen(other)
        self.assertIn('title="Your household: Favorite"', self.html("f=1"))
        prefs = PlannerSettings.for_user(other)
        prefs.weather_lat, prefs.weather_lon = LONE_TREE
        prefs.save()
        self.client.force_login(other)
        self.assertNotIn("Your household:", self.html("f=1&src=home"))       # someone else's space


class PresetAndMemoryTests(Base):
    def test_last_used_filters_are_remembered(self):
        self.html("f=1&type=sitdown&mi=3&feat=outdoor")
        self.assertEqual(QueryDict(EatOutSettings.objects.get(user=self.user).last_query).getlist("type"), ["sitdown"])
        html = self.html()                                                  # a bare visit opens with them
        self.assertIn('name="mi" value="3" checked', html)
        self.assertIn('name="feat" value="outdoor" checked', html)

    def test_presets_save_show_and_delete(self):
        response = self.client.post(reverse("albright_meals:eat_out_preset"),
                                    {"name": "Date night", "query": "f=1&type=fine&type=sitdown&feat=reservations",
                                     "next": "f=1"})
        self.assertEqual(response.status_code, 302)
        preset = EatOutPreset.objects.get(user=self.user)
        self.assertIn("type=fine", preset.query)
        html = self.html("f=1")
        self.assertIn('id="eo-presets"', html)
        self.assertIn(">Date night</a>", html)
        self.assertIn("?" + preset.query.replace("&", "&amp;") + "&amp;src=current", html)
        on = self.html(preset.query + "&src=current")
        self.assertIn('class="eo-preset is-on"', on)
        self.client.post(reverse("albright_meals:eat_out_preset_delete", args=[preset.pk]))
        self.assertFalse(EatOutPreset.objects.exists())

    def test_someone_elses_preset_is_404(self):
        other = User.objects.create_user(username="kim", password=PASSWORD)
        preset = EatOutPreset.objects.create(user=other, name="Theirs", query="f=1")
        response = self.client.post(reverse("albright_meals:eat_out_preset_delete", args=[preset.pk]))
        self.assertEqual(response.status_code, 404)


class FetchTests(Base):
    """What reaches Overpass, the 30-minute cache, and failures."""

    def query(self, qs):
        cache.clear()
        self.http.reset_mock()
        self.html(qs)
        return self.http.call_args[1]["data"]["data"]

    def test_radius_and_snapped_centre(self):
        self.assertIn("(around:2414,39.52200,-104.88100)", self.query("f=1&mi=1"))
        self.assertIn("(around:8851,39.52200,-104.88100)", self.query("f=1"))
        self.assertIn("(around:41038,39.52200,-104.88100)", self.query("f=1&mi=25"))
        self.assertIn("(around:8851,39.52200,-104.76200)", self.query("f=1&lat=39.51860&lon=-104.76120&label=Parker"))

    def test_query_asks_for_every_kind_of_place_to_eat(self):
        query = self.query("f=1")
        self.assertTrue(query.startswith(f"[out:json][timeout:{eatout.TIMEOUT}];"))
        self.assertIn('nwr["amenity"~"^(bar|biergarten|cafe|fast_food|food_court|ice_cream|pub|restaurant)$"]["name"]',
                      query)
        self.assertIn('nwr["shop"~"^(bakery|chocolate|confectionery|pastry)$"]["name"]', query)
        self.assertTrue(query.endswith("out center tags;"))
        self.assertEqual(self.http.call_args[1]["timeout"], eatout.TIMEOUT)
        self.assertEqual(self.http.call_args[1]["headers"]["User-Agent"], eatout.USER_AGENT)

    def test_cache_hit_does_not_call_the_provider(self):
        self.html("f=1&mi=10")
        self.assertEqual(self.http.call_count, 1)
        self.html("f=1&mi=3&type=bar&feat=outdoor&when=any")        # any filter change, a narrower radius
        self.html("f=1&lat=39.52500&lon=-104.88150&label=Nearby")   # a few hundred feet away: the same grid cell
        self.assertEqual(self.http.call_count, 1)
        self.html("f=1&mi=25")                                        # wider: fetches
        self.assertEqual(self.http.call_count, 2)
        cache.clear()
        real_set = cache.set
        with mock.patch("albright_meals.eatout.cache.set", side_effect=real_set) as cache_set:
            self.html("f=1")
        fresh = [c for c in cache_set.call_args_list if str(c[0][0]).startswith("meals.eatout2:")
                 and not str(c[0][0]).endswith(":stale")]
        self.assertEqual([c[0][2] for c in fresh], [30 * 60])

    def test_overpass_down_says_unavailable_never_no_matches(self):
        self.use(error=requests.Timeout())
        with self.assertLogs("albright_meals.eatout", level="WARNING"):
            html = self.html("f=1")
        self.assertIn("Restaurant search is unavailable right now", html)
        self.assertNotIn("No places matched", html)
        self.assertNotIn("Admin detail", html)

    def test_admins_see_what_went_wrong(self):
        self.user.is_superuser = True
        self.user.save()
        self.use(status_code=429, text="<html><body><h1>429 Too Many Requests</h1></body></html>")
        with self.assertLogs("albright_meals.eatout", level="WARNING"):
            html = self.html("f=1")
        self.assertIn("Admin detail: overpass-api.de: HTTP 429 (429 Too Many Requests)", html)

    def test_server_side_timeout_remark_is_a_failure(self):
        self.use(payload={"elements": [], "remark": "runtime error: Query timed out in \"query\" at line 2"})
        with self.assertLogs("albright_meals.eatout", level="WARNING"):
            self.assertIn("Restaurant search is unavailable right now", self.html("f=1"))

    def test_no_places_at_all_says_no_matches(self):
        self.use(payload={"elements": []})
        html = self.html("f=1")
        self.assertIn("No places matched these filters.", html)
        self.assertNotIn("unavailable", html)


class LocationPostTests(Base):
    def post(self, body):
        return self.client.post(reverse("albright_meals:eat_out_location"), data=body, content_type="application/json")

    def test_saves_a_new_fix(self):
        prefs = PlannerSettings.for_user(self.user)
        prefs.weather_auto_lat = prefs.weather_auto_lon = None
        prefs.save()
        with mock.patch("albright_planner.weather.reverse_geocode",
                        return_value={"label": "Denver, Colorado", "lat": Decimal("39.74000"), "lon": Decimal("-104.99000")}):
            body = self.post('{"lat": 39.74, "lon": -104.99}').json()
        self.assertTrue(body["ok"] and body["changed"])
        prefs.refresh_from_db()
        self.assertEqual(float(prefs.weather_auto_lat), 39.74)

    def test_leaves_the_fix_alone_when_auto_is_off_and_rejects_bad_coordinates(self):
        PlannerSettings.objects.filter(user=self.user).update(weather_auto=False)
        self.assertFalse(self.post('{"lat": 40.0, "lon": -105.0}').json()["changed"])
        self.assertEqual(self.post('{"lat": "nope", "lon": -104.99}').status_code, 400)


@override_settings(EAT_OUT_OVERPASS_URLS=[MAIN, FR])
class HostTests(TestCase):
    def setUp(self):
        cache.clear()

    def test_falls_back_to_the_next_host(self):
        def post(url, **kwargs):
            if url == MAIN:
                return FakeResponse(None, 502, "# 502 Gateway Timeout")
            return FakeResponse({"elements": lone_tree_elements()})
        with mock.patch("albright_meals.eatout.requests.post", side_effect=post), \
                self.assertLogs("albright_meals.eatout", level="WARNING"):
            result = eatout.fetch(*LONE_TREE, "5")
        self.assertEqual((len(result.elements), result.host), (10, "overpass.openstreetmap.fr"))

    def test_uses_the_last_good_copy_when_every_host_fails(self):
        with mock.patch("albright_meals.eatout.requests.post", side_effect=overpass_post()):
            eatout.fetch(*LONE_TREE, "10")
        cache.delete(eatout._cache_key(*LONE_TREE, "10"))                    # fresh copy expired
        with mock.patch("albright_meals.eatout.requests.post", side_effect=overpass_post(error=requests.ConnectionError())), \
                self.assertLogs("albright_meals.eatout", level="ERROR"):
            result = eatout.fetch(*LONE_TREE, "5")
        self.assertEqual((len(result.elements), result.host), (10, "stale"))

    def test_every_host_failing_returns_none_with_each_error(self):
        with mock.patch("albright_meals.eatout.requests.post", side_effect=overpass_post(error=requests.Timeout())), \
                self.assertLogs("albright_meals.eatout", level="ERROR"):
            result = eatout.fetch(*LONE_TREE, "5")
        self.assertIsNone(result.elements)
        self.assertEqual(result.errors, ["overpass-api.de: Timeout", "overpass.openstreetmap.fr: Timeout"])


@override_settings(EAT_OUT_OVERPASS_URLS=[MAIN])
class CheckCommandTests(TestCase):
    def run_command(self, **kwargs):
        out = StringIO()
        with mock.patch("albright_meals.eatout.requests.post", side_effect=overpass_post(**kwargs)):
            call_command("check_eat_out", "--far", "10", stdout=out)
        return out.getvalue()

    def test_prints_key_status_count_and_first_result(self):
        out = self.run_command()
        self.assertIn("API key present: not needed", out)
        self.assertIn("overpass-api.de: HTTP status 200", out)
        self.assertIn("results: 10, first: Hacienda Colorado", out)
        self.assertIn("radius 10 mi (16898 m)", out)
        self.assertIn("OK:", out)

    def test_prints_the_exact_error(self):
        out = self.run_command(status_code=502, text="# 502 Gateway Timeout\n\nAt capacity.")
        self.assertIn("HTTP status 502", out)
        self.assertIn("error: 502 Gateway Timeout", out)
        self.assertIn("FAILED:", out)
