"""Home's quick-add popups (albright_labs/quick_actions.py) post straight to each app's own save view with the header
X-Quick-Add: 1. Those views keep their own validation and save code and only answer differently: JSON from here
instead of a redirect and a flash message.

    if quick.wanted(request):
        return quick.ok(f"Task added", url)          # or quick.error("Type a title.") / quick.form_error(form)

hub.js closes the popup, shows a toast ("Task added · View") and refreshes the Today panel and tile badges.
"""
from django.http import JsonResponse

HEADER = "X-Quick-Add"


def wanted(request):
    return request.headers.get(HEADER) == "1"


def ok(message, url="", link="View", **extra):
    return JsonResponse({"ok": True, "message": message, "url": url, "link": link if url else "", **extra})


def error(errors, status=400, **extra):
    errors = [errors] if isinstance(errors, str) else [str(e) for e in errors]
    return JsonResponse({"ok": False, "errors": errors, **extra}, status=status)


def form_errors(form):
    """A form's errors as plain lines ("Title: This field is required.")."""
    lines = []
    for name, errs in form.errors.items():
        label = form.fields[name].label if name in form.fields and form.fields[name].label else name.replace("_", " ").capitalize()
        for e in errs:
            lines.append(e if name == "__all__" else f"{label}: {e}")
    return lines


def form_error(form, status=400):
    return error(form_errors(form), status=status)
