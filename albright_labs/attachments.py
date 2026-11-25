"""Attachments - files and photos on Planner tasks, notes and recipes. Every rule is here; the views (attachment_views) and
templates (albright_labs/_attachments.html) only call it. The bytes go through attachment_storage.

Who: seeing a task or note (sharing.can_view - the owner, or a person or circle it's shared with) lets you see and
open its attachments; editing it (its can_edit) lets you add and remove them. Anyone else gets a 404.

What: images (jpg, png, gif, webp, heic/heif), pdf, txt, csv, docx, xlsx, pptx - checked by extension AND by
content (Pillow opens every image; a pdf starts %PDF-; an Office file is a zip with its parts; text is text with
no markup). Never svg or html. At most settings.ATTACHMENT_MAX_BYTES per file and ATTACHMENT_MAX_PER_PARENT per
task or note.

Images: turned upright (EXIF orientation), EXIF/GPS and other metadata stripped by re-encoding, downscaled to
MAX_SIDE on the long side, and given a ~THUMB_SIDE px WebP thumbnail. iPhone HEIC/HEIF becomes JPEG so every
browser shows it (pillow-heif). An animated GIF is kept as it is (GIFs carry no EXIF).

Storage: each attachment (file + thumbnail) counts against its owner's tier (albright_labs.storage) - add()
reserves the bytes in the same transaction as the row, under a lock on the person's storage row, and refuses an
upload that would take them over; a deleted attachment gives its bytes back (post_delete - also when its task or
note goes).

Deleting a task or note deletes its attachments (connect_signals: post_delete); a fork of a repeating task or a
duplicate doesn't copy them (albright_planner.planner).

The AI assistant's photos (albright_assistant.photos) are attachments too, on the person's chat message: only the
chat's owner sees them, they're added only with the message (never through the upload form - can_edit is False),
and they go when the message or its chat is deleted."""
import hashlib
import io
import os
import re
import uuid
import zipfile

from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from django.db.models import Count, Exists, F, IntegerField, OuterRef, Subquery, Sum, Value
from django.db.models.functions import Coalesce
from django.db.models.signals import post_delete
from django.urls import reverse
from django.utils import timezone

from . import attachment_storage, storage
from .models import Attachment

try:
    from PIL import Image, ImageOps, UnidentifiedImageError
except ImportError:                                    # pragma: no cover - Pillow is in requirements.txt
    Image = ImageOps = None
    UnidentifiedImageError = OSError

try:
    import pillow_heif
    pillow_heif.register_heif_opener()
    HEIF_SUPPORTED = True
except ImportError:                                    # pragma: no cover
    HEIF_SUPPORTED = False

MAX_SIDE = 2560
THUMB_SIDE = 400
JPEG_QUALITY = 88

# The two kinds of parent: the apps_registry key in the upload form -> the model label.
PARENTS = {"planner": "albright_planner.ScheduleItem", "notes": "albright_notes.Note",
           "assistant": "albright_assistant.Message", "meals": "albright_meals.Recipe"}

IMAGE_TYPES = {"jpg": "JPEG", "jpeg": "JPEG", "png": "PNG", "gif": "GIF", "webp": "WEBP", "heic": "HEIF", "heif": "HEIF"}
# Pillow's format -> how we store it. MPO is a multi-frame JPEG (some phones); HEIC is HEIF.
# The filename is not trusted: iPhone Safari often sends a HEIC photo named image.jpeg.
KIND = {"JPEG": "JPEG", "MPO": "JPEG", "PNG": "PNG", "GIF": "GIF", "WEBP": "WEBP", "HEIF": "HEIF", "HEIC": "HEIF"}
OUTPUT = {"JPEG": ("jpg", "image/jpeg"), "HEIF": ("jpg", "image/jpeg"), "PNG": ("png", "image/png"),
          "GIF": ("gif", "image/gif"), "WEBP": ("webp", "image/webp")}
