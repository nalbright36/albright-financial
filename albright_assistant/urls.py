"""/account/assistant/ (Your AI assistant - setup) and /assistant/... (the chat's JSON endpoints). See views.py."""
from django.urls import path

from . import views

app_name = "assistant"

urlpatterns = [
    path("account/assistant/", views.setup, name="setup"),
    path("account/assistant/connect/", views.connect, name="connect"),
    path("account/assistant/settings/", views.save_settings, name="settings"),
    path("account/assistant/models/", views.refresh_models, name="refresh_models"),
    path("account/assistant/disconnect/", views.disconnect, name="disconnect"),
    path("assistant/leave/", views.leave, name="leave"),
    path("assistant/state/", views.state, name="state"),
    path("assistant/send/", views.send, name="send"),
    path("assistant/chats/<int:pk>/", views.chat_detail, name="chat"),
    path("assistant/chats/<int:pk>/delete/", views.chat_delete, name="chat_delete"),
    path("assistant/chats/delete-all/", views.chats_delete_all, name="chats_delete_all"),
    path("assistant/runs/<int:pk>/", views.run_poll, name="run"),
    path("assistant/runs/<int:pk>/approve/", views.run_approve, name="run_approve"),
    path("assistant/runs/<int:pk>/cancel/", views.run_cancel, name="run_cancel"),
    path("assistant/actions/<int:pk>/undo/", views.action_undo, name="action_undo"),
]
