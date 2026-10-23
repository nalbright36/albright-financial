"""Ideas: duplicate detection, merging, moving a Bot suggestion over, and
the "Copy as Claude Code prompt" text."""
import re
from difflib import SequenceMatcher

from .models import AuditEntry, Idea
from .prompts import CLOSING

DUPLICATE_SIMILARITY = 0.8   # titles at least this similar (case/punctuation ignored) count as a possible duplicate

APP_CONTEXT = {
    "trade": "The Trading app (albright_trading_app).",
    "resell": "The Reselling app (albright_reselling_app).",
    "opps": "The Opportunities app (albright_crypto_app).",
    "admin": "The Admin area (albright_control, /control/).",
    "all": "All three apps (Trading, Reselling, Opportunities) - shared templates/styles where possible.",
}


def _normal(title):
    return re.sub(r"[^a-z0-9]+", " ", (title or "").lower()).strip()


def similarity(a, b):
    a, b = _normal(a), _normal(b)
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def find_duplicate(title, exclude_pk=None):
    """The open idea whose title best matches (>= DUPLICATE_SIMILARITY), or None."""
    best, best_score = None, DUPLICATE_SIMILARITY
    candidates = Idea.objects.exclude(status__in=Idea.CLOSED_STATUSES).only("pk", "title")
    if exclude_pk:
        candidates = candidates.exclude(pk=exclude_pk)
    for idea in candidates:
        score = similarity(title, idea.title)
        if score >= best_score:
            best, best_score = idea, score
    return best


def merge(keep, duplicate, user=None):
    """Merges duplicate into keep: both original messages are kept (and the
    description, tags and links combined), then the duplicate is deleted."""
    def join(a, b, label):
        a, b = (a or "").strip(), (b or "").strip()
        if not b or b == a:
            return a
        return f"{a}\n\n--- {label} #{duplicate.pk} ---\n{b}" if a else b
    keep.original_message = join(keep.original_message, duplicate.original_message, "Merged from idea")
    keep.description = join(keep.description, duplicate.description, "Merged from idea")
    keep.my_notes = join(keep.my_notes, duplicate.my_notes, "Notes from idea")
    keep.tags = list(dict.fromkeys(list(keep.tags or []) + list(duplicate.tags or [])))
    keep.links = list(dict.fromkeys(list(keep.links or []) + list(duplicate.links or [])))
    if keep.possible_duplicate_of_id == duplicate.pk:
        keep.possible_duplicate_of = None
    keep.save()
    Idea.objects.filter(possible_duplicate_of=duplicate).update(possible_duplicate_of=keep)
    details = f'#{duplicate.pk} "{duplicate.title[:80]}" merged into #{keep.pk} "{keep.title[:80]}"'
    duplicate.delete()
    AuditEntry.objects.create(user=user, action="idea_merged", details=details)
    return keep


SUGGESTION_APPS = {"trade": "trade", "resell": "resell", "opps": "opps", "all": "all"}


def from_suggestion(suggestion, user=None):
    """A Bot suggestion becomes an idea (source moved_from_suggestion); the suggestion is removed."""
    idea = Idea.objects.create(
        title=suggestion.title[:200], description=suggestion.description,
        original_message=f"(Moved from Bot suggestion #{suggestion.pk}{' - ' + suggestion.area if suggestion.area else ''})\n"
                         f"{suggestion.description}",
        app=SUGGESTION_APPS.get(suggestion.app, "all"), category="improvement",
        priority=suggestion.priority if suggestion.priority in dict(Idea.PRIORITIES) else "medium",
        my_notes=suggestion.my_notes, source="moved_from_suggestion", token=suggestion.token,
        possible_duplicate_of=find_duplicate(suggestion.title))
    AuditEntry.objects.create(user=user, action="idea_created",
                              details=f'#{idea.pk} "{idea.title[:80]}" moved from Bot suggestion #{suggestion.pk}')
    suggestion.delete()
    return idea


def claude_code_prompt(idea):
    lines = [idea.title.strip(), "", f"App: {APP_CONTEXT.get(idea.app, APP_CONTEXT['all'])}"]
    if idea.description.strip():
        lines += ["", idea.description.strip()]
    if idea.my_notes.strip():
        lines += ["", "My notes:", idea.my_notes.strip()]
    lines += ["", CLOSING]
    return "\n".join(lines)
