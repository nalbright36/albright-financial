from django.urls import path

from . import idea_views, user_views, views

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
    path("users/", user_views.users, name="users"),
    path("ideas/", idea_views.ideas, name="ideas"),
    path("ideas/new/", idea_views.idea_add, name="idea_add"),
    path("ideas/<int:pk>/", idea_views.idea_detail, name="idea"),
    path("suggestions/<int:pk>/to-idea/", idea_views.suggestion_to_idea, name="suggestion_to_idea"),
]
