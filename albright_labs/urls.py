from django.urls import path

from . import activity_views, circle_views, invite_views, push_views, share_views, views

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
    # Sharing (share_views; the rules: albright_labs.sharing) and People (albright_labs.connections).
    path("share/<str:label>/<int:pk>/", share_views.share, name="share"),
    path("home/account/people/", share_views.people_page, name="people"),
    path("home/account/people/search/", share_views.people_search, name="people_search"),
    path("home/account/people/<int:pk>/remove/", share_views.people_remove, name="people_remove"),
    path("home/account/circles/sharing/", share_views.defaults, name="sharing_defaults"),
    path("home/account/circles/shared-with-me/", share_views.shared_with_me, name="shared_with_me"),
    path("home/account/household/", circle_views.household_redirect, name="household"),
    # Activity and notifications (activity_views; the rules: albright_labs.activity).
    path("home/account/activity/", activity_views.activity_page, name="activity"),
    path("home/account/notifications/", activity_views.notification_settings, name="notification_settings"),
    path("home/account/notifications/push/subscribe/", push_views.subscribe, name="push_subscribe"),
    path("home/account/notifications/push/unsubscribe/", push_views.unsubscribe, name="push_unsubscribe"),
    path("home/account/notifications/push/<int:pk>/remove/", push_views.remove, name="push_remove"),
    path("notifications/panel/", activity_views.panel, name="notifications_panel"),
    path("notifications/<int:pk>/open/", activity_views.open_event, name="notification_open"),
    path("notifications/read-all/", activity_views.read_all, name="notifications_read_all"),
    path("home/nav/", views.save_nav, name="save_nav"),
    path("home/picker/", views.save_picker, name="save_picker"),
    path("home/pin/", views.toggle_pin, name="toggle_pin"),
    path("home/nav/show/<slug:key>/", views.show_app, name="show_app"),
    # An invite link: the only way to make an account yourself (404 without a usable invite).
    path("join/<str:token>/", invite_views.signup, name="signup"),
]
