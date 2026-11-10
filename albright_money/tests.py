"""Albright Money: the registry entry and colors (Trading now sky blue, Money green), privacy by space (Personal vs a
circle's, 404s), balances, splits and transfers, quick add, CSV import (mapping, preview, duplicates, rules,
nothing stored), "Always categorize", the budget math (rollover, left to budget), the dashboard, the launcher
line and Home tile, and the stylesheet on every page."""
import io
import pathlib
import re
from datetime import date, timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.contrib.staticfiles import finders
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase
from django.urls import reverse

from albright_labs import apps_registry, launcher, tiles
from albright_labs.testing import with_apps

from . import budget, imports, money, quickadd
from albright_labs import circles
from albright_labs.models import CircleMembership
from albright_labs.testing import kitchen, personal

from .models import (Account, Budget, BudgetLine, Category, Rule, Split, Transaction)

PASSWORD = "pw-not-real-12345"
D = Decimal


def url(name, *args):
    return reverse(f"albright_money:{name}", args=args)


def static(path):
    return pathlib.Path(finders.find(path)).read_text(encoding="utf-8")


class Base(TestCase):
    def setUp(self):
        cache.clear()
        outlook = mock.patch("albright_trading_app.views.get_market_outlook", return_value=None)
        outlook.start()
        self.addCleanup(outlook.stop)
        self.me = with_apps(User.objects.create_user(username="nate", password=PASSWORD, first_name="Nate"))
        self.wife = with_apps(User.objects.create_user(username="sam", password=PASSWORD, first_name="Sam"), ["money"])
        self.stranger = with_apps(User.objects.create_user(username="kim", password=PASSWORD), ["money"])
        self.client.force_login(self.me)
        self.today = date.today()
        self.month = self.today.replace(day=1)

    def as_user(self, user):
        client = Client()
        client.force_login(user)
        return client

    def household(self, *people):
        """A circle sharing Money (and Meals) with me as owner and these people in it -> its Space."""
        return kitchen(self.me, *people, name="The Albrights")

    def account(self, name="Checking", owner=None, household=None, **kw):
        kw.setdefault("starting_date", self.month - timedelta(days=60))
        return Account.objects.create(name=name, owner=owner or self.me, space=household or personal(owner or self.me), **kw)

    def cat(self, name, owner=None, household=None):
        space = household or personal(owner or self.me)
        money.ensure_categories(owner or self.me, space)
        return Category.objects.get(money.scope_filter(owner or self.me, space), name=name)

    def txn(self, account, amount, payee="", category=None, day=None, **kw):
        return Transaction.objects.create(account=account, amount=D(str(amount)), payee=payee, category=category,
                                          date=day or self.today, **kw)


# ---------------------------------------------------------------------------
# Registry and colors
# ---------------------------------------------------------------------------

class RegistryAndColorTests(Base):
    def test_trading_is_sky_blue_and_money_is_green(self):
        self.assertEqual(apps_registry.BY_KEY["trade"].color, "#38BDF8")
        money_app = apps_registry.BY_KEY["money"]
        self.assertEqual((money_app.label, money_app.short_label, money_app.brand, money_app.accent, money_app.color),
                         ("Money", "Money", "Albright Money", "money", "#34D399"))
        css = static("style.css")
        self.assertIn("--app-trading: #38BDF8;", css)                 # dark
        self.assertIn("--app-trading: #0284C7;", css)                 # light
        self.assertIn("--app-money: #34D399;", css)
        self.assertIn("--app-money: #10B981;", css)
        trading = css[css.index('body[data-app="trading"] {'):]
        trading = trading[:trading.index("}")]
        self.assertIn("--accent-rgb: 56, 189, 248;", trading)
        self.assertIn("--accent: #38BDF8;", trading)
        self.assertIn("--accent-contrast: #FFFFFF;", trading)
        self.assertIn('[data-theme="light"] body[data-app="trading"] { --accent-rgb: 2, 132, 199; }', css)
        money_rule = css[css.index('body[data-app="money"] {'):]
        self.assertIn("--accent-rgb: 16, 185, 129;", money_rule[:money_rule.index("}")])
        # Clearly not Reselling's blue.
        self.assertNotIn("59, 130, 246", trading)
        self.assertNotEqual(apps_registry.BY_KEY["trade"].color, apps_registry.BY_KEY["resell"].color)

    def test_semantic_colors_stay(self):
        css = static("style.css")
        self.assertIn("--emerald: #34D399;", css)                     # gains stay green
        self.assertIn("--gain: var(--emerald);", css)
        self.assertIn("--loss: var(--red);", css)
        js = static("js/grok_bot.js")
        self.assertIn('cssVar(up ? "--gain" : "--loss")', js)          # the chart's gain/loss lines

    def test_tiles_and_launcher_have_money(self):
        for css in (static("labs/launcher.css"),):
            self.assertIn('.app-launcher__tile[data-app-target="money"] { --app-color: var(--app-money); }', css)
        self.assertIn('.labs-pick[data-app-accent="money"] { --pick-color: var(--app-money); }', static("style.css"))
        page = self.client.get(url("dashboard")).content.decode()
        self.assertRegex(page, r'data-app-key="money" data-app-target="money" title="Albright Money"')
        self.assertIn('<body data-app="money">', page)
        self.assertIn("M21 11h-4a2 2 0 000 4h4z", page)               # the wallet icon

    def test_new_users_get_no_access(self):
        newbie = User.objects.create_user(username="new", password=PASSWORD)
        self.assertEqual(self.as_user(newbie).get(url("dashboard")).status_code, 404)
        self.assertEqual(self.as_user(with_apps(newbie, ["notes"])).get(url("dashboard")).status_code, 404)
        self.assertIn("money", apps_registry.grantable_keys())

    def test_stylesheet_and_script_on_every_money_page(self):
        a = self.account()
        t = self.txn(a, -10, "Coffee")
        for path in (url("dashboard"), url("transactions"), url("transaction", t.pk), url("transfer"), url("accounts"),
                     url("account", a.pk), url("import"), url("categories"), url("budget")):
            page = self.client.get(path).content.decode()
            self.assertRegex(page, r'<link rel="stylesheet" href="/static/albright_money/money\.css\?v=\d+">', path)
            self.assertRegex(page, r'<script src="/static/albright_money/money\.js\?v=\d+"></script>', path)
            self.assertIn('<body data-app="money">', page, path)
            self.assertEqual(page.count('id="nav-back"'), 1, path)      # the shared back chip


