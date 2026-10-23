from django.urls import path

from . import inbox_views, views

app_name = "albright_crypto_app"  # the Opportunities app - namespace kept with the package name

urlpatterns = [
    path("", inbox_views.inbox, name="inbox"),
    path("hidden/", inbox_views.hidden, name="hidden"),
    path("hidden/<int:pk>/show/", inbox_views.show_in_inbox, name="show_in_inbox"),
    path("inbox/<int:pk>/keep/", inbox_views.inbox_keep, name="inbox_keep"),
    path("inbox/<int:pk>/skip/", inbox_views.inbox_skip, name="inbox_skip"),
    path("inbox/<int:pk>/undo/", inbox_views.inbox_undo, name="inbox_undo"),
    path("inbox/<int:pk>/walkthrough/", inbox_views.inbox_walkthrough, name="inbox_walkthrough"),
    path("inbox/<int:pk>/draft/", inbox_views.inbox_draft, name="inbox_draft"),
    path("pipeline/", inbox_views.pipeline, name="pipeline"),
    path("pipeline/<int:pk>/move/", inbox_views.pipeline_move, name="pipeline_move"),
    # The old Crypto / Income pages -> the Inbox (?tab=blocked/excluded -> Hidden, doing/submitted -> Pipeline).
    path("crypto/", inbox_views.old_crypto, name="old_crypto"),
    path("income/", inbox_views.old_income, name="old_income"),
    path("opportunity/<int:pk>/", views.opportunity_detail, name="opportunity"),
    path("opportunity/<int:pk>/step/<int:step_id>/toggle/", views.toggle_step, name="toggle_step"),
    path("add/", views.add_opportunity, name="add"),
    path("earnings/", views.earnings, name="earnings"),
    path("settings/", views.settings_view, name="settings"),
    path("settings/filters/", views.filters_view, name="filters"),
    path("leave/", views.leave, name="leave"),
    path("profile/", views.profile_view, name="profile"),
    path("drafts/<int:pk>/download/", views.draft_download, name="draft_download"),
    path("gmail/", views.gmail_setup, name="gmail"),
    path("gmail/connect/", views.gmail_connect, name="gmail_connect"),
    path("gmail/callback/", views.gmail_callback, name="gmail_callback"),
    path("gmail/disconnect/", views.gmail_disconnect, name="gmail_disconnect"),
]
