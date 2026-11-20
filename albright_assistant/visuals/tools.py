"""The assistant's Visuals tools - local to the assistant (not Bot API operations): they run on the server as the
person (runner._execute hands them the run). list_metrics, get_series and get_stats only read; show_visual checks
the spec (spec.clean), checks every dataset's query (metrics.clean_query + one run), and saves a draft Visual on the
chat - the chat shows it as a card (Save / full screen / PNG). "replaces" makes a new version of a card in the same
chat ("make it weekly", "add carbs")."""
import json

from django.db import transaction

from . import metrics, spec, stats

MAX_DATASETS = 4
SERIES_INLINE_ROWS = 400        # get_series sends the rows themselves up to this; past it, a summary per metric

QUERY_SCHEMA = {
    "type": "object",
    "properties": {
        "metrics": {"type": "array", "items": {"type": "string"}, "description": "Metric keys from list_metrics (1-6)"},
        "start": {"type": "string", "description": "YYYY-MM-DD (default: 90 days before end)"},
        "end": {"type": "string", "description": "YYYY-MM-DD (default: today)"},
        "granularity": {"type": "string", "enum": list(metrics.GRANULARITIES)},
        "aggregation": {"type": "string", "enum": list(metrics.AGGREGATIONS),
                        "description": "Default: each metric's own (weight: last, the rest: sum)"},
    },
    "required": ["metrics"],
}

DEFINITIONS = [
    {"name": "list_metrics",
     "description": "What can be charted from the person's account: [{key, label, app, unit, default_aggregation}]. "
                    "Call it first when they ask for a chart, a trend or whether two things are related.",
     "parameters": {"type": "object", "properties": {}}},
    {"name": "get_series",
     "description": "The numbers for metrics over a date range, as tidy rows {date, metric (label), key, value} - "
                    "bucketed by day, week (Monday) or month. At most 2 years and 5,000 rows. Long results come back "
                    "summarized per metric (count, min, max, mean, first, last); charts get every row via show_visual.",
     "parameters": QUERY_SCHEMA},
    {"name": "get_stats",
     "description": "Computed statistics for two metrics over a range: Pearson and Spearman correlation of the days "
                    "both have data (metric_b shifted by lag_days), paired_days (the sample size) and a trend for "
                    "each (change per week). Always use these numbers - never calculate statistics yourself.",
     "parameters": {"type": "object", "properties": {
         "metric_a": {"type": "string"}, "metric_b": {"type": "string"},
         "start": {"type": "string", "description": "YYYY-MM-DD (default: 90 days before end)"},
         "end": {"type": "string", "description": "YYYY-MM-DD (default: today)"},
         "lag_days": {"type": "integer", "description": "Pair metric_a on a day with metric_b this many days later "
                                                        "(0-60; default 0)"}},
         "required": ["metric_a", "metric_b"]}},
    {"name": "show_visual",
     "description": "Show a chart card in the chat. spec: a Vega-Lite v6 spec WITHOUT data - every data is "
                    "{\"name\": <a key of datasets>}; the server fills in the rows {date, metric, key, value} (use "
                    "field \"date\" type temporal, \"value\" quantitative, color by \"metric\" - the app colors are "
                    "added for you). Marks: line, bar, point, area, rule, text; layer/hconcat/vconcat allowed, at most "
                    "4 views; no url, values, params, href or image. Add tooltips. datasets: {\"series_1\": a "
                    "get_series query, ...} (1-4). title: short. summary: 1-3 plain sentences from get_stats numbers "
                    "(with the number of days; a correlation never proves cause). replaces: the visual_id of a card "
                    "in this chat to make a new version of it (\"make it weekly\", \"add carbs\").",
     "parameters": {"type": "object", "properties": {
         "title": {"type": "string"}, "summary": {"type": "string"},
         "spec": {"type": "object", "description": "Vega-Lite v6 spec without data"},
         "datasets": {"type": "object", "description": "{name: get_series query}",
                      "additionalProperties": QUERY_SCHEMA},
         "replaces": {"type": "integer"}},
         "required": ["title", "spec", "datasets"]}},
]
NAMES = {d["name"] for d in DEFINITIONS}


