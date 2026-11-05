"""Notes' shares move to the one Share system (schema): the old NoteShare table is gone - ShareGrant has its rows (0002_shares_to_grants)."""
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("albright_notes", "0002_shares_to_grants"),
    ]

    operations = [
        migrations.DeleteModel(name="NoteShare"),
    ]
