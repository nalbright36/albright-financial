"""{{ n|filesize }} - bytes as B / KB / MB / GB (binary units, one decimal: albright_labs.storage.format_bytes);
{{ n|filesize_limit }} drops a ".0" (a tier's "1 GB")."""
from django import template

from albright_labs.storage import format_bytes, format_limit

register = template.Library()


@register.filter
def filesize(value):
    return format_bytes(value)


@register.filter
def filesize_limit(value):
    return format_limit(value)
