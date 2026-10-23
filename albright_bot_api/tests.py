"""Bot API: tokens (shown once, stored hashed), auth (401/403/405/429,
HTTPS), every endpoint's data and writes, suggestions pending until I
accept, the hard limits (no endpoint can bid, change max bids, touch the
watchlist or ledger entries, change prices, submit applications, trade, or
read credentials/profile), escaping, and the activity log."""
import json
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.conf import settings
from django.contrib.auth.models import User
from django.test import Client, TestCase, override_settings
from django.urls import URLPattern, reverse
from django.utils import timezone

from albright_bot_api.auth import RATE_LIMIT
from albright_bot_api.models import (BotRequestLog, BotToken, InventorySuggestion, LotNote, OpportunityNote,
                                     TradingNote, hash_token)

ALL = ["trading:notes", "reseller:read", "reseller:suggest", "opportunities:read", "opportunities:suggest"]
HOME_OUTLOOK = "albright_trading_app.views.get_market_outlook"
XSS = '<script>alert("x")</script><b>bold</b>'


def url(name, *args):
    return reverse(f"bot_api_v1:{name}", args=args)


@override_settings(BOT_API_REQUIRE_HTTPS=False, CRYPTO={**settings.CRYPTO, "allowlist": ["github.com"]})
class BotApiTestCase(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="owner", password="pw-not-real-12345")
        self.token, self.raw = BotToken.issue(self.user, "Grok bot", ALL)
        self.api = Client()

    def call(self, method, path, body=None, raw=None, **extra):
        headers = {"HTTP_AUTHORIZATION": f"Bearer {raw if raw is not None else self.raw}"} if raw != "" else {}
        if method == "GET":
            return self.api.get(path, **headers, **extra)
        return getattr(self.api, method.lower())(path, data=json.dumps(body or {}), content_type="application/json",
                                                 **headers, **extra)

    # sample data -------------------------------------------------------
    def reseller_data(self):
        from albright_reselling_app.tests import test_dashboard as td, test_ledger as tl
        now = timezone.now()
        self.cand = td._make_lot("cand-1", now + timedelta(hours=2), current_price=12.0)
        td._make_evaluation(self.cand, True, max_bid=30.0, headroom=18.0)
        self.check = td._make_lot("check-1", now + timedelta(hours=1), current_price=5.0)
        td._make_evaluation(self.check, False, max_bid=40.0, headroom=35.0, confidence="low")  # passes the review settings (min headroom 30)
        self.closed = td._make_lot("closed-1", now - timedelta(hours=1))
        td._make_evaluation(self.closed, True)
        self.listed = tl._make_entry(self.user, item="Morgan dollar", cost="22.00", status="holding",
                                     listed_at=timezone.localdate() - timedelta(days=20), list_price=Decimal("45.00"))
        self.sold = tl._make_entry(self.user, item="Sold one", status="sold_out")
        self.other_user = User.objects.create_user(username="other", password="pw-not-real-12345")
        self.others = tl._make_entry(self.other_user, item="Not mine")

    def opportunity_data(self):
        from albright_crypto_app.models import Opportunity, Source
        from albright_crypto_app.tests.test_pipeline import make
        self.fresh = make(title="Fresh bounty", reward_usd=Decimal("200"), reward_type="bounty")
        self.blocked = make(title="Scam", text="Enter your seed phrase to claim 500 USDC")
        self.excluded = make(title="Deposit promo", reward_usd=Decimal("40"), reward_type="promo_reward",
                             requires_spending=True)
        self.old = make(title="Old bounty")
        Opportunity.objects.filter(pk=self.old.pk).update(discovered_at=timezone.now() - timedelta(days=8))
        remotive = Source.objects.create(kind="remotive", name="Remotive", url="https://remotive.com/api/remote-jobs",
                                         opportunity_type="income", terms_checked=True)
        self.remotive_job = Opportunity.objects.create(source=remotive, opportunity_type="income", external_id="r1",
                                                       title="Remotive job", url="https://remotive.com/remote-jobs/x/1")


