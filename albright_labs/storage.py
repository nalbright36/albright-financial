"""Storage - how much each person's attachments take up, their tier's limit, and keeping uploads under it.

Usage is every Attachment a person OWNS (uploaded) - the file and its thumbnail (Attachment.stored_bytes). Files
others shared with them don't count. It's cached on UserStorage.used_bytes and kept right in the same transaction
as each change: reserve() checks the limit and adds the bytes under select_for_update on the person's row (two
uploads at once can't both squeeze in), and an Attachment's post_delete (also when a task or note takes its
attachments with it) takes them off with an F() expression. recalculate() recounts from the attachments
themselves (manage.py recalc_storage_usage, Admin > Storage).

The limit comes from the person's StorageTier, or the default tier when they have none (UserStorage.tier is
null) - so changing the default moves everyone without an explicit tier. A tier below someone's usage is
allowed: they keep their files and can't upload until they're under it again.

    get_storage_limits(user) -> StorageLimits (tier, limit_bytes, max_file_bytes - room for more per-tier rules)
    get_storage_limit(user)  -> limit_bytes
    get_storage_usage(user)  -> {"used_bytes", "limit_bytes", "percent", "remaining_bytes", ...}
"""
from dataclasses import dataclass

from django.conf import settings
from django.db import transaction
from django.db.models import F, Sum, Value
from django.db.models.functions import Greatest

GB = 1024 ** 3
FALLBACK_LIMIT = GB                     # only if no tier exists at all (the migration makes Standard, 1 GB)
WARN_PERCENT = 80


class StorageFull(Exception):
    pass


# ---------------------------------------------------------------------------
# Sizes - binary units, so "1 GB" is exactly a 1 GB tier's limit
# ---------------------------------------------------------------------------

def format_bytes(n):
    """0 B, 512 B, 1.5 KB, 312.0 MB, 1.0 GB - binary units (1 KB = 1024 B), one decimal above bytes."""
    n = max(int(n or 0), 0)
    if n < 1024:
        return f"{n} B"
    for unit in ("KB", "MB", "GB", "TB"):
        n /= 1024
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}"


def format_limit(n):
    """A limit reads as a round number when it is one: "1 GB", "2.5 GB", "500 MB"."""
    text = format_bytes(n)
    return text.replace(".0 ", " ")


# ---------------------------------------------------------------------------
# Tiers and limits
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class StorageLimits:
    tier: object                         # the StorageTier, or None when there's none at all
    limit_bytes: int
    max_file_bytes: int                  # today the site-wide setting for everyone; a tier could set its own

    @property
    def tier_name(self):
        return self.tier.name if self.tier else "Standard"


def default_tier():
    """The default tier - Standard (1 GB) is made if there are no tiers at all (the migration makes it too)."""
    from .models import StorageTier
    tier = StorageTier.objects.filter(is_default=True).first()
    if tier is None:
        tier = StorageTier.objects.order_by("sort_order", "pk").first()
    if tier is None:
        tier, _ = StorageTier.objects.get_or_create(slug="standard", defaults={
            "name": "Standard", "limit_bytes": FALLBACK_LIMIT, "is_default": True})
    return tier


def storage_row(user, lock=False):
    """The person's UserStorage (made on first use). lock: select_for_update - inside a transaction."""
    from .models import UserStorage
    row, _ = UserStorage.objects.get_or_create(user=user)
    if lock:
        row = UserStorage.objects.select_for_update(of=("self",)).select_related("tier").get(pk=row.pk)
    return row


def _limits_for(row):
    tier = row.tier if row is not None and row.tier_id else default_tier()
    return StorageLimits(tier=tier, limit_bytes=tier.limit_bytes if tier else FALLBACK_LIMIT,
                         max_file_bytes=getattr(settings, "ATTACHMENT_MAX_BYTES", 15 * 1024 * 1024))


def get_storage_limits(user):
    from .models import UserStorage
    return _limits_for(UserStorage.objects.select_related("tier").filter(user=user).first())


def get_storage_limit(user):
    return get_storage_limits(user).limit_bytes


def status_for(percent):
    """"ok" (the app's accent), "warn" (amber, 80-99%) or "full" (red, 100%+)."""
    if percent >= 100:
        return "full"
    return "warn" if percent >= WARN_PERCENT else "ok"


