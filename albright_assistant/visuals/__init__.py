"""Visuals: charts and summaries the AI assistant builds from the person's own data - without ever running code the
model wrote. The model only:

  1. asks what can be charted (list_metrics) and for the numbers (get_series, get_stats) - metrics.py, stats.py run
     the queries on the server, as the person, only over apps they can open that aren't archived;
  2. returns a Vega-Lite spec that names datasets ("series_1") instead of containing data - spec.py checks it
     against a strict allowlist (marks, channels, no URLs or data, no expression signals, at most 4 views);
  3. the server fills in the real rows (every time a Visual is opened, so saved ones stay current), and the trusted
     Vega libraries vendored in static/assistant/vendor draw it with the CSP-safe expression interpreter and a
     loader that refuses every network request (static/assistant/visuals.js).

models.Visual holds one: a chat's draft versions, and the saved ones (Account > Visuals, Home tiles, sharing).
"""