class TokenTests(BotApiTestCase):
    """Token management lives in the Admin area now (superuser + password)."""
    PASSWORD = "pw-not-real-12345"

    def admin_login(self):
        self.user.is_superuser = True
        self.user.save()
        self.client.force_login(self.user)

    def test_token_is_shown_once_and_stored_only_as_a_hash(self):
        self.admin_login()
        response = self.client.post(reverse("albright_control:bot_api"),
                                    {"name": "Second bot", "scopes": ["reseller:read", "trading:notes"],
                                     "password": self.PASSWORD})
        raw = response.context["new_token"]
        self.assertTrue(raw.startswith("abt_") and len(raw) > 40)
        self.assertContains(response, raw)
        self.assertContains(response, 'data-copy-target="new-token-value"')
        token = BotToken.objects.get(name="Second bot")
        self.assertEqual(token.token_hash, hash_token(raw))
        self.assertEqual(token.scopes, ["trading:notes", "reseller:read"])
        for field in token._meta.concrete_fields:   # the raw token is stored nowhere
            self.assertNotEqual(str(getattr(token, field.attname)), raw)
            self.assertNotIn(raw[8:], str(getattr(token, field.attname)))
        again = self.client.get(reverse("albright_control:bot_api"))
        self.assertNotContains(again, raw)
        self.assertContains(again, token.display_prefix)
        self.assertEqual(self.call("GET", url("docs"), raw=raw).status_code, 200)

    def test_needs_a_scope(self):
        self.admin_login()
        response = self.client.post(reverse("albright_control:bot_api"), {"name": "x", "password": self.PASSWORD})
        self.assertContains(response, "Pick at least one scope.")
        self.assertFalse(BotToken.objects.filter(name="x").exists())

    def test_list_and_revoke(self):
        self.call("GET", url("docs"))
        self.admin_login()
        page = self.client.get(reverse("albright_control:bot_api"))
        self.assertContains(page, "Grok bot")
        self.assertContains(page, "opportunities:suggest")
        self.assertIsNotNone(BotToken.objects.get().last_used_at)
        self.client.post(reverse("albright_control:revoke_token", args=[self.token.pk]), {"password": self.PASSWORD})
        self.token.refresh_from_db()
        self.assertFalse(self.token.is_active)
        self.assertEqual(self.call("GET", url("docs")).status_code, 401)

    def test_not_a_superuser_gets_404(self):
        self.client.force_login(self.user)
        self.assertEqual(self.client.post(reverse("albright_control:revoke_token", args=[self.token.pk]),
                                          {"password": self.PASSWORD}).status_code, 404)
        self.assertTrue(BotToken.objects.get(pk=self.token.pk).is_active)

    def test_old_pages_redirect_to_admin(self):
        self.assertRedirects(self.client.get("/bot-api/"), reverse("albright_control:bot_api"),
                             status_code=301, fetch_redirect_response=False)
        self.assertRedirects(self.client.get("/bot-api/activity/"), reverse("albright_control:bot_activity"),
                             status_code=301, fetch_redirect_response=False)


