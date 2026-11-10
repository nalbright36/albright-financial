"""Circle fitness challenges (slice 1): points, the leaderboard, access, upsert logging, comments."""
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from albright_labs import circles
from albright_labs.models import ActivityEvent
from albright_labs.testing import kitchen, with_apps

from . import challenges as ch
from .challenges import Challenge, ChallengeEntry, ChallengeRule, EntryComment

PASSWORD = "pw-not-real-12345"
D = Decimal


def url(name, *args):
    return reverse(f"albright_fitness:{name}", args=args)


class Base(TestCase):
    def setUp(self):
        self.today = timezone.localdate()
        self.me = with_apps(User.objects.create_user(username="nate", password=PASSWORD, first_name="Nate"), ["fitness"])
        self.sister = with_apps(User.objects.create_user(username="sam", password=PASSWORD, first_name="Sam"), ["fitness"])
        self.outsider = with_apps(User.objects.create_user(username="kim", password=PASSWORD, first_name="Kim"), ["fitness"])
        self.space = kitchen(self.me, self.sister, name="Albright Family", apps=("fitness",))
        self.circle = self.space.circle
        self.client.force_login(self.me)

    def as_user(self, user):
        c = Client()
        c.force_login(user)
        return c

    def challenge(self, **fields):
        fields.setdefault("name", "December steps")
        fields.setdefault("start_date", self.today)
        fields.setdefault("end_date", self.today + timedelta(days=30))
        fields.setdefault("circle", self.circle)
        fields.setdefault("created_by", self.me)
        challenge = Challenge.objects.create(**fields)
        ChallengeRule.objects.create(challenge=challenge, activity=ch.STEPS, unit="steps",
                                     points=D("1"), per_units=D("1000"))
        return challenge


class PointsTests(Base):
    def test_points_are_amount_over_per_units_times_points(self):
        rule = ChallengeRule(activity=ch.STEPS, unit="steps", points=D("1"), per_units=D("1000"))
        self.assertEqual(ch.points_for(rule, 8000), D("8.00"))
        self.assertEqual(ch.points_for(rule, 1500), D("1.50"))
        self.assertEqual(ch.points_for(rule, 0), D("0.00"))

    def test_daily_cap_limits_points(self):
        rule = ChallengeRule(activity=ch.STEPS, unit="steps", points=D("1"), per_units=D("1000"), daily_cap=D("10"))
        self.assertEqual(ch.points_for(rule, 20000), D("10.00"))
        self.assertEqual(ch.points_for(rule, 5000), D("5.00"))

    def test_a_run_rule_scores_without_a_schema_change(self):
        challenge = self.challenge()
        ChallengeRule.objects.create(challenge=challenge, activity=ch.RUN, unit="mi", points=D("10"), per_units=D("1"))
        entry = ch.log_entry(challenge, self.me, day=self.today, activity_key=ch.RUN, amount="3.1")
        self.assertEqual(entry.points, D("31.00"))


class LeaderboardTests(Base):
    def test_orders_by_points_and_ties_share_a_rank(self):
        challenge = self.challenge()
        ch.log_entry(challenge, self.me, day=self.today, activity_key=ch.STEPS, amount=8000)
        ch.log_entry(challenge, self.sister, day=self.today, activity_key=ch.STEPS, amount=8000)
        board = ch.leaderboard(challenge)
        self.assertEqual([(r["name"], r["points"], r["rank"]) for r in board],
                         [("Nate", D("8.00"), 1), ("Sam", D("8.00"), 1)])
        ch.log_entry(challenge, self.sister, day=self.today, activity_key=ch.STEPS, amount=12000)
        board = ch.leaderboard(challenge)
        self.assertEqual([(r["name"], r["points"], r["rank"]) for r in board],
                         [("Sam", D("12.00"), 1), ("Nate", D("8.00"), 2)])

    def test_members_with_no_entries_are_listed_at_zero(self):
        challenge = self.challenge()
        board = ch.leaderboard(challenge)
        self.assertEqual({r["name"] for r in board}, {"Nate", "Sam"})
        self.assertTrue(all(r["points"] == D("0.00") for r in board))


