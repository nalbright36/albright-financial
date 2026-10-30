from django.urls import path

from . import invite_views, views

app_name = "labs"

urlpatterns = [
    path("", views.root, name="root"),
    path("home/", views.hub, name="hub"),
    path("home/tiles/<slug:key>/", views.tile, name="tile"),
    path("home/launcher/", views.launcher_lines, name="launcher_lines"),
    path("home/landing/", views.save_landing, name="save_landing"),
    path("home/account/", views.account, name="account"),
    path("home/nav/", views.save_nav, name="save_nav"),
    path("home/nav/show/<slug:key>/", views.show_app, name="show_app"),
    # An invite link: the only way to make an account yourself (404 without a usable invite).
    path("join/<str:token>/", invite_views.signup, name="signup"),
]
