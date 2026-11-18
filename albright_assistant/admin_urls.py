"""/control/assistant/ - Admin > AI assistant (admin_views)."""
from django.urls import path

from . import admin_views

app_name = "assistant_admin"

urlpatterns = [
    path("", admin_views.overview, name="overview"),
    path("<int:pk>/toggle/", admin_views.toggle, name="toggle"),
]
