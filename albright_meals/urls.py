from django.urls import path

from . import views

app_name = "albright_meals"

urlpatterns = [
    path("", views.week, name="week"),
    path("notices/seen/", views.notices_seen, name="notices_seen"),
    path("plan/add/", views.plan_add, name="plan_add"),
    path("plan/<int:pk>/", views.plan_entry, name="plan_entry"),
    path("plan/<int:pk>/move/", views.plan_move, name="plan_move"),
    path("plan/<int:pk>/status/", views.plan_status, name="plan_status"),
    path("plan/<int:pk>/delete/", views.plan_delete, name="plan_delete"),
    path("settings/", views.settings_save, name="settings"),
    path("groceries/", views.grocery_page, name="groceries"),
    path("groceries/list/", views.grocery_list, name="grocery_list"),
    path("groceries/<int:pk>/toggle/", views.grocery_toggle, name="grocery_toggle"),
    path("groceries/<int:pk>/delete/", views.grocery_delete, name="grocery_delete"),
    path("groceries/<int:pk>/merge/", views.grocery_merge, name="grocery_merge"),
    path("groceries/clear-checked/", views.grocery_clear, name="grocery_clear"),
    path("groceries/from-week/", views.grocery_from_week, name="grocery_from_week"),
    path("ideas/", views.ideas, name="ideas"),
    path("ideas/<int:pk>/plan/", views.idea_plan, name="idea_plan"),
    path("ideas/<int:pk>/decline/", views.idea_decline, name="idea_decline"),
    path("recipes/", views.recipes, name="recipes"),
    path("recipes/new/", views.recipe_edit, name="recipe_add"),
    path("recipes/import/", views.recipe_import, name="recipe_import"),
    path("recipes/import/save/", views.recipe_import_save, name="recipe_import_save"),
    path("recipes/<int:pk>/", views.recipe, name="recipe"),
    path("recipes/<int:pk>/edit/", views.recipe_edit, name="recipe_edit"),
    path("recipes/<int:pk>/plan/", views.recipe_plan, name="recipe_plan"),
    path("recipes/<int:pk>/rate/", views.recipe_rate, name="recipe_rate"),
    path("recipes/<int:pk>/save-copy/", views.recipe_save_copy, name="recipe_save_copy"),
    path("recipes/<int:pk>/visibility/", views.recipe_visibility, name="recipe_visibility"),
    path("recipes/<int:pk>/share/", views.recipe_share, name="recipe_share"),
    path("recipes/<int:pk>/groceries/", views.recipe_groceries, name="recipe_groceries"),
    path("recipes/<int:pk>/reimport/", views.recipe_reimport, name="recipe_reimport"),
    path("recipes/<int:pk>/nutrition/", views.recipe_nutrition_page, name="recipe_nutrition"),
    path("recipes/<int:pk>/nutrition/calculate/", views.recipe_nutrition_calculate, name="recipe_nutrition_calculate"),
    path("recipes/<int:pk>/nutrition/lines/<int:line_id>/", views.recipe_nutrition_line, name="recipe_nutrition_line"),
    path("recipes/<int:pk>/nutrition/confirm/", views.recipe_nutrition_confirm, name="recipe_nutrition_confirm"),
    path("recipes/<int:pk>/nutrition/manual/", views.recipe_nutrition_manual, name="recipe_nutrition_manual"),
]
