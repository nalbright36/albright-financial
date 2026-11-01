from django.urls import path

from . import views

app_name = "albright_planner"

urlpatterns = [
    path("", views.home, name="home"),
    path("day/", views.day_view, name="day"),
    path("week/", views.week_view, name="week"),
    path("month/", views.month_view, name="month"),
    path("add/", views.quick_add, name="quick_add"),
    path("new/", views.new_item, name="new"),
    path("settings/", views.settings_view, name="settings"),
    path("meals/", views.toggle_meals, name="toggle_meals"),
    path("from-note/<int:note_id>/", views.from_note, name="from_note"),
    path("<int:pk>/", views.edit_item, name="edit"),
    path("<int:pk>/toggle/", views.toggle, name="toggle"),
    path("<int:pk>/move/", views.move, name="move"),
    path("<int:pk>/remove/", views.remove, name="remove"),
    path("<int:pk>/note/", views.item_note, name="item_note"),
    path("<int:pk>/checklist/<int:row_id>/", views.checklist_toggle, name="checklist_toggle"),
    path("from-groceries/", views.from_groceries, name="from_groceries"),
    path("<int:pk>/share/", views.share_add, name="share_add"),
    path("<int:pk>/share/<int:share_id>/", views.share_update, name="share_update"),
]
