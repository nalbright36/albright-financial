"""The model's Vega-Lite spec, checked against an allowlist, then filled with the real rows.

clean(spec, dataset_names) -> a safe copy, or SpecError saying what to fix:
  - views: unit specs (with a mark) inside layer / hconcat / vconcat / concat - at most MAX_VIEWS; facet and repeat
    aren't allowed (they multiply views)
  - marks: line, bar, point, area, rule, text (also as {"type": ...}); never image, or any other
  - data: only {"name": "<one of the declared datasets>"} - no "url", no "values", no inline data anywhere; the
    server adds the rows as top-level "datasets" (inject)
  - encodings: no href, url or image channels (nor "href" on a mark)
  - params / signals: removed (selections and their expressions aren't needed for these charts)
  - keys outside the allowlist are dropped; any "url" key, or a string starting with data:/javascript:/http(s): in
    a place that can load or link, is refused
The browser side also draws with a loader that refuses every request and the CSP-safe expression interpreter, so a
missed case still can't load anything or run code.
"""
import copy
import json

SCHEMA = "https://vega.github.io/schema/vega-lite/v6.json"
MAX_VIEWS = 4
MAX_SPEC_BYTES = 40_000
MARKS = {"line", "bar", "point", "area", "rule", "text"}
BANNED_CHANNELS = {"href", "url", "image"}
TOP_KEYS = {"$schema", "title", "description", "width", "height", "autosize", "padding", "data", "mark", "encoding",
            "transform", "layer", "hconcat", "vconcat", "concat", "columns", "resolve", "config", "spacing", "view",
            "projection", "name", "align", "bounds", "center"}
VIEW_KEYS = TOP_KEYS - {"$schema", "autosize", "padding", "config"}
CONFIG_KEYS = {"axis", "axisX", "axisY", "legend", "title", "view", "mark", "line", "bar", "point", "area", "rule",
               "text", "range", "header", "font", "numberFormat", "timeFormat", "customFormatTypes"}
MARK_KEYS = {"type", "point", "interpolate", "strokeWidth", "strokeDash", "opacity", "fillOpacity", "size", "color",
             "filled", "tooltip", "align", "baseline", "dx", "dy", "fontSize", "fontWeight", "angle", "orient",
             "cornerRadius", "cornerRadiusEnd", "thickness", "width", "binSpacing", "clip", "line", "stroke", "fill",
             "strokeCap", "text"}
TRANSFORMS = {"filter", "calculate", "fold", "aggregate", "joinaggregate", "window", "timeUnit", "bin", "pivot",
              "regression", "loess", "density", "stack", "flatten", "impute", "sample", "lookup", "as", "groupby",
              "ops", "fields", "frame", "sort", "ignorePeers", "on", "method", "order", "params", "extent", "bandwidth",
              "value", "key", "limit", "default", "keyvals", "field", "op", "offset", "steps", "minsteps", "maxsteps",
              "counts", "cumulative", "resolve"}
DANGEROUS_PREFIXES = ("data:", "javascript:", "http:", "https:", "//", "file:", "vbscript:")


class SpecError(ValueError):
    pass


def _check_strings(value, where):
    """No string anywhere that could be a link or a data URL loader."""
    if isinstance(value, str):
        low = value.strip().lower()
        if low.startswith(DANGEROUS_PREFIXES) and where != "$schema":
            raise SpecError(f"Remove the link/URL in {where} - charts can't load or link anything.")
    elif isinstance(value, dict):
        for k, v in value.items():
            if k in ("url", "href", "image"):
                raise SpecError(f"\"{k}\" isn't allowed ({where}).")
            _check_strings(v, f"{where}.{k}")
    elif isinstance(value, list):
        for i, v in enumerate(value):
            _check_strings(v, f"{where}[{i}]")


def _clean_data(data, names, where):
    if not isinstance(data, dict) or set(data) - {"name"} or data.get("name") not in names:
        raise SpecError(f"{where}.data must be {{\"name\": one of {sorted(names)}}} - the server adds the rows.")
    return {"name": data["name"]}


def _clean_mark(mark, where):
    if isinstance(mark, str):
        if mark not in MARKS:
            raise SpecError(f"Mark \"{mark}\" isn't allowed - use {', '.join(sorted(MARKS))}.")
        return mark
    if isinstance(mark, dict):
        if mark.get("type") not in MARKS:
            raise SpecError(f"Mark type {mark.get('type')!r} isn't allowed - use {', '.join(sorted(MARKS))}.")
        return {k: v for k, v in mark.items() if k in MARK_KEYS}
    raise SpecError(f"{where}.mark must be a mark type.")


def _clean_encoding(enc, where):
    if not isinstance(enc, dict):
        raise SpecError(f"{where}.encoding must be an object.")
    bad = BANNED_CHANNELS & set(enc)
    if bad:
        raise SpecError(f"The {', '.join(sorted(bad))} channel isn't allowed.")
    return copy.deepcopy(enc)


