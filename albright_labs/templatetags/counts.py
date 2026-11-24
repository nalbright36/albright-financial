"""{{ n|plural:"item" }} -> "17 items" / "1 item"; {{ n|plural:"box,boxes" }} for an irregular one (albright_labs.text).
A builtin (settings TEMPLATES) - no {% load %} needed. The count and its word are one piece of text, so a flex button's
gap can never split them ("17 item s")."""
from django import template

from albright_labs.text import plural as _plural

register = template.Library()


@register.filter(name="plural")
def plural(n, words="item"):
    one, _, many = str(words).partition(",")
    return _plural(n, one.strip(), many.strip() or None)
