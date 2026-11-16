"""Attachment endpoints (the rules: albright_labs.attachments; the bytes: attachment_storage).

    POST /attachments/upload/              parent=planner|notes, id, file (one or more) - edit access to the parent
    GET  /attachments/<id>/                the file - view access; ?download=1 always downloads
    GET  /attachments/<id>/thumb/          an image's ~400px WebP thumbnail
    POST /attachments/<id>/delete/         edit access

No access - not signed in, not shared with you, view-only for a write - is a 404, never a 403. Upload and delete
answer JSON to fetch/XHR (attachments.js) and redirect back to `next` for a plain form post (no JavaScript).
Files are served with nosniff, inline only for images and PDFs (everything else downloads), and an ETag with
Cache-Control: private, max-age=86400 so a phone keeps the photos it already has."""
from django.contrib import messages
from django.http import FileResponse, Http404, HttpResponseNotModified, JsonResponse
from django.shortcuts import redirect
from django.template.loader import render_to_string
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_GET, require_POST

from . import attachment_storage, attachments

INLINE = ("image/", "application/pdf")


def _wants_json(request):
    return request.headers.get("X-Requested-With") == "XMLHttpRequest" or \
        "application/json" in request.headers.get("Accept", "")


def _back(request):
    target = request.POST.get("next", "")
    if target and url_has_allowed_host_and_scheme(target, allowed_hosts={request.get_host()},
                                                  require_https=request.is_secure()):
        return target
    return "/"


@require_POST
def upload(request):
    app = request.POST.get("parent", "")
    parent = attachments.find_parent(app, request.POST.get("id"))
    if not request.user.is_authenticated or not attachments.can_edit(request.user, parent):
        raise Http404
    files = request.FILES.getlist("file")
    added, errors = [], []
    if not files:
        errors.append("Choose a file to attach.")
    for upload_file in files:
        try:
            added.append(attachments.add(request.user, parent, upload_file))
        except attachments.AttachmentError as exc:
            errors.append(str(exc))
    if _wants_json(request):
        rows = []
        for a in added:
            row = attachments.describe(a)
            row["html"] = render_to_string("albright_labs/_attachment_item.html", {"a": row, "can_edit": True},
                                           request=request)
            rows.append(row)
        status = 201 if added else 400
        return JsonResponse({"ok": bool(added), "attachments": rows, "errors": errors,
                             "count": attachments.for_parent(parent).count()}, status=status)
    for a in added:
        messages.success(request, f"Attached “{a.original_name}”.")
    for e in errors:
        messages.error(request, e)
    return redirect(_back(request))


def _serve(request, pk, thumb):
    attachment = attachments.visible_attachment(request.user, pk)
    if attachment is None or (thumb and not attachment.has_thumb):
        raise Http404
    etag = f'"{attachment.sha256}{"-t" if thumb else ""}"'
    headers = {"ETag": etag, "Cache-Control": "private, max-age=86400", "X-Content-Type-Options": "nosniff"}
    if etag in [t.strip() for t in request.headers.get("If-None-Match", "").split(",")]:
        response = HttpResponseNotModified()
        for k, v in headers.items():
            response[k] = v
        return response
    handle = attachment_storage.open_attachment(attachment, thumb=thumb)
    if handle is None:
        raise Http404
    mime = "image/webp" if thumb else attachment.mime_type
    inline = mime.startswith(INLINE) and request.GET.get("download") != "1"
    name = attachment.original_name
    if thumb:
        name = name.rsplit(".", 1)[0] + "-thumb.webp"
    response = FileResponse(handle, content_type=mime, as_attachment=not inline, filename=name)
    for k, v in headers.items():
        response[k] = v
    return response


@require_GET
def serve(request, pk):
    return _serve(request, pk, thumb=False)


@require_GET
def serve_thumb(request, pk):
    return _serve(request, pk, thumb=True)


@require_POST
def delete(request, pk):
    attachment = attachments.visible_attachment(request.user, pk, edit=True)
    if attachment is None:
        raise Http404
    parent = attachment.parent
    name = attachment.original_name
    attachments.remove(attachment)
    if _wants_json(request):
        return JsonResponse({"ok": True, "count": attachments.for_parent(parent).count()})
    messages.success(request, f"Removed “{name}”.")
    return redirect(_back(request))
