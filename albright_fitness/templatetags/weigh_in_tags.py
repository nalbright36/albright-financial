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


@register.simple_tag
def workout_participants(occ):
    """A shared workout's task (a program people joined - programs.join): [{"user", "state"}] for everyone doing it,
    state "done" / "skipped" / "open" for that date - each person's own task. [] for anything else."""
    item = getattr(occ, "item", None)
    if item is None or not item.is_workout or not item.source_id.startswith("day-") or not item.source_id[4:].isdigit():
        return []
    from albright_planner.models import ItemCompletion, ScheduleItem

    from albright_fitness import programs
    from albright_fitness.models import ProgramDay
    day = ProgramDay.objects.filter(pk=item.source_id[4:]).select_related("program__owner").first()
    if day is None or not day.program.members.filter(ended_at__isnull=True).exists():
        return []
    people = programs.participants(day.program)
    items = {i.owner_id: i.pk for i in ScheduleItem.objects.filter(source_app="fitness", source_id=item.source_id,
                                                                    owner__in=people, archived=False)}
    states = {c.item_id: c.status for c in ItemCompletion.objects.filter(item_id__in=items.values(),
                                                                          occurrence_date=occ.occurrence_date)}
    return [{"user": u, "state": states.get(items.get(u.pk), "open")} for u in people]
