"""Mounted at /control/resume/ (see albright_trading_proj/urls.py)."""
from django.urls import path

from . import admin_views as v

app_name = "albright_resume_admin"

urlpatterns = [
    path("", v.overview, name="overview"),
    path("profile/", v.profile, name="profile"),
    path("experience/", v.experience, name="experience"),
    path("education/", v.education, name="education"),
    path("skills/", v.skills, name="skills"),
    path("projects/", v.projects, name="projects"),
    path("projects/new/", v.project_edit, name="project_new"),
    path("projects/<int:pk>/", v.project_edit, name="project"),
    path("chatbot/", v.chatbot, name="chatbot"),
    path("ai-summary/", v.drafts, name="drafts"),
    path("ai-summary/<int:pk>/", v.draft, name="draft"),
    path("questions/", v.questions, name="questions"),
    path("conversations/", v.conversations, name="conversations"),
    path("conversations/export.csv", v.conversations_csv, name="conversations_csv"),
    path("conversations/gaps/", v.knowledge_gaps, name="knowledge_gaps"),
    path("conversations/<uuid:public_id>/", v.conversation, name="conversation"),
]
