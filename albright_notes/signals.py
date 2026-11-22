"""Smart links, the other way (smart.py): a dated note item's Planner task checked off, reopened, moved or deleted,
or a tagged grocery line checked on the list - the note's line follows. Skipped while a note save is pushing its
own changes out (smart.quiet)."""
from django.db.models.signals import post_delete, post_save, pre_delete
from django.dispatch import receiver

from albright_meals.models import GroceryItem
from albright_planner.models import ItemCompletion, ScheduleItem

from . import smart


@receiver(post_save, sender=ItemCompletion, dispatch_uid="notes_task_completed")
@receiver(post_delete, sender=ItemCompletion, dispatch_uid="notes_task_reopened")
def completion_changed(sender, instance, raw=False, **kwargs):
    if raw or smart.is_quiet():
        return
    task = ScheduleItem.objects.filter(pk=instance.item_id, source_app=smart.TASK_SOURCE).first()
    if task is not None:
        smart.task_changed(task)


@receiver(post_save, sender=ScheduleItem, dispatch_uid="notes_task_saved")
def task_saved(sender, instance, raw=False, created=False, **kwargs):
    if raw or created or smart.is_quiet() or instance.source_app != smart.TASK_SOURCE:
        return
    smart.task_changed(instance)


@receiver(pre_delete, sender=ScheduleItem, dispatch_uid="notes_task_deleted")
def task_deleted(sender, instance, **kwargs):
    if not smart.is_quiet() and instance.source_app == smart.TASK_SOURCE:
        smart.task_deleted(instance)


@receiver(post_save, sender=GroceryItem, dispatch_uid="notes_grocery_saved")
def grocery_saved(sender, instance, raw=False, created=False, **kwargs):
    if raw or created or smart.is_quiet():
        return
    before = getattr(instance, "_activity_before", None)          # albright_meals.signals._before
    if before is not None and before.get("checked") == instance.checked:
        return
    smart.grocery_changed(instance)
