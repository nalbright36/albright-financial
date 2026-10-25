import re

from django import forms
from django.forms import modelformset_factory

from .models import EducationItem, Project, ResumeSettings, Skill, WorkExperience

MAX_PDF_BYTES = 5 * 1024 * 1024
MAX_IMAGE_BYTES = 2 * 1024 * 1024


def _style(form):
    for name, field in form.fields.items():
        widget = field.widget
        if isinstance(widget, (forms.CheckboxInput,)):
            continue
        widget.attrs.setdefault("class", "ledger-input")
        if isinstance(widget, forms.Textarea):
            widget.attrs.setdefault("rows", 4)


class StyledModelForm(forms.ModelForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        _style(self)


class SwitchesForm(StyledModelForm):
    class Meta:
        model = ResumeSettings
        fields = ["page_enabled", "chat_enabled"]


class ProfileForm(StyledModelForm):
    class Meta:
        model = ResumeSettings
        fields = ["full_name", "headline", "location", "open_to", "summary", "contact_email", "book_call_url",
                  "linkedin_url", "github_url"]
        widgets = {"summary": forms.Textarea(attrs={"rows": 6})}


class ChatbotForm(StyledModelForm):
    class Meta:
        model = ResumeSettings
        fields = ["knowledge_doc", "include_resume_in_chat", "off_limits_topics", "starter_questions",
                  "monthly_cap_usd", "max_message_chars", "max_messages_per_session", "per_ip_per_minute",
                  "per_ip_per_hour", "per_ip_per_day", "global_per_day"]
        widgets = {"knowledge_doc": forms.Textarea(attrs={"rows": 24, "style": "font-family: var(--font-mono); font-size: 0.8125rem;"}),
                   "off_limits_topics": forms.Textarea(attrs={"rows": 5}),
                   "starter_questions": forms.Textarea(attrs={"rows": 4})}

    LIMITS = {"monthly_cap_usd": (0, 100), "max_message_chars": (50, 2000), "max_messages_per_session": (1, 50),
              "per_ip_per_minute": (1, 30), "per_ip_per_hour": (1, 200), "per_ip_per_day": (1, 500),
              "global_per_day": (1, 5000)}

    def clean(self):
        data = super().clean()
        for name, (lo, hi) in self.LIMITS.items():
            value = data.get(name)
            if value is not None and not (lo <= value <= hi):
                self.add_error(name, f"Must be between {lo} and {hi}.")
        if len(data.get("knowledge_doc") or "") > 60_000:
            self.add_error("knowledge_doc", "Keep the knowledge document under 60,000 characters (cost control).")
        return data


class PDFUploadForm(forms.Form):
    pdf = forms.FileField(label="Resume PDF (max 5 MB)")

    def clean_pdf(self):
        f = self.cleaned_data["pdf"]
        if f.size > MAX_PDF_BYTES:
            raise forms.ValidationError("PDF must be 5 MB or smaller.")
        data = f.read()
        if not data.startswith(b"%PDF-"):
            raise forms.ValidationError("That file isn't a PDF.")
        name = re.sub(r"[^A-Za-z0-9._-]+", "-", f.name or "resume.pdf").strip("-.")[:80] or "resume"
        if not name.lower().endswith(".pdf"):
            name += ".pdf"
        return data, name


def sniff_image(data):
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


class ProjectForm(StyledModelForm):
    screenshot_file = forms.FileField(required=False, label="Screenshot (PNG/JPEG/WebP, max 2 MB)")
    remove_screenshot = forms.BooleanField(required=False, label="Remove current screenshot")

    class Meta:
        model = Project
        fields = ["title", "tagline", "description", "details_for_ai", "tech", "link_url", "repo_url",
                  "show_in_built", "published", "order", "screenshot_alt"]
        widgets = {"description": forms.Textarea(attrs={"rows": 4}), "details_for_ai": forms.Textarea(attrs={"rows": 10})}

    def clean_screenshot_file(self):
        f = self.cleaned_data.get("screenshot_file")
        if not f:
            return None
        if f.size > MAX_IMAGE_BYTES:
            raise forms.ValidationError("Screenshot must be 2 MB or smaller.")
        data = f.read()
        ctype = sniff_image(data)
        if not ctype:
            raise forms.ValidationError("Use a PNG, JPEG or WebP image.")
        return data, ctype

    def save(self, commit=True):
        project = super().save(commit=False)
        upload = self.cleaned_data.get("screenshot_file")
        if self.cleaned_data.get("remove_screenshot"):
            project.screenshot, project.screenshot_content_type = None, ""
        if upload:
            project.screenshot, project.screenshot_content_type = upload
        if commit:
            project.save()
        return project


class WorkForm(StyledModelForm):
    class Meta:
        model = WorkExperience
        fields = ["role", "organization", "location", "start_label", "end_label", "highlights", "order", "published"]


class EducationForm(StyledModelForm):
    class Meta:
        model = EducationItem
        fields = ["kind", "title", "institution", "year_label", "details", "order", "published"]
        widgets = {"details": forms.Textarea(attrs={"rows": 2})}


class SkillForm(StyledModelForm):
    class Meta:
        model = Skill
        fields = ["name", "category", "order", "published"]


WorkFormSet = modelformset_factory(WorkExperience, form=WorkForm, extra=1, can_delete=True)
EducationFormSet = modelformset_factory(EducationItem, form=EducationForm, extra=1, can_delete=True)
SkillFormSet = modelformset_factory(Skill, form=SkillForm, extra=3, can_delete=True)


class DraftReviewForm(forms.Form):
    summary = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 6}))
    skills = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 12}),
                             help_text="One per line, 'Category: Skill'. Edit freely.")
    apply_summary = forms.BooleanField(required=False, label="Replace my resume summary with this summary")
    apply_skills = forms.BooleanField(required=False, label="Add these skills to my skills list (skips duplicates)")
    publish_skills = forms.BooleanField(required=False, label="…and publish the new skills now (otherwise they're added hidden)")
    apply_projects = forms.BooleanField(required=False, label="Use the project summaries below as each project's public description")

    def __init__(self, *args, projects=(), **kwargs):
        super().__init__(*args, **kwargs)
        for pid, title, text in projects:
            self.fields[f"project_{pid}"] = forms.CharField(required=False, label=title, initial=text,
                                                             widget=forms.Textarea(attrs={"rows": 3}))
        _style(self)
