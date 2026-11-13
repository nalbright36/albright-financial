"""Connections (Option C, slice 1): Connection (a pair of people - a REMOVED row hides person ShareGrants
between them without deleting them; they are restored on reconnect) and ShareGrant.hidden_at."""
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("albright_labs", "0012_pushsubscription"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="sharegrant",
            name="hidden_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.CreateModel(
            name="Connection",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("status", models.CharField(choices=[("connected", "Connected"), ("removed", "Removed")],
                                            default="connected", max_length=10)),
                ("removed_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("removed_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                                                 related_name="+", to=settings.AUTH_USER_MODEL)),
                ("user_a", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE,
                                             related_name="connections_as_a", to=settings.AUTH_USER_MODEL)),
                ("user_b", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE,
                                             related_name="connections_as_b", to=settings.AUTH_USER_MODEL)),
            ],
        ),
        migrations.AddConstraint(
            model_name="connection",
            constraint=models.UniqueConstraint(fields=("user_a", "user_b"), name="labs_one_connection_per_pair"),
        ),
        migrations.AddConstraint(
            model_name="connection",
            constraint=models.CheckConstraint(condition=models.Q(("user_a_id__lt", models.F("user_b_id"))),
                                              name="labs_connection_ordered"),
        ),
    ]
