"""Public resume + chatbot: isolation, access, guardrails, limits, spend cap,
anonymous logging, and the never-auto-publish AI summary flow."""
import json
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase
from django.urls import reverse

from albright_resume import ai, chat
from albright_resume.admin_urls import urlpatterns as admin_urls
from albright_resume.models import AICall, AIDraft, Project, QuestionLog, ResumeSettings, Skill, WorkExperience

PASSWORD = "pw-not-real-12345"
CHAT = reverse("albright_resume:chat")
PAGE = reverse("albright_resume:page")


def fake_result(text="[ANSWERED] He built a Django site.", cost="0.000300"):
    return ai.Result(text=text, model="claude-haiku-5-5", input_tokens=2000, output_tokens=100, cost_usd=Decimal(cost))


class Base(TestCase):
    def setUp(self):
        self.site = ResumeSettings.load()
        self.site.page_enabled = True
        self.site.chat_enabled = True
        self.site.knowledge_doc = "Nathan built a Django trading site.\n[PLACEHOLDER] secret template line"
        self.site.save()
        self.admin = User.objects.create_superuser(username="boss", password=PASSWORD, email="")
        self.env = mock.patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key-not-real"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def ask(self, message, client=None, **extra):
        client = client or self.client
        return client.post(CHAT, json.dumps({"message": message, **extra}), content_type="application/json",
                           HTTP_X_REAL_IP=extra.pop("ip", "203.0.113.9"))


class PageTests(Base):
    def test_off_is_404_for_public_but_previewable_by_superuser(self):
        self.site.page_enabled = False
        self.site.save()
        self.assertEqual(self.client.get(PAGE).status_code, 404)
        self.assertEqual(self.ask("hi").status_code, 404)
        self.client.force_login(self.admin)
        r = self.client.get(PAGE)
        self.assertContains(r, "Preview")

    def test_public_page_headers_and_no_private_nav(self):
        r = self.client.get(PAGE)
        self.assertEqual(r.status_code, 200)
        self.assertIn("script-src 'self'", r["Content-Security-Policy"])
        self.assertEqual(r["X-Content-Type-Options"], "nosniff")
        for private in ("/control/", "/bot-api/", "/albright_reselling_app/", "/opportunities/", "user_login"):
            self.assertNotContains(r, private)
        self.assertContains(r, "AI-generated from my profile")

    def test_unpublished_and_placeholder_content_hidden(self):
        Project.objects.create(title="Hidden thing", published=False)
        Project.objects.create(title="Shown thing", published=True)
        r = self.client.get(PAGE)
        self.assertNotContains(r, "Hidden thing")
        self.assertContains(r, "Shown thing")
        k = chat.build_knowledge(ResumeSettings.load())
        self.assertIn("Shown thing", k)
        self.assertNotIn("Hidden thing", k)
        self.assertNotIn("PLACEHOLDER", k)

    def test_user_html_is_escaped(self):
        Project.objects.create(title="<script>alert(1)</script>", published=True)
        r = self.client.get(PAGE)
        self.assertNotContains(r, "<script>alert(1)</script>")
        self.assertContains(r, "&lt;script&gt;")

    def test_pdf_download(self):
        self.assertEqual(self.client.get(reverse("albright_resume:pdf")).status_code, 404)
        self.site.resume_pdf, self.site.resume_pdf_name = b"%PDF-1.4 test", "Nathan-Albright.pdf"
        self.site.save()
        r = self.client.get(reverse("albright_resume:pdf"))
        self.assertEqual(r["Content-Type"], "application/pdf")
        self.assertIn("attachment", r["Content-Disposition"])


