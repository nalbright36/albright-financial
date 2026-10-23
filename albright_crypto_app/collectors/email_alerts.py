"""Email alerts: job-alert emails (Upwork, Fiverr, LinkedIn, Indeed, any
site that emails alerts) from one Gmail label, read-only (gmail.py).

Each new message gets one AI call that splits it into its separate
postings - title, link, pay, skills, deadline, and each posting's fit with
My profile (the Inbox's fit step, checked by relevance.validate_fit) - under
the same rules as fact extraction (only facts in the text; extraction.validate() checks
every posting, including that its evidence quote is in the email). A
posting's link must be one actually in the email (the AI picks it by
number), so it always points at the original posting. Every message ID is
recorded (ProcessedItem) once handled, so nothing is imported - or sent to
the AI - twice. Messages are never sent, deleted, labelled or modified.
"""
import logging

from ..ai_tasks import run_json
from ..extraction import ExtractionLimitReached, INCOME_SYSTEM_PROMPT, quote_in_text, validate
from ..gmail import GmailReader, header, links_in, message_text
from ..links import is_web_url
from ..models import GmailConnection, MyProfile, ProcessedItem
from ..relevance import BATCH_FIT_FIELDS, BATCH_FIT_RULES, profile_brief, validate_fit
from .base import BaseCollector, RawOpportunity, SourceUnavailable

log = logging.getLogger(__name__)

DEFAULT_LABEL = "opportunities"
MAX_NEW_MESSAGES = 10      # per scan - each is one AI call
LIST_SIZE = 25

SPLIT_PROMPT = """You split one job-alert email into the separate job postings it lists.
Use ONLY facts stated in the email. Never guess or use outside knowledge; anything not stated is null / "unknown".
The email's links are numbered [1], [2], ... below the text. For each posting, give the number of the link
that opens THAT posting (not unsubscribe, settings, or search links), or null if there isn't one.
%s

Return ONLY a JSON object:
{"postings": [{
  "title": short title as written in the email,
  "link": link number or null,
  "posting_text": the posting's text copied EXACTLY from the email (max 1500 chars),
%s
  ...and every field of this extraction schema for the posting:
%s
}]}
Return {"postings": []} if the email lists no postings.""" % (
    BATCH_FIT_RULES, BATCH_FIT_FIELDS, INCOME_SYSTEM_PROMPT.split("Return ONLY a JSON object, no other text:")[1])


def label_for(source):
    return ((source.options or {}).get("label") or DEFAULT_LABEL).strip()


class EmailAlertsCollector(BaseCollector):
    kind = "email"

    def collect(self, source):
        if not self.use_ai:
            log.info("Email alerts need AI to split messages - skipped (--no-ai)")
            return
        connection = GmailConnection.objects.filter(pk=GmailConnection.SINGLETON_PK).first()
        if connection is None:
            return
        reader = GmailReader(connection)
        label_id = reader.label_id(label_for(source))
        if label_id is None:
            raise SourceUnavailable(f'Gmail label "{label_for(source)}" not found')
        done = set(ProcessedItem.objects.filter(source=source).values_list("item_id", flat=True))
        new_ids = [m for m in reader.message_ids(label_id, LIST_SIZE) if m not in done][:MAX_NEW_MESSAGES]
        for message_id in new_ids:
            message = reader.message(message_id)
            try:
                postings = self.split(message)
            except ExtractionLimitReached as exc:
                log.info("Email alerts: stopping - %s", exc)
                return   # the rest are picked up next time (not marked processed)
            ProcessedItem.objects.create(source=source, item_id=message_id, opportunities_created=len(postings))
            yield from postings

    def split(self, message):
        subject, body = message_text(message)
        links = links_in(body)
        numbered = "\n".join(f"[{i}] {url}" for i, url in enumerate(links, 1))
        email_text = f"Subject: {subject}\n\n{body}"
        profile = MyProfile.load()
        data, _cost = run_json(SPLIT_PROMPT, f"=== CANDIDATE PROFILE ===\n{profile_brief(profile)}\n\n"
                               f"=== EMAIL ===\n{email_text}\n\nLINKS:\n{numbered}", "email_split",
                               client=self.ai_client)
        results = []
        for i, posting in enumerate(data.get("postings") or []):
            if not isinstance(posting, dict) or not str(posting.get("title") or "").strip():
                continue
            title = str(posting["title"]).strip()[:500]
            excerpt = str(posting.get("posting_text") or "").strip()
            text = excerpt if excerpt and quote_in_text(excerpt, email_text) else title
            facts = validate(posting, email_text, "income")
            url = _link(posting.get("link"), links) or f"https://mail.google.com/mail/u/0/#all/{message['id']}"
            results.append(RawOpportunity(
                external_id=f"email:{message['id']}:{i}",
                url=url,
                title=title,
                text=text,
                metadata={"email_subject": subject[:300], "email_id": message["id"],
                          "from": header(message, "From")[:200], "link_in_email": url in links,
                          "fit": validate_fit(posting, profile)},   # taken out again by pipeline.store
                facts=facts,
                facts_complete=True,
            ))
        return results


def _link(number, links):
    try:
        index = int(number) - 1
    except (TypeError, ValueError):
        return None
    return links[index] if 0 <= index < len(links) and is_web_url(links[index]) else None

