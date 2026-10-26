"""Public resume page and chatbot endpoint. No login required.

Isolation: these views read only albright_resume models, never other apps'
data, never request.user data except is_superuser (so Nathan can preview the
page while it is switched off)."""
import json
from collections import OrderedDict

from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import render
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_POST

from . import ai, chat, conversations
from .models import EducationItem, Project, ResumeSettings, Skill, WorkExperience
from .security import public_headers

SESSION_KEY = "resume_chat"
MAX_BODY_BYTES = 8192


def _is_owner(request):
    user = getattr(request, "user", None)
    return bool(user and user.is_authenticated and user.is_superuser)


def _site_or_404(request):
    site = ResumeSettings.load()
    if not site.page_enabled and not _is_owner(request):
        raise Http404
    return site


@require_GET
@ensure_csrf_cookie
@public_headers
def resume_page(request):
    site = _site_or_404(request)
    projects = list(Project.objects.filter(published=True))
    skills = OrderedDict()
    for s in Skill.objects.filter(published=True):
        skills.setdefault(s.category or "Skills", []).append(s)
    edu = list(EducationItem.objects.filter(published=True))
    chat_live = site.chat_enabled or _is_owner(request)
    return render(request, "albright_resume/resume.html", {
        "site": site,
        "preview": not site.page_enabled,
        "chat_live": chat_live,
        "chat_preview": chat_live and not site.chat_enabled,
        "built": [p for p in projects if p.show_in_built],
        "other_projects": [p for p in projects if not p.show_in_built],
        "jobs": WorkExperience.objects.filter(published=True),
        "skills": skills,
        "education": [e for e in edu if e.kind == "education"],
        "certifications": [e for e in edu if e.kind == "certification"],
        "first_name": (site.full_name or "Nathan").split()[0],
    })


@require_GET
@public_headers
def resume_pdf(request):
    site = _site_or_404(request)
    if not site.resume_pdf:
        raise Http404
    response = HttpResponse(bytes(site.resume_pdf), content_type="application/pdf")
    name = site.resume_pdf_name or "resume.pdf"
    response["Content-Disposition"] = f'attachment; filename="{name}"'
    response["Cache-Control"] = "public, max-age=3600"
    return response


@require_GET
@public_headers
def project_screenshot(request, pk, version):
    _site_or_404(request)
    project = Project.objects.filter(pk=pk).first()
    if not project or not project.screenshot or (not project.published and not _is_owner(request)):
        raise Http404
    if project.screenshot_content_type not in Project.IMAGE_TYPES:
        raise Http404
    response = HttpResponse(bytes(project.screenshot), content_type=project.screenshot_content_type)
    response["Cache-Control"] = "public, max-age=86400"
    return response


def _reply(payload, status=200):
    payload.setdefault("ok", status == 200)
    return JsonResponse(payload, status=status)


@require_POST
@never_cache
@public_headers
def chat_api(request):
    """POST JSON {"message": "...", "website": ""} with X-CSRFToken header.
    Order of checks: switched on -> input -> session cap -> rate limit -> spend cap -> AI."""
    site = _site_or_404(request)
    owner = _is_owner(request)
    if not site.chat_enabled and not owner:
        return _reply({"ok": False, "unavailable": True, "reply": chat.UNAVAILABLE})
    if len(request.body) > MAX_BODY_BYTES:
        return _reply({"error": "Message too long."}, 400)
    try:
        data = json.loads(request.body or b"{}")
    except (ValueError, UnicodeDecodeError):
        return _reply({"error": "Bad request."}, 400)
    if not isinstance(data, dict):
        return _reply({"error": "Bad request."}, 400)
    if str(data.get("website") or "").strip():  # honeypot: humans never fill this
        return _reply({"reply": chat.NOT_SURE_FALLBACK, "remaining": 0})
    question = chat.clean_message(data.get("message"))
    if not question:
        return _reply({"error": "Please type a question."}, 400)
    if len(question) > site.max_message_chars:
        return _reply({"error": f"Please keep questions under {site.max_message_chars} characters."}, 400)

    state = request.session.get(SESSION_KEY) or {"n": 0, "h": []}
    used = int(state.get("n", 0))
    if used >= site.max_messages_per_session:
        return _reply({"ok": False, "limit": True, "remaining": 0,
                       "reply": "That's the question limit for this conversation. Thanks for your interest! "
                                "Please use the Contact me button to reach Nathan directly."})

    def keep(outcome, reply, tokens=0, cost=0):
        """The conversation record (anonymous; see conversations.py) - the
        session only ever holds its random id."""
        conversations.record_turn(request, state, site, question, outcome, reply, tokens, cost, alert=not owner)
        request.session[SESSION_KEY] = state

    limited = chat.check_rate_limits(request, site)
    if limited:
        chat.log_question(question, "rate_limited", turn=used + 1)
        keep("rate_limited", limited)
        return _reply({"ok": False, "limit": True, "reply": limited}, 429)
    if chat.cap_reached(site):
        chat.log_question(question, "cap", turn=used + 1)
        keep("cap", chat.UNAVAILABLE)
        return _reply({"ok": False, "unavailable": True, "reply": chat.UNAVAILABLE})
    if not ai.api_key_configured():
        chat.log_question(question, "unavailable", turn=used + 1)
        keep("unavailable", chat.UNAVAILABLE)
        return _reply({"ok": False, "unavailable": True, "reply": chat.UNAVAILABLE})

    history = [tuple(pair) for pair in state.get("h", []) if isinstance(pair, (list, tuple)) and len(pair) == 2]
    outcome, reply, tokens, cost = chat.answer_detailed(site, question, history)
    chat.log_question(question, "not_sure" if outcome == "blocked" else outcome, reply, turn=used + 1)
    keep(outcome, reply, tokens, cost)
    if outcome == "error":
        return _reply({"ok": False, "unavailable": True, "reply": reply})
    used += 1
    history.append((question, reply))
    state.update({"n": used, "h": [list(p) for p in history[-chat.HISTORY_TURNS:]]})
    request.session[SESSION_KEY] = state
    return _reply({"reply": reply, "outcome": "not_sure" if outcome == "blocked" else outcome,
                   "remaining": max(site.max_messages_per_session - used, 0)})