class AuthTests(BotApiTestCase):
    def test_missing_wrong_and_revoked_tokens_are_401(self):
        self.assertEqual(self.call("GET", url("docs"), raw="").status_code, 401)
        self.assertEqual(self.call("GET", url("docs"), raw="abt_wrong").status_code, 401)
        response = self.api.get(url("docs"), HTTP_AUTHORIZATION=f"Token {self.raw}")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response["WWW-Authenticate"], 'Bearer realm="albright-bot-api"')
        self.token.revoke()
        self.assertEqual(self.call("GET", url("docs")).status_code, 401)
        self.assertEqual(BotRequestLog.objects.filter(token=self.token, status=401).count(), 1)

    def test_missing_scope_is_403(self):
        _token, raw = BotToken.issue(self.user, "Read only", ["reseller:read"])
        self.assertEqual(self.call("GET", url("reseller_lots"), raw=raw).status_code, 200)
        for method, path in (("GET", url("opportunities_new")), ("POST", url("trading_notes")),
                             ("POST", url("reseller_lot_note", 1))):
            with self.subTest(path=path):
                response = self.call(method, path, {"title": "t", "text": "x"}, raw=raw)
                self.assertEqual(response.status_code, 403)
                self.assertIn("scope", response.json()["error"])
        self.assertFalse(TradingNote.objects.exists())

    def test_wrong_method_is_405(self):
        self.assertEqual(self.call("POST", url("reseller_lots")).status_code, 405)
        self.assertEqual(self.call("GET", url("trading_notes")).status_code, 405)
        self.assertEqual(self.call("DELETE", url("trading_notes")).status_code, 405)
        self.assertEqual(self.call("PUT", url("reseller_inventory")).status_code, 405)

    @override_settings(BOT_API_REQUIRE_HTTPS=True)
    def test_https_only_in_production(self):
        response = self.call("GET", url("docs"))
        self.assertEqual(response.status_code, 403)
        self.assertIn("HTTPS", response.json()["error"])
        self.assertEqual(self.call("GET", url("docs"), secure=True).status_code, 200)
        self.assertEqual(self.call("GET", url("docs"), HTTP_X_FORWARDED_PROTO="https").status_code, 200)

    def test_production_default_requires_https(self):
        from albright_bot_api import auth
        with self.settings(DEBUG=False):
            del settings.BOT_API_REQUIRE_HTTPS
            try:
                self.assertTrue(auth.require_https())
            finally:
                settings.BOT_API_REQUIRE_HTTPS = False

    def test_rate_limit_60_per_minute_per_token(self):
        for _ in range(RATE_LIMIT):
            self.assertEqual(self.call("GET", url("docs")).status_code, 200)
        self.assertEqual(self.call("GET", url("docs")).status_code, 429)
        _other, other_raw = BotToken.issue(self.user, "Other", ["reseller:read"])
        self.assertEqual(self.call("GET", url("docs"), raw=other_raw).status_code, 200)   # per token
        BotRequestLog.objects.filter(token=self.token).update(created_at=timezone.now() - timedelta(seconds=61))
        self.assertEqual(self.call("GET", url("docs")).status_code, 200)

    def test_csrf_exempt_only_for_token_requests(self):
        api = Client(enforce_csrf_checks=True)
        response = api.post(url("trading_notes"), data=json.dumps({"title": "t", "text": "x"}),
                            content_type="application/json", HTTP_AUTHORIZATION=f"Bearer {self.raw}")
        self.assertEqual(response.status_code, 201)
        api.force_login(self.user)   # a logged-in browser session alone gets nothing
        response = api.post(url("trading_notes"), data=json.dumps({"title": "t", "text": "x"}),
                            content_type="application/json")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(TradingNote.objects.count(), 1)

    def test_bad_json_and_text_limits(self):
        response = self.api.post(url("trading_notes"), data="not json", content_type="application/json",
                                 HTTP_AUTHORIZATION=f"Bearer {self.raw}")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.call("POST", url("trading_notes"), {"title": "t", "text": "x" * 5001}).status_code, 400)
        self.assertEqual(self.call("POST", url("trading_notes"), {"title": "t" * 201, "text": "x"}).status_code, 400)
        self.assertEqual(self.call("POST", url("trading_notes"), {"title": "t", "text": "x" * 5000}).status_code, 201)
        self.assertEqual(self.call("POST", url("trading_notes"), {"title": "t", "text": 5}).status_code, 400)


