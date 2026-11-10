"""Grocery lines in task checklists (checklist.py): checking a line anywhere - the grocery list, a task, the Bot
API - re-checks whether each task it's in is now complete (or should reopen). Deleting a line keeps its last
checked state as a tick on the rows that listed it, so the task's history stays right. Deleting an Admin Idea
clears its tasks' links to its page."""
from django.db.models.signals import post_save, pre_delete
from django.dispatch import receiver

from albright_control.models import Idea
from albright_meals.models import GroceryItem

from . import checklist
from .models import ScheduleItem, TaskChecklistItem, idea_path


@receiver(post_save, sender=GroceryItem, dispatch_uid="planner_grocery_saved")
def grocery_saved(sender, instance, raw=False, **kwargs):
    if raw:
        return
    for row in TaskChecklistItem.objects.filter(grocery_item=instance).select_related("task__owner"):
        checklist.reevaluate(row.task, checklist.occurrence_for_groceries(row.task), instance.checked_by)


@receiver(pre_delete, sender=GroceryItem, dispatch_uid="planner_grocery_deleted")
def grocery_deleted(sender, instance, **kwargs):
    checklist.keep_grocery_tick(instance)


@receiver(pre_delete, sender=Idea, dispatch_uid="planner_idea_deleted")
def idea_deleted(sender, instance, **kwargs):
    """The FK nulls itself (SET_NULL); the tasks' links back to that Idea's page go too."""
    ScheduleItem.objects.filter(idea=instance, source_url=idea_path(instance.pk)).update(source_url="")
