"""Circles replace the Household (schema + data).

Schema: Circle, CircleMembership, CircleInvite and Space; UserPreference.current_spaces (the space each app last
showed); Invitation.circle_invite (a sign-up invite that brings a circle invite with it). Circle.legacy_household
remembers which household a circle came from, for Meals' and Money's data migrations - 0009 removes it.

Data - each Household becomes a Circle, so nobody loses anything they can see:
  - same name and creator; Meals and Money enabled;
  - every member ACTIVE and opted in to Meals and Money; the creator is the owner (or, if the creator had left, the
    longest-standing member - a circle always has an owner);
  - a Space for the circle - and it's what Meals and Money open on for its members (UserPreference.current_spaces),
    so the household's plan, list and shared money are right where they were (their Personal space is a tap away);
  - a pending household invite (unused, unrevoked, unexpired) becomes a pending circle invite, linked to the same
    sign-up invite, so signing up with it still brings the person in (they now choose their apps when accepting).
"""
import hashlib
import secrets

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models
from django.utils import timezone

APPS = ["meals", "money"]


def households_to_circles(apps, schema_editor):
    Household = apps.get_model("albright_labs", "Household")
    HouseholdMember = apps.get_model("albright_labs", "HouseholdMember")
    Circle = apps.get_model("albright_labs", "Circle")
    CircleMembership = apps.get_model("albright_labs", "CircleMembership")
    CircleInvite = apps.get_model("albright_labs", "CircleInvite")
    Space = apps.get_model("albright_labs", "Space")
    Invitation = apps.get_model("albright_labs", "Invitation")
    UserPreference = apps.get_model("albright_labs", "UserPreference")
    now = timezone.now()
    for household in Household.objects.all().order_by("pk"):
        circle = Circle.objects.create(name=household.name, created_by_id=household.created_by_id,
                                       enabled_apps=list(APPS), legacy_household=household.pk)
        Circle.objects.filter(pk=circle.pk).update(created_at=household.created_at)
        members = list(HouseholdMember.objects.filter(household=household).order_by("joined_at", "pk"))
        owner_id = household.created_by_id if any(m.user_id == household.created_by_id for m in members) else \
            (members[0].user_id if members else None)
        for m in members:
            CircleMembership.objects.create(circle=circle, user_id=m.user_id, status="active", joined_at=m.joined_at,
                                            role="owner" if m.user_id == owner_id else "member", apps=list(APPS))
        space = Space.objects.create(circle=circle)
        for m in members:
            # Only the remembered space changes; a missing row is made with the model's defaults - exactly what
            # someone without a row already has (UserPreference.for_user), so nobody's app access changes.
            pref, _created = UserPreference.objects.get_or_create(user_id=m.user_id)
            pref.current_spaces = {**(pref.current_spaces or {}), **{app: space.pk for app in APPS}}
            pref.save(update_fields=["current_spaces"])
        for invite in Invitation.objects.filter(household=household, used_at__isnull=True, revoked=False, expires_at__gt=now):
            raw = secrets.token_urlsafe(32)                       # nobody needs this one: the sign-up link carries them
            ci = CircleInvite.objects.create(circle=circle, email=invite.email.lower(), invited_by_id=invite.created_by_id,
                                             token_hash=hashlib.sha256(raw.encode()).hexdigest(), expires_at=invite.expires_at)
            Invitation.objects.filter(pk=invite.pk).update(circle_invite=ci)


def undo(apps, schema_editor):
    Circle = apps.get_model("albright_labs", "Circle")
    Circle.objects.filter(legacy_household__isnull=False).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("albright_labs", "0007_household"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="Circle",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=100)),
                ("color", models.CharField(blank=True, max_length=7)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("enabled_apps", models.JSONField(blank=True, default=list)),
                ("legacy_household", models.IntegerField(blank=True, null=True)),
                ("created_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                                                 related_name="+", to=settings.AUTH_USER_MODEL)),
            ],
            options={"ordering": ["name", "pk"]},
        ),
        migrations.CreateModel(
            name="CircleMembership",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("role", models.CharField(choices=[("owner", "Owner"), ("member", "Member")], default="member", max_length=6)),
                ("status", models.CharField(choices=[("invited", "Invited"), ("active", "Active"), ("left", "Left"),
                                                     ("removed", "Removed")], default="invited", max_length=7)),
                ("joined_at", models.DateTimeField(blank=True, null=True)),
                ("apps", models.JSONField(blank=True, default=list)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("circle", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="memberships",
                                             to="albright_labs.circle")),
                ("invited_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                                                 related_name="+", to=settings.AUTH_USER_MODEL)),
                ("user", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="circle_memberships",
                                           to=settings.AUTH_USER_MODEL)),
            ],
            options={"ordering": ["joined_at", "pk"],
                     "constraints": [models.UniqueConstraint(fields=("circle", "user"), name="labs_one_membership_per_circle")]},
        ),
        migrations.CreateModel(
            name="CircleInvite",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("email", models.EmailField(blank=True, max_length=254)),
                ("token_hash", models.CharField(editable=False, max_length=64, unique=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("expires_at", models.DateTimeField()),
                ("accepted_at", models.DateTimeField(blank=True, null=True)),
                ("declined_at", models.DateTimeField(blank=True, null=True)),
                ("revoked_at", models.DateTimeField(blank=True, null=True)),
                ("circle", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="invites",
                                             to="albright_labs.circle")),
                ("invited_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                                                 related_name="+", to=settings.AUTH_USER_MODEL)),
                ("user", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE,
                                           related_name="circle_invites", to=settings.AUTH_USER_MODEL)),
            ],
            options={"ordering": ["-created_at", "-pk"]},
        ),
        migrations.CreateModel(
            name="Space",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("circle", models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE,
                                                related_name="space", to="albright_labs.circle")),
                ("user", models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE,
                                              related_name="personal_space", to=settings.AUTH_USER_MODEL)),
            ],
            options={"constraints": [models.CheckConstraint(
                condition=models.Q(("circle__isnull", True), ("user__isnull", False))
                | models.Q(("circle__isnull", False), ("user__isnull", True)),
                name="labs_space_personal_or_circle")]},
        ),
        migrations.AddField(
            model_name="userpreference",
            name="current_spaces",
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddField(
            model_name="invitation",
            name="circle_invite",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                                    related_name="signups", to="albright_labs.circleinvite"),
        ),
        migrations.RunPython(households_to_circles, undo),
    ]