class AccessTests(Base):
    def test_outsider_gets_404(self):
        challenge = self.challenge()
        other = self.as_user(self.outsider)
        self.assertEqual(other.get(url("challenge", challenge.pk)).status_code, 404)
        self.assertEqual(other.post(url("challenge_log", challenge.pk), {
            "date": self.today.isoformat(), "activity": "steps", "amount": "1000"}).status_code, 404)
        self.assertNotContains(self.as_user(self.outsider).get(url("challenges")), "December steps")

    def test_opted_out_member_gets_404(self):
        challenge = self.challenge()
        circles.set_participation(self.sister, self.circle, "fitness", False)
        self.assertEqual(self.as_user(self.sister).get(url("challenge", challenge.pk)).status_code, 404)

    def test_participant_can_open_the_page_and_see_the_board(self):
        challenge = self.challenge()
        page = self.as_user(self.sister).get(url("challenge", challenge.pk))
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "December steps")
        self.assertContains(page, "Leaderboard")
        self.assertContains(page, "1 pt per 1,000 steps")


class LogTests(Base):
    def test_logging_the_same_day_and_activity_replaces_the_entry(self):
        challenge = self.challenge()
        first = ch.log_entry(challenge, self.me, day=self.today, activity_key=ch.STEPS, amount=4000, note="walk")
        second = ch.log_entry(challenge, self.me, day=self.today, activity_key=ch.STEPS, amount=9000, note="walk + errands")
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(ChallengeEntry.objects.filter(challenge=challenge, user=self.me).count(), 1)
        second.refresh_from_db()
        self.assertEqual((second.amount, second.points, second.note), (D("9000.00"), D("9.00"), "walk + errands"))

    def test_post_upserts_and_future_dates_are_refused(self):
        challenge = self.challenge()
        self.assertEqual(self.client.post(url("challenge_log", challenge.pk), {
            "date": self.today.isoformat(), "activity": "steps", "amount": "3500"}).status_code, 302)
        self.assertEqual(ChallengeEntry.objects.get(challenge=challenge, user=self.me).points, D("3.50"))
        self.client.post(url("challenge_log", challenge.pk), {
            "date": (self.today + timedelta(days=1)).isoformat(), "activity": "steps", "amount": "1000"})
        self.assertEqual(ChallengeEntry.objects.filter(challenge=challenge).count(), 1)

    def test_date_outside_the_challenge_is_refused(self):
        challenge = self.challenge(start_date=self.today, end_date=self.today)
        with self.assertRaises(ch.ChallengeError):
            ch.log_entry(challenge, self.me, day=self.today - timedelta(days=1), activity_key=ch.STEPS, amount=1000)


class CommentTests(Base):
    def test_participants_can_comment(self):
        challenge = self.challenge()
        entry = ch.log_entry(challenge, self.me, day=self.today, activity_key=ch.STEPS, amount=5000)
        self.assertEqual(self.as_user(self.sister).post(url("challenge_comment", entry.pk), {"text": "Nice walk!"}).status_code, 302)
        comment = EntryComment.objects.get(entry=entry)
        self.assertEqual((comment.user, comment.text), (self.sister, "Nice walk!"))
        self.assertContains(self.client.get(url("challenge", challenge.pk)), "Nice walk!")

    def test_outsider_cannot_comment(self):
        challenge = self.challenge()
        entry = ch.log_entry(challenge, self.me, day=self.today, activity_key=ch.STEPS, amount=5000)
        self.assertEqual(self.as_user(self.outsider).post(url("challenge_comment", entry.pk), {"text": "nope"}).status_code, 404)
        self.assertFalse(EntryComment.objects.exists())


class CreateTests(Base):
    def test_create_adds_a_steps_rule_and_an_activity_event(self):
        response = self.client.post(url("challenge_new"), {
            "name": "Holiday steps", "circle": self.circle.pk,
            "start_date": self.today.isoformat(),
            "end_date": (self.today + timedelta(days=20)).isoformat(),
            "steps_points": "1", "steps_per": "1000"})
        challenge = Challenge.objects.get(name="Holiday steps")
        self.assertRedirects(response, url("challenge", challenge.pk))
        rule = challenge.rules.get()
        self.assertEqual((rule.activity, rule.points, rule.per_units, rule.daily_cap),
                         (ch.STEPS, D("1.00"), D("1000.00"), None))
        event = ActivityEvent.objects.get(app="fitness", verb="challenge", circle=self.circle)
        self.assertIn("Holiday steps", event.summary)
        self.assertEqual(event.actor, self.me)

    def test_board_json_returns_the_table(self):
        challenge = self.challenge()
        ch.log_entry(challenge, self.me, day=self.today, activity_key=ch.STEPS, amount=1000)
        data = self.client.get(url("challenge_board", challenge.pk)).json()
        self.assertIn("Nate", data["html"])
        self.assertIn("fx-board-table", data["html"])
