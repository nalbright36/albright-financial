from django.urls import path

from . import views

app_name = "roundup"
urlpatterns = [
    path("", views.briefing, name="briefing"),
    path("refresh/", views.refresh, name="refresh"),
    path("schedule/", views.save_schedule, name="schedule"),
    path("turn-on/", views.turn_on, name="turn_on"),
    path("not-now/", views.not_now, name="not_now"),
]
