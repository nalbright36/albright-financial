"""Photos in assistant messages (a recipe card, a plate of food, a nutrition label, a handwritten grocery list).

The browser already shrinks each photo to 2048px on the long side and re-encodes it as JPEG (no EXIF/GPS) -
assistant.js. The server doesn't trust that: each file is checked here (an image type, at most MAX_BYTES, at most
MAX_PHOTOS per message) and then stored as an attachment on the person's message by albright_labs.attachments,
which checks the content really is that image, turns HEIC into JPEG (pillow-heif), strips all metadata by
re-encoding, and counts it in the person's storage quota. The photos go when the message or its chat is deleted.

To the provider: the stored file as base64 (for_provider), JPEG or PNG - anything else is re-encoded to JPEG
(xAI takes only those two). Only the most recent RECENT_TURNS messages with photos send them again on later
steps; older ones become a short note, so a long chat doesn't resend every photo on every call. What a photo costs
is on the person's own provider account, like the rest of the chat.
"""
import base64
import io

from albright_labs import attachment_storage, attachments

MAX_PHOTOS = 4
MAX_BYTES = 5 * 1024 * 1024
RECENT_TURNS = 2
IMAGE_EXTENSIONS = ("jpg", "jpeg", "png", "webp", "heic", "heif", "gif")
SEND_AS_IS = ("image/jpeg", "image/png")
NO_VISION = "Your current model can't read images - switch models in AI assistant settings."


class PhotoError(ValueError):
    pass


def check(files):
    """The uploads, checked before anything is saved - or PhotoError with a sentence for the person."""
    files = [f for f in files if f]
    if len(files) > MAX_PHOTOS:
        raise PhotoError(f"Send at most {MAX_PHOTOS} photos in one message.")
    for f in files:
        ext = attachments._extension(getattr(f, "name", ""))
        if ext not in IMAGE_EXTENSIONS:
            raise PhotoError("Only photos can be sent to the assistant (JPEG, PNG, HEIC, WebP or GIF).")
        if (f.size or 0) > MAX_BYTES:
            raise PhotoError(f"“{attachments.clean_name(f.name)}” is over the 5 MB limit for a photo.")
    return files


def attach(user, message, files):
    """Store the checked uploads on the message (inside the caller's transaction). AttachmentError (e.g. not really
    an image, or storage full) is turned into PhotoError."""
    out = []
    for f in files:
        try:
            out.append(attachments.add(user, message, f))
        except attachments.AttachmentError as exc:
            raise PhotoError(str(exc)) from None
    return out


def of_messages(messages):
    """{message id: [Attachment]} for these messages, in one query."""
    from albright_assistant.models import Message
    ids = [m.pk for m in messages]
    found = {}
    if not ids:
        return found
    for a in attachments.Attachment.objects.filter(content_type=attachments._ct(Message), object_id__in=ids):
        if a.is_image:
            found.setdefault(a.object_id, []).append(a)
    return found


def describe(attachment):
    """For the chat: the thumbnail to show and the full-size image to open."""
    row = attachments.describe(attachment)
    return {"id": row["id"], "thumb": row["thumb_url"], "url": row["url"], "name": row["name"],
            "width": row["width"], "height": row["height"]}


def for_provider(attachment):
    """{"mime", "data": base64} of the stored image, or None if its bytes are missing."""
    handle = attachment_storage.open_attachment(attachment)
    if handle is None:
        return None
    raw, mime = handle.read(), attachment.mime_type
    if mime not in SEND_AS_IS:
        from PIL import Image
        img = Image.open(io.BytesIO(raw))
        img.seek(0)
        out = io.BytesIO()
        img.convert("RGB").save(out, "JPEG", quality=88)
        raw, mime = out.getvalue(), "image/jpeg"
    return {"mime": mime, "data": base64.b64encode(raw).decode("ascii")}
