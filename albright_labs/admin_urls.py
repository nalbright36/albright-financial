"""Mounted at /control/labs/ (the Admin area - superusers only)."""
from django.urls import path

from . import public_views

app_name = "labs_admin"

urlpatterns = [
    path("", public_views.invite_requests_page, name="invite_requests"),
    path("<int:pk>/send/", public_views.invite_request_send, name="invite_request_send"),
    path("<int:pk>/decline/", public_views.invite_request_decline, name="invite_request_decline"),
]