def definitions(user):
    return DEFINITIONS if metrics.available(user) else []


def _summary(rows):
    out = {}
    for row in rows:
        s = out.setdefault(row["key"], {"metric": row["metric"], "count": 0, "min": None, "max": None, "total": 0.0,
                                        "first": row, "last": row})
        v = row["value"]
        s["count"] += 1
        s["total"] += v
        s["min"] = v if s["min"] is None else min(s["min"], v)
        s["max"] = v if s["max"] is None else max(s["max"], v)
        s["last"] = row
    for s in out.values():
        s["mean"] = round(s.pop("total") / s["count"], 2) if s["count"] else None
        s["first"] = {"date": s["first"]["date"], "value": s["first"]["value"]}
        s["last"] = {"date": s["last"]["date"], "value": s["last"]["value"]}
    return out


def run(run, call):
    """(text for the model, data for the message, is_error)."""
    user, args = run.user, call.arguments or {}
    try:
        if call.name == "list_metrics":
            return json.dumps({"metrics": metrics.list_metrics(user)}), None, False
        if call.name == "get_series":
            query = metrics.clean_query(user, args)
            rows = metrics.series(user, query)
            if len(rows) <= SERIES_INLINE_ROWS:
                return json.dumps({"query": query, "rows": rows}), None, False
            return json.dumps({"query": query, "row_count": len(rows), "summary": _summary(rows),
                               "note": "Too many rows to list - use show_visual to chart them all."}), None, False
        if call.name == "get_stats":
            return json.dumps(stats.get_stats(user, args.get("metric_a"), args.get("metric_b"), args.get("start"),
                                              args.get("end"), args.get("lag_days", 0))), None, False
        if call.name == "show_visual":
            return _show(run, args)
    except (metrics.MetricError, spec.SpecError) as exc:
        return json.dumps({"error": str(exc)}), None, True
    return json.dumps({"error": f"No tool named {call.name!r}."}), None, True


def _show(run, args):
    from albright_assistant.models import Visual
    user = run.user
    datasets = args.get("datasets")
    if isinstance(datasets, str):                       # some models send the object as JSON text
        try:
            datasets = json.loads(datasets)
        except ValueError:
            datasets = None
    if not isinstance(datasets, dict) or not 1 <= len(datasets) <= MAX_DATASETS:
        raise spec.SpecError(f"datasets must name 1-{MAX_DATASETS} get_series queries, e.g. {{\"series_1\": {{...}}}}.")
    queries, counts = {}, {}
    for name, raw in datasets.items():
        if not isinstance(name, str) or not name.replace("_", "").isalnum() or len(name) > 40:
            raise spec.SpecError("Dataset names are short words like series_1.")
        queries[name] = metrics.clean_query(user, raw)
        counts[name] = len(metrics.series(user, queries[name]))
    checked = spec.clean(args.get("spec"), queries.keys())
    title = " ".join(str(args.get("title") or "").split())[:120] or "Visual"
    summary = str(args.get("summary") or "").strip()[:1500]
    previous = None
    if args.get("replaces"):
        previous = Visual.objects.filter(pk=args["replaces"], owner=user).filter(chat=run.chat).first() \
            or Visual.objects.filter(pk=args["replaces"], owner=user, saved=True).first()
    with transaction.atomic():
        visual = Visual.objects.create(
            owner=user, chat=run.chat, title=title, summary=summary, spec=checked, queries=queries,
            previous=previous if previous and not previous.saved else None,
            saved_as=(previous if previous and previous.saved else previous.saved_as if previous else None),
            version=(previous.version + 1) if previous else 1)
    return json.dumps({"visual_id": visual.pk, "version": visual.version, "rows": counts,
                       "shown": "The card is on screen with the chart, your title and summary, and Save / Full "
                                "screen / Download PNG. Reply with one short line at most - don't repeat the summary."}), \
        {"visual": visual.pk}, False