class EndpointTests(BotApiTestCase):
    def test_docs_describe_every_endpoint(self):
        response = self.call("GET", url("docs"))
        self.assertEqual(response["Content-Type"], "text/plain; charset=utf-8")
        text = response.content.decode()
        from albright_bot_api.urls import api_urlpatterns
        for pattern in api_urlpatterns:
            route = str(pattern.pattern).replace("<int:lot_id>", "<id>").replace("<int:entry_id>", "<id>") \
                .replace("<int:opportunity_id>", "<id>")
            self.assertIn(route, text, route)
        for phrase in ("Authorization: Bearer", "401", "403", "429", "5000", "PLAIN TEXT", "WHAT YOU CANNOT DO"):
            self.assertIn(phrase, text)
        self.assertIn("YOUR TOKEN'S SCOPES: " + ", ".join(ALL), text)

    @mock.patch(HOME_OUTLOOK, return_value=None)
    def test_trading_notes_on_the_grok_bot_page_newest_first_and_escaped(self, _outlook):
        self.call("POST", url("trading_notes"), {"date": "2026-10-20", "title": "Older", "text": "first"})
        response = self.call("POST", url("trading_notes"), {"date": "2026-10-21", "title": "Spread idea", "text": XSS})
        self.assertEqual(response.status_code, 201)
        note = TradingNote.objects.get(pk=response.json()["id"])
        self.assertEqual((note.owner, note.token, note.text), (self.user, self.token, XSS))
        self.assertEqual(self.call("POST", url("trading_notes"), {"date": "21/10/2026", "title": "x", "text": "y"}).status_code, 400)
        self.client.force_login(self.user)
        page = self.client.get(reverse("grok_bot")).content.decode()   # not even connected to Alpaca
        self.assertIn('id="bot-notes"', page)
        self.assertLess(page.index("Spread idea"), page.index("Older"))
        self.assertIn("&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt;&lt;b&gt;bold&lt;/b&gt;", page)
        self.assertNotIn(XSS, page)

    def test_reseller_lots(self):
        self.reseller_data()
        lots = self.call("GET", url("reseller_lots")).json()["lots"]
        self.assertEqual([(l["id"], l["list"]) for l in lots], [(self.check.pk, "check_by_hand"), (self.cand.pk, "candidate")])
        cand = lots[1]
        for key in ("title", "url", "source", "category", "current_bid", "max_bid", "headroom", "ends_at", "image_url",
                    "confidence", "flags", "matched_keyword"):
            self.assertIn(key, cand)
        self.assertEqual((cand["current_bid"], cand["max_bid"], cand["headroom"]), ("12.00", "30.00", "18.00"))

    def test_lot_note_in_the_expandable_row_escaped(self):
        self.reseller_data()
        response = self.call("POST", url("reseller_lot_note", self.cand.pk), {"text": XSS})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(self.call("POST", url("reseller_lot_note", 999999), {"text": "x"}).status_code, 404)
        self.client.force_login(self.user)
        page = self.client.get(reverse("albright_reselling_app:dashboard")).content.decode()
        detail = page.split('class="bot-lot-note"', 1)[1][:600]
        self.assertIn('<span class="badge neutral">Grok</span>', detail)
        self.assertIn("&lt;script&gt;", detail)
        self.assertNotIn(XSS, page)

    def test_inventory_only_my_unsold_items_with_markdown(self):
        self.reseller_data()
        items = self.call("GET", url("reseller_inventory")).json()["items"]
        self.assertEqual([i["id"] for i in items], [self.listed.pk])
        item = items[0]
        self.assertEqual((item["cost"], item["list_price"], item["days_listed"]), ("22.00", "45.00", 20))
        self.assertEqual(item["next_markdown"]["step_days"] is not None, True)
        self.assertIn("label", item["next_markdown"])

    def test_inventory_suggestion_stays_pending_until_i_accept(self):
        self.reseller_data()
        response = self.call("POST", url("reseller_inventory_suggestion", self.listed.pk),
                             {"suggested_price": "39.99", "listing_draft": f"1921 Morgan {XSS}", "reason": "comps"})
        self.assertEqual((response.status_code, response.json()["status"]), (201, "pending"))
        suggestion = InventorySuggestion.objects.get()
        self.listed.refresh_from_db()
        self.assertEqual((suggestion.status, self.listed.list_price), ("pending", Decimal("45.00")))  # nothing changed

        self.client.force_login(self.user)
        page = self.client.get(reverse("albright_reselling_app:ledger_manage", args=[self.listed.pk])).content.decode()
        self.assertIn('id="grok-suggestions"', page)
        self.assertIn("List at $39.99", page)
        self.assertIn("&lt;script&gt;", page)
        self.assertNotIn(XSS, page)

        self.client.post(reverse("albright_bot_api:decide_suggestion", args=[suggestion.pk, "accept"]))
        suggestion.refresh_from_db()
        self.listed.refresh_from_db()
        self.assertEqual((suggestion.status, self.listed.list_price), ("accepted", Decimal("39.99")))

    def test_dismiss_and_validation(self):
        self.reseller_data()
        self.call("POST", url("reseller_inventory_suggestion", self.listed.pk), {"suggested_price": "30"})
        suggestion = InventorySuggestion.objects.get()
        self.client.force_login(self.user)
        self.client.post(reverse("albright_bot_api:decide_suggestion", args=[suggestion.pk, "dismiss"]))
        suggestion.refresh_from_db()
        self.listed.refresh_from_db()
        self.assertEqual((suggestion.status, self.listed.list_price), ("dismissed", Decimal("45.00")))
        for body, status in (({}, 400), ({"suggested_price": "-5"}, 400), ({"suggested_price": "abc"}, 400)):
            self.assertEqual(self.call("POST", url("reseller_inventory_suggestion", self.listed.pk), body).status_code, status)
        for entry in (self.sold, self.others):   # sold items and other people's items don't exist for the bot
            self.assertEqual(self.call("POST", url("reseller_inventory_suggestion", entry.pk),
                                       {"suggested_price": "1"}).status_code, 404)

    def test_someone_else_cannot_accept_my_suggestion(self):
        self.reseller_data()
        self.call("POST", url("reseller_inventory_suggestion", self.listed.pk), {"suggested_price": "30"})
        self.client.force_login(self.other_user)
        response = self.client.post(reverse("albright_bot_api:decide_suggestion", args=[InventorySuggestion.objects.get().pk, "accept"]))
        self.assertEqual(response.status_code, 404)

    def test_new_opportunities(self):
        self.opportunity_data()
        rows = self.call("GET", url("opportunities_new")).json()["opportunities"]
        self.assertEqual([r["title"] for r in rows], ["Fresh bounty"])   # not blocked/excluded/old/Remotive
        for key in ("id", "type", "url", "reward_usd", "ev_per_hour", "confidence", "safety_verdict", "skills_required", "text"):
            self.assertIn(key, rows[0])

    def test_opportunity_notes_and_proposal_drafts(self):
        from albright_crypto_app.models import ApplicationDraft
        self.opportunity_data()
        self.assertEqual(self.call("POST", url("opportunity_note", self.fresh.pk), {"text": XSS}).status_code, 201)
        self.assertEqual(self.call("POST", url("opportunity_proposal", self.fresh.pk),
                                   {"text": "Hi - I can fix this."}).status_code, 201)
        for opp in (self.blocked, self.remotive_job):
            self.assertEqual(self.call("POST", url("opportunity_note", opp.pk), {"text": "x"}).status_code, 404)
        self.assertEqual(list(OpportunityNote.objects.values_list("kind", flat=True).order_by("pk")), ["note", "proposal"])
        self.assertFalse(ApplicationDraft.objects.exists())
        self.fresh.refresh_from_db()
        self.assertEqual(self.fresh.status, "new")
        self.client.force_login(self.user)
        page = self.client.get(reverse("albright_crypto_app:opportunity", args=[self.fresh.pk])).content.decode()
        self.assertIn('id="grok-notes"', page)
        self.assertIn("Proposal draft", page)
        self.assertIn("&lt;script&gt;", page)
        self.assertNotIn(XSS, page)


