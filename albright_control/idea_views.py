"""Admin > Ideas: add, detail (edit, status buttons, notes, merge, delete,
Claude Code prompt, my Planner tasks for it), and "Promote" from the Bot
suggestions inbox. The list itself is review_list_views.py. Superusers only."""
from django import forms
from django.contrib import messages
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from albright_bot_api.models import SiteSuggestion

from .access import audit, superuser_required
from .ideas import claude_code_prompt, find_duplicate, from_suggestion, merge
from .models import Idea

INPUT = {"class": "ledger-input"}


class MainTaskForm(forms.Form):
    """Add idea to Planner: the Idea's main task (titled like the Idea) on a date."""
    date = forms.DateField(widget=forms.DateInput(attrs={**INPUT, "type": "date"}, format="%Y-%m-%d"))
    notes = forms.CharField(required=False, max_length=5000, label="Notes (optional)",
                            widget=forms.Textarea(attrs={**INPUT, "rows": 2}))


class SubtaskForm(forms.Form):
    """Add subtask: one more step under the main task, its own Planner task and date."""
    title = forms.CharField(max_length=200, widget=forms.TextInput(attrs=INPUT))
    date = forms.DateField(widget=forms.DateInput(attrs={**INPUT, "type": "date"}, format="%Y-%m-%d"))


def _task_form(idea, user=None):
    from albright_planner.planner import today
    return MainTaskForm(initial={"date": today(user)}, auto_id="idea-task-%s")


def _subtask_form(user=None):
    from albright_planner.planner import today
    return SubtaskForm(initial={"date": today(user)}, auto_id="idea-sub-%s")


def _new_task(idea, user, title, day, notes="", parent=None):
    """A Planner task for this Idea, owned by me: linked by the idea FK (not source_app/source_id - one Idea can
    have several), its source link the Idea's page, made in my zone. Owner-made, so the Planner's "ideas" source
    toggle doesn't apply."""
    from albright_planner.models import PlannerSettings, ScheduleItem, idea_path
    return ScheduleItem.objects.create(
        owner=user, kind=ScheduleItem.TASK, title=title[:200], date=day, notes=notes, idea=idea, parent=parent,
        source_url=idea_path(idea.pk), created_by=user.username, timezone=PlannerSettings.for_user(user).timezone)


def _main_task(idea, user):
    return idea.planner_tasks.filter(owner=user, parent__isnull=True, archived=False).order_by("pk").first()


def _row(item):
    from albright_planner.planner import current_or_first
    day = current_or_first(item)
    return {"item": item, "date": day, "done": item.completions.filter(occurrence_date=day).exists()}


def planner_block(idea, user):
    """My (never anyone else's) Planner tasks for this Idea: {"main": row or None, "subs": [rows], "done": n,
    "total": n}; a row is {"item", "date" (its occurrence), "done"}."""
    main = _main_task(idea, user)
    if main is None:
        return {"main": None, "subs": [], "done": 0, "total": 0}
    rows = [_row(main)] + [_row(item) for item in main.subtasks.filter(archived=False).order_by("date", "pk")]
    return {"main": rows[0], "subs": rows[1:], "done": sum(r["done"] for r in rows), "total": len(rows)}


def _back(request, idea):
    """After adding to the Planner: the safe "next" (the Ideas list) or the Idea page."""
    target = request.POST.get("next", "")
    if url_has_allowed_host_and_scheme(target, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
        return redirect(target)
    return redirect("albright_control:idea", pk=idea.pk)


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
                return render(request, "albright_control/idea.html", _detail_context(request, idea, form))
            idea = form.save()
        elif action in ("add_main", "add_task"):          # add_task: older forms' name for it
            if _main_task(idea, request.user) is not None:
                messages.info(request, "Already in your Planner.")
                return _back(request, idea)
            task_form = MainTaskForm(request.POST, auto_id="idea-task-%s")
            if not task_form.is_valid():
                return render(request, "albright_control/idea.html",
                              _detail_context(request, idea, form, task_form=task_form))
            item = _new_task(idea, request.user, idea.title, task_form.cleaned_data["date"],
                             notes=task_form.cleaned_data["notes"])
            audit(request, "idea_task", f'#{idea.pk} "{idea.title[:80]}": Planner task {item.date:%Y-%m-%d}')
            messages.success(request, f"Added to your Planner for {item.date:%b} {item.date.day}.")
            return _back(request, idea)
        elif action == "add_subtask":
            main = _main_task(idea, request.user)
            sub_form = SubtaskForm(request.POST, auto_id="idea-sub-%s")
            if not sub_form.is_valid():
                return render(request, "albright_control/idea.html",
                              _detail_context(request, idea, form, sub_form=sub_form))
            if main is None:            # the first subtask puts the idea itself in the Planner too
                main = _new_task(idea, request.user, idea.title, sub_form.cleaned_data["date"])
            item = _new_task(idea, request.user, sub_form.cleaned_data["title"], sub_form.cleaned_data["date"],
                             parent=main)
            audit(request, "idea_task", f'#{idea.pk} "{idea.title[:80]}": subtask {item.date:%Y-%m-%d}')
            messages.success(request, f"Added the subtask for {item.date:%b} {item.date.day}.")
            return _back(request, idea)
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
    return render(request, "albright_control/idea.html", _detail_context(request, idea, form))


def _detail_context(request, idea, form, task_form=None, sub_form=None):
    return {"idea": idea, "form": form, "prompt": claude_code_prompt(idea), "statuses": Idea.STATUSES,
            "merge_targets": Idea.objects.exclude(pk=idea.pk).exclude(status__in=Idea.CLOSED_STATUSES)[:200],
            "duplicates": idea.possible_duplicates.all(),
            "planner": planner_block(idea, request.user), "task_form": task_form or _task_form(idea, request.user),
            "sub_form": sub_form or _subtask_form(request.user)}


@superuser_required
@require_POST
def suggestion_to_idea(request, pk):
    suggestion = get_object_or_404(SiteSuggestion, pk=pk)
    idea = from_suggestion(suggestion, user=request.user)
    from .review_list_views import _wants_json
    if _wants_json(request):        # the Suggestions page stays put; the next one shows
        from .review_list import KINDS, new_counts
        return JsonResponse({"ok": True, "id": suggestion.pk, "idea": idea.pk,
                             "idea_url": reverse("albright_control:idea", args=[idea.pk]),
                             "counts": KINDS["suggestions"].counts(), "new_counts": new_counts()})
    messages.success(request, f"Promoted to Idea #{idea.pk}")
    return redirect("albright_control:idea", pk=idea.pk)
