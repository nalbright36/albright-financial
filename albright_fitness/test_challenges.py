"""Circle fitness challenges: points, the leaderboard, access, upsert logging, comments, photos, rule editing."""
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


# 1x1-ish JPEG / PNG / WebP prefixes: sniff_image only checks magic bytes (no Pillow).
JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xd9"
PNG = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + b"\x00" * 8
WEBP = b"RIFF" + b"\x20\x00\x00\x00" + b"WEBP" + b"\x00" * 16


def _photo(name, data, content_type="image/jpeg"):
    from django.core.files.uploadedfile import SimpleUploadedFile
    return SimpleUploadedFile(name, data, content_type=content_type)


class RuleEditTests(Base):
    def test_creator_and_circle_owner_can_add_and_edit_rules(self):
        challenge = self.challenge()
        page = self.client.get(url("challenge", challenge.pk))
        self.assertContains(page, "Edit rules")
        self.assertEqual(self.client.get(url("challenge_rules", challenge.pk)).status_code, 200)
        self.assertEqual(self.client.post(url("challenge_rule_save", challenge.pk), {
            "activity": "run", "points": "10", "per_units": "1", "unit": "mi"}).status_code, 302)
        rule = challenge.rules.get(activity=ch.RUN)
        self.assertEqual((rule.points, rule.per_units, rule.unit, rule.daily_cap),
                         (D("10.00"), D("1.00"), "mi", None))
        # A member who didn't create it (and isn't an owner) cannot.
        sam = self.as_user(self.sister)
        self.assertNotContains(sam.get(url("challenge", challenge.pk)), "Edit rules")
        self.assertEqual(sam.get(url("challenge_rules", challenge.pk)).status_code, 404)
        self.assertEqual(sam.post(url("challenge_rule_save", challenge.pk), {
            "activity": "bike", "points": "5", "per_units": "1"}).status_code, 404)
        self.assertFalse(challenge.rules.filter(activity=ch.BIKE).exists())

    def test_circle_owner_can_edit_a_challenge_they_did_not_create(self):
        challenge = self.challenge(created_by=self.sister)
        self.assertTrue(ch.can_edit_rules(self.me, challenge))
        self.assertTrue(ch.can_edit_rules(self.sister, challenge))  # creator
        self.client.post(url("challenge_rule_save", challenge.pk), {
            "activity": "lift", "points": "3", "per_units": "1", "unit": "sessions", "daily_cap": "12"})
        rule = challenge.rules.get(activity=ch.LIFT)
        self.assertEqual((rule.points, rule.daily_cap, rule.unit), (D("3.00"), D("12.00"), "sessions"))

    def test_outsider_gets_404_on_rules(self):
        challenge = self.challenge()
        other = self.as_user(self.outsider)
        self.assertEqual(other.get(url("challenge_rules", challenge.pk)).status_code, 404)
        self.assertEqual(other.post(url("challenge_rule_save", challenge.pk), {
            "activity": "run", "points": "1", "per_units": "1"}).status_code, 404)

    def test_editing_a_rule_recalculates_points_and_the_board(self):
        challenge = self.challenge()
        entry = ch.log_entry(challenge, self.me, day=self.today, activity_key=ch.STEPS, amount=8000)
        self.assertEqual(entry.points, D("8.00"))
        ch.set_rule(challenge, self.me, ch.STEPS, points="2", per_units="1000")
        entry.refresh_from_db()
        self.assertEqual(entry.points, D("16.00"))
        self.assertEqual(ch.leaderboard(challenge)[0]["points"], D("16.00"))

    def test_removing_a_rule_zeroes_those_entries_and_can_be_restored(self):
        challenge = self.challenge()
        ch.set_rule(challenge, self.me, ch.RUN, points="10", per_units="1")
        run = ch.log_entry(challenge, self.me, day=self.today, activity_key=ch.RUN, amount="3")
        steps = ch.log_entry(challenge, self.sister, day=self.today, activity_key=ch.STEPS, amount=1000)
        self.client.post(url("challenge_rule_delete", challenge.pk), {"activity": "run"})
        run.refresh_from_db()
        steps.refresh_from_db()
        self.assertFalse(challenge.rules.filter(activity=ch.RUN).exists())
        self.assertEqual((run.points, steps.points), (D("0.00"), D("1.00")))
        # The entry (and later a photo) stays, and adding the rule back scores it again.
        ch.set_rule(challenge, self.me, ch.RUN, points="10", per_units="1")
        run.refresh_from_db()
        self.assertEqual(run.points, D("30.00"))

    def test_the_last_rule_cannot_be_removed(self):
        challenge = self.challenge()
        with self.assertRaises(ch.ChallengeError):
            ch.remove_rule(challenge, self.me, ch.STEPS)
        self.assertTrue(challenge.rules.filter(activity=ch.STEPS).exists())
        self.client.post(url("challenge_rule_delete", challenge.pk), {"activity": "steps"})
        self.assertTrue(challenge.rules.filter(activity=ch.STEPS).exists())

    def test_log_form_lists_only_activities_the_challenge_scores(self):
        challenge = self.challenge()
        page = self.client.get(url("challenge", challenge.pk))
        self.assertContains(page, 'value="steps"')
        self.assertNotContains(page, 'value="run"')
        self.assertNotContains(page, 'value="bike"')
        self.assertNotContains(page, 'value="lift"')
        ch.set_rule(challenge, self.me, ch.RUN, points="10", per_units="1", unit="mi")
        page = self.client.get(url("challenge", challenge.pk))
        self.assertContains(page, ">Running (mi)<")
        self.assertContains(page, 'value="run"')
        self.assertContains(page, 'value="steps"')
        self.assertNotContains(page, 'value="bike"')
        self.assertNotContains(page, ">Biking")
        self.assertNotContains(page, ">Lifting")


