"""Anonymous conversation records for the resume chatbot (Admin > Resume >
Conversations).

What's stored per conversation: a random ID (uuid4 - not the session key, not
derived from the IP), when it started and last changed, how many messages,
the referring site's bare domain ("linkedin.com"), mobile or desktop (worked
out from the user agent, which is then discarded), auto-flags, and my review
notes. Per message: who said it, the text (emails and phone numbers redacted
unless "store contact details" is on), the outcome, tokens and cost.

Never stored: IP addresses, session keys, cookies, full user agents. The
visitor's session holds only the conversation's random ID so later messages
join the same record. Conversations are deleted after chat_retention_days
(prune_resume_chats).
"""
import logging
import re
from datetime import timedelta
from urllib.parse import urlparse

from django.db import transaction
from django.db.models import F
from django.db.models.functions import Coalesce
from django.urls import reverse
from django.utils import timezone

from .models import ChatConversation, ChatMessage

log = logging.getLogger(__name__)

SESSION_CONVERSATION_KEY = "conversation"   # inside the chat's session state: the random public_id only
LONG_CONVERSATION = 8                        # messages (visitor + assistant)

# chat.answer outcomes (and the view's early exits) -> ChatMessage outcomes
OUTCOME_MAP = {"answered": "answered", "not_sure": "not_sure", "declined": "declined_off_limits",
               "blocked": "blocked_leak_guard", "error": "unavailable", "cap": "unavailable",
               "unavailable": "unavailable", "rate_limited": "rate_limited"}

_MOBILE_UA = re.compile(r"Mobi|iPhone|iPod|Android|Windows Phone|BlackBerry|Opera Mini|IEMobile", re.I)

# "Possible employer": hiring words, or a company named by the visitor.
_EMPLOYER_WORDS = re.compile(
    r"\b(hir(e|es|ed|ing)|recruit(er|ers|ing)?|role|roles|position|opening|vacanc(y|ies)|interview(s|ing)?|"
    r"team|salary|salaries|compensation|job|jobs|offer|candidate|onboard(ing)?|headcount)\b", re.I)
_COMPANY = re.compile(
    r"\b[A-Z][\w&'-]*(?:\s+[A-Z][\w&'-]*)*\s+(?:Inc|LLC|Ltd|Corp|Corporation|Co|GmbH|PLC|Group|Technologies|Labs)\b\.?"
    r"|\b(?:at|from|with|for|join)\s+(?:[A-Z][a-z]+[A-Z]\w*|[A-Z]{2,}\w*)\b"    # "at LinkedIn", "from IBM"
    r"|\b(?:our|my)\s+(?:company|firm|startup|organization|org|client|agency)\b")
# Prompt-injection wording (counted alongside the reply-side leak guard).
_INJECTION = re.compile(
    r"ignore (all |any |the )?(previous|prior|above|earlier) (instructions|rules|prompts?)|system prompt|"
    r"reveal (your|the) (instructions|prompt|rules)|repeat (your|the) (instructions|prompt)|you are now|"
    r"developer mode|jailbreak|disregard (the|your|all) (rules|instructions)|print (the|your) (profile|prompt)",
    re.I)


def device_type(user_agent):
    """"mobile" or "desktop" - the only thing kept from the user agent."""
    return "mobile" if _MOBILE_UA.search(user_agent or "") else "desktop"


