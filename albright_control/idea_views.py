"""Admin > Ideas: list (filters, search, sortable, Quick wins), add,
detail (edit, status buttons, notes, merge, delete, Claude Code prompt),
and "Move to Ideas" from the Bot suggestions inbox. Superusers only."""
from urllib.parse import urlencode

from django import forms
from django.contrib import messages
from django.db.models import Case, IntegerField, Q, Value, When
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from albright_bot_api.models import SiteSuggestion

from .access import audit, superuser_required
from .ideas import claude_code_prompt, find_duplicate, from_suggestion, merge
from .models import Idea

INPUT = {"class": "ledger-input"}
RANKS = {   # for sorting choice columns by meaning, not alphabetically
    "priority": {"low": 0, "medium": 1, "high": 2, "urgent": 3},
    "impact": {"low": 0, "medium": 1, "high": 2},
    "effort": {"small": 0, "medium": 1, "large": 2},
    "status": {k: i for i, (k, _l) in enumerate(Idea.STATUSES)},
}
SORTS = ["title", "app", "category", "priority", "impact", "effort", "status", "created"]


def _rank(field):
    return Case(*[When(**{field: k}, then=Value(v)) for k, v in RANKS[field].items()], default=Value(-1),
                output_field=IntegerField())


def _lines(text):
    return [part.strip() for part in (text or "").replace(",", "\n").splitlines() if part.strip()]


class IdeaForm(forms.ModelForm):
    tags_text = forms.CharField(required=False, label="Tags", widget=forms.TextInput(attrs={**INPUT, "placeholder": "comma-separated"}))
    links_text = forms.CharField(required=False, label="Links", widget=forms.Textarea(attrs={**INPUT, "rows": 2, "placeholder": "one URL per line"}))

    class Meta:
        model = Idea
        fields = ["title", "description", "app", "category", "priority", "impact", "effort", "status",
                  "target_date", "my_notes"]
        widgets = {"description": forms.Textarea(attrs={**INPUT, "rows": 5}),
                   "my_notes": forms.Textarea(attrs={**INPUT, "rows": 4}),
                   "target_date": forms.DateInput(attrs={**INPUT, "type": "date"}, format="%Y-%m-%d")}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name, field in self.fields.items():
            field.widget.attrs.setdefault("class", "ledger-input")
        if self.instance.pk:
            self.fields["tags_text"].initial = ", ".join(self.instance.tags or [])
            self.fields["links_text"].initial = "\n".join(self.instance.links or [])

    def clean_links_text(self):
        from albright_bot_api.links_check import is_http_url
        links = [line.strip() for line in (self.cleaned_data["links_text"] or "").splitlines() if line.strip()]
        bad = [link for link in links if not is_http_url(link)]
        if bad:
            raise forms.ValidationError(f"Not an http(s) link: {bad[0]}")
        return list(dict.fromkeys(links))

    def clean(self):
        cleaned = super().clean()
        for name in ("title", "description", "my_notes"):
            if len(cleaned.get(name) or "") > 5000:
                self.add_error(name, "At most 5,000 characters.")
        return cleaned

    def save(self, commit=True):
        self.instance.tags = list(dict.fromkeys(_lines(self.cleaned_data.get("tags_text"))))[:20]
        self.instance.links = self.cleaned_data.get("links_text") or []
        return super().save(commit=commit)


class NewIdeaForm(IdeaForm):
    original_message = forms.CharField(required=False, label="Original message (optional)",
                                       widget=forms.Textarea(attrs={**INPUT, "rows": 3}))

    class Meta(IdeaForm.Meta):
        fields = ["title", "description", "app", "category", "priority", "impact", "effort", "target_date", "my_notes"]


