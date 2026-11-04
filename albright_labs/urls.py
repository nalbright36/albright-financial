from django.urls import path

from . import circle_views, invite_views, views

app_name = "labs"

urlpatterns = [
    path("", views.root, name="root"),
    path("home/", views.hub, name="hub"),
    path("home/tiles/<slug:key>/", views.tile, name="tile"),
    path("home/launcher/", views.launcher_lines, name="launcher_lines"),
    path("home/landing/", views.save_landing, name="save_landing"),
    path("home/account/", views.account, name="account"),
    # Circles (circle_views; the rules: albright_labs.circles). The old Household page goes to Circles.
    path("home/account/circles/", circle_views.circles_page, name="circles"),
    path("home/account/circles/<int:pk>/", circle_views.circle_page, name="circle"),
    path("home/account/circles/invites/<int:pk>/", circle_views.invite_page, name="circle_invite"),
    path("circles/join/<str:token>/", circle_views.invite_link, name="circle_join"),
    path("home/account/household/", circle_views.household_redirect, name="household"),
    path("home/nav/", views.save_nav, name="save_nav"),
    path("home/picker/", views.save_picker, name="save_picker"),
    path("home/pin/", views.toggle_pin, name="toggle_pin"),
    path("home/nav/show/<slug:key>/", views.show_app, name="show_app"),
    # An invite link: the only way to make an account yourself (404 without a usable invite).
    path("join/<str:token>/", invite_views.signup, name="signup"),
]
