"""The Bot API's optional "share" field on create endpoints (Planner items, notes, recipes, workouts). The new thing is
first shared the way its owner shares new things in that app (albright_labs.sharing.apply_defaults - every create
endpoint does that); "share" adds one more - only with one of the OWNER's circles that shares the app (they're
active in it and opted in) or a person's exact username / email who can open the app:

    "share": {"circle": 3, "permission": "edit"}    or    "share": {"user": "sam", "permission": "view"}
"""
from .auth import ApiError


def validate(token, app, data):
    """Check "share" BEFORE anything is created (a bad one is a 400 and nothing is made)."""
    from albright_labs import sharing
    value = data.get("share")
    if value in (None, ""):
        return
    if not isinstance(value, dict) or not ({"circle", "user"} & set(value)):
        raise ApiError(400, '"share" must be {"circle": <id>} or {"user": "<exact username or email>"}, with an '
                            'optional "permission": "view" | "edit".', "rejected: bad share")
    if (value.get("permission") or sharing.VIEW) not in sharing.PERMISSIONS:
        raise ApiError(400, '"share": "permission" must be "view" or "edit".', "rejected: bad share permission")
    try:
        sharing.resolve_target(token.owner, sharing.APPS[app], value.get("circle"),
                        str(value.get("user") or "") if value.get("circle") in (None, "") else "")
    except sharing.ShareError as exc:
        raise ApiError(400, f'"share": {exc}', "rejected: share not allowed") from exc


def apply(token, obj, data):
    """Defaults, then the "share" field. Returns [{"circle" | "user", "permission"}] - who it's shared with now."""
    from albright_labs import sharing
    sharing.apply_defaults(obj, token.owner)
    value = data.get("share")
    if value not in (None, ""):
        if not isinstance(value, dict) or not ({"circle", "user"} & set(value)):
            raise ApiError(400, '"share" must be {"circle": <id>} or {"user": "<exact username or email>"}, with an '
                                'optional "permission": "view" | "edit".', "rejected: bad share")
        permission = value.get("permission") or sharing.VIEW
        try:
            sharing.share(token.owner, obj, permission, circle_id=value.get("circle"),
                          who=str(value.get("user") or "") if value.get("circle") in (None, "") else "")
        except sharing.ShareError as exc:
            raise ApiError(400, f'"share": {exc}', "rejected: share not allowed") from exc
    return [{"circle": g.circle_id, "user": g.user.get_username() if g.user_id else None, "permission": g.permission}
            for g in sharing.grants(obj)]
