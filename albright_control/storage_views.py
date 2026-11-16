"""Admin > System > Storage: everyone's attachment storage against their tier (albright_labs.storage), and the
tiers themselves.

  - totals: storage used across all users, how many users, how many at 80% or more;
  - a row per user: tier, attachment count, used, limit, a usage bar (accent / amber at 80% / red at 100%) -
    sortable by percent (default, highest first), used or username, and searchable by username; "Change tier"
    and "Recalculate" (recount from the attachments) on each row;
  - a user's page: their 20 biggest attachments - name, size, which task or note, date. Names only: nothing here
    opens a file (the file views check sharing as usual, admin or not);
  - tiers: add / edit (the limit is entered in GB, stored in bytes), make default; a tier anyone is on - or the
    default - can't be deleted.
Superusers only (access.superuser_required: anyone else gets a 404, like every Admin page). Every change is in the
audit log."""
from decimal import Decimal
from urllib.parse import urlencode

from django import forms
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.db.models import Count, Sum
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.text import slugify

from albright_labs import storage
from albright_labs.models import Attachment, StorageTier, UserStorage

from .access import audit, superuser_required

SORTS = {"percent": "Percent", "used": "Used", "username": "Username"}


class TierForm(forms.Form):
    name = forms.CharField(max_length=60)
    slug = forms.SlugField(max_length=60, required=False, help_text="Left empty: made from the name.")
    limit_gb = forms.DecimalField(label="Limit (GB)", min_value=Decimal("0.01"), max_digits=9, decimal_places=2)
    sort_order = forms.IntegerField(min_value=0, max_value=32767, initial=0, required=False)

    def __init__(self, *args, tier=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.tier = tier

    def clean_slug(self):
        slug = self.cleaned_data.get("slug") or slugify(self.data.get("name", ""))[:60]
        if not slug:
            raise forms.ValidationError("Give it a slug.")
        clash = StorageTier.objects.filter(slug=slug)
        if self.tier is not None:
            clash = clash.exclude(pk=self.tier.pk)
        if clash.exists():
            raise forms.ValidationError("Another tier has that slug.")
        return slug

    def save(self):
        tier = self.tier or StorageTier()
        data = self.cleaned_data
        tier.name, tier.slug = data["name"].strip(), data["slug"]
        tier.limit_bytes = int(data["limit_gb"] * storage.GB)
        tier.sort_order = data.get("sort_order") or 0
        tier.save()
        return tier


def _rows():
    users = get_user_model().objects.annotate(attachment_count=Count("attachments")).order_by("username")
    rows_by_user = {r.user_id: r for r in UserStorage.objects.select_related("tier")}
    default = storage.default_tier()
    rows = []
    for user in users:
        row = rows_by_user.get(user.pk)
        tier = row.tier if row and row.tier_id else default
        limits = storage.StorageLimits(tier=tier, limit_bytes=tier.limit_bytes if tier else storage.FALLBACK_LIMIT,
                                       max_file_bytes=0)
        usage = storage.describe(row.used_bytes if row else 0, limits)
        rows.append({"user": user, "usage": usage, "count": user.attachment_count,
                     "explicit_tier": row.tier_id if row else None, "tier": tier})
    return rows


def _tiers():
    out = []
    for tier in StorageTier.objects.all():
        out.append({"tier": tier, "users": storage.tier_users(tier), "can_delete": storage.can_delete_tier(tier),
                    "limit_gb": (Decimal(tier.limit_bytes) / storage.GB).quantize(Decimal("0.01")).normalize()})
    return out


def _back(request):
    keep = {k: request.POST.get(k) for k in ("sort", "q") if request.POST.get(k)}
    url = reverse("albright_control:storage")
    return url + ("?" + urlencode(keep) if keep else "")


def _post(request):
    action = request.POST.get("action")
    User = get_user_model()
    if action in ("set_tier", "recalc"):
        user = get_object_or_404(User, pk=request.POST.get("user"))
        if action == "set_tier":
            raw = request.POST.get("tier", "")
            tier = get_object_or_404(StorageTier, pk=raw) if raw else None
            storage.set_tier(user, tier)
            audit(request, "storage_tier", f"{user.username}: {tier.name if tier else 'default tier'}")
            messages.success(request, f"{user.username} is now on {tier.name if tier else 'the default tier'}.")
        else:
            wrong = storage.recalculate([user])
            used = storage.get_storage_usage(user)["used_label"]
            audit(request, "storage_recalc", f"{user.username}: " + (
                f"{wrong[0][1]} -> {wrong[0][2]} B" if wrong else "already right"))
            messages.success(request, f"Recalculated {user.username}: {used}" +
                             (f" (was {storage.format_bytes(wrong[0][1])})." if wrong else " - it was already right."))
        return redirect(_back(request))
    if action == "tier_save":
        tier = get_object_or_404(StorageTier, pk=request.POST["tier_id"]) if request.POST.get("tier_id") else None
        form = TierForm(request.POST, tier=tier)
        if not form.is_valid():
            return form
        saved = form.save()
        if not StorageTier.objects.filter(is_default=True).exists():
            storage.set_default_tier(saved)
        audit(request, "storage_tier_saved", f"{saved.name} ({saved.slug}): {saved.limit_bytes} B")
        messages.success(request, f"Saved the {saved.name} tier ({storage.format_limit(saved.limit_bytes)}).")
        return redirect(reverse("albright_control:storage") + "#tiers")
    if action in ("tier_default", "tier_delete"):
        tier = get_object_or_404(StorageTier, pk=request.POST.get("tier_id"))
        if action == "tier_default":
            storage.set_default_tier(tier)
            audit(request, "storage_tier_default", tier.name)
            messages.success(request, f"{tier.name} is now the default tier.")
        elif not storage.can_delete_tier(tier):
            messages.error(request, f"{tier.name} can't be deleted - "
                           + ("it's the default tier." if tier.is_default else "people are on it. Move them first."))
        else:
            audit(request, "storage_tier_deleted", tier.name)
            tier.delete()
            messages.success(request, f"Deleted the {tier.name} tier.")
        return redirect(reverse("albright_control:storage") + "#tiers")
    messages.error(request, "Unknown action.")
    return redirect(_back(request))


@superuser_required
def storage_page(request):
    tier_form = None
    if request.method == "POST":
        result = _post(request)
        if not isinstance(result, TierForm):
            return result
        tier_form = result
    sort = request.GET.get("sort", "percent")
    sort = sort if sort in SORTS else "percent"
    q = request.GET.get("q", "").strip()[:150]
    rows = _rows()
    total_used = sum(r["usage"]["used_bytes"] for r in rows)
    over = sum(1 for r in rows if r["usage"]["percent"] >= storage.WARN_PERCENT)
    user_count = len(rows)
    if q:
        rows = [r for r in rows if q.lower() in r["user"].username.lower()]
    if sort == "username":
        rows.sort(key=lambda r: r["user"].username.lower())
    elif sort == "used":
        rows.sort(key=lambda r: (-r["usage"]["used_bytes"], r["user"].username.lower()))
    else:
        rows.sort(key=lambda r: (-r["usage"]["percent"], -r["usage"]["used_bytes"], r["user"].username.lower()))
    edit = None
    if request.GET.get("edit_tier"):
        edit = StorageTier.objects.filter(pk=request.GET["edit_tier"]).first()
    if tier_form is None:
        tier_form = TierForm(tier=edit, initial={
            "name": edit.name, "slug": edit.slug, "sort_order": edit.sort_order,
            "limit_gb": (Decimal(edit.limit_bytes) / storage.GB).quantize(Decimal("0.01")).normalize(),
        } if edit else {"limit_gb": 1, "sort_order": 0})
    return render(request, "albright_control/storage.html", {
        "rows": rows, "sort": sort, "sorts": SORTS, "q": q, "total_used": total_used, "user_count": user_count,
        "over_count": over, "tiers": _tiers(), "tier_choices": StorageTier.objects.all(), "tier_form": tier_form,
        "edit_tier": edit or getattr(tier_form, "tier", None), "default_tier": storage.default_tier(),
        "warn_percent": storage.WARN_PERCENT,
    })


def _parent_label(attachment, parents):
    found = parents.get((attachment.content_type_id, attachment.object_id))
    kind = {"scheduleitem": "Task", "note": "Note"}.get(attachment.content_type.model, attachment.content_type.model)
    return f"{kind}: {found}" if found else f"{kind} #{attachment.object_id} (gone)"


@superuser_required
def storage_user(request, pk):
    user = get_object_or_404(get_user_model(), pk=pk)
    biggest = list(Attachment.objects.filter(owner=user).select_related("content_type")
                   .order_by("-size_bytes", "-pk")[:20])
    parents = {}
    for a in biggest:
        key = (a.content_type_id, a.object_id)
        if key not in parents:
            model = a.content_type.model_class()
            obj = model.objects.filter(pk=a.object_id).only("title").first() if model else None
            parents[key] = obj.title if obj else None
    totals = Attachment.objects.filter(owner=user).aggregate(n=Count("pk"), files=Sum("size_bytes"),
                                                             thumbs=Sum("thumb_size_bytes"))
    return render(request, "albright_control/storage_user.html", {
        "person": user, "usage": storage.get_storage_usage(user), "count": totals["n"] or 0,
        "rows": [{"a": a, "parent": _parent_label(a, parents)} for a in biggest],
        "files_bytes": totals["files"] or 0, "thumb_bytes": totals["thumbs"] or 0,
        "explicit": UserStorage.objects.filter(user=user, tier__isnull=False).exists(),
    })

