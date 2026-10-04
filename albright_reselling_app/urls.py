from django.urls import path
from . import insights_views, ledger_views, scanner_views, views

app_name = "albright_reselling_app"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("ledger/", views.ledger, name="ledger"),
    path("ledger/scorecard/", ledger_views.scorecard, name="ledger_scorecard"),
    path("ledger/<int:entry_id>/manage/", ledger_views.manage_entry, name="ledger_manage"),
    path("ledger/<int:entry_id>/sell/", ledger_views.record_sale, name="ledger_record_sale"),
    path("ledger/<int:entry_id>/status/", ledger_views.set_status, name="ledger_set_status"),
    path("ledger/<int:entry_id>/split-legacy/", ledger_views.split_legacy, name="ledger_split_legacy"),
    path("auction-scanner/", views.auction_scanner, name="auction_scanner"),
    path("auction-scanner/<int:scan_id>/", views.scan_detail, name="scan_detail"),
    path("sleeper-segments/", views.sleeper_segments, name="sleeper_segments"),
    path("historical-data/", views.historical_data, name="historical_data"),
    path("sleeper-segments/analysis/<int:analysis_id>/", views.analysis_results, name="analysis_results"),
    path("scanner/review/<int:lot_id>/request/", scanner_views.request_review, name="ai_review_request"),
    path("scanner/review/<int:review_id>/", scanner_views.review_detail, name="ai_review_detail"),
    path("scanner/reviews/", scanner_views.review_history, name="ai_review_history"),
    path("scanner/win/<int:lot_id>/", ledger_views.win_lot, name="ledger_win_lot"),
    path("scanner/watch/<int:lot_id>/", scanner_views.watch_lot, name="watch_lot"),
    path("insights/", insights_views.insights_page, name="insights"),
    path("insights/suggestions/<int:suggestion_id>/apply/", insights_views.apply_suggestion,
         name="apply_suggestion"),
    path("insights/suggestions/<int:suggestion_id>/dismiss/", insights_views.dismiss_suggestion,
         name="dismiss_suggestion"),
    path("insights/overrides/<int:override_id>/undo/", insights_views.undo_override, name="undo_override"),
    path("insights/feedback/export/", insights_views.export_lot_feedback, name="export_lot_feedback"),
    path("insights/feedback/<int:feedback_id>/resolve/", insights_views.resolve_lot_feedback,
         name="resolve_lot_feedback"),
    path("scanner/lot/<int:lot_id>/feedback/", insights_views.create_lot_feedback, name="lot_feedback"),
]