# ---------------------------------------------------------------------------
# Privacy by space
# ---------------------------------------------------------------------------

class HouseholdTests(Base):
    def test_the_old_household_page_is_now_circles(self):
        self.assertRedirects(self.client.get(url("household")), reverse("labs:circles"), fetch_redirect_response=False)
        h = self.household(self.wife)
        self.client.get(url("categories") + f"?space={h.pk}")
        self.assertTrue(Category.objects.filter(space=h, name="Groceries").exists())   # the circle's default set

    def test_only_people_with_money_access(self):
        no_money = with_apps(User.objects.create_user(username="pat", password=PASSWORD), ["notes"])
        h = self.household(no_money)
        ours = self.account("Joint", household=h)
        self.assertNotIn(ours, Account.objects.visible_to(no_money))       # in the circle, but no Money: nothing

    def test_personal_is_private_shared_is_shared(self):
        h = self.household(self.wife)
        mine = self.account("My card")
        ours = self.account("Joint", household=h)
        hers = self.account("Her savings", owner=self.wife)
        t_mine, t_ours = self.txn(mine, -20, "Secret gift"), self.txn(ours, -50, "Publix")
        sam = self.as_user(self.wife)
        # Sam: the shared account and its transactions, never my personal ones (404).
        self.assertEqual(sam.get(url("account", ours.pk)).status_code, 200)
        self.assertEqual(sam.get(url("transaction", t_ours.pk)).status_code, 200)
        self.assertEqual(sam.get(url("account", mine.pk)).status_code, 404)
        self.assertEqual(sam.get(url("transaction", t_mine.pk)).status_code, 404)
        self.assertEqual(sam.post(url("set_category", t_mine.pk), {"category": ""}).status_code, 404)
        self.assertEqual(sam.post(url("transaction_delete", t_mine.pk)).status_code, 404)
        listing = sam.get(url("transactions")).content.decode()
        self.assertIn("Publix", listing)
        self.assertNotIn("Secret gift", listing)
        self.assertNotIn("My card", sam.get(url("accounts")).content.decode())
        # And hers stays hers.
        self.assertEqual(self.client.get(url("account", hers.pk)).status_code, 404)

    def test_someone_outside_the_household_sees_nothing(self):
        h = self.household(self.wife)
        ours = self.account("Joint", household=h)
        t = self.txn(ours, -5, "Coffee")
        kim = self.as_user(self.stranger)
        for path in (url("account", ours.pk), url("transaction", t.pk)):
            self.assertEqual(kim.get(path).status_code, 404)
        self.assertNotIn("Coffee", kim.get(url("transactions")).content.decode())
        cat = self.cat("Groceries", household=h)
        self.assertEqual(kim.post(url("category", cat.pk), {"name": "x", "kind": "expense"}).status_code, 404)

    def test_personal_categories_rules_and_budgets(self):
        h = self.household(self.wife)
        mine = self.cat("Dining")
        rule = Rule.objects.create(owner=self.me, space=personal(self.me), payee_contains="chipotle", category=mine)
        sam = self.as_user(self.wife)
        self.assertEqual(sam.post(url("category", mine.pk), {"name": "Mine now", "kind": "expense"}).status_code, 404)
        self.assertEqual(sam.post(url("rule_delete", rule.pk)).status_code, 404)
        budget.save_plan(self.me, personal(self.me), self.month, {mine.pk: D("300")})
        sam_page = sam.get(url("budget") + f"?space={personal(self.wife).pk}").content.decode()
        self.assertNotIn('value="300.00"', sam_page)                   # her personal budget, not mine

    def test_leaving_and_removing(self):
        h = self.household(self.wife)
        ours = self.account("Joint", household=h)
        circles.remove(self.me, h.circle, self.wife)
        self.assertEqual(self.as_user(self.wife).get(url("account", ours.pk)).status_code, 404)
        with self.assertRaises(circles.CircleError):                    # the only owner can't just leave
            circles.leave(self.me, h.circle)
        self.assertTrue(CircleMembership.objects.filter(user=self.me, status="active").exists())
        self.assertTrue(Account.objects.filter(pk=ours.pk).exists())


