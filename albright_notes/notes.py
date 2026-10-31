"""Note logic shared by the pages (views.py) and the Bot API
(albright_bot_api.notes_api): creating, saving with a conflict check,
tags, search, and who a note can be shared with."""
import re

from django.contrib.auth import get_user_model
from django.contrib.postgres.search import SearchQuery, SearchRank, SearchVector
from django.db import transaction
from django.db.models import F, Q, TextField
from django.db.models.functions import Cast

from .models import MAX_BODY, MAX_TAG_LENGTH, MAX_TAGS, ME, Note

UNTITLED = "Untitled note"
SEARCH_WORDS = 8


class Conflict(Exception):
    """The note changed since the editor loaded it (another person, a bot, another tab)."""

    def __init__(self, note):
        super().__init__("conflict")
        self.note = note


def clean_tags(raw):
    """A list (or comma-separated text) -> lowercase tags without "#", deduplicated, at most MAX_TAGS."""
    if isinstance(raw, str):
        raw = raw.split(",")
    tags = []
    for tag in raw or []:
        tag = re.sub(r"\s+", " ", str(tag)).strip().lstrip("#").strip().lower()[:MAX_TAG_LENGTH]
        if tag and tag not in tags:
            tags.append(tag)
    return tags[:MAX_TAGS]


def split_capture(text):
    """Quick capture: the first line is the title, the rest (if any was pasted) the body."""
    lines = (text or "").replace("\r\n", "\n").strip().split("\n")
    title = lines[0].strip().lstrip("#").strip()[:200] or UNTITLED
    return title, "\n".join(lines[1:]).strip()


def user_label(user):
    return (user.get_full_name() or user.get_username())[:150]


def create(owner, title, body="", tags=(), pinned=False, token=None, author=None):
    """A new note. token: the bot that wrote it (labeled with its name); author: the person, if not a bot."""
    return Note.objects.create(
        owner=owner, title=(title or "").strip()[:200] or UNTITLED, body=(body or "")[:MAX_BODY],
        tags=clean_tags(tags), pinned=bool(pinned), created_by=token.name[:100] if token else ME, bot_token=token,
        updated_by=token.name if token else user_label(author or owner))


def save(note, by, base_version=None, **fields):
    """Change title/body/tags (any subset). base_version: the version the editor started from - a newer
    one in the database raises Conflict (None skips the check: overwrite on purpose, or a bot's append).
    Returns the saved note (version bumped)."""
    with transaction.atomic():
        current = Note.objects.select_for_update().get(pk=note.pk)
        if base_version is not None and current.version != base_version:
            raise Conflict(current)
        if "title" in fields:
            current.title = (fields["title"] or "").strip()[:200] or UNTITLED
        if "body" in fields:
            current.body = (fields["body"] or "")[:MAX_BODY]
        if "tags" in fields:
            current.tags = clean_tags(fields["tags"])
        current.version = F("version") + 1
        current.updated_by = by[:150]
        current.save()
        current.refresh_from_db()
    return current


def search(qs, query):
    """PostgreSQL full-text search over title (weighted highest), tags and body. Each word also matches as a
    prefix ("groc" finds "groceries"), so it works while typing. Best matches first."""
    words = re.findall(r"\w+", query or "")[:SEARCH_WORDS]
    if not words:
        return qs
    query = SearchQuery(" & ".join(f"{word}:*" for word in words), search_type="raw", config="english")
    vector = (SearchVector("title", weight="A", config="english")
              + SearchVector(Cast("tags", TextField()), weight="B", config="english")
              + SearchVector("body", weight="C", config="english"))
    return qs.annotate(search=vector, rank=SearchRank(vector, query)).filter(search=query) \
        .order_by("-pinned", "-rank", "-updated_at")


def notes_users():
    """Everyone who can open Notes (Admin > Users), the people a note can be shared with."""
    return get_user_model().objects.filter(is_active=True).filter(
        Q(is_superuser=True) | Q(labs_preference__allowed_apps__contains=["notes"])).order_by("username")


def all_tags(qs):
    return sorted({tag for tags in qs.values_list("tags", flat=True) for tag in (tags or [])})
