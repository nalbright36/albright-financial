"""One Share system (schema): ShareGrant (anything - content type + id - shown to one person or one circle, to view or
edit) and SharingDefault (what a person's new items in an app are shared with). Each app's own migration copies its
old shares in (Planner 0012, Notes 0002, Meals 0012, Fitness 0005) and then drops the old table."""
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("albright_labs", "0009_remove_household"),
        ("contenttypes", "0002_remove_content_type_name"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="ShareGrant",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("object_id", models.PositiveBigIntegerField()),
                ("permission", models.CharField(choices=[("view", "Can view"), ("edit", "Can edit")], default="view", max_length=4)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("seen_at", models.DateTimeField(blank=True, null=True)),
                ("circle", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE,
                                             related_name="share_grants", to="albright_labs.circle")),
                ("content_type", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="+",
                                                   to="contenttypes.contenttype")),
                ("created_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                                                 related_name="+", to=settings.AUTH_USER_MODEL)),
                ("user", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE,
                                           related_name="share_grants", to=settings.AUTH_USER_MODEL)),
            ],
            options={
                "ordering": ["created_at", "pk"],
                "indexes": [models.Index(fields=["content_type", "object_id"], name="albright_la_content_3660cb_idx")],
                "constraints": [
                    models.CheckConstraint(condition=models.Q(("circle__isnull", True), ("user__isnull", False))
                                           | models.Q(("circle__isnull", False), ("user__isnull", True)),
                                           name="labs_share_user_or_circle"),
                    models.UniqueConstraint(condition=models.Q(("user__isnull", False)), fields=("content_type", "object_id", "user"),
                                            name="labs_share_once_per_user"),
                    models.UniqueConstraint(condition=models.Q(("circle__isnull", False)), fields=("content_type", "object_id", "circle"),
                                            name="labs_share_once_per_circle"),
                ],
            },
        ),
        migrations.CreateModel(
            name="SharingDefault",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("app", models.CharField(max_length=10)),
                ("mode", models.CharField(choices=[("me", "Only me"), ("circle", "A circle"), ("people", "Specific people")],
                                          default="me", max_length=6)),
                ("permission", models.CharField(choices=[("view", "Can view"), ("edit", "Can edit")], default="view", max_length=4)),
                ("people", models.JSONField(blank=True, default=dict)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("circle", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                                             related_name="+", to="albright_labs.circle")),
                ("user", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="sharing_defaults",
                                           to=settings.AUTH_USER_MODEL)),
            ],
            options={"constraints": [models.UniqueConstraint(fields=("user", "app"), name="labs_one_default_per_app")]},
        ),
    ]
