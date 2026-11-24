from django.urls import path

from albright_labs.items import item_page

from . import views

app_name = "albright_planner"

urlpatterns = [
    path("", views.home, name="home"),
    path("day/", views.day_view, name="day"),
    path("week/", views.week_view, name="week"),
    path("month/", views.month_view, name="month"),
    path("add/", views.quick_add, name="quick_add"),
    path("new/", views.new_item, name="new"),
    path("notes-preview/", views.notes_preview, name="notes_preview"),
    path("settings/", views.settings_view, name="settings"),
    path("timezone/", views.set_timezone, name="timezone"),
    path("location/", views.set_weather_location, name="weather_location"),
    path("weather-collapse/", views.weather_collapse, name="weather_collapse"),
    path("meals/", views.toggle_meals, name="toggle_meals"),
    path("view-options/", views.view_options, name="view_options"),
    path("from-note/<int:note_id>/", views.from_note, name="from_note"),
    path("item/<int:pk>/", item_page(views.view_item), name="item"),
    path("<int:pk>/", item_page(views.edit_item), name="edit"),
    path("<int:pk>/duplicate/", views.duplicate, name="duplicate"),
    path("<int:pk>/quick/", views.quick_edit, name="quick_edit"),
    path("<int:pk>/share-box/", views.share_box, name="share_box"),
    path("<int:pk>/note-choices/", views.note_choices, name="note_choices"),
    path("<int:pk>/toggle/", views.toggle, name="toggle"),
    path("<int:pk>/skip/", views.skip, name="skip"),
    path("<int:pk>/move/", views.move, name="move"),
    path("<int:pk>/remove/", views.remove, name="remove"),
    path("<int:pk>/note/", item_page(views.item_note), name="item_note"),
    path("<int:pk>/checklist/<int:row_id>/", views.checklist_toggle, name="checklist_toggle"),
    path("<int:pk>/checklist/add/", views.checklist_add, name="checklist_add"),
    path("from-groceries/", views.from_groceries, name="from_groceries"),
]
