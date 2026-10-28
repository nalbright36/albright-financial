from django.urls import path

from . import views

app_name = "labs"

urlpatterns = [
    path("", views.root, name="root"),
    path("home/", views.hub, name="hub"),
    path("home/tiles/<slug:key>/", views.tile, name="tile"),
    path("home/landing/", views.save_landing, name="save_landing"),
    path("home/account/", views.account, name="account"),
    path("home/nav/", views.save_nav, name="save_nav"),
    path("home/nav/show/<slug:key>/", views.show_app, name="show_app"),
]
