"""The public home page for visitors, its invite form and footer pages, and Admin > Invite requests.

  "/" (visitors)  - landing: the marketing page (templates/albright_labs/landing.html). It extends
                    public_base.html, not the app's index.html - no app nav, no launcher, nothing private.
                    Strict headers (albright_resume.security.landing_headers): scripts and styles from this
                    site only, plus Google Fonts. Everything is static files; there's no inline style or script.
  "/invite/"      - the "Request an invite" form's POST (albright_labs.invite_requests): CSRF, a valid email,
                    5 posts an hour per IP, a honeypot field. It ends on "/?invite=thanks#invite" (the thanks
                    message in place of the form) whether the email was new, a repeat or a bot's.
  "/privacy/", "/terms/", "/contact/" - placeholder pages in the same look.
  /control/labs/  - Admin > Invite requests: Send invite (a real Invitation, emailed) or Decline.
"""
from django.conf import settings
from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.templatetags.static import static
from django.urls import reverse
from django.views.decorators.http import require_POST

from albright_control.access import audit, superuser_required
from albright_resume.security import landing_headers

from . import apps_registry, emails, invites, ratelimit
from . import invite_requests as flow
from .forms import InviteRequestForm
from .models import InviteRequest

TITLE = "Albright Labs - everything you run at home, in one place"
DESCRIPTION = ("Plan the week, cook from your own recipe book, log workouts and meals, keep notes and watch your "
               "money. One invite-only app, shared with the people you live with.")
THANKS = "Thanks - we'll email you an invite link."
THANKS_PARAM = "thanks"

PAGES = {
    "privacy": ("Privacy", "Our privacy policy is on its way. Until it's here, the short version: everything you "
                           "add starts private, you choose what each circle can see, and we don't sell your data."),
    "terms": ("Terms", "Our terms of use are on their way. Albright Labs is invite-only while it grows."),
    "contact": ("Contact", "A contact form is on its way. In the meantime, request an invite from the home page "
                           "and add a note - it comes straight to us."),
}


def _public_context(home, **extra):
    """What every public page needs: the share card (Open Graph) and where the nav's section links point."""
    return {"home": home, "meta_title": TITLE, "meta_description": DESCRIPTION,
            "canonical": emails.site_url("/"), "og_image": emails.site_url(static("icons/og-image.png")),
            **extra}


@landing_headers
def landing(request, form=None, status=200):
    """"/" for someone not signed in (labs.views.root sends them here)."""
    thanks = form is None and request.GET.get("invite") == THANKS_PARAM
    return render(request, "albright_labs/landing.html",
                  _public_context("", form=form or InviteRequestForm(), thanks=thanks, thanks_text=THANKS),
                  status=status)


@require_POST
def invite_request(request):
    if ratelimit.blocked(request, "invite"):
        return ratelimit.too_many(request, "invite")
    ratelimit.hit(request, "invite")
    form = InviteRequestForm(request.POST)
    if form.data.get("website"):                     # the honeypot: a bot filled the hidden field
        return _thanks()
    if not form.is_valid():
        return landing(request, form=form, status=400)
    flow.record(request, form.cleaned_data["email"], form.cleaned_data["note"])
    return _thanks()


def _thanks():
    return redirect(f"{reverse('labs:root')}?invite={THANKS_PARAM}#invite")


@landing_headers
def placeholder(request, page):
    title, body = PAGES[page]
    return render(request, "albright_labs/public_page.html",
                  _public_context(reverse("labs:root"), page_title=title, body=body,
                                  meta_title=f"{title} · Albright Labs"))


# --- Admin > Invite requests ------------------------------------------------------------------------------------

STATUS_FILTERS = (("", "All"), ("new", "New"), ("invited", "Invited"), ("declined", "Declined"))


def _app_choices():
    return [(app.key, app.label, app.key in flow.DEFAULT_APPS)
            for app in apps_registry.APPS if not app.superuser_only]


@superuser_required
def invite_requests_page(request, sent=None):
    status = request.GET.get("status", "")
    if status not in dict(STATUS_FILTERS):
        status = ""
    rows = InviteRequest.objects.select_related("handled_by", "invitation")
    counts = {key: (rows.filter(status=key).count() if key else rows.count()) for key, _ in STATUS_FILTERS}
    if status:
        rows = rows.filter(status=status)
    return render(request, "albright_labs/invite_requests.html", {
        "rows": rows[:200], "status": status, "filters": [(k, label, counts[k]) for k, label in STATUS_FILTERS],
        "app_choices": _app_choices(), "email_configured": settings.EMAIL_CONFIGURED, "sent": sent})


@superuser_required
@require_POST
def invite_request_send(request, pk):
    row = get_object_or_404(InviteRequest, pk=pk)
    if row.status != "new":
        messages.error(request, f"That request is already {row.get_status_display().lower()}.")
        return redirect("labs_admin:invite_requests")
    allowed = set(apps_registry.grantable_keys())
    apps = [key for key in apps_registry.grantable_keys() if key in set(request.POST.getlist("apps")) & allowed]
    invite, raw, ok, message = flow.send_invite(row, request.user, apps)
    audit(request, "invite_request_sent", f"#{row.pk} {row.email}: invite #{invite.pk}, "
                                          f"{', '.join(apps) or 'no apps'}; {'emailed' if ok else 'not emailed - ' + message}")
    return invite_requests_page(request, sent={"row": row, "link": invites.link(raw), "ok": ok, "message": message})


@superuser_required
@require_POST
def invite_request_decline(request, pk):
    row = get_object_or_404(InviteRequest, pk=pk)
    if row.status != "new":
        messages.error(request, f"That request is already {row.get_status_display().lower()}.")
    else:
        flow.decline(row, request.user)
        audit(request, "invite_request_declined", f"#{row.pk} {row.email}")
        messages.success(request, f"Declined the request from {row.email}. Nothing was sent.")
    return redirect("labs_admin:invite_requests")
