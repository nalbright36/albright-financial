"""Households - shared by every app that uses one (Money, Meals). A person is in at most one. Whoever created a
household manages it (My account > Household): rename, add or remove members, and invite someone new - the invite
signs them up straight into it."""
from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.db.models import Q

from .models import Household, HouseholdMember, Invitation, household_of

# What a household invite can give someone (and only those the inviter has): the apps that use households.
HOUSEHOLD_APPS = ("meals", "money")

__all__ = ["household_of", "create", "members", "add_member", "remove_member", "find_person", "invite", "can_manage",
           "HouseholdError"]


class HouseholdError(ValueError):
    """Something the person can fix - the message is shown as is."""


def members(household):
    if household is None:
        return []
    return [m.user for m in HouseholdMember.objects.filter(household=household).select_related("user")]


def member_ids(household):
    return set(HouseholdMember.objects.filter(household=household).values_list("user_id", flat=True)) if household else set()


@transaction.atomic
def create(user, name=""):
    """A new household with `user` as its only member (and its creator)."""
    if household_of(user):
        raise HouseholdError("You're already in a household.")
    name = " ".join((name or "").split())[:100] or f"{(user.first_name or user.get_username()).strip()}'s household"
    household = Household.objects.create(name=name, created_by=user)
    HouseholdMember.objects.create(household=household, user=user)
    return household


def can_manage(user, household):
    """The creator manages it (a superuser too, so a household is never stuck without one)."""
    return household is not None and (household.created_by_id == user.pk or (user.is_superuser and
                                                                             household_of(user) == household))


def find_person(text):
    """An account by its exact username or email (any case) - never a partial match or a list."""
    text = (text or "").strip()
    if not text:
        return None
    found = get_user_model().objects.filter(is_active=True).filter(Q(username__iexact=text) | Q(email__iexact=text))
    return found.first() if found.count() == 1 else None


def add_member(household, user):
    if HouseholdMember.objects.filter(user=user).exists():
        raise HouseholdError("They're already in a household.")
    try:
        with transaction.atomic():
            return HouseholdMember.objects.create(household=household, user=user)
    except IntegrityError:
        raise HouseholdError("They're already in a household.") from None


def remove_member(household, user):
    deleted, _ = HouseholdMember.objects.filter(household=household, user=user).delete()
    return bool(deleted)


def invite(household, by, email, name=""):
    """An invite (the existing invite system) that signs them up into this household, with the household apps the
    inviter can open. Returns (invite, raw token)."""
    from . import app_access
    from .models import UserPreference
    email = (email or "").strip()
    if not email or "@" not in email:
        raise HouseholdError("Type their email address.")
    if get_user_model().objects.filter(email__iexact=email).exists():
        raise HouseholdError("They already have an account - add them by their username or email above instead.")
    pref = UserPreference.for_user(by)
    apps = [key for key in HOUSEHOLD_APPS if app_access.can_use(by, pref, key)]
    return Invitation.create(email=email, name=name[:120], apps=apps, created_by=by, household=household)
