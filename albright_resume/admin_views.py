"""Nathan's management pages for the public resume, under /control/resume/.
Superuser-only (404 for everyone else) using the Admin area's own guard, and
every change is written to the Admin audit log."""
import csv
from datetime import datetime, time, timedelta

from django.contrib import messages
from django.core.paginator import Paginator
from django.db.models import Count, Q, Sum
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from albright_control.access import audit, superuser_required

from . import ai, chat, conversations as conv_records, summarize
from .forms import (ChatbotForm, DraftReviewForm, EducationFormSet, PDFUploadForm, ProfileForm, ProjectForm,
                    SkillFormSet, SwitchesForm, WorkFormSet)
from .models import (PLACEHOLDER, AICall, AIDraft, ChatConversation, ChatMessage, EducationItem, Project,
                     QuestionLog, ResumeSettings, Skill, WorkExperience)


def _placeholder_count():
    site = ResumeSettings.load()
    count = sum(PLACEHOLDER in (getattr(site, f) or "") for f in ("headline", "summary", "knowledge_doc", "open_to"))
    for model, fields in ((Project, ("title", "tagline", "description", "details_for_ai")),
                          (WorkExperience, ("role", "organization", "highlights")),
                          (EducationItem, ("title", "institution", "details"))):
        for obj in model.objects.all():
            count += any(PLACEHOLDER in (getattr(obj, f) or "") for f in fields)
    return count


@superuser_required
def overview(request):
    site = ResumeSettings.load()
    if request.method == "POST":
        form = SwitchesForm(request.POST, instance=site)
        if form.is_valid():
            form.save()
            audit(request, "resume_switches", f"page={'on' if site.page_enabled else 'off'} chat={'on' if site.chat_enabled else 'off'}")
            messages.success(request, "Saved.")
            return redirect("albright_resume_admin:overview")
    else:
        form = SwitchesForm(instance=site)
    week = timezone.now() - timedelta(days=7)
    spend = chat.month_spend()
    return render(request, "albright_resume/admin/overview.html", {
        "site": site, "form": form, "spend": spend,
        "cap_pct": min(int(spend / site.monthly_cap_usd * 100), 100) if site.monthly_cap_usd else 100,
        "key_set": ai.api_key_configured(), "model": ai.config()["MODEL"],
        "placeholders": _placeholder_count(),
        "week_questions": QuestionLog.objects.filter(created_at__gte=week).count(),
        "week_conversations": ChatConversation.objects.filter(started_at__gte=week).count(),
        "unreviewed": ChatConversation.objects.filter(reviewed=False).count(),
        "gap_count": len(conv_records.knowledge_gaps()),
        "counts": {"jobs": WorkExperience.objects.filter(published=True).count(),
                   "projects": Project.objects.filter(published=True).count(),
                   "skills": Skill.objects.filter(published=True).count(),
                   "education": EducationItem.objects.filter(published=True).count()},
    })


@superuser_required
def profile(request):
    site = ResumeSettings.load()
    form = ProfileForm(request.POST or None, instance=site)
    pdf_form = PDFUploadForm()
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "upload_pdf":
            pdf_form = PDFUploadForm(request.POST, request.FILES)
            form = ProfileForm(instance=site)
            if pdf_form.is_valid():
                site.resume_pdf, site.resume_pdf_name = pdf_form.cleaned_data["pdf"]
                site.resume_pdf_updated_at = timezone.now()
                site.save()
                audit(request, "resume_pdf_upload", site.resume_pdf_name)
                messages.success(request, "PDF uploaded.")
                return redirect("albright_resume_admin:profile")
        elif action == "remove_pdf":
            site.resume_pdf, site.resume_pdf_name, site.resume_pdf_updated_at = None, "", None
            site.save()
            audit(request, "resume_pdf_remove")
            messages.success(request, "PDF removed.")
            return redirect("albright_resume_admin:profile")
        elif form.is_valid():
            form.save()
            audit(request, "resume_profile_edit")
            messages.success(request, "Profile saved.")
            return redirect("albright_resume_admin:profile")
    return render(request, "albright_resume/admin/profile.html", {"site": site, "form": form, "pdf_form": pdf_form})


