from django.urls import path

from . import views

app_name = "albright_meals"

urlpatterns = [
    path("", views.week, name="week"),
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
    path("groceries/clear-checked/", views.grocery_clear, name="grocery_clear"),
    path("groceries/from-week/", views.grocery_from_week, name="grocery_from_week"),
    path("ideas/", views.ideas, name="ideas"),
    path("ideas/<int:pk>/plan/", views.idea_plan, name="idea_plan"),
    path("ideas/<int:pk>/decline/", views.idea_decline, name="idea_decline"),
    path("recipes/", views.recipes, name="recipes"),
    path("recipes/new/", views.recipe_edit, name="recipe_add"),
    path("recipes/<int:pk>/", views.recipe, name="recipe"),
    path("recipes/<int:pk>/edit/", views.recipe_edit, name="recipe_edit"),
    path("recipes/<int:pk>/plan/", views.recipe_plan, name="recipe_plan"),
    path("recipes/<int:pk>/groceries/", views.recipe_groceries, name="recipe_groceries"),
]