# ---------------------------------------------------------------------------
# Accounts and balances
# ---------------------------------------------------------------------------

class AccountTests(Base):
    def test_balance_is_starting_plus_transactions(self):
        a = self.account(starting_balance=D("1000.00"), starting_date=self.today - timedelta(days=10))
        self.txn(a, "-42.18", "Publix")
        self.txn(a, "2500", "Payroll")
        self.txn(a, "-999", "Before the start", day=self.today - timedelta(days=30))   # before the starting date
        self.assertEqual(money.balance(a), D("3457.82"))

    def test_credit_card_shows_amount_owed(self):
        response = self.client.post(url("accounts"), {"name": "Visa", "kind": "credit", "institution": "Chase", "last4": "4242",
                                                      "starting_balance": "500.00", "starting_date": self.today.isoformat()})
        self.assertEqual(response.status_code, 302)
        card = Account.objects.get(name="Visa")
        self.assertEqual(card.starting_balance, D("-500.00"))           # owed = negative balance
        self.txn(card, "-25.00", "Gas")
        self.assertEqual(money.shown_balance(card, money.balance(card)), ("Owed", D("525.00")))
        self.assertIn("Owed", self.client.get(url("accounts")).content.decode())

    def test_last_four_only(self):
        response = self.client.post(url("accounts"), {"name": "Checking", "kind": "checking", "last4": "123456789012",
                                                      "starting_balance": "0", "starting_date": self.today.isoformat()})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Account.objects.exists())
        field_names = {f.name for f in Account._meta.get_fields()}
        for forbidden in ("account_number", "routing_number", "password", "username", "login"):
            self.assertNotIn(forbidden, field_names)
        self.assertEqual(Account._meta.get_field("last4").max_length, 4)

    def test_amounts_are_decimals(self):
        from django.db import models
        for model, field in ((Account, "starting_balance"), (Transaction, "amount"), (Split, "amount"),
                             (BudgetLine, "planned"), (Rule, "min_amount")):
            self.assertIsInstance(model._meta.get_field(field), models.DecimalField)

    def test_sharing_an_account_moves_its_categories(self):
        h = self.household(self.wife)
        a = self.account()
        t = self.txn(a, -30, "Publix", category=self.cat("Groceries"))
        self.client.post(url("account", a.pk), {"name": "Checking", "kind": "checking", "starting_balance": "0",
                                                "starting_date": a.starting_date.isoformat(), "space": h.pk})
        a.refresh_from_db()
        t.refresh_from_db()
        self.assertEqual(a.space, h)
        self.assertEqual(t.category, self.cat("Groceries", household=h))


# ---------------------------------------------------------------------------
# Transactions: quick add, splits, transfers, inline category and rules
# ---------------------------------------------------------------------------