def referrer_domain(request):
    """The referring site's bare domain ("linkedin.com"), never the full URL.
    Empty for direct visits and for links within this site."""
    host = (urlparse(request.META.get("HTTP_REFERER") or "").hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    own = (request.get_host().split(":")[0] or "").lower().removeprefix("www.")
    return "" if not host or host == own else host[:100]


def visitor_text(text, site):
    from .chat import redact
    return text if site.store_contact_details else redact(text)


def conversation_for(request, state):
    """The visitor's conversation (by the random id in their chat session
    state), created on their first message. Mutates state to remember it."""
    public_id = state.get(SESSION_CONVERSATION_KEY)
    conv = ChatConversation.objects.filter(public_id=public_id).first() if public_id else None
    if conv is None:
        conv = ChatConversation.objects.create(
            referrer_domain=referrer_domain(request),
            device=device_type(request.META.get("HTTP_USER_AGENT")),
        )
        state[SESSION_CONVERSATION_KEY] = str(conv.public_id)
    return conv


def compute_flags(conv):
    msgs = list(conv.messages.all())
    outcomes = {m.outcome for m in msgs if m.role == "assistant"}
    visitor = " \n".join(m.text for m in msgs if m.role == "visitor")
    flags = []
    if "not_sure" in outcomes:
        flags.append("knowledge_gap")
    if "declined_off_limits" in outcomes:
        flags.append("off_limits")
    if "blocked_leak_guard" in outcomes or _INJECTION.search(visitor):
        flags.append("injection")
    if len(msgs) >= LONG_CONVERSATION:
        flags.append("long")
    if _EMPLOYER_WORDS.search(visitor) or _COMPANY.search(visitor):
        flags.append("possible_employer")
    return flags


def record_turn(request, state, site, question, outcome, reply="", tokens=0, cost=0, alert=True):
    """Stores the visitor's question and the assistant's reply (or the
    limit/unavailable notice) and refreshes the conversation's flags.
    alert=False for my own preview chats (recorded, but never Telegrammed)."""
    now = timezone.now()
    with transaction.atomic():
        conv = conversation_for(request, state)
        ChatMessage.objects.create(conversation=conv, role="visitor", text=visitor_text(question, site)[:2000])
        ChatMessage.objects.create(conversation=conv, role="assistant", text=(reply or "")[:4000],
                                   outcome=OUTCOME_MAP.get(outcome, "unavailable"), tokens=tokens or 0,
                                   cost_usd=cost or 0)
        ChatConversation.objects.filter(pk=conv.pk).update(message_count=F("message_count") + 2, last_message_at=now)
        conv.refresh_from_db()
        conv.flags = compute_flags(conv)
        conv.save(update_fields=["flags"])
    if alert and "possible_employer" in conv.flags and site.employer_alerts and conv.employer_alert_sent_at is None:
        send_employer_alert(conv)
    return conv


def send_employer_alert(conv):
    """One Telegram message per conversation: no visitor text beyond the first
    question (truncated to 100 characters), and a link to the transcript."""
    claimed = ChatConversation.objects.filter(pk=conv.pk, employer_alert_sent_at__isnull=True).update(
        employer_alert_sent_at=timezone.now())
    if not claimed:
        return False
    from html import escape
    try:
        from albright_reselling_app.scanner.alerts import SITE_BASE_URL, send_telegram
    except ImportError:   # pragma: no cover - the site always has it
        return False
    first = conv.messages.filter(role="visitor").order_by("created_at", "pk").values_list("text", flat=True).first() or ""
    first = first if len(first) <= 100 else first[:99].rstrip() + "…"
    link = SITE_BASE_URL + reverse("albright_resume_admin:conversation", args=[conv.public_id])
    send_telegram("\n".join([
        "<b>Resume chat: possible employer</b>",
        f"First question: {escape(first)}",
        f'<a href="{escape(link)}">Read the transcript</a>',
    ]))
    return True


def prune(site, now=None):
    """Deletes conversations whose last activity is older than the retention
    period. Returns how many conversations were deleted."""
    now = now or timezone.now()
    cutoff = now - timedelta(days=max(1, int(site.chat_retention_days or 0)))
    old = ChatConversation.objects.annotate(last=Coalesce("last_message_at", "started_at")).filter(last__lt=cutoff)
    ids = list(old.values_list("pk", flat=True))
    ChatConversation.objects.filter(pk__in=ids).delete()
    return len(ids)


# ---------------------------------------------------------------------------
# Knowledge gaps: not_sure questions, grouped by similar wording
# ---------------------------------------------------------------------------

_STOP = set("a an the is are was were do does did can could would should will what whats which who whom how "
            "about of for to in on at with his her their he she nathan nathans you your me my i tell please "
            "any some has have had be been and or".split())


def _key(question):
    words = re.findall(r"[a-z0-9+#]+", question.lower())
    return frozenset(w.rstrip("s") for w in words if w not in _STOP and len(w) > 1)


def knowledge_gaps(since=None):
    """[{question, count, last_asked, conversations: [public_id...], examples}]
    - each not_sure answer's question, grouped when their key words mostly overlap."""
    answers = ChatMessage.objects.filter(role="assistant", outcome="not_sure").select_related("conversation")
    if since:
        answers = answers.filter(created_at__gte=since)
    groups = []
    for answer in answers.order_by("-created_at"):
        question = (ChatMessage.objects.filter(conversation=answer.conversation_id, role="visitor",
                                               created_at__lte=answer.created_at, pk__lt=answer.pk)
                    .order_by("-created_at", "-pk").values_list("text", flat=True).first())
        if not question:
            continue
        key = _key(question)
        for group in groups:
            union = key | group["key"]
            if union and len(key & group["key"]) / len(union) >= 0.5:
                group["count"] += 1
                group["key"] = group["key"] | key
                if question not in group["examples"] and len(group["examples"]) < 3:
                    group["examples"].append(question)
                if answer.conversation.public_id not in group["conversations"]:
                    group["conversations"].append(answer.conversation.public_id)
                break
        else:
            groups.append({"key": key, "question": question, "count": 1, "last_asked": answer.created_at,
                           "examples": [question], "conversations": [answer.conversation.public_id]})
    return sorted(groups, key=lambda g: (-g["count"], -g["last_asked"].timestamp()))
