"""Grocery lines in task checklists (checklist.py): checking a line anywhere - the grocery list, a task, the Bot
API - re-checks whether each task it's in is now complete (or should reopen). Deleting a line keeps its last
checked state on the rows that listed it, so the task's history stays right."""
from django.db.models.signals import post_save, pre_delete
from django.dispatch import receiver

from albright_meals.models import GroceryItem

from . import checklist
from .models import TaskChecklistItem


@receiver(post_save, sender=GroceryItem, dispatch_uid="planner_grocery_saved")
def grocery_saved(sender, instance, raw=False, **kwargs):
    if raw:
        return
    for row in TaskChecklistItem.objects.filter(grocery_item=instance).select_related("task__owner"):
        checklist.reevaluate(row.task)


@receiver(pre_delete, sender=GroceryItem, dispatch_uid="planner_grocery_deleted")
def grocery_deleted(sender, instance, **kwargs):
    TaskChecklistItem.objects.filter(grocery_item=instance).update(done=instance.checked)