class TransactionTests(Base):
    def test_quick_add(self):
        a = self.account()
        self.client.post(url("quick_add"), {"account": a.pk, "text": "42.18 Publix groceries"})
        t = Transaction.objects.get()
        self.assertEqual((t.amount, t.payee, t.category.name, t.source, t.entered_by), (D("-42.18"), "Publix", "Groceries", "manual", self.me))
        self.assertFalse(t.needs_review)
        self.client.post(url("quick_add"), {"account": a.pk, "text": "+1,200 Payroll"})
        self.assertEqual(Transaction.objects.get(payee="Payroll").amount, D("1200.00"))
        self.client.post(url("quick_add"), {"account": a.pk, "text": "Mystery 9.99"})
        mystery = Transaction.objects.get(payee="Mystery")
        self.assertEqual(mystery.amount, D("-9.99"))
        self.assertTrue(mystery.needs_review)                           # no category, no rule

    def test_quick_add_parser(self):
        cats = list(money.ensure_categories(self.me))
        self.assertEqual(quickadd.parse("12 Dr Smith personal care", cats)["category"].name, "Personal care")
        self.assertEqual(quickadd.parse("2500 ACME income", cats)["amount"], D("2500"))     # Income: money in
        self.assertIsNone(quickadd.parse("Publix groceries", cats))

    def test_splits_count_by_their_categories(self):
        a = self.account()
        t = self.txn(a, "-100.00", "Target")
        groceries, shopping = self.cat("Groceries"), self.cat("Shopping")
        form = {"date": self.today.isoformat(), "amount": "-100.00", "payee": "Target", "account": a.pk, "category": "",
                "notes": "", "tags": "", "split_category": [groceries.pk, shopping.pk], "split_amount": ["-60.00", "-40.00"],
                "split_note": ["", ""]}
        self.client.post(url("transaction", t.pk), form)
        t.refresh_from_db()
        self.assertEqual(sorted((s.category.name, s.amount) for s in t.splits.all()),
                         [("Groceries", D("-60.00")), ("Shopping", D("-40.00"))])
        self.assertIsNone(t.category)
        self.assertFalse(t.needs_review)
        totals = money.category_totals([groceries, shopping], self.month, budget.month_end(self.month))
        self.assertEqual((totals[groceries.pk], totals[shopping.pk]), (D("-60.00"), D("-40.00")))

    def test_splits_must_add_up(self):
        a = self.account()
        t = self.txn(a, "-100.00", "Target")
        g = self.cat("Groceries")
        response = self.client.post(url("transaction", t.pk), {
            "date": self.today.isoformat(), "amount": "-100.00", "payee": "Target", "account": a.pk,
            "split_category": [g.pk, g.pk], "split_amount": ["-60.00", "-30.00"], "split_note": ["", ""]})
        self.assertContains(response, "The splits add up to -90.00, not -100.00.")
        self.assertFalse(t.splits.exists())

    def test_transfers_are_not_spending(self):
        checking, savings = self.account("Checking"), self.account("Savings", kind="savings")
        self.client.post(url("transfer"), {"from_account": checking.pk, "to_account": savings.pk, "amount": "500",
                                           "date": self.today.isoformat()})
        out, into = Transaction.objects.get(account=checking), Transaction.objects.get(account=savings)
        self.assertTrue(out.is_transfer and into.is_transfer)
        self.assertEqual((out.amount, into.amount, out.transfer_pair, into.transfer_pair), (D("-500"), D("500"), into, out))
        self.assertEqual(money.balance(savings), D("500"))
        # Even with a category, a transfer never counts as spending.
        other = self.cat("Other")
        Transaction.objects.filter(pk=out.pk).update(category=other)
        self.assertEqual(money.category_totals([other], self.month, budget.month_end(self.month)), {})
        self.client.post(url("transaction_delete", out.pk))
        self.assertFalse(Transaction.objects.exists())                  # both sides go

    def test_editing_a_transfer_keeps_both_sides_aligned(self):
        checking, savings = self.account("Checking"), self.account("Savings", kind="savings")
        self.client.post(url("transfer"), {"from_account": checking.pk, "to_account": savings.pk, "amount": "500",
                                           "date": self.today.isoformat(), "note": "Move"})
        out = Transaction.objects.get(account=checking)
        into = out.transfer_pair
        response = self.client.post(url("transaction", out.pk), {
            "date": (self.today - timedelta(days=1)).isoformat(), "amount": "-400.00", "payee": "Rent float",
            "account": checking.pk, "category": "", "notes": "", "tags": "", "needs_review": False})
        self.assertEqual(response.status_code, 302)
        out.refresh_from_db()
        into.refresh_from_db()
        self.assertEqual((out.amount, into.amount), (D("-400.00"), D("400.00")))
        self.assertEqual(out.date, into.date)
        self.assertEqual((out.payee, into.payee), ("Rent float", "Rent float"))
        self.assertTrue(out.is_transfer and into.is_transfer)

    def test_cannot_mark_a_normal_spend_as_a_transfer(self):
        a = self.account()
        t = self.txn(a, "-25.00", "Coffee", category=self.cat("Dining"))
        self.client.post(url("transaction", t.pk), {
            "date": self.today.isoformat(), "amount": "-25.00", "payee": "Coffee", "account": a.pk,
            "category": t.category_id, "notes": "", "tags": "", "is_transfer": "on", "needs_review": False})
        t.refresh_from_db()
        self.assertFalse(t.is_transfer)
        totals = money.category_totals([t.category], self.month, budget.month_end(self.month))
        self.assertEqual(totals[t.category_id], D("-25.00"))

    def test_cannot_move_a_transfer_to_another_account_on_edit(self):
        checking, savings, other = (
            self.account("Checking"), self.account("Savings", kind="savings"), self.account("Cash", kind="cash"))
        self.client.post(url("transfer"), {"from_account": checking.pk, "to_account": savings.pk, "amount": "50",
                                           "date": self.today.isoformat()})
        out = Transaction.objects.get(account=checking)
        response = self.client.post(url("transaction", out.pk), {
            "date": self.today.isoformat(), "amount": "-50.00", "payee": out.payee, "account": other.pk,
            "category": "", "notes": "", "tags": "", "needs_review": False})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "A transfer&#x27;s account can&#x27;t change here")   # escaped in the page
        out.refresh_from_db()
        self.assertEqual(out.account_id, checking.pk)

    def test_category_menu_is_the_accounts_own_set(self):
        h = self.household(self.wife)
        ours = self.account("Joint", household=h)
        t = self.txn(ours, -10, "Coffee")
        personal = self.cat("Dining")
        self.assertEqual(self.client.post(url("set_category", t.pk), {"category": personal.pk}).status_code, 404)
        shared = self.cat("Dining", household=h)
        self.assertEqual(self.client.post(url("set_category", t.pk), {"category": shared.pk}).status_code, 302)

    def test_list_filters_and_search(self):
        a, b = self.account("Checking"), self.account("Visa", kind="credit")
        g = self.cat("Groceries")
        self.txn(a, -50, "Publix", category=g)
        self.txn(b, -20, "Shell gas")
        self.txn(a, -5, "Old one", day=self.month - timedelta(days=40))
        def payees(query):
            page = self.client.get(url("transactions") + query).content.decode()
            return re.findall(r'class="mn-txn__payee">([^<]+)<', page)
        self.assertEqual(sorted(payees("")), ["Old one", "Publix", "Shell gas"])
        self.assertEqual(payees(f"?account={b.pk}"), ["Shell gas"])
        self.assertEqual(payees(f"?category={g.pk}"), ["Publix"])
        self.assertEqual(payees("?category=none&review="), ["Shell gas", "Old one"])
        self.assertEqual(sorted(payees(f"?month={self.month:%Y-%m}")), ["Publix", "Shell gas"])
        self.assertEqual(payees("?q=shell"), ["Shell gas"])
        Transaction.objects.filter(payee="Shell gas").update(needs_review=True)
        self.assertEqual(payees("?review=1"), ["Shell gas"])
        page = self.client.get(url("transactions")).content.decode()
        self.assertIn(f'<h2 class="mn-day__date"', page)               # grouped by date
        self.assertIn('id="mn-quick"', page)                           # quick add at the top
        self.assertIn('placeholder="42.18 Publix groceries"', page)


