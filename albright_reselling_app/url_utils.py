"""Shared "send me back where I came from" handling for this app's forms."""
from django.shortcuts import resolve_url
from django.utils.http import url_has_allowed_host_and_scheme


def safe_next_url(request, default):
    """The request's "next" (POST, then GET) when it's a URL on this site -
    same host, no scheme/host tricks like "//evil.com" or
    "https://evil.com" - otherwise `default` (a URL name or path). Never an
    open redirect."""
    next_url = request.POST.get("next") or request.GET.get("next")
    if next_url and url_has_allowed_host_and_scheme(
        next_url, allowed_hosts={request.get_host()}, require_https=request.is_secure(),
    ):
        return next_url
    return resolve_url(default)


def wants_json(request):
    """A fetch() call from this app's own JS (it always sends this header)."""
    return request.headers.get("x-requested-with") == "XMLHttpRequest"
