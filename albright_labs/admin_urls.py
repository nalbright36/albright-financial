"""Mounted at /control/labs/ (the Admin area - superusers only)."""
from django.urls import path

from . import views

app_name = "labs_admin"

urlpatterns = [
    path("", views.landing_settings, name="settings"),
]