def _formset_page(request, formset_class, queryset, title, intro, action, url_name):
    formset = formset_class(request.POST or None, queryset=queryset)
    if request.method == "POST" and formset.is_valid():
        formset.save()
        audit(request, action)
        messages.success(request, f"{title} saved.")
        return redirect(f"albright_resume_admin:{url_name}")
    return render(request, "albright_resume/admin/formset.html", {"formset": formset, "title": title, "intro": intro})


@superuser_required
def experience(request):
    return _formset_page(request, WorkFormSet, WorkExperience.objects.all(), "Work history",
                         "Your real roles only. Unpublished rows stay hidden from the page and the chatbot.",
                         "resume_work_edit", "experience")


@superuser_required
def education(request):
    return _formset_page(request, EducationFormSet, EducationItem.objects.all(), "Education & certifications",
                         "Degrees, courses and certifications. Unpublished rows stay hidden.", "resume_education_edit",
                         "education")


@superuser_required
def skills(request):
    return _formset_page(request, SkillFormSet, Skill.objects.all(), "Skills",
                         "Grouped by category on the page. Use 'AI summary' to draft a list from your projects.",
                         "resume_skills_edit", "skills")


@superuser_required
def projects(request):
    return render(request, "albright_resume/admin/projects.html", {"projects": Project.objects.all()})


@superuser_required
def project_edit(request, pk=None):
    project = get_object_or_404(Project, pk=pk) if pk else None
    if request.method == "POST" and request.POST.get("action") == "delete" and project:
        title = project.title
        project.delete()
        audit(request, "resume_project_delete", title)
        messages.success(request, f"Deleted “{title}”.")
        return redirect("albright_resume_admin:projects")
    form = ProjectForm(request.POST or None, request.FILES or None, instance=project)
    if request.method == "POST" and form.is_valid():
        saved = form.save()
        audit(request, "resume_project_add" if not pk else "resume_project_edit", saved.title)
        messages.success(request, "Project saved.")
        return redirect("albright_resume_admin:project", pk=saved.pk)
    return render(request, "albright_resume/admin/project_edit.html", {"form": form, "project": project})


@superuser_required
def chatbot(request):
    site = ResumeSettings.load()
    form = ChatbotForm(request.POST or None, instance=site)
    if request.method == "POST" and form.is_valid():
        form.save()
        audit(request, "resume_chatbot_edit")
        messages.success(request, "Chatbot settings saved.")
        return redirect("albright_resume_admin:chatbot")
    knowledge = chat.build_knowledge(site)
    return render(request, "albright_resume/admin/chatbot.html", {
        "form": form, "site": site, "knowledge": knowledge, "knowledge_chars": len(knowledge),
        "est_tokens": len(chat.system_prompt(site, knowledge)) // 4,
    })


@superuser_required
def drafts(request):
    site = ResumeSettings.load()
    if request.method == "POST" and request.POST.get("action") == "generate":
        try:
            draft = summarize.generate_draft(site, request.user)
        except summarize.SummarizeError as exc:
            messages.error(request, str(exc))
            return redirect("albright_resume_admin:drafts")
        audit(request, "resume_ai_draft", f"draft #{draft.pk}")
        messages.success(request, "Draft ready. Review and edit it. Nothing changes on your page until you press Apply.")
        return redirect("albright_resume_admin:draft", pk=draft.pk)
    return render(request, "albright_resume/admin/drafts.html", {
        "drafts": AIDraft.objects.all()[:30], "key_set": ai.api_key_configured(),
        "project_count": Project.objects.count()})


