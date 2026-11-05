"""Notes' shares move to the one Share system (data): NoteShare (person, view / edit) -> albright_labs.ShareGrant, checked per person (counts in the output). """
from django.db import migrations

from albright_labs.share_copy import copy_back, copy_shares


def forwards(apps, schema_editor):
    copy_shares(apps, app_label="albright_notes", model="Note", share_model="NoteShare", item_field="note", user_field="user",
                permission="permission", created_by=lambda share, item: item.owner_id, seen_field=None, label="Notes")


def backwards(apps, schema_editor):
    copy_back(apps, app_label="albright_notes", model="Note", share_model="NoteShare", item_field="note", user_field="user",
              permission="permission", seen_field=None)


class Migration(migrations.Migration):

    dependencies = [
        ("albright_notes", "0001_initial"),
        ("albright_labs", "0010_sharing"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
