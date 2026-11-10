"""A site suggestion is an inbox item, not an Idea: new, declined or promoted.

Forward: considering / planned / in_progress -> new, done -> promoted; new and
declined stay. Then the choices shrink to the three and promoted_idea is added.
Going back: promoted -> done (the others were already valid Idea statuses)."""
from django.db import migrations, models
import django.db.models.deletion

FORWARD = {"considering": "new", "planned": "new", "in_progress": "new", "done": "promoted"}
BACKWARD = {"promoted": "done"}

OLD_PLUS_PROMOTED = [('new', 'New'), ('considering', 'Considering'), ('planned', 'Planned'),
                     ('in_progress', 'In progress'), ('done', 'Done'), ('declined', 'Declined'),
                     ('promoted', 'Promoted')]


def remap(mapping):
    def run(apps, schema_editor):
        SiteSuggestion = apps.get_model("albright_bot_api", "SiteSuggestion")
        for old, new in mapping.items():
            SiteSuggestion.objects.filter(status=old).update(status=new)
    return run


class Migration(migrations.Migration):

    dependencies = [
        ('albright_bot_api', '0006_alter_sitesuggestion_app'),
        ('albright_control', '0003_idea'),
    ]

    operations = [
        # 1. Allow promoted alongside the old statuses, so rows can move.
        migrations.AlterField(
            model_name='sitesuggestion',
            name='status',
            field=models.CharField(choices=OLD_PLUS_PROMOTED, db_index=True, default='new', max_length=11),
        ),
        # 2. Move the rows.
        migrations.RunPython(remap(FORWARD), remap(BACKWARD)),
        # 3. Only the inbox statuses, and the link to the Idea a suggestion was promoted to.
        migrations.AlterField(
            model_name='sitesuggestion',
            name='status',
            field=models.CharField(choices=[('new', 'New'), ('declined', 'Declined'), ('promoted', 'Promoted')],
                                   db_index=True, default='new', max_length=11),
        ),
        migrations.AddField(
            model_name='sitesuggestion',
            name='promoted_idea',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                                    related_name='from_suggestions', to='albright_control.idea'),
        ),
    ]
