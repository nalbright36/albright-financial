from django.urls import path

from . import views

app_name = "albright_control"

urlpatterns = [
    path("", views.overview, name="overview"),
    path("bot-api/", views.bot_api, name="bot_api"),
    path("bot-api/tokens/<int:pk>/revoke/", views.revoke_token, name="revoke_token"),
    path("bot-api/tokens/<int:pk>/scopes/", views.edit_token_scopes, name="edit_token_scopes"),
    path("bot-api/tokens/<int:pk>/rotate/", views.rotate_token, name="rotate_token"),
    path("bot-api/activity/", views.bot_activity, name="bot_activity"),
    path("suggestions/", views.suggestions, name="suggestions"),
    path("suggestions/<int:pk>/", views.update_suggestion, name="update_suggestion"),
    path("health/", views.health, name="health"),
    path("integrations/", views.integrations_view, name="integrations"),
    path("integrations/<str:key>/test/", views.test_integration, name="test_integration"),
    path("ai-usage/", views.ai_usage_view, name="ai_usage"),
    path("data/", views.data_view, name="data"),
    path("data/backup/<str:key>.zip", views.backup, name="backup"),
    path("audit/", views.audit_log, name="audit"),
]
