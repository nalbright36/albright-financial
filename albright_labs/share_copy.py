"""The data migrations' one job (Planner 0012, Notes 0002, Meals 0012, Fitness 0005): copy an app's old per-person
share rows into ShareGrant, then check - per person - that what's shared with them is exactly the same before and
after (the counts are printed in the migration output). Any difference stops the migration (nothing is kept).

Kept free of the apps' models on purpose: it only uses the historical models the migration hands it."""
from collections import Counter


def copy_shares(apps, *, app_label, model, share_model, item_field, user_field, permission=None, created_by=None,
                seen_field=None, label=""):
    """permission: a field name on the share row, or None (every old share was view-only). created_by: a function
    (share row, item) -> user id."""
    ContentType = apps.get_model("contenttypes", "ContentType")
    ShareGrant = apps.get_model("albright_labs", "ShareGrant")
    Share = apps.get_model(app_label, share_model)
    content_type, _created = ContentType.objects.get_or_create(app_label=app_label, model=model.lower())
    rows = list(Share.objects.select_related(item_field))
    before = Counter((getattr(r, f"{user_field}_id"), getattr(r, permission) if permission else "view") for r in rows)
    grants = []
    for r in rows:
        item = getattr(r, item_field)
        grants.append(ShareGrant(
            content_type=content_type, object_id=item.pk, user_id=getattr(r, f"{user_field}_id"),
            permission=getattr(r, permission) if permission else "view",
            created_by_id=created_by(r, item) if created_by else None,
            seen_at=getattr(r, seen_field) if seen_field else None))
    ShareGrant.objects.bulk_create(grants)
    for r in rows:                                             # keep when each was shared (auto_now_add set "now")
        ShareGrant.objects.filter(content_type=content_type, object_id=getattr(r, item_field).pk,
                                  user_id=getattr(r, f"{user_field}_id")).update(created_at=r.created_at)
    after = Counter(ShareGrant.objects.filter(content_type=content_type).values_list("user_id", "permission"))
    people = sorted({u for u, _p in before} | {u for u, _p in after})
    print(f"\n  {label or model}: {len(rows)} share(s) -> {sum(after.values())} grant(s)")
    for user_id in people:
        b = {p: before[(user_id, p)] for p in ("view", "edit")}
        a = {p: after[(user_id, p)] for p in ("view", "edit")}
        print(f"    user #{user_id}: before view={b['view']} edit={b['edit']}  after view={a['view']} edit={a['edit']}")
        if a != b:
            raise RuntimeError(f"{label or model}: user #{user_id} would see something different - nothing was changed.")


def copy_back(apps, *, app_label, model, share_model, item_field, user_field, permission=None, seen_field=None,
              extra=None):
    """Backwards: the grants (person ones) back into the old table."""
    ContentType = apps.get_model("contenttypes", "ContentType")
    ShareGrant = apps.get_model("albright_labs", "ShareGrant")
    Share = apps.get_model(app_label, share_model)
    content_type = ContentType.objects.filter(app_label=app_label, model=model.lower()).first()
    if content_type is None:
        return
    for g in ShareGrant.objects.filter(content_type=content_type, user__isnull=False):
        fields = {f"{item_field}_id": g.object_id, f"{user_field}_id": g.user_id}
        if permission:
            fields[permission] = g.permission
        if seen_field:
            fields[seen_field] = g.seen_at
        fields.update((extra or (lambda grant: {}))(g))
        Share.objects.get_or_create(**fields)
    ShareGrant.objects.filter(content_type=content_type).delete()
