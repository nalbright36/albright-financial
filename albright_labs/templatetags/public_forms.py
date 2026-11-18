"""{% public_input field %} for the logged-out form pages (templates/_public_field.html): the field's own widget
(its type, autocomplete, maxlength, required...) with the public input class, aria-invalid when it has errors,
and aria-describedby pointing at its help and first error."""
from django import template

register = template.Library()


@register.simple_tag
def public_input(field, **attrs):
    widget_attrs = {"class": "ld-input pa-input"}
    described = []
    if field.help_text:
        described.append(f"{field.auto_id}-help")
    if field.errors:
        widget_attrs["aria-invalid"] = "true"
        described.append(f"{field.auto_id}-error")
    if described:
        widget_attrs["aria-describedby"] = " ".join(described)
    widget_attrs.update(attrs)
    return field.as_widget(attrs=widget_attrs)
