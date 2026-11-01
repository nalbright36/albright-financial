"""Deletes the items the automatic sources made (Reselling watchlist auction ends, Reselling ledger markdown
steps, Opportunities deadlines/follow-ups), with their check-offs, occurrence changes and shares.

Only rows the sync itself made are touched: source_app is one of those sources, created_by is that source
(what services.upsert_source_item writes), and not from a bot. Items you added yourself (no source_app),
note-linked items, bot-posted items, and items from any other app are left alone.

Each deleted one is also recorded in DeletedSourceItem, so it is never added again even if its source is
turned back on. Going backwards does nothing (the items can't be brought back)."""
from django.db import migrations

AUTO_SOURCES = ("watchlist", "markdown", "opportunities")


def auto_items(ScheduleItem):
    return [item for item in ScheduleItem.objects.filter(source_app__in=AUTO_SOURCES, from_bot=False)
            if item.created_by == item.source_app]


def forward(apps, schema_editor):
    ScheduleItem = apps.get_model("albright_planner", "ScheduleItem")
    ItemCompletion = apps.get_model("albright_planner", "ItemCompletion")
    ItemException = apps.get_model("albright_planner", "ItemException")
    ItemShare = apps.get_model("albright_planner", "ItemShare")
    DeletedSourceItem = apps.get_model("albright_planner", "DeletedSourceItem")
    items = auto_items(ScheduleItem)
    ids = [item.pk for item in items]
    for item in items:
        DeletedSourceItem.objects.get_or_create(owner_id=item.owner_id, source_app=item.source_app,
                                                source_id=item.source_id)
    ItemCompletion.objects.filter(item_id__in=ids).delete()
    ItemException.objects.filter(item_id__in=ids).delete()
    ItemShare.objects.filter(item_id__in=ids).delete()
    ScheduleItem.objects.filter(pk__in=ids).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('albright_planner', '0003_sources_off_by_default'),
    ]

    operations = [
        migrations.RunPython(forward, migrations.RunPython.noop),
    ]