@superuser_required
def draft(request, pk):
    draft = get_object_or_404(AIDraft, pk=pk)
    titles = dict(Project.objects.values_list("id", "title"))
    rows = [(pid, titles[int(pid)], text) for pid, text in draft.project_summaries.items() if int(pid) in titles]
    form = DraftReviewForm(request.POST or None, projects=rows,
                           initial={"summary": draft.summary, "skills": draft.skills})
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "discard":
            draft.status = "discarded"
            draft.save(update_fields=["status"])
            messages.success(request, "Draft discarded.")
            return redirect("albright_resume_admin:drafts")
        if form.is_valid():
            data = form.cleaned_data
            draft.summary, draft.skills = data["summary"], data["skills"]
            draft.project_summaries = {str(pid): data.get(f"project_{pid}", "") for pid, _, _ in rows}
            changes = []
            if action == "apply":
                site = ResumeSettings.load()
                if data["apply_summary"] and data["summary"].strip():
                    site.summary = data["summary"].strip()
                    site.save()
                    changes.append("summary")
                if data["apply_skills"]:
                    existing = {n.lower() for n in Skill.objects.values_list("name", flat=True)}
                    added = 0
                    for category, name in summarize.parse_skill_lines(data["skills"]):
                        if name and name.lower() not in existing:
                            Skill.objects.create(name=name, category=category, published=data["publish_skills"])
                            existing.add(name.lower())
                            added += 1
                    changes.append(f"{added} skills" + (" (published)" if data["publish_skills"] else " (hidden)"))
                if data["apply_projects"]:
                    for pid, _, _ in rows:
                        text = (data.get(f"project_{pid}") or "").strip()
                        if text:
                            Project.objects.filter(pk=pid).update(description=text, ai_summary=text)
                    changes.append("project descriptions")
                if changes:
                    draft.status, draft.applied_at = "applied", timezone.now()
                    audit(request, "resume_ai_apply", f"draft #{draft.pk}: " + ", ".join(changes))
                    messages.success(request, "Applied: " + ", ".join(changes) + ".")
                else:
                    messages.info(request, "Tick at least one 'apply' box. Edits saved to the draft.")
            else:
                messages.success(request, "Draft saved.")
            draft.save()
            return redirect("albright_resume_admin:draft", pk=draft.pk)
    return render(request, "albright_resume/admin/draft.html", {"draft": draft, "form": form, "rows": rows})


def _outcome_rows():
    counts = dict(QuestionLog.objects.order_by().values_list("outcome").annotate(n=Count("id")))
    return [(key, label, counts.get(key, 0)) for key, label in QuestionLog.OUTCOMES]


@superuser_required
def questions(request):
    if request.method == "POST" and request.POST.get("action") == "clear":
        deleted, _ = QuestionLog.objects.all().delete()
        audit(request, "resume_questions_clear", f"{deleted} rows")
        messages.success(request, f"Deleted {deleted} logged questions.")
        return redirect("albright_resume_admin:questions")
    outcome = request.GET.get("outcome", "")
    qs = QuestionLog.objects.all()
    if outcome in dict(QuestionLog.OUTCOMES):
        qs = qs.filter(outcome=outcome)
    page = Paginator(qs, 50).get_page(request.GET.get("page"))
    month = chat.month_start()
    usage = AICall.objects.filter(created_at__gte=month).values("purpose").annotate(
        calls=Count("id"), cost=Sum("cost_usd"), tin=Sum("input_tokens"), tout=Sum("output_tokens"),
        tcache=Sum("cache_read_tokens")).order_by("purpose")
    return render(request, "albright_resume/admin/questions.html", {
        "page": page, "outcome": outcome, "outcomes": QuestionLog.OUTCOMES,
        "outcome_rows": _outcome_rows(),
        "usage": usage, "spend": chat.month_spend(), "site": ResumeSettings.load(),
        "failures": AICall.objects.filter(created_at__gte=month, ok=False).count(),
    })


# ---------------------------------------------------------------------------
# Conversations (anonymous records - see conversations.py)
# ---------------------------------------------------------------------------

def _parse_date(value):
    try:
        return datetime.strptime(value or "", "%Y-%m-%d").date()
    except ValueError:
        return None


