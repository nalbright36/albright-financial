from django.urls import path

from albright_bot_api import oauth_views

from . import offline, quick_actions
from . import (activity_views, api_key_views, attachment_views, circle_views, invite_views, public_views, push_views,
               search_views, share_views, views)

app_name = "labs"

urlpatterns = [
    path("", views.root, name="root"),
    # The public home page's invite form and footer pages (public_views) - no login.
    path("invite/", public_views.invite_request, name="invite_request"),
    path("privacy/", public_views.placeholder, {"page": "privacy"}, name="privacy"),
    path("terms/", public_views.placeholder, {"page": "terms"}, name="terms"),
    path("contact/", public_views.placeholder, {"page": "contact"}, name="contact"),
    path("home/", views.hub, name="hub"),
    # The global search (search_views; the one source: albright_notes.links.find).
    path("search/", search_views.search, name="search"),
    path("search/recent/", search_views.remember, name="search_recent"),
    path("home/tiles/<slug:key>/", views.tile, name="tile"),
    path("home/launcher/", views.launcher_lines, name="launcher_lines"),
    path("home/landing/", views.save_landing, name="save_landing"),
    path("home/account/", views.account, name="account"),
    path("home/account/offline/", offline.settings, name="offline_settings"),
    # API keys / Connect your AI (api_key_views; the keys and the API: albright_bot_api). The approval page for an
    # AI connecting over MCP + OAuth (albright_bot_api.oauth_views) lives here so it looks like My account too.
    path("account/api-keys/", api_key_views.api_keys, name="api_keys"),
    path("account/api-keys/<int:pk>/access/", api_key_views.api_key_scopes, name="api_key_scopes"),
    path("account/api-keys/<int:pk>/rename/", api_key_views.api_key_rename, name="api_key_rename"),
    path("account/api-keys/<int:pk>/rotate/", api_key_views.api_key_rotate, name="api_key_rotate"),
    path("account/api-keys/<int:pk>/revoke/", api_key_views.api_key_revoke, name="api_key_revoke"),
    path("account/api-keys/authorize/", oauth_views.authorize, name="api_key_authorize"),
    # Circles (circle_views; the rules: albright_labs.circles). The old Household page goes to Circles.
    path("home/account/circles/", circle_views.circles_page, name="circles"),
    path("home/account/circles/<int:pk>/", circle_views.circle_page, name="circle"),
    path("home/account/circles/invites/<int:pk>/", circle_views.invite_page, name="circle_invite"),
    path("circles/join/<str:token>/", circle_views.invite_link, name="circle_join"),
    # Sharing (share_views; the rules: albright_labs.sharing) and People (albright_labs.connections).
    path("share/<str:label>/<int:pk>/", share_views.share, name="share"),
    # Attachments on Planner tasks and notes (attachment_views; the rules: albright_labs.attachments).
    path("attachments/upload/", attachment_views.upload, name="attachment_upload"),
    path("attachments/<int:pk>/", attachment_views.serve, name="attachment"),
    path("attachments/<int:pk>/thumb/", attachment_views.serve_thumb, name="attachment_thumb"),
    path("attachments/<int:pk>/delete/", attachment_views.delete, name="attachment_delete"),
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
    path("home/my-tiles/toggle/", views.toggle_tile, name="toggle_tile"),
    path("home/quick/<str:name>/", quick_actions.popup, name="quick_action"),
    path("home/refresh/", quick_actions.refresh, name="hub_refresh"),
    path("home/my-tiles/order/", views.save_tile_order, name="save_tile_order"),
    path("home/pin/", views.toggle_pin, name="toggle_pin"),
    path("home/nav/show/<slug:key>/", views.show_app, name="show_app"),
    # An invite link: the only way to make an account yourself (404 without a usable invite).
    path("join/<str:token>/", invite_views.signup, name="signup"),
    # A recipe share link counting as an invite (albright_meals.share_links): a signed reference to the link.
    path("join/recipe/<str:reference>/", invite_views.share_signup, name="share_signup"),
]
