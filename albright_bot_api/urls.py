from django.urls import path

from . import api_views, views

# The site pages (Trading app nav "Bot API") - logged-in only.
app_name = "albright_bot_api"
urlpatterns = [
    path("", views.tokens, name="tokens"),
    path("tokens/<int:pk>/revoke/", views.revoke, name="revoke"),
    path("activity/", views.activity, name="activity"),
    path("suggestions/<int:pk>/<str:decision>/", views.decide_suggestion, name="decide_suggestion"),
]

# The API itself (/api/bot/v1/) - token-authenticated JSON; included under the "bot_api_v1" namespace.
api_urlpatterns = [
    path("docs", api_views.docs, name="docs"),
    path("trading/notes", api_views.trading_notes, name="trading_notes"),
    path("reseller/lots", api_views.reseller_lots, name="reseller_lots"),
    path("reseller/lots/<int:lot_id>/notes", api_views.reseller_lot_note, name="reseller_lot_note"),
    path("reseller/inventory", api_views.reseller_inventory, name="reseller_inventory"),
    path("reseller/inventory/<int:entry_id>/suggestions", api_views.reseller_inventory_suggestion,
         name="reseller_inventory_suggestion"),
    path("opportunities/new", api_views.opportunities_new, name="opportunities_new"),
    path("opportunities/<int:opportunity_id>/notes", api_views.opportunity_note, name="opportunity_note"),
    path("opportunities/<int:opportunity_id>/proposal-draft", api_views.opportunity_proposal,
         name="opportunity_proposal"),
]