class AlwaysCategorizeTests(Base):
    def test_changing_a_category_offers_a_rule_and_yes_makes_it(self):
        a = self.account()
        t = self.txn(a, "-23.10", "PUBLIX #1234 TAMPA", needs_review=True)
        other = self.txn(a, "-60.00", "PUBLIX #0999 TAMPA", needs_review=True)
        g = self.cat("Groceries")
        res = self.client.post(url("set_category", t.pk), {"category": g.pk}, HTTP_X_REQUESTED_WITH="XMLHttpRequest").json()
        self.assertEqual(res["offer"]["payee"], "publix")
        self.assertEqual(res["offer"]["category"], "Groceries")
        self.assertFalse(res["needs_review"])
        res = self.client.post(res["offer"]["url"], HTTP_X_REQUESTED_WITH="XMLHttpRequest").json()
        rule = Rule.objects.get()
        self.assertEqual((rule.payee_contains, rule.category, rule.space), ("publix", g, personal(self.me)))
        self.assertEqual(res["changed"], 1)
        other.refresh_from_db()
        self.assertEqual((other.category, other.needs_review), (g, False))     # the others it matches, too
        # Already a rule for it: no offer next time.
        third = self.txn(a, "-5", "PUBLIX #77 TAMPA")
        res = self.client.post(url("set_category", third.pk), {"category": g.pk}, HTTP_X_REQUESTED_WITH="XMLHttpRequest").json()
        self.assertIsNone(res["offer"])

    def test_without_js_the_offer_comes_back_as_a_form(self):
        a = self.account()
        t = self.txn(a, "-9", "Netflix")
        sub = self.cat("Subscriptions")
        response = self.client.post(url("set_category", t.pk), {"category": sub.pk, "next": url("transactions")})
        self.assertEqual(response["Location"], url("transactions") + f"?offer={t.pk}")
        page = self.client.get(response["Location"]).content.decode()
        self.assertIn(f'action="{url("always", t.pk)}"', page)
        self.assertIn("Always categorize <strong>netflix</strong> as <strong>Subscriptions</strong>?", page)

    def test_rule_text_from_a_payee(self):
        self.assertEqual(money.rule_key("PUBLIX #1234 TAMPA FL"), "publix")
        self.assertEqual(money.rule_key("SQ *BLUE BOTTLE 2231"), "sq blue bottle")
        self.assertEqual(money.rule_key("Netflix.com"), "netflix com")
        a = self.account()
        rule = Rule.objects.create(owner=self.me, space=personal(self.me), payee_contains="Netflix.com", category=self.cat("Subscriptions"))
        self.assertEqual(money.rule_for(Transaction(account=a, payee="NETFLIX.COM 866-579", amount=D("-15"))), rule)

    def test_rules_match(self):
        a = self.account()
        g, d = self.cat("Groceries"), self.cat("Dining")
        Rule.objects.create(owner=self.me, space=personal(self.me), payee_contains="publix", category=g)
        Rule.objects.create(owner=self.me, space=personal(self.me), payee_contains="publix deli", category=d, max_amount=D("20"))
        self.assertEqual(money.rule_for(Transaction(account=a, payee="PUBLIX DELI 12", amount=D("-12"))).category, d)
        self.assertEqual(money.rule_for(Transaction(account=a, payee="PUBLIX DELI 12", amount=D("-80"))).category, g)
        other = self.account("Visa")
        Rule.objects.create(owner=self.me, space=personal(self.me), payee_contains="shell", category=d, account=other)
        self.assertIsNone(money.rule_for(Transaction(account=a, payee="SHELL", amount=D("-30"))))


