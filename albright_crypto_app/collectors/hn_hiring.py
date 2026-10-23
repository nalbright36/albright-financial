"""Hacker News "Ask HN: Who is hiring?" via the official HN Firebase API
(https://github.com/HackerNews/API): the whoishiring account's latest
monthly thread, its top-level comments (each one posting).

Every new comment is classified by AI, in batches (one call per BATCH
comments), with the same "only facts in the text" rules as extraction. The
same call also scores each posting's fit with My profile (the Inbox's fit
step - relevance.validate_fit checks what comes back), so filtering HN costs
no extra AI calls: every posting is stored, and the prefilter and the fit
score decide what reaches the Inbox (the rest goes to Hidden, with why).
Classified comments are recorded (ProcessedItem) whether kept or not, so
none is sent to the AI twice. Read-only GETs, at most MAX_NEW_COMMENTS per
run and once per MIN_INTERVAL.
"""
import html
import logging
import re
from datetime import datetime, timedelta, timezone as dt_timezone

from ..ai_tasks import run_json
from ..extraction import ExtractionLimitReached, INCOME_SYSTEM_PROMPT, validate
from ..models import MyProfile, ProcessedItem
from ..relevance import BATCH_FIT_FIELDS, BATCH_FIT_RULES, profile_brief, validate_fit
from .base import BaseCollector, RawOpportunity, SourceUnavailable

log = logging.getLogger(__name__)

API = "https://hacker-news.firebaseio.com/v0"
THREAD_TITLE = "ask hn: who is hiring?"
MAX_THREAD_LOOKUPS = 15       # the account's newest submissions checked for the thread
MAX_NEW_COMMENTS = 60         # per run
BATCH = 15                    # comments per AI call
KEEP_WORK_TYPES = {"contract", "freelance", "part_time"}

CLASSIFY_PROMPT = """You read job postings from a Hacker News "Who is hiring?" thread. Each posting has an id.
Use ONLY facts stated in each posting; anything not stated is null / "unknown" / false. Never guess.
%s

Return ONLY a JSON object:
{"postings": [{
  "id": the posting's id,
  "title": "Company - role" as written (short),
  "work_types": list from "full_time","contract","freelance","part_time","internship" - only those the posting states,
  "remote": true only if the posting says remote work is allowed,
%s
  ...and every field of this extraction schema for the posting:
%s
}]}""" % (BATCH_FIT_RULES, BATCH_FIT_FIELDS, INCOME_SYSTEM_PROMPT.split("Return ONLY a JSON object, no other text:")[1])


def plain_text(comment_html):
    """HN comment HTML -> text; links written as their URL."""
    text = re.sub(r'(?is)<a\b[^>]*href="([^"]+)"[^>]*>.*?</a>', lambda m: html.unescape(m.group(1)), comment_html or "")
    text = re.sub(r"(?i)<p>", "\n\n", text)
    text = html.unescape(re.sub(r"<[^>]+>", "", text))
    return re.sub(r"\n{3,}", "\n\n", text).strip()


class HnHiringCollector(BaseCollector):
    kind = "hn_hiring"
    min_interval = timedelta(hours=6)
    request_delay_seconds = 0.5

    def item(self, item_id):
        return self.get(f"{API}/item/{item_id}.json").json() or {}

    def latest_thread(self):
        user = self.get(f"{API}/user/whoishiring.json").json() or {}
        for item_id in (user.get("submitted") or [])[:MAX_THREAD_LOOKUPS]:
            story = self.item(item_id)
            if story.get("type") == "story" and not story.get("deleted") and not story.get("dead") \
                    and (story.get("title") or "").lower().startswith(THREAD_TITLE):
                return story
        return None

    def collect(self, source):
        if not self.use_ai:
            log.info("HN Who is hiring needs AI to filter postings - skipped (--no-ai)")
            return
        thread = self.latest_thread()
        if thread is None:
            raise SourceUnavailable("No recent \"Ask HN: Who is hiring?\" thread from whoishiring")
        done = set(ProcessedItem.objects.filter(source=source).values_list("item_id", flat=True))
        comments = []
        for comment_id in thread.get("kids") or []:
            if str(comment_id) in done:
                continue
            comment = self.item(comment_id)
            if comment.get("deleted") or comment.get("dead") or not comment.get("text"):
                ProcessedItem.objects.get_or_create(source=source, item_id=str(comment_id))
                continue
            comments.append(comment)
            if len(comments) >= MAX_NEW_COMMENTS:
                break
        profile = MyProfile.load()
        for start in range(0, len(comments), BATCH):
            batch = comments[start:start + BATCH]
            try:
                kept = self.classify(batch, thread, profile)
            except ExtractionLimitReached as exc:
                log.info("HN Who is hiring: stopping - %s", exc)
                return   # unclassified comments wait for the next run
            for comment in batch:
                ProcessedItem.objects.get_or_create(source=source, item_id=str(comment["id"]),
                                                    defaults={"opportunities_created": int(comment["id"] in kept)})
            yield from kept.values()

    def classify(self, batch, thread, profile):
        texts = {c["id"]: plain_text(c["text"]) for c in batch}
        user = f"=== CANDIDATE PROFILE ===\n{profile_brief(profile)}\n\n" + "\n\n".join(
            f"=== POSTING id={cid} ===\n{text}" for cid, text in texts.items())
        data, _cost = run_json(CLASSIFY_PROMPT, user, "hn_filter", client=self.ai_client, max_tokens=6000)
        kept = {}
        for posting in data.get("postings") or []:
            if not isinstance(posting, dict):
                continue
            try:
                cid = int(posting.get("id"))
            except (TypeError, ValueError):
                continue
            if cid not in texts:
                continue  # only ids we actually sent
            text = texts[cid]
            facts = validate(posting, text, "income")
            work_types = [w for w in posting.get("work_types") or [] if isinstance(w, str)]
            remote = posting.get("remote") is True
            if KEEP_WORK_TYPES & set(work_types) and facts["reward_type"] == "unknown":
                facts["reward_type"] = "freelance"
            if remote and not facts.get("location_requirement"):
                facts["location_requirement"] = "Remote"
            comment = next(c for c in batch if c["id"] == cid)
            kept[cid] = RawOpportunity(
                external_id=f"hn:{cid}",
                url=f"https://news.ycombinator.com/item?id={cid}",
                title=(str(posting.get("title") or "").strip() or text.split("\n", 1)[0])[:500],
                text=text,
                metadata={"thread_id": thread.get("id"), "thread_title": thread.get("title"), "by": comment.get("by"),
                          "work_types": work_types, "remote": remote,
                          "fit": validate_fit(posting, profile)},   # taken out again by pipeline.store
                published=datetime.fromtimestamp(comment["time"], tz=dt_timezone.utc) if comment.get("time") else None,
                facts=facts,
                facts_complete=True,
            )
        return kept