def _filtered_conversations(params):
    """The list's filters (flag, reviewed, date range) - shared with the CSV export."""
    qs = ChatConversation.objects.all()
    flag = params.get("flag", "")
    if flag in dict(ChatConversation.FLAGS):
        qs = qs.filter(flags__contains=[flag])
    reviewed = params.get("reviewed", "")
    if reviewed in ("yes", "no"):
        qs = qs.filter(reviewed=reviewed == "yes")
    start, end = _parse_date(params.get("start")), _parse_date(params.get("end"))
    tz = timezone.get_current_timezone()
    if start:
        qs = qs.filter(started_at__gte=datetime.combine(start, time.min, tzinfo=tz))
    if end:
        qs = qs.filter(started_at__lt=datetime.combine(end + timedelta(days=1), time.min, tzinfo=tz))
    filters = {"flag": flag if flag in dict(ChatConversation.FLAGS) else "", "reviewed": reviewed,
               "start": start.isoformat() if start else "", "end": end.isoformat() if end else ""}
    return qs.order_by("-started_at"), filters


@superuser_required
def conversations(request):
    if request.method == "POST" and request.POST.get("action") == "delete_all":
        if request.POST.get("confirm") != "DELETE":
            messages.error(request, 'Type DELETE to confirm deleting every conversation.')
        else:
            deleted = ChatConversation.objects.count()
            ChatConversation.objects.all().delete()
            audit(request, "resume_conversations_delete_all", f"{deleted} conversations")
            messages.success(request, f"Deleted {deleted} conversation{'s' if deleted != 1 else ''}.")
        return redirect("albright_resume_admin:conversations")
    qs, filters = _filtered_conversations(request.GET)
    page = Paginator(qs, 40).get_page(request.GET.get("page"))
    query = "&".join(f"{k}={v}" for k, v in filters.items() if v)
    return render(request, "albright_resume/admin/conversations.html", {
        "page": page, "filters": filters, "flag_choices": ChatConversation.FLAGS, "query": query,
        "total": qs.count(), "all_count": ChatConversation.objects.count(), "site": ResumeSettings.load(),
    })


@superuser_required
def conversation(request, public_id):
    conv = get_object_or_404(ChatConversation, public_id=public_id)
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "delete":
            conv.delete()
            audit(request, "resume_conversation_delete", str(public_id)[:8])
            messages.success(request, "Conversation deleted.")
            return redirect("albright_resume_admin:conversations")
        if action == "review":
            conv.reviewed = not conv.reviewed
            conv.save(update_fields=["reviewed"])
            messages.success(request, "Marked reviewed." if conv.reviewed else "Marked not reviewed.")
        elif action == "notes":
            conv.my_notes = (request.POST.get("my_notes") or "")[:5000]
            conv.save(update_fields=["my_notes"])
            messages.success(request, "Notes saved.")
        return redirect("albright_resume_admin:conversation", public_id=public_id)
    msgs = list(conv.messages.all())
    return render(request, "albright_resume/admin/conversation.html", {
        "conv": conv, "messages_list": msgs,
        "total_cost": sum((m.cost_usd for m in msgs), start=0), "total_tokens": sum(m.tokens for m in msgs),
    })


@superuser_required
def knowledge_gaps(request):
    return render(request, "albright_resume/admin/knowledge_gaps.html", {
        "gaps": conv_records.knowledge_gaps(), "site": ResumeSettings.load(),
    })


@superuser_required
def conversations_csv(request):
    """One row per message of the filtered conversations (same filters as the list)."""
    qs, filters = _filtered_conversations(request.GET)
    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = 'attachment; filename="resume_conversations.csv"'
    writer = csv.writer(response)
    writer.writerow(["conversation", "started_at", "referrer", "device", "flags", "reviewed", "my_notes",
                     "message_at", "role", "outcome", "text", "tokens", "cost_usd"])
    rows = 0
    for conv in qs.prefetch_related("messages"):
        for m in conv.messages.all():
            writer.writerow([str(conv.public_id)[:8], conv.started_at.isoformat(), conv.referrer_domain, conv.device,
                             "; ".join(conv.flag_labels), "yes" if conv.reviewed else "no", conv.my_notes,
                             m.created_at.isoformat(), m.role, m.get_outcome_display() if m.outcome else "",
                             m.text, m.tokens, f"{m.cost_usd:.6f}"])
            rows += 1
    audit(request, "resume_conversations_export", f"{qs.count()} conversations, {rows} messages")
    return response