@superuser_required
def ideas(request):
    qs = Idea.objects.all()
    f = {key: request.GET.get(key, "") for key in ("app", "category", "priority", "status", "tag", "q", "view")}
    choices = {"app": Idea.APPS, "category": Idea.CATEGORIES, "priority": Idea.PRIORITIES, "status": Idea.STATUSES}
    for key, options in choices.items():
        if f[key] in dict(options):
            qs = qs.filter(**{key: f[key]})
    if f["tag"]:
        qs = qs.filter(tags__contains=[f["tag"]])
    if f["q"]:
        qs = qs.filter(Q(title__icontains=f["q"]) | Q(description__icontains=f["q"]) |
                       Q(original_message__icontains=f["q"]) | Q(my_notes__icontains=f["q"]))
    sort = request.GET.get("sort", "")
    direction = "asc" if request.GET.get("dir") == "asc" else "desc"
    if f["view"] == "quick":
        # Quick wins: open ideas, high impact + small effort first.
        qs = (qs.exclude(status__in=Idea.CLOSED_STATUSES)
              .annotate(quick=Case(When(impact="high", effort="small", then=Value(1)), default=Value(0),
                                   output_field=IntegerField()),
                        impact_rank=_rank("impact"), effort_rank=_rank("effort"), priority_rank=_rank("priority"))
              .order_by("-quick", "-impact_rank", "effort_rank", "-priority_rank", "-created_at"))
    elif sort in SORTS:
        prefix = "" if direction == "asc" else "-"
        if sort in RANKS:
            qs = qs.annotate(sort_rank=_rank(sort)).order_by(f"{prefix}sort_rank", "-created_at")
        else:
            qs = qs.order_by(f"{prefix}{'created_at' if sort == 'created' else sort}", "-pk")
    base = {k: v for k, v in f.items() if v and k != "view"}
    headers = []
    for key in SORTS:
        next_dir = "asc" if sort == key and direction == "desc" else "desc"
        headers.append({"key": key, "label": key.capitalize() if key != "created" else "Created",
                        "url": "?" + urlencode({**base, "sort": key, "dir": next_dir}),
                        "active": sort == key and f["view"] != "quick", "dir": direction})
    all_tags = sorted({t for tags in Idea.objects.values_list("tags", flat=True) for t in (tags or [])})
    return render(request, "albright_control/ideas.html", {
        "ideas": qs[:500], "filters": f, "choices": choices, "headers": headers, "tags": all_tags,
        "quick_url": "?" + urlencode({**base, "view": "quick"}), "all_url": "?" + urlencode(base),
        "count": qs.count(),
    })


@superuser_required
def idea_add(request):
    form = NewIdeaForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        idea = form.save(commit=False)
        idea.source, idea.status = "manual", "new"
        idea.original_message = form.cleaned_data.get("original_message") or ""
        idea.possible_duplicate_of = find_duplicate(idea.title)
        idea.save()
        audit(request, "idea_created", f'#{idea.pk} "{idea.title[:80]}" (manual)')
        messages.success(request, "Idea added." + (f" It looks like #{idea.possible_duplicate_of_id} - merge?" if idea.possible_duplicate_of_id else ""))
        return redirect("albright_control:idea", pk=idea.pk)
    return render(request, "albright_control/idea_add.html", {"form": form})


@superuser_required
def idea_detail(request, pk):
    idea = get_object_or_404(Idea.objects.select_related("possible_duplicate_of", "token"), pk=pk)
    form = IdeaForm(instance=idea)
    if request.method == "POST":
        action = request.POST.get("action")
        old_status = idea.status
        if action == "status" and request.POST.get("status") in dict(Idea.STATUSES):
            idea.status = request.POST["status"]
            idea.save()
        elif action == "edit":
            form = IdeaForm(request.POST, instance=idea)
            if not form.is_valid():
                return render(request, "albright_control/idea.html", _detail_context(idea, form))
            idea = form.save()
        elif action == "merge":
            target = Idea.objects.filter(pk=request.POST.get("into")).exclude(pk=idea.pk).first()
            if target is None:
                messages.error(request, "Pick the idea to merge into.")
                return redirect("albright_control:idea", pk=idea.pk)
            merge(target, idea, user=request.user)
            messages.success(request, f"Merged into #{target.pk} - both original messages kept.")
            return redirect("albright_control:idea", pk=target.pk)
        elif action == "delete":
            audit(request, "idea_deleted", f'#{idea.pk} "{idea.title[:80]}"')
            idea.delete()
            messages.success(request, "Idea deleted.")
            return redirect("albright_control:ideas")
        if idea.status != old_status:
            audit(request, "idea_status", f'#{idea.pk} "{idea.title[:80]}": {old_status} -> {idea.status}')
        messages.success(request, "Saved.")
        return redirect("albright_control:idea", pk=idea.pk)
    return render(request, "albright_control/idea.html", _detail_context(idea, form))


def _detail_context(idea, form):
    return {"idea": idea, "form": form, "prompt": claude_code_prompt(idea), "statuses": Idea.STATUSES,
            "merge_targets": Idea.objects.exclude(pk=idea.pk).exclude(status__in=Idea.CLOSED_STATUSES)[:200],
            "duplicates": idea.possible_duplicates.all()}


@superuser_required
@require_POST
def suggestion_to_idea(request, pk):
    suggestion = get_object_or_404(SiteSuggestion, pk=pk)
    idea = from_suggestion(suggestion, user=request.user)
    messages.success(request, f'Moved to Ideas as #{idea.pk}.')
    return redirect("albright_control:idea", pk=idea.pk)