class PhotoTests(Base):
    def test_logging_with_a_photo_shows_it_in_the_feed(self):
        challenge = self.challenge()
        response = self.client.post(url("challenge_log", challenge.pk), {
            "date": self.today.isoformat(), "activity": "steps", "amount": "5000",
            "photo": _photo("walk.jpg", JPEG)})
        self.assertEqual(response.status_code, 302)
        entry = ChallengeEntry.objects.get(challenge=challenge, user=self.me)
        self.assertEqual(entry.photo.content_type, "image/jpeg")
        self.assertEqual(bytes(entry.photo.data), JPEG)
        page = self.client.get(url("challenge", challenge.pk))
        self.assertContains(page, url("challenge_photo", entry.pk))
        image = self.client.get(url("challenge_photo", entry.pk))
        self.assertEqual(image.status_code, 200)
        self.assertEqual(image["Content-Type"], "image/jpeg")
        self.assertEqual(image["X-Content-Type-Options"], "nosniff")
        self.assertEqual(image.content, JPEG)
        # A participant can see someone else's photo; an outsider cannot.
        sam = self.as_user(self.sister)
        self.assertEqual(sam.get(url("challenge_photo", entry.pk)).status_code, 200)
        self.assertEqual(self.as_user(self.outsider).get(url("challenge_photo", entry.pk)).status_code, 404)

    def test_a_non_image_or_oversized_file_is_refused_and_does_not_log(self):
        challenge = self.challenge()
        self.client.post(url("challenge_log", challenge.pk), {
            "date": self.today.isoformat(), "activity": "steps", "amount": "1000",
            "photo": _photo("note.txt", b"<html><script>alert(1)</script>", "text/plain")})
        self.assertFalse(ChallengeEntry.objects.exists())
        big = _photo("big.jpg", b"\xff\xd8\xff" + b"x" * ch.MAX_PHOTO_BYTES)
        self.client.post(url("challenge_log", challenge.pk), {
            "date": self.today.isoformat(), "activity": "steps", "amount": "1000", "photo": big})
        self.assertFalse(ChallengeEntry.objects.exists())

    def test_relogging_without_a_photo_keeps_the_existing_one(self):
        challenge = self.challenge()
        first = ch.log_entry(challenge, self.me, day=self.today, activity_key=ch.STEPS,
                             amount=4000, photo=JPEG)
        self.assertEqual(bytes(first.photo.data), JPEG)
        second = ch.log_entry(challenge, self.me, day=self.today, activity_key=ch.STEPS, amount=9000)
        self.assertEqual(first.pk, second.pk)
        second.photo.refresh_from_db()
        self.assertEqual(bytes(second.photo.data), JPEG)
        # A new photo replaces the old bytes (content type follows the sniffed type, not the filename).
        ch.log_entry(challenge, self.me, day=self.today, activity_key=ch.STEPS,
                     amount=9000, photo=_photo("shot.jpg", PNG, "image/jpeg"))
        second.photo.refresh_from_db()
        self.assertEqual(second.photo.content_type, "image/png")
        self.assertEqual(bytes(second.photo.data), PNG)

    def test_webp_is_accepted(self):
        self.assertEqual(ch.sniff_image(WEBP), "image/webp")
        challenge = self.challenge()
        entry = ch.log_entry(challenge, self.me, day=self.today, activity_key=ch.STEPS,
                             amount=1000, photo=WEBP)
        self.assertEqual(entry.photo.content_type, "image/webp")
