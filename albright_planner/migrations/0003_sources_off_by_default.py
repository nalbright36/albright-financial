"""Sources become opt-in: PlannerSettings.sources_off (everything on unless listed) is replaced by sources_on
(everything off unless listed), starting empty for everyone - so every automatic source is off for all users.
Adds DeletedSourceItem, which keeps a deleted source item from ever being added again."""
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('albright_planner', '0002_links_sources_bots'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.RemoveField(
            model_name='plannersettings',
            name='sources_off',
        ),
        migrations.AddField(
            model_name='plannersettings',
            name='sources_on',
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.CreateModel(
            name='DeletedSourceItem',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('source_app', models.CharField(max_length=40)),
                ('source_id', models.CharField(max_length=64)),
                ('deleted_at', models.DateTimeField(auto_now_add=True)),
                ('owner', models.ForeignKey(on_delete=models.deletion.CASCADE, related_name='+',
                                            to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'constraints': [models.UniqueConstraint(fields=('owner', 'source_app', 'source_id'),
                                                        name='planner_deleted_source_once')],
            },
        ),
    ]