# ---------------------------------------------------------------------------
# CSV import
# ---------------------------------------------------------------------------

CSV = (b"Date,Description,Amount\n"
       b"11/01/2026,PUBLIX #1234,-42.18\n"
       b"11/02/2026,NETFLIX.COM,-15.49\n"
       b"11/03/2026,PAYROLL ACME,2500.00\n"
       b"11/04/2026,=HYPERLINK(\"http://x\"),-1.00\n")


class ImportTests(Base):
    def upload(self, account, data=CSV, name="bank.csv"):
        return self.client.post(url("import"), {"step": "upload", "account": account.pk,
                                                "file": SimpleUploadedFile(name, data, content_type="text/csv")})

    def test_map_once_preview_then_one_step_next_time(self):
        a = self.account()
        page = self.upload(a).content.decode()
        self.assertIn('name="step" value="map"', page)                 # the first time: map the columns
        self.assertIn('<option value="Date" selected>', page)          # guessed from the names
        payload = re.search(r'name="payload" value="([^"]+)"', page).group(1)
        page = self.client.post(url("import"), {"step": "map", "payload": payload, "date": "Date",
                                                "description": "Description", "style": "amount", "amount": "Amount",
                                                "date_format": "%m/%d/%Y"}).content.decode()
        a.refresh_from_db()
        self.assertEqual(a.csv_mapping, {"date": "Date", "description": "Description", "date_format": "%m/%d/%Y",
                                         "amount": "Amount", "negate": False})
        self.assertIn('name="step" value="import"', page)              # preview
        self.assertIn("<strong>4</strong> new", page)
        # Next time: straight to the preview.
        self.assertIn('name="step" value="import"', self.upload(a).content.decode())

    def test_import_applies_rules_flags_the_rest_and_skips_duplicates(self):
        a = self.account(csv_mapping={"date": "Date", "description": "Description", "amount": "Amount",
                                      "date_format": "%m/%d/%Y"})
        g = self.cat("Groceries")
        Rule.objects.create(owner=self.me, space=personal(self.me), payee_contains="publix", category=g)
        # Entered by hand already: "Publix" on Nov 1 for 42.18.
        self.txn(a, "-42.18", "Publix", day=date(2026, 11, 1), category=g)
        page = self.upload(a).content.decode()
        self.assertIn("<strong>3</strong> new &middot; <strong>1</strong> already there (skipped)", page)
        payload = re.search(r'name="step" value="import"><input type="hidden" name="payload" value="([^"]+)"', page).group(1)
        response = self.client.post(url("import"), {"step": "import", "payload": payload})
        self.assertEqual(response.status_code, 302)
        imported = Transaction.objects.filter(source="import")
        self.assertEqual(imported.count(), 3)
        self.assertTrue(imported.get(payee="NETFLIX.COM").needs_review)
        self.assertFalse(imported.filter(payee__startswith="PUBLIX").exists())      # the duplicate
        formula = imported.get(amount=D("-1.00"))
        self.assertEqual(formula.payee, 'HYPERLINK("http://x")')                      # no formula kept
        # Importing the same file again adds nothing.
        page = self.upload(a).content.decode()
        self.assertIn("<strong>0</strong> new", page)

    def test_debit_credit_columns_and_date_formats(self):
        headers = ["Posted", "Payee", "Debit", "Credit"]
        rows = [["2026-11-05", "Shell", "30.00", ""], ["2026-11-06", "Refund", "", "12.50"], ["bad", "x", "1", ""]]
        parsed, problems = imports.normalize(headers, rows, {"date": "Posted", "description": "Payee", "debit": "Debit",
                                                             "credit": "Credit", "date_format": "%Y-%m-%d"})
        self.assertEqual([(r["payee"], r["amount"]) for r in parsed], [("Shell", "-30.00"), ("Refund", "12.50")])
        self.assertEqual(problems[0][0], 4)
        self.assertEqual(imports.parse_amount("(1,234.56)"), D("-1234.56"))
        self.assertEqual(imports.parse_amount("$12"), D("12.00"))
        self.assertIsNone(imports.parse_amount("=1+1"))

    def test_similar_descriptions(self):
        self.assertTrue(imports.similar("Publix", "PUBLIX SUPER MARKETS #1234"))
        self.assertTrue(imports.similar("Netflix", "NETFLIX.COM"))
        self.assertFalse(imports.similar("Shell", "Publix"))

    def test_limits_and_nothing_stored(self):
        a = self.account()
        big = b"Date,Description,Amount\n" + b"11/01/2026,x,-1\n" * 70000
        response = self.upload(a, data=big)
        self.assertRedirects(response, url("import"), fetch_redirect_response=False)
        self.assertFalse(Transaction.objects.exists())
        self.assertIn("too big", " ".join(m.message for m in response.wsgi_request._messages))
        self.assertEqual(imports.MAX_BYTES, 1_000_000)
        from django.conf import settings
        self.assertLessEqual(imports.MAX_BYTES, settings.FILE_UPLOAD_MAX_MEMORY_SIZE)   # never a temp file

    def test_import_is_atomic_on_failure(self):
        a = self.account(csv_mapping={"date": "Date", "description": "Description", "amount": "Amount",
                                      "date_format": "%m/%d/%Y"})
        rows = [{"date": "2026-11-01", "payee": "One", "amount": "-1.00", "duplicate": False},
                {"date": "2026-11-02", "payee": "Two", "amount": "-2.00", "duplicate": False}]
        real_save = Transaction.save

        def boom(self, *args, **kwargs):
            if self.payee == "Two":
                raise RuntimeError("boom")
            return real_save(self, *args, **kwargs)

        with mock.patch.object(Transaction, "save", boom):
            with self.assertRaises(RuntimeError):
                imports.import_rows(a, rows, self.me)
        self.assertFalse(Transaction.objects.exists())  # first row rolled back with the second

    def test_tampered_payload_is_refused(self):
        a = self.account()
        response = self.client.post(url("import"), {"step": "import", "payload": "not-signed"})
        self.assertRedirects(response, url("import"), fetch_redirect_response=False)
        self.assertFalse(Transaction.objects.exists())
        other = self.account("Hers", owner=self.wife)
        token = imports.pack({"account": other.pk, "rows": [{"date": "2026-11-01", "payee": "x", "amount": "-1"}]})
        self.assertEqual(self.client.post(url("import"), {"step": "import", "payload": token}).status_code, 404)
        self.assertTrue(a.pk)

    def test_set_category_refuses_splits(self):
        a = self.account()
        t = self.txn(a, "-100.00", "Target")
        g, s = self.cat("Groceries"), self.cat("Shopping")
        Split.objects.create(transaction=t, category=g, amount=D("-60.00"))
        Split.objects.create(transaction=t, category=s, amount=D("-40.00"))
        response = self.client.post(url("set_category", t.pk), {"category": g.pk},
                                    HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(t.splits.count(), 2)
        t.refresh_from_db()
        self.assertIsNone(t.category_id)


# ---------------------------------------------------------------------------
# Budgets
# ---------------------------------------------------------------------------

class BudgetTests(Base):
    def test_planned_spent_remaining_and_colors(self):
        a = self.account()
        g, d, h = self.cat("Groceries"), self.cat("Dining"), self.cat("Housing")
        budget.save_plan(self.me, personal(self.me), self.month, {g.pk: D("500"), d.pk: D("100"), h.pk: D("1000")})
        self.txn(a, "-200", "Publix", category=g)
        self.txn(a, "-95", "Chipotle", category=d)
        self.txn(a, "-1100", "Rent", category=h)
        self.txn(a, "20", "Refund", category=g)                       # a refund brings spending down
        v = budget.month_view(self.me, personal(self.me), self.month)
        rows = {r["category"].name: r for r in v["rows"]}
        self.assertEqual((rows["Groceries"]["spent"], rows["Groceries"]["remaining"], rows["Groceries"]["status"]),
                         (D("180"), D("320"), "under"))
        self.assertEqual((rows["Dining"]["remaining"], rows["Dining"]["status"]), (D("5"), "near"))
        self.assertEqual((rows["Housing"]["remaining"], rows["Housing"]["status"], rows["Housing"]["pct"]), (D("-100"), "over", 100))

    def test_income_and_left_to_budget(self):
        a = self.account()
        inc, g = self.cat("Income"), self.cat("Groceries")
        budget.save_plan(self.me, personal(self.me), self.month, {inc.pk: D("5000"), g.pk: D("600")})
        self.txn(a, "2500", "Payroll", category=inc)
        v = budget.month_view(self.me, personal(self.me), self.month)
        self.assertEqual((v["income_planned"], v["income_received"]), (D("5000"), D("2500")))
        self.assertEqual(v["left_to_budget"], D("4400"))
        page = self.client.get(url("budget")).content.decode()
        self.assertIn('id="mn-left-to-budget"', page)
        self.assertIn("$4,400.00", page)

    def test_rollover_carries_unspent_and_overspent(self):
        a = self.account()
        g = self.cat("Groceries")
        g.rollover = True
        g.save()
        dining = self.cat("Dining")
        last, two_ago = budget.add_months(self.month, -1), budget.add_months(self.month, -2)
        budget.save_plan(self.me, personal(self.me), two_ago, {g.pk: D("400"), dining.pk: D("100")})
        budget.save_plan(self.me, personal(self.me), last, {g.pk: D("400"), dining.pk: D("100")})
        budget.save_plan(self.me, personal(self.me), self.month, {g.pk: D("400"), dining.pk: D("100")})
        self.txn(a, "-350", "Publix", category=g, day=two_ago)       # +50
        self.txn(a, "-480", "Publix", category=g, day=last)          # -80  -> -30 carried in
        self.txn(a, "-10", "Chipotle", category=dining, day=last)    # no rollover: nothing carried
        rows = {r["category"].name: r for r in budget.month_view(self.me, personal(self.me), self.month)["rows"]}
        self.assertEqual((rows["Groceries"]["carry"], rows["Groceries"]["available"]), (D("-30"), D("370")))
        self.assertEqual((rows["Dining"]["carry"], rows["Dining"]["available"]), (D("0"), D("100")))

    def test_copy_last_month(self):
        g = self.cat("Groceries")
        budget.save_plan(self.me, personal(self.me), budget.add_months(self.month, -1), {g.pk: D("450")})
        self.client.post(url("budget"), {"action": "copy", "space": personal(self.me).pk, "month": f"{self.month:%Y-%m}"})
        self.assertEqual(BudgetLine.objects.get(budget__month=self.month, category=g).planned, D("450"))

    def test_saving_from_the_page(self):
        g = self.cat("Groceries")
        self.client.post(url("budget"), {"space": personal(self.me).pk, "month": f"{self.month:%Y-%m}", f"planned_{g.pk}": "525.50",
                                         "rollover": [g.pk]})
        self.assertEqual(BudgetLine.objects.get(category=g).planned, D("525.50"))
        g.refresh_from_db()
        self.assertTrue(g.rollover)
        self.assertEqual(self.client.post(url("budget"), {"space": personal(self.me).pk, f"planned_{g.pk}": "-5"}).status_code, 302)
        self.assertEqual(BudgetLine.objects.get(category=g).planned, D("525.50"))   # negative refused

    def test_shared_household_budget_plus_personal(self):
        h = self.household(self.wife)
        ours, mine = self.account("Joint", household=h), self.account("Mine")
        hg, pg = self.cat("Groceries", household=h), self.cat("Groceries")
        budget.save_plan(self.me, h, self.month, {hg.pk: D("800")})
        budget.save_plan(self.me, personal(self.me), self.month, {pg.pk: D("100")})
        self.txn(ours, "-300", "Publix", category=hg)
        self.txn(mine, "-40", "Snacks", category=pg)
        self.assertEqual(budget.left_this_month(self.me), D("560"))    # 500 + 60
        self.assertEqual(budget.left_this_month(self.wife), D("500"))  # the circle's only - mine is mine
        self.assertEqual(Budget.objects.count(), 2)
        self.assertIn("$800.00", self.as_user(self.wife).get(url("budget")).content.decode().replace('value="800.00"', "$800.00"))


# ---------------------------------------------------------------------------
# Dashboard, launcher, Home
# ---------------------------------------------------------------------------

class DashboardTests(Base):
    def setUp(self):
        super().setUp()
        self.a = self.account(starting_balance=D("1000"))
        self.g, self.d = self.cat("Groceries"), self.cat("Dining")
        budget.save_plan(self.me, personal(self.me), self.month, {self.g.pk: D("500"), self.d.pk: D("200")})
        self.txn(self.a, "-120", "Publix", category=self.g)
        self.txn(self.a, "-80", "Chipotle", category=self.d)
        self.txn(self.a, "-15", "Mystery", needs_review=True)

    def test_numbers(self):
        page = self.client.get(url("dashboard")).content.decode()
        safe = page.split('id="mn-safe"', 1)[1].split("</div>", 1)[0]
        self.assertIn("$500.00", safe)                                  # 700 budgeted - 200 spent
        spent = page.split('id="mn-spent"', 1)[1].split("</div>", 2)[0]
        self.assertIn("$200.00", spent)
        self.assertIn("of $700.00 budgeted", spent)
        self.assertIn('class="mn-stat__value is-warn">1<', page)       # needs review
        self.assertIn("$785.00", page)                                  # the balance
        top = page.split('id="mn-top-h"', 1)[1].split("</ol>", 1)[0]
        self.assertLess(top.index("Groceries"), top.index("Dining"))
        self.assertIn("Mystery", page.split('id="mn-review"', 1)[1])
        self.assertIn("Chipotle", page.split('id="mn-latest-h"', 1)[1])

    def test_launcher_line_and_home_tile(self):
        self.assertEqual(launcher.money(self.me), "$500 left this month")
        self.assertEqual(launcher.LINES["money"], launcher.money)
        tile = tiles.money(self.me)
        self.assertEqual((tile["value"], tile["note"]), ("$500", "left this month · 1 to review"))
        self.txn(self.a, "-900", "Big shop", category=self.g)
        self.assertEqual(launcher.money(self.me), "$400 over this month")
        nobody = with_apps(User.objects.create_user(username="lee", password=PASSWORD), ["money"])
        self.assertEqual(launcher.money(nobody), "No budget this month")
        response = self.client.get(reverse("labs:tile", args=["money"]))
        self.assertEqual(response.json()["value"], "$400")
