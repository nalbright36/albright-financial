"""{% weigh_in_box o %}: a weigh-in task's "Log weight" control (or its result) inside a Planner row, the view
panel or Home's Today panel - albright_fitness/_weigh_in_box.html. Nothing for any other item, or for someone
else's weigh-in."""
from django import template

from albright_fitness import weigh_in

register = template.Library()


@register.inclusion_tag("albright_fitness/_weigh_in_box.html", takes_context=True)
def weigh_in_box(context, occ, here=""):
    request = context.get("request")
    user = getattr(request, "user", None)
    item = getattr(occ, "item", None)
    if item is None or not item.is_weigh_in or user is None:
        return {"wi": None}
    wi = weigh_in.box(item, occ.occurrence_date, bool(getattr(occ, "done", False)), user)
    return {"wi": wi, "here": here or (request.get_full_path() if request else ""), "csrf_token": context.get("csrf_token")}
