from django.urls import path

from . import views

app_name = "albright_crypto_app"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("opportunity/<int:pk>/", views.opportunity_detail, name="opportunity"),
    path("opportunity/<int:pk>/step/<int:step_id>/toggle/", views.toggle_step, name="toggle_step"),
    path("add/", views.add_opportunity, name="add"),
    path("earnings/", views.earnings, name="earnings"),
    path("settings/", views.settings_view, name="settings"),
    path("leave/", views.leave, name="leave"),
]