def _clean_transforms(items, where):
    if not isinstance(items, list):
        raise SpecError(f"{where}.transform must be a list.")
    out = []
    for t in items[:20]:
        if not isinstance(t, dict) or not t or set(t) - TRANSFORMS:
            raise SpecError(f"Transform {json.dumps(t)[:80]} isn't allowed.")
        if "lookup" in t and "from" in t:
            raise SpecError("lookup from another source isn't allowed.")
        out.append(copy.deepcopy(t))
    return out


def _clean_view(view, names, counter, where, top=False):
    if not isinstance(view, dict):
        raise SpecError(f"{where} must be an object.")
    for key in ("facet", "repeat", "spec"):
        if key in view:
            raise SpecError("facet and repeat aren't allowed - use layer or concat (at most 4 views).")
    allowed = TOP_KEYS if top else VIEW_KEYS
    out = {}
    for key, value in view.items():
        if key not in allowed:
            continue                                       # params, signals, usermeta, datasets ... dropped
        if key == "data":
            out["data"] = _clean_data(value, names, where)
        elif key == "mark":
            out["mark"] = _clean_mark(value, where)
        elif key == "encoding":
            out["encoding"] = _clean_encoding(value, where)
        elif key == "transform":
            out["transform"] = _clean_transforms(value, where)
        elif key in ("layer", "hconcat", "vconcat", "concat"):
            if not isinstance(value, list) or not value:
                raise SpecError(f"{where}.{key} must be a non-empty list.")
            out[key] = [_clean_view(v, names, counter, f"{where}.{key}[{i}]") for i, v in enumerate(value)]
        elif key == "config":
            out["config"] = {k: copy.deepcopy(v) for k, v in value.items() if k in CONFIG_KEYS} \
                if isinstance(value, dict) else {}
        else:
            out[key] = copy.deepcopy(value)
    if "mark" in out:
        counter[0] += 1
        if counter[0] > MAX_VIEWS:
            raise SpecError(f"At most {MAX_VIEWS} views (marks) in one chart.")
    elif not any(k in out for k in ("layer", "hconcat", "vconcat", "concat")):
        raise SpecError(f"{where} has no mark (use line, bar, point, area, rule or text).")
    return out


def clean(spec, dataset_names):
    """A checked copy of the model's spec (without data) - or SpecError."""
    if isinstance(spec, str):
        try:
            spec = json.loads(spec)
        except ValueError:
            raise SpecError("The spec isn't valid JSON.") from None
    if not isinstance(spec, dict):
        raise SpecError("The spec must be a Vega-Lite JSON object.")
    if len(json.dumps(spec)) > MAX_SPEC_BYTES:
        raise SpecError("The spec is too big - keep it simple.")
    names = set(dataset_names)
    _check_strings({k: v for k, v in spec.items() if k != "$schema"}, "spec")
    out = _clean_view(spec, names, [0], "spec", top=True)
    out["$schema"] = SCHEMA
    if "data" not in out and not _uses_data(out):
        raise SpecError("Name a dataset in data, e.g. {\"name\": \"series_1\"}.")
    return out


def _uses_data(view):
    if "data" in view:
        return True
    return any(_uses_data(v) for key in ("layer", "hconcat", "vconcat", "concat") for v in view.get(key) or [])


def _views(view):
    yield view
    for key in ("layer", "hconcat", "vconcat", "concat"):
        for v in view.get(key) or []:
            yield from _views(v)


def inject(spec, datasets, colors):
    """The spec ready to draw: the rows as top-level "datasets", the full width, and each metric in its app's color
    (a color encoding on "metric" gets the domain/range; a single-metric view without one gets the mark color)."""
    out = copy.deepcopy(spec)
    out["datasets"] = datasets
    out.setdefault("width", "container")
    out.setdefault("autosize", {"type": "fit", "contains": "padding"})
    domain = [label for label in colors]
    inherited = out.get("data", {}).get("name")
    for view in _views(out):
        name = view.get("data", {}).get("name") or inherited
        enc = view.get("encoding") or {}
        color = enc.get("color")
        if isinstance(color, dict) and color.get("field") == "metric" and "scale" not in color:
            color["scale"] = {"domain": domain, "range": [colors[d] for d in domain]}
        elif "mark" in view and not color and name in datasets:
            labels = {row["metric"] for row in datasets[name]}
            if len(labels) == 1 and next(iter(labels)) in colors:
                hue = colors[next(iter(labels))]
                view["mark"] = {"type": view["mark"], "color": hue} if isinstance(view["mark"], str) \
                    else {**view["mark"], "color": view["mark"].get("color", hue)}
    return out