class HardLimitTests(BotApiTestCase):
    """Nothing the API can do bids, changes a max bid, touches the watchlist
    or ledger entries, changes a price, submits an application, trades, or
    reads credentials/keys/tokens/profile."""

    def every_request(self):
        self.reseller_data()
        self.opportunity_data()
        calls = [("GET", url("docs"), None), ("GET", url("reseller_lots"), None), ("GET", url("reseller_inventory"), None),
                 ("GET", url("opportunities_new"), None),
                 ("POST", url("trading_notes"), {"title": "t", "text": "buy SPY"}),
                 ("POST", url("reseller_lot_note", self.cand.pk), {"text": "bid $25", "max_bid": "99", "watch": True}),
                 ("POST", url("reseller_inventory_suggestion", self.listed.pk),
                  {"suggested_price": "10", "list_price": "1", "status": "sold_out"}),
                 ("POST", url("opportunity_note", self.fresh.pk), {"text": "apply now", "status": "applied"}),
                 ("POST", url("opportunity_proposal", self.fresh.pk), {"text": "Dear client", "send": True})]
        return [(method, path, self.call(method, path, body)) for method, path, body in calls]

    def test_api_routes_are_only_these_get_and_post_views(self):
        from albright_bot_api import api_views
        from albright_bot_api.urls import api_urlpatterns
        for pattern in api_urlpatterns:
            self.assertIsInstance(pattern, URLPattern)
            self.assertEqual(pattern.callback.__module__, api_views.__name__)
        self.assertEqual(len(api_urlpatterns), 11)   # incl. site-suggestions and ideas (Admin)

    def test_no_forbidden_side_effects(self):
        from albright_reselling_app.models import LedgerEntry, LedgerSale
        from albright_reselling_app.scanner_models import BidWatch, LotUserEstimate, SourcedLot
        from albright_crypto_app.models import ApplicationDraft, Opportunity
        from albright_trading_app.models import OptionTrade, Trade
        from albright_reselling_app.scanner_models import BidWatch as BW
        self.reseller_data()
        watch = BW.objects.create(lot=self.cand, my_max_bid=Decimal("25.00"), status="watching")
        before = {
            "entries": list(LedgerEntry.objects.values_list("pk", "list_price", "status", "cost").order_by("pk")),
            "sales": LedgerSale.objects.count(),
            "watches": list(BidWatch.objects.values_list("pk", "my_max_bid", "status")),
            "estimates": LotUserEstimate.objects.count(),
            "lots": list(SourcedLot.objects.values_list("pk", "current_price").order_by("pk")),
            "trades": (Trade.objects.count(), OptionTrade.objects.count()),
        }
        with mock.patch("alpaca.trading.client.TradingClient", side_effect=AssertionError("tried to trade")), \
                mock.patch("albright_reselling_app.scanner.alerts.send_telegram", side_effect=AssertionError("sent")):
            self.opportunity_data()
            for method, path, body in (("POST", url("reseller_lot_note", self.cand.pk), {"text": "bid", "max_bid": "99"}),
                                       ("POST", url("reseller_inventory_suggestion", self.listed.pk),
                                        {"suggested_price": "10", "list_price": "1"}),
                                       ("POST", url("opportunity_proposal", self.fresh.pk), {"text": "Dear client"}),
                                       ("POST", url("opportunity_note", self.fresh.pk), {"text": "x", "status": "applied"}),
                                       ("POST", url("trading_notes"), {"title": "t", "text": "buy"}),
                                       ("GET", url("reseller_lots"), None), ("GET", url("reseller_inventory"), None),
                                       ("GET", url("opportunities_new"), None)):
                self.assertLess(self.call(method, path, body).status_code, 300, path)
        after = {
            "entries": list(LedgerEntry.objects.values_list("pk", "list_price", "status", "cost").order_by("pk")),
            "sales": LedgerSale.objects.count(),
            "watches": list(BidWatch.objects.values_list("pk", "my_max_bid", "status")),
            "estimates": LotUserEstimate.objects.count(),
            "lots": list(SourcedLot.objects.values_list("pk", "current_price").order_by("pk")),
            "trades": (Trade.objects.count(), OptionTrade.objects.count()),
        }
        self.assertEqual(before, after)
        self.assertEqual(watch.my_max_bid, BW.objects.get(pk=watch.pk).my_max_bid)
        self.assertFalse(ApplicationDraft.objects.exists())
        self.assertEqual(Opportunity.objects.get(pk=self.fresh.pk).status, "new")
        self.assertEqual(InventorySuggestion.objects.get().status, "pending")

    def test_no_credentials_keys_tokens_or_profile_in_any_response(self):
        from albright_trading_app.models import AlpacaCredentials
        from albright_crypto_app.models import GmailConnection, MyProfile
        creds = AlpacaCredentials(user=self.user, is_paper=True)
        creds.set_credentials("PKSECRETKEY123", "SECRETVALUE456")
        creds.save()
        gmail = GmailConnection(pk=1, email="private@example.com", scope="x")
        gmail.set_token({"refresh_token": "1//REFRESHSECRET"})
        gmail.save()
        profile = MyProfile.load()
        profile.summary, profile.name = "PROFILESECRET summary", "Private Name"
        profile.save()
        markers = ["PKSECRETKEY123", "SECRETVALUE456", creds.encrypted_api_key[:20], "REFRESHSECRET",
                   gmail.encrypted_token[:20], "private@example.com", "PROFILESECRET", "Private Name",
                   self.token.token_hash, self.raw]
        for method, path, response in self.every_request():
            body = response.content.decode()
            for marker in markers:
                self.assertNotIn(marker, body, f"{path} leaked {marker[:12]}")

    def test_unknown_paths_and_methods_do_nothing(self):
        self.assertEqual(self.api.get("/api/bot/v1/reseller/watchlist", HTTP_AUTHORIZATION=f"Bearer {self.raw}").status_code, 404)
        self.assertEqual(self.call("DELETE", url("reseller_lot_note", 1)).status_code, 405)