OFFICE = {"docx": ("word/", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
          "xlsx": ("xl/", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
          "pptx": ("ppt/", "application/vnd.openxmlformats-officedocument.presentationml.presentation")}
TEXT = {"txt": "text/plain", "csv": "text/csv"}
ALLOWED = [*IMAGE_TYPES, "pdf", *TEXT, *OFFICE]
# For <input type="file" accept>: on a phone "image/*" offers the camera and the photo library, the rest Files.
ACCEPT = "image/*,.heic,.heif,.pdf,.txt,.csv,.docx,.xlsx,.pptx"
# Markup a text file must not carry (it's served as a download with nosniff anyway - belt and braces).
MARKUP = re.compile(rb"<\s*(?:!doctype\s+html|html|script|svg|iframe|body|head)\b", re.I)
SAFE_NAME = re.compile(r"[\x00-\x1f\x7f/\\]+")


class AttachmentError(ValueError):
    pass


def max_bytes():
    return getattr(settings, "ATTACHMENT_MAX_BYTES", 15 * 1024 * 1024)


def max_per_parent():
    return getattr(settings, "ATTACHMENT_MAX_PER_PARENT", 20)


def size_label(n):
    return storage.format_bytes(n)


def size_limit_label(n):
    return storage.format_limit(n)


# ---------------------------------------------------------------------------
# Parents and access
# ---------------------------------------------------------------------------

def parent_model(app):
    from django.apps import apps
    label = PARENTS.get(app)
    return apps.get_model(label) if label else None


def _ct(model_or_obj):
    return ContentType.objects.get_for_model(model_or_obj)


def _is_chat_message(parent):
    return parent is not None and parent._meta.label == PARENTS["assistant"]


def can_view(user, parent):
    from . import sharing
    if _is_chat_message(parent):                       # an assistant photo: the chat's owner only
        return bool(user and user.is_authenticated and parent.chat.user_id == user.pk)
    if parent is not None and parent._meta.label == PARENTS.get("meals"):
        # Library readers and view-only shares can see a recipe's photos; anonymous visitors cannot
        # (public share links are a separate path and do not use this).
        return bool(hasattr(parent, "can_view") and parent.can_view(user))
    return parent is not None and sharing.can_view(user, parent)


def can_edit(user, parent):
    """Edit access to the task or note (a Planner item another app keeps up to date is locked: no one edits it).
    Never for an assistant message - its photos come only with the message."""
    if _is_chat_message(parent):
        return False
    return parent is not None and can_view(user, parent) and parent.can_edit(user)


def find_parent(app, pk):
    model = parent_model(app)
    try:
        return model.objects.filter(pk=int(pk)).first() if model else None
    except (TypeError, ValueError):
        return None


def for_parent(parent):
    """The parent's attachments (metadata only - the bytes are never loaded here)."""
    return Attachment.objects.filter(content_type=_ct(parent), object_id=parent.pk)


def visible_attachment(user, pk, edit=False):
    """The attachment if this user may see it (edit=True: may change its parent), else None - the views 404."""
    attachment = Attachment.objects.select_related("content_type").filter(pk=pk).first()
    if attachment is None or attachment.content_type.model_class()._meta.label not in PARENTS.values():
        return None
    parent = attachment.parent
    allowed = can_edit(user, parent) if edit else can_view(user, parent)
    return attachment if allowed else None


# ---------------------------------------------------------------------------
# Checking and processing an upload
# ---------------------------------------------------------------------------

def _extension(name):
    return os.path.splitext(name or "")[1].lower().lstrip(".")


def clean_name(name, ext=None):
    """The name to show and download as: no path or control characters, at most 255 characters, and - for a
    HEIC turned into JPEG - the new extension."""
    base = SAFE_NAME.sub("_", os.path.basename((name or "").replace("\\", "/"))).strip() or "file"
    if ext:
        stem = os.path.splitext(base)[0] or "file"
        base = f"{stem}.{ext}"
    if len(base) > 255:
        stem, dot_ext = os.path.splitext(base)
        base = stem[:255 - len(dot_ext)] + dot_ext
    return base


def _save_image(img, fmt):
    """Re-encode: only pixels (and the colour profile) survive - no EXIF, GPS, XMP or comments."""
    out = io.BytesIO()
    icc = img.info.get("icc_profile")
    transparency = img.info.get("transparency")
    img.info = {}
    extra = {"icc_profile": icc} if icc else {}
    if fmt in ("JPEG", "HEIF"):
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        img.save(out, "JPEG", quality=JPEG_QUALITY, optimize=True, progressive=True, **extra)
    elif fmt == "PNG":
        if transparency is not None:
            extra["transparency"] = transparency
        img.save(out, "PNG", optimize=True, **extra)
    elif fmt == "WEBP":
        img.save(out, "WEBP", quality=JPEG_QUALITY, method=4, **extra)
    else:                                              # a single-frame GIF
        if transparency is not None:
            extra["transparency"] = transparency
        img.save(out, "GIF", **{k: v for k, v in extra.items() if k != "icc_profile"})
    return out.getvalue()


def _thumbnail(img):
    thumb = img.copy()
    if thumb.mode not in ("RGB", "RGBA"):
        thumb = thumb.convert("RGBA" if "A" in thumb.getbands() or "transparency" in img.info else "RGB")
    thumb.thumbnail((THUMB_SIDE, THUMB_SIDE), Image.LANCZOS)
    thumb.info = {}
    out = io.BytesIO()
    thumb.save(out, "WEBP", quality=80, method=4)
    return out.getvalue()


def _looks_like_heif(raw):
    """True when the bytes are HEIC/HEIF (iPhones often name these .jpeg). Not AVIF."""
    if len(raw) < 12 or raw[4:8] != b"ftyp":
        return False
    size = int.from_bytes(raw[0:4], "big")
    box = raw[8:size] if 16 <= size <= len(raw) else raw[8:64]
    if b"avif" in box or b"avis" in box:
        return False
    return any(tag in box for tag in (b"heic", b"heif", b"heix", b"heim", b"heis", b"mif1", b"msf1"))


def process_image(raw, ext):
    """(data, thumb, mime, out_ext, width, height) for an image upload - or AttachmentError if the bytes
    aren't a photo we can use. Trusts the actual format, not the filename: a HEIC named .jpeg (what iPhone
    Safari often sends) is converted to JPEG, a misnamed PNG stays a PNG, and a non-image is refused."""
    if Image is None:
        raise AttachmentError("Images can't be added right now.")
    heif_bytes = _looks_like_heif(raw)
    if (heif_bytes or IMAGE_TYPES.get(ext) == "HEIF") and not HEIF_SUPPORTED:
        raise AttachmentError("HEIC photos can't be added right now - send it as JPEG.")
    try:
        probe = Image.open(io.BytesIO(raw))
        probe.verify()
        img = Image.open(io.BytesIO(raw))
        kind = KIND.get(img.format)
        if kind is None:
            raise AttachmentError(
                "That file doesn't look like a photo we can use. Try a JPEG, PNG, GIF, WebP or HEIC photo.")
        if kind == "GIF" and getattr(img, "is_animated", False):
            data = raw                                 # keep the animation; GIFs hold no EXIF to strip
            img.seek(0)
            frame = img.convert("RGBA")
            width, height = img.size
            thumb = _thumbnail(frame)
        else:
            img.load()
            img = ImageOps.exif_transpose(img)
            if max(img.size) > MAX_SIDE:
                img.thumbnail((MAX_SIDE, MAX_SIDE), Image.LANCZOS)
            width, height = img.size
            thumb = _thumbnail(img)
            data = _save_image(img, kind)
    except AttachmentError:
        raise
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError, SyntaxError, ValueError) as exc:
        if heif_bytes and not HEIF_SUPPORTED:
            raise AttachmentError("HEIC photos can't be added right now - send it as JPEG.") from exc
        raise AttachmentError(
            "We couldn't read that photo. Try saving it as a JPEG or PNG and attach it again.") from exc
    out_ext, mime = OUTPUT[kind]
    return data, thumb, mime, out_ext, width, height


def _check_office(raw, ext):
    folder, mime = OFFICE[ext]
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            names = z.namelist()
    except (zipfile.BadZipFile, ValueError) as exc:
        raise AttachmentError(f"That file isn't really a .{ext} document.") from exc
    if "[Content_Types].xml" not in names or not any(n.startswith(folder) for n in names):
        raise AttachmentError(f"That file isn't really a .{ext} document.")
    return mime


def _check_text(raw, ext):
    if b"\x00" in raw:
        raise AttachmentError(f"That file isn't a plain-text .{ext}.")
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise AttachmentError(f"That file isn't a plain-text .{ext}.")
    if MARKUP.search(raw[:4096]):
        raise AttachmentError("Files with web page markup (HTML, SVG, scripts) can't be attached.")
    return TEXT[ext]


def check_upload(upload):
    """(raw bytes, ext) for an allowed upload, or AttachmentError with a sentence for the person."""
    name = getattr(upload, "name", "") or ""
    ext = _extension(name)
    if ext in ("svg", "svgz", "html", "htm", "xhtml", "xml", "js", "mhtml"):
        raise AttachmentError("SVG and HTML files can't be attached.")
    if ext not in ALLOWED:
        raise AttachmentError("That kind of file can't be attached - photos, PDFs, text, CSV and Office files can.")
    limit = max_bytes()
    if (upload.size or 0) > limit:
        raise AttachmentError(f"“{clean_name(name)}” is over the {size_limit_label(limit)} limit.")
    raw = upload.read(limit + 1)
    if len(raw) > limit:
        raise AttachmentError(f"“{clean_name(name)}” is over the {size_limit_label(limit)} limit.")
    if not raw:
        raise AttachmentError(f"“{clean_name(name)}” is empty.")
    return raw, ext


def _key(user, ext):
    now = timezone.now()
    return f"attachments/{user.pk}/{now:%Y}/{now:%m}/{uuid.uuid4().hex}.{ext}"


def add(user, parent, upload):
    """Check, process and store one upload on `parent` (the caller has checked edit access). Returns the
    Attachment, or raises AttachmentError."""
    if for_parent(parent).count() >= max_per_parent():
        noun = {PARENTS["notes"]: "note", PARENTS["assistant"]: "message",
                PARENTS["meals"]: "recipe"}.get(parent._meta.label, "task")
        raise AttachmentError(f"A {noun} can have at most {max_per_parent()} attachments.")
    usage = storage.get_storage_usage(user)
    if usage["full"]:                                   # already full: say so before any processing
        raise AttachmentError(storage.full_message(usage["used_bytes"], usage["limit_bytes"]))
    raw, ext = check_upload(upload)
    width = height = None
    thumb = None
    out_ext = ext
    if ext in IMAGE_TYPES:
        data, thumb, mime, out_ext, width, height = process_image(raw, ext)
    elif ext == "pdf":
        if not raw.startswith(b"%PDF-"):
            raise AttachmentError("That file isn't really a PDF.")
        data, mime = raw, "application/pdf"
    elif ext in OFFICE:
        data, mime = raw, _check_office(raw, ext)
    else:
        data, mime = raw, _check_text(raw, ext)
    # Always use the extension we actually stored (HEIC or a misnamed photo.jpeg -> .jpg; a PNG called .jpg -> .png).
    name = clean_name(upload.name, out_ext if out_ext != ext else None)
    try:
        with transaction.atomic():
            # The size after downscaling, plus the thumbnail - checked and added under the lock on their row.
            storage.reserve(user, len(data) + len(thumb or b""))
            attachment = Attachment.objects.create(
                owner=user, content_type=_ct(parent), object_id=parent.pk, key=_key(user, out_ext),
                original_name=name, mime_type=mime, size_bytes=len(data), width=width, height=height, has_thumb=thumb is not None,
                thumb_size_bytes=len(thumb or b""), sha256=hashlib.sha256(data).hexdigest())
            attachment_storage.save_attachment_bytes(attachment, data, thumb)
    except storage.StorageFull as e:
        raise AttachmentError(str(e)) from None
    return attachment


def remove(attachment):
    attachment.delete()


# ---------------------------------------------------------------------------
# For templates, rows and the Bot API
# ---------------------------------------------------------------------------

def kind(attachment):
    """The file-type icon to show: image, pdf, doc, sheet, slides, text."""
    if attachment.is_image:
        return "image"
    ext = _extension(attachment.original_name)
    return {"pdf": "pdf", "docx": "doc", "xlsx": "sheet", "csv": "sheet", "pptx": "slides"}.get(ext, "text")


def describe(attachment):
    url = reverse("labs:attachment", args=[attachment.pk])
    return {"id": attachment.pk, "name": attachment.original_name, "type": attachment.mime_type,
            "size": attachment.size_bytes, "size_label": size_label(attachment.size_bytes), "url": url,
            "thumb_url": reverse("labs:attachment_thumb", args=[attachment.pk]) if attachment.has_thumb else url,
            "download_url": url + "?download=1", "is_image": attachment.is_image, "kind": kind(attachment),
            "width": attachment.width, "height": attachment.height,
            "delete_url": reverse("labs:attachment_delete", args=[attachment.pk])}


def section(user, parent, app, editable=None, next_url=""):
    """Context for albright_labs/_attachments.html."""
    rows = [describe(a) for a in for_parent(parent)]
    editable = can_edit(user, parent) if editable is None else editable
    return {"app": app, "parent_id": parent.pk, "images": [r for r in rows if r["is_image"]],
            "files": [r for r in rows if not r["is_image"]], "count": len(rows), "can_edit": editable,
            "upload_url": reverse("labs:attachment_upload"), "accept": ACCEPT, "max": max_per_parent(),
            "max_bytes": max_bytes(), "max_label": size_limit_label(max_bytes()), "next": next_url,
            "storage": storage.get_storage_usage(user) if editable else None}   # only shown with Add files


def api_list(parent, request=None):
    """Read-only metadata for the Bot API: [{"id", "name", "type", "size", "url"}]."""
    return api_lists(type(parent), [parent.pk], request).get(parent.pk, [])


def api_lists(model, ids, request=None):
    """{parent id: [metadata]} for many parents in one query."""
    out = {}
    for a in Attachment.objects.filter(content_type=_ct(model), object_id__in=list(ids)):
        url = reverse("labs:attachment", args=[a.pk])
        out.setdefault(a.object_id, []).append({
            "id": a.pk, "name": a.original_name, "type": a.mime_type, "size": a.size_bytes,
            "url": request.build_absolute_uri(url) if request else url})
    return out


def with_counts(queryset):
    """The queryset annotated with attachment_count (the paperclip on rows and cards)."""
    counts = Attachment.objects.filter(content_type=_ct(queryset.model), object_id=OuterRef("pk")) \
        .order_by().values("object_id").annotate(n=Count("pk")).values("n")
    return queryset.annotate(attachment_count=Coalesce(Subquery(counts, output_field=IntegerField()), Value(0)))


def storage_used(user):
    """(count, total bytes - files and thumbnails) of the attachments this person added."""
    totals = Attachment.objects.filter(owner=user).aggregate(n=Count("pk"),
                                                             total=Sum(F("size_bytes") + F("thumb_size_bytes")))
    return totals["n"] or 0, totals["total"] or 0


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------

def _delete_for_parent(sender, instance, **kwargs):
    Attachment.objects.filter(content_type=_ct(sender), object_id=instance.pk).delete()


def _delete_bytes(sender, instance, **kwargs):
    attachment_storage.delete_attachment_bytes(instance)
    storage.release(instance.owner_id, instance.stored_bytes)   # same transaction as the delete


def orphans():
    """Attachments whose task or note no longer exists (a parent deleted without its signal - a raw delete)."""
    from django.apps import apps
    stale = Attachment.objects.none()
    for label in PARENTS.values():
        model = apps.get_model(label)
        ct = _ct(model)
        existing = model.objects.filter(pk=OuterRef("object_id"))
        stale = stale | Attachment.objects.filter(content_type=ct).exclude(Exists(existing))
    other = Attachment.objects.exclude(content_type__in=[_ct(apps.get_model(label)) for label in PARENTS.values()])
    return stale | other


def connect_signals():
    """Deleting a task or note deletes its attachments; deleting an attachment its bytes (AppConfig.ready)."""
    from django.apps import apps
    for label in PARENTS.values():
        post_delete.connect(_delete_for_parent, sender=apps.get_model(label), dispatch_uid=f"attachments-{label}")
    post_delete.connect(_delete_bytes, sender=Attachment, dispatch_uid="attachment-bytes")