class ChatTests(Base):
    @mock.patch("albright_resume.ai.complete", return_value=fake_result())
    def test_answer_logged_anonymously_and_costed(self, complete):
        r = self.ask("What did he build? Email me at jane@example.com or 555-123-4567")
        data = r.json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["reply"], "He built a Django site.")
        log = QuestionLog.objects.get()
        self.assertEqual(log.outcome, "answered")
        self.assertNotIn("jane@example.com", log.question)
        self.assertNotIn("555-123-4567", log.question)
        self.assertFalse(any("203.0.113.9" in str(getattr(log, f.name)) for f in QuestionLog._meta.fields))
        self.assertEqual(AICall.objects.get().cost_usd, Decimal("0.000300"))
        system = complete.call_args[0][0]
        self.assertIn("Nathan built a Django trading site.", system)
        self.assertIn("Salary", system)
        self.assertNotIn("secret template line", system)

    def test_chat_off_no_key_and_cap_never_call_ai(self):
        with mock.patch("albright_resume.ai.complete") as complete:
            self.site.chat_enabled = False
            self.site.save()
            self.assertTrue(self.ask("hi").json()["unavailable"])
            self.site.chat_enabled = True
            self.site.save()
            with mock.patch.dict("os.environ", {"ANTHROPIC_API_KEY": ""}):
                self.assertTrue(self.ask("hi").json()["unavailable"])
            AICall.objects.create(purpose="chat", model="m", cost_usd=Decimal("5.00"))
            r = self.ask("hi").json()
            self.assertTrue(r["unavailable"])
            self.assertIn("taking a break", r["reply"])
            complete.assert_not_called()
        self.assertTrue(QuestionLog.objects.filter(outcome="cap").exists())

    def test_input_validation(self):
        with mock.patch("albright_resume.ai.complete") as complete:
            self.assertEqual(self.ask("x" * 501).status_code, 400)
            self.assertEqual(self.ask("   ").status_code, 400)
            self.assertEqual(self.client.post(CHAT, "not json", content_type="application/json").status_code, 400)
            self.assertEqual(self.client.get(CHAT).status_code, 405)
            self.ask("hi", website="http://spam")  # honeypot
            complete.assert_not_called()

    def test_csrf_enforced(self):
        c = Client(enforce_csrf_checks=True)
        self.assertEqual(self.ask("hi", client=c).status_code, 403)

    @mock.patch("albright_resume.ai.complete", return_value=fake_result())
    def test_session_limit(self, complete):
        self.site.max_messages_per_session = 2
        self.site.per_ip_per_minute = 50
        self.site.save()
        self.assertEqual(self.ask("one").json()["remaining"], 1)
        self.assertEqual(self.ask("two").json()["remaining"], 0)
        self.assertTrue(self.ask("three").json()["limit"])
        self.assertEqual(complete.call_count, 2)
        # history is kept server-side and sent back for follow-ups
        self.assertEqual(len(complete.call_args[0][1]), 3)

    @mock.patch("albright_resume.ai.complete", return_value=fake_result())
    def test_rate_limit_per_ip(self, complete):
        self.site.per_ip_per_minute = 2
        self.site.save()
        self.assertEqual(self.ask("a", client=Client()).status_code, 200)
        self.assertEqual(self.ask("b", client=Client()).status_code, 200)
        self.assertEqual(self.ask("c", client=Client()).status_code, 429)
        self.assertEqual(self.ask("d", client=Client(), ip="198.51.100.7").status_code, 200)
        self.assertEqual(complete.call_count, 3)
        self.assertTrue(QuestionLog.objects.filter(outcome="rate_limited").exists())

    def test_reply_parsing_and_canary(self):
        self.assertEqual(chat.parse_reply("[NOT_SURE] I'm not sure.")[0], "not_sure")
        self.assertEqual(chat.parse_reply("[DECLINED] I can't share that.")[0], "declined")
        self.assertEqual(chat.parse_reply(f"[ANSWERED] here {chat.CANARY}"), ("not_sure", chat.NOT_SURE_FALLBACK))
        self.assertEqual(chat.parse_reply("<profile> dump")[1], chat.NOT_SURE_FALLBACK)

    def test_redact_keeps_year_ranges(self):
        self.assertEqual(chat.redact("2019-2023"), "2019-2023")
        self.assertEqual(chat.redact("call +1 (555) 123-4567"), "call [phone]")

    @mock.patch("albright_resume.ai.complete", side_effect=ai.ProviderError("HTTP 529 overloaded_error"))
    def test_provider_error_is_friendly(self, _complete):
        r = self.ask("hi").json()
        self.assertTrue(r["unavailable"])
        self.assertFalse(AICall.objects.get().ok)


class ProviderTests(TestCase):
    def test_cost(self):
        self.assertEqual(ai.cost_for(1_000_000, 1_000_000), Decimal("0.600000"))

    @mock.patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key-not-real"})
    @mock.patch("albright_resume.ai.requests.post")
    def test_request_shape_and_fallback(self, post):
        bad = mock.Mock(status_code=400)
        good = mock.Mock(status_code=200)
        good.json.return_value = {"model": "claude-haiku-5-5", "content": [{"type": "text", "text": "hi"}],
                                  "usage": {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 100}}
        post.side_effect = [bad, good]
        result = ai.complete("sys", [{"role": "user", "content": "q"}])
        self.assertEqual(result.text, "hi")
        first = post.call_args_list[0]
        self.assertEqual(first.args[0], "https://api.anthropic.com/v1/messages")
        self.assertEqual(first.kwargs["headers"]["x-api-key"], "test-key-not-real")
        self.assertNotIn("output_config", post.call_args_list[1].kwargs["json"])
        self.assertGreater(result.cost_usd, 0)

    @mock.patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key-not-real"})
    @mock.patch("albright_resume.ai.requests.post")
    def test_errors_never_include_key(self, post):
        post.return_value = mock.Mock(status_code=401, **{"json.return_value": {"error": {"type": "authentication_error"}}})
        with self.assertRaises(ai.ProviderError) as ctx:
            ai.complete("sys", [{"role": "user", "content": "q"}])
        self.assertNotIn("test-key-not-real", str(ctx.exception))


