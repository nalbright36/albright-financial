"""The optional AI short version: only their own connection, only what the briefing already shows, capped, and
the rule-based summary whenever anything's off."""
from datetime import date, time
from unittest import mock

from django.urls import reverse

from albright_assistant import access, providers
from albright_assistant.models import Connection
from albright_labs.models import UserPreference

from . import ai, schedule, service
from .models import AI, RULES, WEEK, RoundupIssue
from .tests import PASSWORD, Base

KEY = "sk-test-roundup-SECRET-9876"
WRITTEN = "A calm week: Pickleball Tuesday at 6:30 PM, and Wednesday looks rainy, so plan inside."


class AiBase(Base):
    def setUp(self):
        super().setUp()
        UserPreference.objects.update_or_create(user=self.me, defaults={"assistant_allowed": True})
        Connection.objects.create(user=self.me, provider="anthropic", key_encrypted=access.encrypt(KEY),
                                  key_last4=KEY[-4:], model="claude-test")
        self.r = schedule.preset(self.me, WEEK)
        self.r.ai_summary = True
        self.r.save()
        self.item("Pickleball", date=date(2026, 11, 24), kind="event", checkable=False, start_time=time(18, 30))
        self.item("Lexi's book club", owner=self.wife, date=date(2026, 11, 25))
        self.provider = mock.Mock(label="Anthropic")
        self.provider.chat.return_value = providers.Reply(text=WRITTEN)
        p = mock.patch("albright_assistant.providers.get", return_value=self.provider)
        p.start()
        self.addCleanup(p.stop)

    def make(self):
        return service.issue_for(self.r, date(2026, 11, 23), date(2026, 11, 29), rebuild=True)


class AiSummaryTests(AiBase):
    def test_written_by_their_ai_from_what_they_can_see(self):
        issue = self.make()
        self.assertEqual((issue.summary, issue.summary_source), (WRITTEN, AI))
        self.assertEqual(issue.payload["ai_by"], "Anthropic · claude-test")
        args = self.provider.chat.call_args
        self.assertEqual(args.args[0], KEY)                 # their own key, decrypted only for the call
        self.assertEqual(args.args[1], "claude-test")
        sent = args.args[3][0]["text"]
        self.assertIn("Pickleball", sent)
        self.assertNotIn("book club", sent)                 # Lexi's private item never leaves
        self.assertNotIn("http", sent)
        self.assertEqual(args.args[4], [])                  # no tools

    def test_errors_fall_back_to_rules_and_never_log_the_key(self):
        self.provider.chat.side_effect = providers.ProviderError("timeout", "Anthropic", "claude-test")
        with self.assertLogs("albright_roundup.ai", "WARNING") as logs:
            issue = self.make()
        self.assertEqual(issue.summary_source, RULES)
        self.assertIn("Pickleball", issue.summary)
        self.assertNotIn(KEY, "\n".join(logs.output))
        self.provider.chat.side_effect = RuntimeError(KEY)  # even an error that carries the key
        with self.assertLogs("albright_roundup.ai", "WARNING") as logs:
            self.assertEqual(self.make().summary_source, RULES)
        self.assertNotIn(KEY, "\n".join(logs.output))

    def test_empty_answer_falls_back(self):
        self.provider.chat.return_value = providers.Reply(text="  ")
        self.assertEqual(self.make().summary_source, RULES)

    def test_off_not_allowed_or_disabled_never_calls(self):
        self.r.ai_summary = False
        self.r.save()
        self.assertEqual(self.make().summary_source, RULES)
        self.r.ai_summary = True
        self.r.save()
        Connection.objects.filter(user=self.me).update(disabled=True)
        self.assertEqual(self.make().summary_source, RULES)
        Connection.objects.filter(user=self.me).update(disabled=False)
        UserPreference.objects.filter(user=self.me).update(assistant_allowed=False)
        self.assertEqual(self.make().summary_source, RULES)
        self.provider.chat.assert_not_called()

    def test_capped_a_day(self):
        for n in range(ai.MAX_A_DAY):
            RoundupIssue.objects.create(roundup=self.r, owner=self.me, scope=WEEK, period_start=date(2026, 1, 1 + n),
                                        period_end=date(2026, 1, 7 + n), payload={}, summary_source=AI)
        self.assertEqual(self.make().summary_source, RULES)
        self.provider.chat.assert_not_called()


class AiSheetTests(AiBase):
    def test_toggle_only_for_people_with_their_ai(self):
        self.client.login(username="nathan", password=PASSWORD)
        r = self.client.get(reverse("roundup:briefing"))
        self.assertContains(r, "Short version by my AI")
        self.assertContains(r, "Written by your AI · Anthropic · claude-test")
        self.client.post(reverse("roundup:schedule"), {f"r{self.r.pk}_on": "1", "push": "1"})
        self.r.refresh_from_db()
        self.assertFalse(self.r.ai_summary)
        self.client.post(reverse("roundup:schedule"), {f"r{self.r.pk}_on": "1", "ai": "1"})
        self.r.refresh_from_db()
        self.assertTrue(self.r.ai_summary)

    def test_no_toggle_and_no_effect_without_it(self):
        self.client.login(username="lexi", password=PASSWORD)
        self.assertNotContains(self.client.get(reverse("roundup:briefing")), "Short version by my AI")
        mine = schedule.preset(self.wife, WEEK)
        self.client.post(reverse("roundup:schedule"), {f"r{mine.pk}_on": "1", "ai": "1"})
        mine.refresh_from_db()
        self.assertFalse(mine.ai_summary)
