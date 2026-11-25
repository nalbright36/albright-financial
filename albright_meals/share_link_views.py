"""Public recipe share links (share_links.py).

  GET  /meals/r/<token>/          the public, read-only page - no sign-in (ungated: apps_registry Meals)
  GET/POST /meals/r/<token>/save/ "Save to my recipes" for someone signed in (confirm, then a copy in their Meals)
  POST /meals/recipes/<pk>/share-link/   the owner's Share box: action=create (default) | regenerate | off

The public page is built from share_links.public_recipe only (never the Recipe), under the strict public headers
(no inline script or style), noindex, Referrer-Policy: no-referrer, never cached, and rate-limited by IP. An unknown
or turned-off token is a friendly "This link is no longer active" page (404)."""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.templatetags.static import static
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST

from albright_labs import circles, emails, ratelimit
from albright_labs.security import landing_headers

from . import scope, share_links


def _private(response):
    response["X-Robots-Tag"] = "noindex, nofollow"
    response["Referrer-Policy"] = "no-referrer"
    return response


def _gone(request):
    return _private(render(request, "albright_meals/shared_recipe_gone.html", {"meta_title": "Link not active"},
                           status=404))


@never_cache
@landing_headers
def shared_recipe(request, token):
    if ratelimit.blocked(request, "recipe_link"):
        return _private(ratelimit.too_many(request, "recipe_link"))
    ratelimit.hit(request, "recipe_link")
    link = share_links.find(token)
    if link is None:
        return _gone(request)
    user = request.user
    if not (user.is_authenticated and user.pk == link.created_by_id):
        share_links.count_view(link)
    public = share_links.public_recipe(link)
    page = request.path
    context = {
        "r": public, "token": link.token,
        "meta_title": f"{public['title']} · Albright Labs", "meta_description": share_links.preview_line(public),
        "canonical": share_links.url(link), "og_image": emails.site_url(static("icons/og-image.png")),
        "signed_in": user.is_authenticated,
    }
    if user.is_authenticated:
        context["can_save"] = circles.can_use_app(user, "meals")
        context["is_mine"] = link.recipe.space_id in scope.space_ids(user) if context["can_save"] else False
        context["mine_url"] = reverse("albright_meals:recipe", args=[link.recipe_id]) if context["is_mine"] else ""
    else:
        context["join_url"] = (reverse("labs:share_signup", args=[share_links.join_reference(link)])
                               if share_links.invites_on() else "")
        context["login_url"] = f"{reverse('user_login')}?next={page}"
    return _private(render(request, "albright_meals/shared_recipe.html", context))


@login_required
def shared_recipe_save(request, token):
    """"Save to my recipes" from a share link, for someone signed in. GET: a small confirm page in the app - its own
    page, because the public page's Referrer-Policy: no-referrer makes browsers send "Origin: null" with a form post
    there, which the CSRF check rightly refuses. POST: the copy (share_links.save_copy), then open it."""
    link = share_links.find(token)
    if link is None:
        return _gone(request)
    if link.recipe.space_id in scope.space_ids(request.user):
        return redirect("albright_meals:recipe", pk=link.recipe_id)
    if request.method != "POST":
        return render(request, "albright_meals/shared_recipe_save.html", {
            "title": link.recipe.title, "by": share_links.sharer_name(link), "token": link.token,
            "can_save": circles.can_use_app(request.user, "meals")})
    copy = share_links.save_copy(link, request.user)
    if copy is None:
        messages.error(request, "Your account can't open Meals yet - ask the site's admin to turn it on.")
        return redirect("albright_meals:shared_recipe", token=token)
    messages.success(request, f"Saved {copy.title} to your recipes.")
    return redirect("albright_meals:recipe", pk=copy.pk)


def _link_json(link):
    if link is None:
        return {"ok": True, "active": False, "url": ""}
    return {"ok": True, "active": True, "url": share_links.url(link), "views": link.view_count,
            "created": f"{link.created_at:%b} {link.created_at.day}, {link.created_at.year}"}


@login_required
@require_POST
def recipe_share_link(request, pk):
    """The Share box's "Copy share link" (make it, or the one there is), "New link" and "Turn off" - the recipe's
    owners only (a space that sees it)."""
    recipe = scope.owned_recipe(request.user, pk)
    action = request.POST.get("action", "create")
    if action == "off":
        share_links.turn_off(recipe)
        link, message = None, "Share link turned off. It no longer works."
    elif action == "regenerate":
        link, message = share_links.regenerate(recipe, request.user), "New share link made. The old one stopped working."
    else:
        link, created = share_links.get_or_create(recipe, request.user)
        message = "Share link made - anyone with it can see this recipe." if created else "Share link copied."
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return JsonResponse({**_link_json(link), "message": message})
    messages.success(request, message)
    nxt = request.POST.get("next", "")
    if nxt and url_has_allowed_host_and_scheme(nxt, allowed_hosts={request.get_host()}):
        return redirect(nxt)
    return redirect(reverse("albright_meals:recipe", args=[recipe.pk]) + "#share-box")