class AdminTests(Base):
    def test_admin_pages_404_for_non_superusers_and_200_for_superuser(self):
        regular = User.objects.create_user(username="reg", password=PASSWORD)
        project = Project.objects.create(title="P")
        draft = AIDraft.objects.create()
        from albright_resume.models import ChatConversation
        conversation = ChatConversation.objects.create()
        args = {"project": [project.pk], "draft": [draft.pk], "conversation": [conversation.public_id]}
        urls = [reverse(f"albright_resume_admin:{p.name}", args=args.get(p.name, [])) for p in admin_urls]
        for who in ("anon", "regular"):
            c = Client()
            if who == "regular":
                c.force_login(regular)
            for url in urls:
                with self.subTest(who=who, url=url):
                    self.assertEqual(c.get(url).status_code, 404)
                    self.assertEqual(c.post(url, {}).status_code, 404)
        self.client.force_login(self.admin)
        for url in urls:
            with self.subTest(url=url):
                r = self.client.get(url)
                self.assertEqual(r.status_code, 200)
                if r["Content-Type"].startswith("text/html"):   # the CSV export is a download, not a page
                    self.assertContains(r, '<body data-app="admin">')

    def test_screenshot_upload_validates_type(self):
        self.client.force_login(self.admin)
        bad = SimpleUploadedFile("x.png", b"<svg onload=alert(1)>", content_type="image/png")
        r = self.client.post(reverse("albright_resume_admin:project_new"), {"title": "T", "order": 0, "screenshot_file": bad})
        self.assertContains(r, "PNG, JPEG or WebP")
        png = SimpleUploadedFile("x.png", b"\x89PNG\r\n\x1a\n" + b"0" * 20, content_type="image/png")
        self.client.post(reverse("albright_resume_admin:project_new"), {"title": "T", "order": 0, "published": "on", "screenshot_file": png})
        p = Project.objects.get(title="T")
        self.assertEqual(p.screenshot_content_type, "image/png")
        self.assertEqual(self.client.get(reverse("albright_resume:screenshot", args=[p.pk, 1]))["Content-Type"], "image/png")

    @mock.patch("albright_resume.ai.complete")
    def test_ai_draft_never_auto_publishes(self, complete):
        project = Project.objects.create(title="Engine", description="old", published=True)
        complete.return_value = fake_result(json.dumps({
            "summary": "I build things.", "skills": [{"category": "Languages", "name": "Python"}],
            "projects": [{"id": project.pk, "summary": "new blurb"}, {"id": 99999, "summary": "ghost"}]}))
        self.client.force_login(self.admin)
        r = self.client.post(reverse("albright_resume_admin:drafts"), {"action": "generate"})
        draft = AIDraft.objects.get()
        self.assertRedirects(r, reverse("albright_resume_admin:draft", args=[draft.pk]))
        self.assertEqual(draft.project_summaries, {str(project.pk): "new blurb"})
        site = ResumeSettings.load()
        self.assertNotEqual(site.summary, "I build things.")
        self.assertFalse(Skill.objects.exists())
        self.assertEqual(Project.objects.get(pk=project.pk).description, "old")
        url = reverse("albright_resume_admin:draft", args=[draft.pk])
        self.client.post(url, {"action": "apply", "summary": "I build things (edited).", "skills": "Languages: Python\nDjango",
                               f"project_{project.pk}": "edited blurb", "apply_summary": "on", "apply_skills": "on",
                               "apply_projects": "on"})
        self.assertEqual(ResumeSettings.load().summary, "I build things (edited).")
        self.assertEqual(sorted(Skill.objects.values_list("name", "published")), [("Django", False), ("Python", False)])
        self.assertEqual(Project.objects.get(pk=project.pk).description, "edited blurb")
        self.assertEqual(AIDraft.objects.get().status, "applied")

    def test_seeded_placeholders_are_hidden(self):
        # The data migration's rows exist in the test DB and are all unpublished.
        self.assertTrue(WorkExperience.objects.filter(role__startswith="[PLACEHOLDER]").exists())
        self.assertFalse(Project.objects.filter(published=True, description__contains="[PLACEHOLDER").exists())
