from django.urls import path, reverse_lazy
from django.views.generic import RedirectView

from . import api_views, fitness_api, mcp, meals_api, notes_api, oauth_views, overview_api, schedule_api, views

# Site pages. Token management and the activity log moved to the Admin area
# (/control/bot-api/); the old URLs redirect there.
app_name = "albright_bot_api"
urlpatterns = [
    path("", views.moved_to_admin, name="tokens"),
    path("tokens/<int:pk>/revoke/", views.moved_to_admin, name="revoke"),
    path("activity/", views.moved_to_admin, {"target": "albright_control:bot_activity"}, name="activity"),
    # The API keys page moved to My account (/account/api-keys/, albright_labs.api_key_views): the old address
    # redirects there for good.
    path("keys/", RedirectView.as_view(url=reverse_lazy("labs:api_keys"), permanent=True), name="my_keys"),
    path("keys/<int:pk>/<str:action>/", RedirectView.as_view(url=reverse_lazy("labs:api_keys"), permanent=True)),
    path("suggestions/<int:pk>/<str:decision>/", views.decide_suggestion, name="decide_suggestion"),
    path("trading-notes/bulk/", views.trading_notes_bulk, name="trading_notes_bulk"),
    path("trading-notes/<int:pk>/delete/", views.trading_note_delete, name="trading_note_delete"),
]

# The API itself (/api/bot/v1/) - token-authenticated JSON; included under the "bot_api_v1" namespace.
api_urlpatterns = [
    path("docs", api_views.docs, name="docs"),
    path("openapi.json", api_views.openapi, name="openapi"),
    path("mcp", mcp.mcp, name="mcp"),
    path("me", api_views.me, name="me"),
    path("me/storage", api_views.my_storage, name="me_storage"),
    path("trading/notes", api_views.trading_notes, name="trading_notes"),
    path("trading/strategies", api_views.trading_strategies, name="trading_strategies"),
    path("reseller/lots", api_views.reseller_lots, name="reseller_lots"),
    path("reseller/lots/<int:lot_id>/notes", api_views.reseller_lot_note, name="reseller_lot_note"),
    path("reseller/inventory", api_views.reseller_inventory, name="reseller_inventory"),
    path("reseller/inventory/<int:entry_id>/suggestions", api_views.reseller_inventory_suggestion,
         name="reseller_inventory_suggestion"),
    path("opportunities", api_views.opportunities_create, name="opportunities_create"),
    path("opportunities/new", api_views.opportunities_new, name="opportunities_new"),
    path("opportunities/<int:opportunity_id>/notes", api_views.opportunity_note, name="opportunity_note"),
    path("opportunities/<int:opportunity_id>/proposal-draft", api_views.opportunity_proposal,
         name="opportunity_proposal"),
    path("site-suggestions", api_views.site_suggestions, name="site_suggestions"),
    path("site-suggestions/counts", api_views.site_suggestion_counts, name="site_suggestion_counts"),
    path("ideas", api_views.ideas, name="ideas"),
    # Fitness (albright_bot_api.fitness_api) - the owner's own workouts, food and weigh-ins.
    path("fitness/workouts", fitness_api.workouts, name="fitness_workouts"),
    path("fitness/food", fitness_api.food, name="fitness_food"),
    path("fitness/food/entries", fitness_api.food_entries, name="fitness_food_entries"),
    path("fitness/foods/search", fitness_api.food_search, name="fitness_food_search"),
    path("fitness/food-entries", fitness_api.food_entries_v2, name="fitness_food_entries_v2"),
    path("fitness/food-entries/recent", fitness_api.food_recent, name="fitness_food_recent"),
    path("fitness/food-entries/<int:entry_id>", fitness_api.food_entry, name="fitness_food_entry"),
    path("fitness/openapi.json", fitness_api.openapi, name="fitness_openapi"),
    path("fitness/weight", fitness_api.weight, name="fitness_weight"),
    path("fitness/today", fitness_api.today, name="fitness_today"),
    path("fitness/summary", fitness_api.summary, name="fitness_summary"),
    path("fitness/programs", fitness_api.programs, name="fitness_programs"),
    path("fitness/programs/<int:item_id>", fitness_api.program_delete, name="fitness_program_delete"),
    path("fitness/programs/<int:item_id>/pause", fitness_api.program_pause, name="fitness_program_pause"),
    path("fitness/programs/<int:item_id>/resume", fitness_api.program_resume, name="fitness_program_resume"),
    # Meals (albright_bot_api.meals_api) - the Recipe Bot.
    path("meals/spaces", meals_api.spaces, name="meals_spaces"),
    path("meals/plan", meals_api.plan, name="meals_plan"),
    path("meals/plan/<int:entry_id>", meals_api.plan_delete, name="meals_plan_entry"),
    path("meals/groceries", meals_api.groceries, name="meals_groceries"),
    path("meals/groceries/<int:item_id>", meals_api.grocery_update, name="meals_grocery_item"),
    path("meals/ideas", meals_api.ideas, name="meals_ideas"),
    path("meals/recipes", meals_api.recipes, name="meals_recipes"),
    path("meals/recipes/import", meals_api.recipes_import, name="meals_recipes_import"),
    path("meals/recipes/<int:recipe_id>", overview_api.recipe_detail, name="meals_recipe"),
    # Notes (albright_bot_api.notes_api).
    path("notes", notes_api.notes_collection, name="notes"),
    path("notes/<int:note_id>", notes_api.note_item, name="note"),
    # Read helpers across apps (albright_bot_api.overview_api).
    path("search", overview_api.search, name="search"),
    path("search/all", overview_api.search_all, name="search_all"),
    path("today", overview_api.today, name="today"),
    # Activity (albright_labs.activity) - the owner's notifications.
    path("activity", api_views.activity, name="activity"),
    # Planner (albright_bot_api.schedule_api).
    path("schedule", schedule_api.schedule_collection, name="schedule"),
    path("schedule/<int:item_id>/complete", schedule_api.schedule_complete, name="schedule_complete"),
    path("schedule/<int:item_id>/uncomplete", schedule_api.schedule_uncomplete, name="schedule_uncomplete"),
    path("schedule/<int:item_id>/skip", schedule_api.schedule_skip, name="schedule_skip"),
    path("schedule/<int:item_id>/checklist/<int:row_id>/complete", schedule_api.schedule_subtask_complete,
         name="schedule_subtask_complete"),
    path("schedule/<int:item_id>/checklist/<int:row_id>/uncomplete", schedule_api.schedule_subtask_uncomplete,
         name="schedule_subtask_uncomplete"),
]

# OAuth for MCP connectors (oauth_views) - at the site root, under the "bot_oauth" namespace. The approval page is in
# My account (labs:api_key_authorize).
oauth_urlpatterns = [
    path(".well-known/oauth-protected-resource", oauth_views.protected_resource, name="protected_resource"),
    path(".well-known/oauth-protected-resource/api/bot/v1/mcp", oauth_views.protected_resource,
         name="protected_resource_mcp"),
    path(".well-known/oauth-authorization-server", oauth_views.authorization_server, name="authorization_server"),
    path("oauth/register", oauth_views.register, name="register"),
    path("oauth/token", oauth_views.token, name="token"),
]
