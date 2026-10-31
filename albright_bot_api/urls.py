from django.urls import path

from . import api_views, meals_api, notes_api, views

# Site pages. Token management and the activity log moved to the Admin area
# (/control/bot-api/); the old URLs redirect there.
app_name = "albright_bot_api"
urlpatterns = [
    path("", views.moved_to_admin, name="tokens"),
    path("tokens/<int:pk>/revoke/", views.moved_to_admin, name="revoke"),
    path("activity/", views.moved_to_admin, {"target": "albright_control:bot_activity"}, name="activity"),
    path("suggestions/<int:pk>/<str:decision>/", views.decide_suggestion, name="decide_suggestion"),
    path("trading-notes/bulk/", views.trading_notes_bulk, name="trading_notes_bulk"),
    path("trading-notes/<int:pk>/delete/", views.trading_note_delete, name="trading_note_delete"),
]

# The API itself (/api/bot/v1/) - token-authenticated JSON; included under the "bot_api_v1" namespace.
api_urlpatterns = [
    path("docs", api_views.docs, name="docs"),
    path("trading/notes", api_views.trading_notes, name="trading_notes"),
    path("trading/strategies", api_views.trading_strategies, name="trading_strategies"),
    path("reseller/lots", api_views.reseller_lots, name="reseller_lots"),
    path("reseller/lots/<int:lot_id>/notes", api_views.reseller_lot_note, name="reseller_lot_note"),
    path("reseller/inventory", api_views.reseller_inventory, name="reseller_inventory"),
    path("reseller/inventory/<int:entry_id>/suggestions", api_views.reseller_inventory_suggestion,
         name="reseller_inventory_suggestion"),
    path("opportunities/new", api_views.opportunities_new, name="opportunities_new"),
    path("opportunities/<int:opportunity_id>/notes", api_views.opportunity_note, name="opportunity_note"),
    path("opportunities/<int:opportunity_id>/proposal-draft", api_views.opportunity_proposal,
         name="opportunity_proposal"),
    path("site-suggestions", api_views.site_suggestions, name="site_suggestions"),
    path("ideas", api_views.ideas, name="ideas"),
    # Meals (albright_bot_api.meals_api) - the Recipe Bot.
    path("meals/plan", meals_api.plan, name="meals_plan"),
    path("meals/plan/<int:entry_id>", meals_api.plan_delete, name="meals_plan_entry"),
    path("meals/groceries", meals_api.groceries, name="meals_groceries"),
    path("meals/groceries/<int:item_id>", meals_api.grocery_update, name="meals_grocery_item"),
    path("meals/ideas", meals_api.ideas, name="meals_ideas"),
    path("meals/recipes", meals_api.recipes, name="meals_recipes"),
    path("meals/recipes/import", meals_api.recipes_import, name="meals_recipes_import"),
    # Notes (albright_bot_api.notes_api).
    path("notes", notes_api.notes_collection, name="notes"),
    path("notes/<int:note_id>", notes_api.note_update, name="note"),
]