def describe(used, limits):
    limit = limits.limit_bytes
    percent = round(used / limit * 100, 1) if limit > 0 else (100.0 if used else 0.0)
    return {"used_bytes": used, "limit_bytes": limit, "percent": percent,
            "remaining_bytes": max(limit - used, 0), "tier": limits.tier, "tier_name": limits.tier_name,
            "status": status_for(percent), "bar": min(percent, 100), "bar_css": f"{min(percent, 100):.1f}%",
            "full": used >= limit,
            "used_label": format_bytes(used), "limit_label": format_limit(limit),
            "label": f"{format_bytes(used)} of {format_limit(limit)} used"}


def get_storage_usage(user):
    """{"used_bytes", "limit_bytes", "percent", "remaining_bytes"} plus what the pages show (tier_name, status,
    bar width, labels)."""
    from .models import UserStorage
    row = UserStorage.objects.select_related("tier").filter(user=user).first()
    return describe(row.used_bytes if row else 0, _limits_for(row))


def full_message(used, limit):
    return (f"You've used {format_bytes(used)} of your {format_limit(limit)} storage. "
            "Delete some attachments to free up space.")


# ---------------------------------------------------------------------------
# Keeping used_bytes right
# ---------------------------------------------------------------------------

def reserve(user, nbytes):
    """Inside the upload's transaction: lock the person's row, refuse (StorageFull) if nbytes would take them over
    their limit, else add nbytes. Rolled back with the upload if anything after it fails."""
    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError("storage.reserve() must run inside the upload's transaction")
    row = storage_row(user, lock=True)
    limit = _limits_for(row).limit_bytes
    if row.used_bytes + nbytes > limit:
        raise StorageFull(full_message(row.used_bytes, limit))
    type(row).objects.filter(pk=row.pk).update(used_bytes=F("used_bytes") + nbytes)


def release(user_id, nbytes):
    """An attachment of theirs is gone: take its bytes off (never below zero)."""
    from .models import UserStorage
    if nbytes:
        UserStorage.objects.filter(user_id=user_id).update(used_bytes=Greatest(F("used_bytes") - nbytes, Value(0)))


def real_usage(user_ids=None):
    """{user id: bytes} from the attachments themselves (file + thumbnail)."""
    from .models import Attachment
    qs = Attachment.objects.all()
    if user_ids is not None:
        qs = qs.filter(owner_id__in=list(user_ids))
    totals = qs.values("owner_id").annotate(total=Sum(F("size_bytes") + F("thumb_size_bytes")))
    return {r["owner_id"]: r["total"] or 0 for r in totals}


def recalculate(users=None):
    """Recount used_bytes for these users (default: everyone). Returns [(user, cached, actual)] for the ones
    whose cached value was wrong - now fixed."""
    from django.contrib.auth import get_user_model

    from .models import UserStorage
    people = list(users) if users is not None else list(get_user_model().objects.order_by("username"))
    wrong = []
    for person in people:
        with transaction.atomic():
            row = storage_row(person, lock=True)
            truth = real_usage([person.pk]).get(person.pk, 0)     # under the lock: no upload or delete is lost
            if row.used_bytes != truth:
                wrong.append((person, row.used_bytes, truth))
                UserStorage.objects.filter(pk=row.pk).update(used_bytes=truth)
    return wrong


def set_tier(user, tier):
    """Put the person on `tier` (None: the default tier)."""
    row = storage_row(user)
    row.tier = tier
    row.save(update_fields=["tier", "updated_at"])
    return row


def set_default_tier(tier):
    from .models import StorageTier
    with transaction.atomic():
        StorageTier.objects.filter(is_default=True).exclude(pk=tier.pk).update(is_default=False)
        StorageTier.objects.filter(pk=tier.pk).update(is_default=True)


def tier_users(tier):
    """How many people the tier applies to: explicitly on it, plus - for the default - everyone without one."""
    from django.contrib.auth import get_user_model

    from .models import UserStorage
    explicit = UserStorage.objects.filter(tier=tier).count()
    if not tier.is_default:
        return explicit
    on_other = UserStorage.objects.filter(tier__isnull=False).exclude(tier=tier).count()
    return get_user_model().objects.count() - on_other


def can_delete_tier(tier):
    """Never the default, never one anyone is explicitly on."""
    from .models import UserStorage
    return not tier.is_default and not UserStorage.objects.filter(tier=tier).exists()