class ActivityLogTests(BotApiTestCase):
    def test_every_request_is_logged_and_filterable(self):
        _other, other_raw = BotToken.issue(self.user, "Other bot", ["reseller:read"])
        self.call("GET", url("docs"))
        self.call("POST", url("trading_notes"), {"title": "Iron condor idea", "text": "x"})
        self.call("GET", url("opportunities_new"), raw=other_raw)   # 403
        self.call("GET", url("docs"), raw="abt_nope")               # 401
        logs = list(BotRequestLog.objects.order_by("pk").values_list("token_name", "endpoint", "status", "summary"))
        self.assertEqual(logs[0], ("Grok bot", "/api/bot/v1/docs", 200, "read docs"))
        self.assertEqual(logs[1], ("Grok bot", "/api/bot/v1/trading/notes", 201, "trading note: Iron condor idea"))
        self.assertEqual(logs[2][1:3], ("/api/bot/v1/opportunities/new", 403))
        self.assertEqual(logs[3][0::2], ("(no valid token)", 401))

        self.user.is_superuser = True
        self.user.save()
        self.client.force_login(self.user)
        page = self.client.get(reverse("albright_control:bot_activity"))
        self.assertContains(page, "trading note: Iron condor idea")
        self.assertContains(page, "/api/bot/v1/opportunities/new")
        filtered = self.client.get(reverse("albright_control:bot_activity"), {"token": self.token.pk})
        self.assertContains(filtered, "Iron condor idea")
        self.assertNotContains(filtered, "/api/bot/v1/opportunities/new")
        self.assertContains(self.client.get(reverse("albright_control:bot_activity"), {"token": "none"}),
                            "rejected: missing or wrong token")

    @mock.patch(HOME_OUTLOOK, return_value=None)
    def test_bot_api_is_in_the_admin_nav_not_trading(self, _outlook):
        self.user.is_superuser = True
        self.user.save()
        self.client.force_login(self.user)
        link = f'<a href="{reverse("albright_control:bot_api")}">Bot API</a>'
        self.assertNotContains(self.client.get(reverse("trading_home")), link)
        page = self.client.get(reverse("albright_control:bot_api"))
        self.assertContains(page, '<body data-app="admin">')
        self.assertContains(page, link)
